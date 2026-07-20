"""Read a partition-table entry's byte offset off a raw disk image.

Pixie's persistent-overlay path serves per-machine qcow2 files whose
``backing_file`` is the shared whole-disk blob. To make the target-side
initrd's job trivial (no partition-scan race), qemu-nbd is spawned with
``--offset=<partition_1_start_bytes>`` so ``/dev/nbd0`` on the target
becomes the ext4 root partition at offset 0, the same shape nbdkit's
``--filter=partition partition=1`` produces for the ephemeral path.

This module wraps ``sfdisk --json <path>`` for the parse. sfdisk on
a raw disk image reads the GPT / MBR directly and reports partitions
by sector offset, which we convert to bytes here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

_SECTOR_BYTES = 512


class PartitionNotFound(RuntimeError):
    """Raised when a requested partition number is not present on
    the blob (unpartitioned image, missing partition, sfdisk parse
    failure). Callers treat this as "serve the whole image, offset
    0" so an unpartitioned blob still works via the same code path.
    """


def partition_start_bytes(blob: Path, partition_number: int = 1) -> int:
    """Return the byte offset of ``partition_number`` on ``blob``.

    Raises :class:`PartitionNotFound` if the blob is unpartitioned,
    the specified partition is absent, or sfdisk fails to parse.
    Callers decide whether to fall back to offset 0 (whole-disk
    serve) or to fail hard; this helper does not embed that policy.
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
        if node.endswith(target_suffix) and not node.endswith(target_suffix + "0"):
            # Distinguish blob1 from blob10, blob11, etc. by ensuring
            # the digit before the suffix isn't itself a digit.
            head = node[: -len(target_suffix)]
            if not head or not head[-1].isdigit():
                start_sectors = entry.get("start")
                if start_sectors is None:
                    raise PartitionNotFound(
                        f"partition {partition_number} on {blob!s} has no start sector"
                    )
                return int(start_sectors) * _SECTOR_BYTES

    raise PartitionNotFound(
        f"partition {partition_number} not found on {blob!s} (sfdisk reported "
        f"{len(parts)} partition(s))"
    )
