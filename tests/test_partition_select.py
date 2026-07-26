"""Root-partition selection: pixie extracts ``rootfs.raw`` by GPT type,
not by a fixed partition number, because nosi images disagree on order
(ubuntu/debian root=p1; arch/fedora root=p3 behind BIOS-boot + ESP).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pixie import _partition
from pixie._partition import PartitionNotFound, root_partition_number

# GPT type GUIDs used across the fixtures.
BIOS = "21686148-6449-6E6F-744E-656564454649"
EFI = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
ROOT_X64 = "4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709"
LINUX_FS = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
XBOOTLDR = "BC13C2FF-59E6-4262-A352-B275FD6F7172"
SWAP = "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F"


def p(n: int, size: int, type_: str) -> dict:
    """Compact sfdisk-style partition entry (start is irrelevant here)."""
    return {"node": f"/dev/x{n}", "start": n * 4096, "size": size, "type": type_}


# Partition lists (sectors), lifted from real nosi images.
_ARCH = [p(1, 2048, BIOS), p(2, 614400, EFI), p(3, 24547295, ROOT_X64)]
_UBUNTU = [
    p(1, 18927312, LINUX_FS),  # root
    p(13, 2095105, XBOOTLDR),  # /boot
    p(14, 8192, BIOS),
    p(15, 217088, EFI),
]


@pytest.fixture
def _patch(monkeypatch):
    def _set(parts):
        monkeypatch.setattr(_partition, "_sfdisk_partitions", lambda blob: parts)

    return _set


def test_arch_layout_picks_p3(_patch):
    """BIOS-boot p1 + ESP p2 must not be mistaken for root; p3 wins."""
    _patch(_ARCH)
    assert root_partition_number(Path("/x")) == 3


def test_ubuntu_layout_picks_p1(_patch):
    """No regression: the generic-Linux-fs root at p1 still wins over the
    ESP / BIOS-boot / XBOOTLDR partitions."""
    _patch(_UBUNTU)
    assert root_partition_number(Path("/x")) == 1


def test_explicit_root_guid_beats_larger_generic(_patch):
    """A tagged Linux-root partition wins its tier even when a generic
    Linux-fs partition is larger (tier order, not size, decides first)."""
    parts = [p(1, 999999, LINUX_FS), p(2, 4096, ROOT_X64)]
    _patch(parts)
    assert root_partition_number(Path("/x")) == 2


def test_largest_generic_wins_within_tier(_patch):
    """Two generic-Linux-fs partitions: the larger is the root."""
    parts = [p(1, 4096, LINUX_FS), p(2, 900000, LINUX_FS)]
    _patch(parts)
    assert root_partition_number(Path("/x")) == 2


def test_mbr_type_83_is_root(_patch):
    """MBR tables report a 2-hex type; 0x83 (Linux) is a root candidate."""
    parts = [p(1, 4096, "ef"), p(2, 900000, "83")]
    _patch(parts)
    assert root_partition_number(Path("/x")) == 2


def test_last_resort_skips_system_partitions(_patch):
    """With no typed Linux partition, the largest non-firmware/boot/swap
    partition wins; a lone huge ESP + swap must not be selected."""
    parts = [
        p(1, 9_000_000, EFI),
        p(2, 8192, SWAP),
        p(3, 500000, "0b0b0b0b-0000-0000-0000-000000000000"),  # unknown data
    ]
    _patch(parts)
    assert root_partition_number(Path("/x")) == 3


def test_all_system_partitions_raises(_patch):
    """A table with only ESP + BIOS-boot + swap has no root candidate."""
    parts = [p(1, 4096, BIOS), p(2, 4096, EFI), p(3, 4096, SWAP)]
    _patch(parts)
    with pytest.raises(PartitionNotFound):
        root_partition_number(Path("/x"))


def test_unpartitioned_raises(_patch):
    """No partitions at all -> PartitionNotFound (caller falls back to
    serving the whole blob)."""
    _patch([])
    with pytest.raises(PartitionNotFound):
        root_partition_number(Path("/x"))
