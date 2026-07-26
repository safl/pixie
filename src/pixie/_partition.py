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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SECTOR_BYTES = 512

# GPT partition type GUIDs (lowercase) that name a Linux root filesystem
# across architectures. sfdisk reports GPT ``type`` as the type GUID.
_LINUX_ROOT_GUIDS = frozenset(
    {
        "4f68bce3-e8cd-4db1-96e7-fbcaf984b709",  # root (x86-64)
        "44479540-f297-41b2-9af7-d131d5f0458a",  # root (x86)
        "b921b045-1df0-41c3-af44-4c6f280d3fae",  # root (arm64/aarch64)
        "69dad710-2ce4-4e3c-b16c-21a1d49abed3",  # root (arm/32-bit)
        "72ec70a6-cf74-40e6-bd49-4bda08e8f224",  # root (riscv64)
    }
)
# Generic "Linux filesystem" data GUID (and the MBR type 0x83), used when
# an image does not tag its root with an arch-specific root GUID.
_LINUX_FS_TYPES = frozenset({"0fc63daf-8483-4772-8e79-3d69d8477de4", "83"})
# Partition types that are never the root: firmware / boot-chain / swap.
# Skipped in the last-resort "largest remaining" tier so a BIOS-boot stub
# or the ESP is never mistaken for the root.
_NON_ROOT_TYPES = frozenset(
    {
        "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",  # EFI System
        "21686148-6449-6e6f-744e-656564454649",  # BIOS boot
        "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f",  # Linux swap
        "bc13c2ff-59e6-4262-a352-b275fd6f7172",  # XBOOTLDR (/boot)
        "ef",  # MBR EFI System
        "82",  # MBR Linux swap
    }
)


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


def _sfdisk_partitions(blob: Path) -> list[dict[str, Any]]:
    """Return sfdisk's partition list for ``blob`` (``node``/``start``/
    ``size``/``type`` dicts). Raises :class:`PartitionNotFound` on a
    missing blob, a missing / non-JSON sfdisk, or an unpartitioned image.
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
    return table.get("partitions") or []


def _partition_number(node: str) -> int | None:
    """Trailing partition index off an sfdisk ``node`` (``/dev/loop0p3``
    -> 3, ``disk.img2`` -> 2). ``None`` when there is no trailing digit."""
    i = len(node)
    while i > 0 and node[i - 1].isdigit():
        i -= 1
    return int(node[i:]) if i < len(node) else None


def root_partition_number(blob: Path) -> int:
    """Pick the partition most likely to hold the Linux root filesystem.

    nosi disk images do NOT agree on partition order: the ubuntu / debian
    cloud images put root at p1 (ESP + BIOS-boot land at p13-15), while
    arch / fedora put BIOS-boot + ESP first and root at p3. Hardcoding p1
    extracts a 1 MiB BIOS-boot stub on the latter, which then fails to
    mount at boot. Select by GPT type instead: an explicit Linux-root
    type GUID wins; else the largest generic-Linux-filesystem partition;
    else the largest partition that is not firmware / boot / swap.

    Raises :class:`PartitionNotFound` if the table has no viable candidate.
    """
    parts = _sfdisk_partitions(blob)
    cands = []
    for entry in parts:
        num = _partition_number(str(entry.get("node") or ""))
        size = entry.get("size")
        if num is None or size is None:
            continue
        cands.append((num, str(entry.get("type") or "").strip().lower(), int(size)))
    if not cands:
        raise PartitionNotFound(f"no partitions on {blob!s}")

    tiers: tuple[Callable[[str], bool], ...] = (
        lambda t: t in _LINUX_ROOT_GUIDS,
        lambda t: t in _LINUX_FS_TYPES,
        lambda t: t not in _NON_ROOT_TYPES,
    )
    for tier in tiers:
        matched = [c for c in cands if tier(c[1])]
        if matched:
            # Largest matching partition wins the tier.
            return max(matched, key=lambda c: c[2])[0]

    raise PartitionNotFound(
        f"no root-filesystem candidate among {len(cands)} partition(s) on {blob!s}"
    )


def partition_info(blob: Path, partition_number: int = 1) -> PartitionInfo:
    """Return the byte range of ``partition_number`` on ``blob``.

    Raises :class:`PartitionNotFound` if the blob is unpartitioned,
    the specified partition is absent, or sfdisk fails to parse.
    """
    parts = _sfdisk_partitions(blob)

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
