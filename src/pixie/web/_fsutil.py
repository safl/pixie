"""Honest on-disk footprint helpers.

Both the Images rollup (:mod:`pixie.web._images`) and the Overlays
view-model (:mod:`pixie.web._overlays`) need the *allocated* size of a
file or directory -- ``st_blocks * 512``, the space actually consumed --
rather than ``st_size``, which over-counts a sparse/COW qcow2. The
formula lived inline in three places before; it lives here once so the
"allocated, not apparent" convention stays a single definition.
"""

from __future__ import annotations

from pathlib import Path


def file_allocated_bytes(path: Path) -> int:
    """Allocated on-disk bytes (``st_blocks * 512``) for one file.
    Missing or unreadable -> 0."""
    try:
        return int(getattr(path.stat(), "st_blocks", 0)) * 512
    except OSError:
        return 0


def dir_allocated_bytes(directory: Path) -> int:
    """Sum of :func:`file_allocated_bytes` over the files directly under
    ``directory`` -- the honest footprint of a blob/artifact/overlay dir.
    Missing dir -> 0. Non-recursive: pixie's state dirs are flat."""
    total = 0
    try:
        for p in directory.iterdir():
            if p.is_file():
                total += file_allocated_bytes(p)
    except OSError:
        return 0
    return total
