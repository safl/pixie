"""Partition-table introspection and extraction for raw disk images.

Pixie's fetch pipeline extracts partition 1 (the Linux root) from every
whole-disk blob it downloads and stores it alongside as a sibling
``rootfs.raw`` file. Both the ephemeral nbdboot path (nbdkit serving
the file with ``--filter=cow``) and the persistent-overlay path
(``qemu-img create -b rootfs.raw`` + ``qemu-nbd``) then point at the
already-extracted partition. Target-side initrd sees ``/dev/nbd0`` as
the ext4 root filesystem at offset 0 in both modes; no partition
scan, no ``--offset`` on qemu-nbd, no partition filter on nbdkit.

Whole-disk blobs still live on disk for the flash modes
(``pixie-flash-once`` / ``pixie-flash-always``), which write the entire
disk image (partition table + BOOT + UEFI + root) to a target's local
disk. Extracting a partition adds one file per fetched image, roughly
partition-1-in-bytes big.

This module wraps ``sfdisk --json <path>`` for the parse. sfdisk on
a raw disk image reads the GPT / MBR directly and reports partitions
by sector offset, which we convert to bytes here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_SECTOR_BYTES = 512


class PartitionNotFound(RuntimeError):
    """Raised when a requested partition number is not present on
    the blob (unpartitioned image, missing partition, sfdisk parse
    failure). Callers decide whether to fall back to whole-image
    serving or to fail hard; this module does not embed that policy.
    """


@dataclass(frozen=True)
class PartitionInfo:
    """Byte range of one partition on a raw disk image."""

    start_bytes: int
    size_bytes: int


def partition_info(blob: Path, partition_number: int = 1) -> PartitionInfo:
    """Return the byte range of ``partition_number`` on ``blob``.

    Raises :class:`PartitionNotFound` if the blob is unpartitioned,
    the specified partition is absent, or sfdisk fails to parse.
    """
    if not blob.is_file():
        raise PartitionNotFound(f"blob {blob!s} does not exist")

    try:
        result = subprocess.run(
            ["sfdisk", "--json", str(blob)],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise PartitionNotFound("sfdisk binary not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise PartitionNotFound(
            f"sfdisk --json exited rc={exc.returncode}: {exc.stderr.strip()}"
        ) from exc

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PartitionNotFound(f"sfdisk output was not JSON: {exc}") from exc

    table = data.get("partitiontable") or {}
    parts = table.get("partitions") or []

    # sfdisk names partitions as ``<blob_path><N>`` for MBR / GPT
    # alike; match by the trailing number after the shared prefix.
    # A missing ``node`` on some sfdisk builds falls back to
    # ``label`` order, which is 1-indexed in the same array.
    target_suffix = str(partition_number)
    for entry in parts:
        node = str(entry.get("node") or "")
        if not node.endswith(target_suffix) or node.endswith(target_suffix + "0"):
            continue
        head = node[: -len(target_suffix)]
        # Distinguish blob1 from blob10, blob11, etc. by ensuring
        # the char before the suffix isn't itself a digit.
        if head and head[-1].isdigit():
            continue
        start_sectors = entry.get("start")
        size_sectors = entry.get("size")
        if start_sectors is None or size_sectors is None:
            raise PartitionNotFound(
                f"partition {partition_number} on {blob!s} missing start / size"
            )
        return PartitionInfo(
            start_bytes=int(start_sectors) * _SECTOR_BYTES,
            size_bytes=int(size_sectors) * _SECTOR_BYTES,
        )

    raise PartitionNotFound(
        f"partition {partition_number} not found on {blob!s} (sfdisk reported "
        f"{len(parts)} partition(s))"
    )


def extract_partition(
    blob: Path,
    output: Path,
    partition_number: int = 1,
    *,
    block_size: int = 4 * 1024 * 1024,
) -> PartitionInfo:
    """Copy ``partition_number`` from ``blob`` into ``output``.

    Reads the partition's byte range from :func:`partition_info` and
    streams that slice of the source file into a fresh ``output``.
    Atomic on the output side: writes to ``output.inflight`` and
    ``os.replace``\\ s into place on success, so a partial extract on
    interrupt leaves the tree in a state where ``output.is_file()``
    is still an honest predicate. Raises :class:`PartitionNotFound`
    if the blob has no matching partition or if sfdisk cannot parse
    the table (an unpartitioned raw filesystem, or a corrupt GPT).

    Returns the :class:`PartitionInfo` used for the copy so the
    caller can log the byte range without a second sfdisk shell-out.
    """
    info = partition_info(blob, partition_number)

    output.parent.mkdir(parents=True, exist_ok=True)
    inflight = output.with_name(output.name + ".inflight")
    with open(blob, "rb") as src, open(inflight, "wb") as dst:
        src.seek(info.start_bytes)
        remaining = info.size_bytes
        while remaining > 0:
            chunk = src.read(min(block_size, remaining))
            if not chunk:
                raise PartitionNotFound(
                    f"unexpected EOF reading partition {partition_number} of {blob!s}"
                )
            dst.write(chunk)
            remaining -= len(chunk)
    # ``os.replace`` is atomic on the same filesystem; using shutil
    # here to catch cross-filesystem moves loudly rather than a
    # partial rename.
    shutil.move(str(inflight), str(output))
    return info
