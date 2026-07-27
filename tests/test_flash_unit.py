"""Unit tests for pixie.flash pure helpers.

flash.py is the destructive disk-writing core; its streaming path needs
real block devices (integration-only), but the digest-normalise /
integrity-verify / compressed-size-parse helpers are pure and were
previously untested. These guard the size-fits check (which decides
whether an image is allowed to flash at all) and the post-write
integrity verify against silent breakage.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pixie.flash import (
    FlashIntegrityError,
    ImageInfo,
    TargetInfo,
    _normalize_digest,
    _parse_compressed_listing,
    _parse_gzip_listing,
    _sha256_file,
    _verify_digest,
    make_plan,
    probe_image,
    probe_target,
    validate_plan,
)


def _block_target(size: int | None, mountpoints: list[str] | None = None) -> TargetInfo:
    """A plausible block-device target, built directly so the pure
    validate/plan branches are testable without root or a real device."""
    return TargetInfo(
        path=Path("/dev/fake"),
        exists=True,
        is_block_device=True,
        size_bytes=size,
        mountpoints=mountpoints or [],
    )


def _img(path: str, fmt: str | None, vsize: int | None) -> ImageInfo:
    return ImageInfo(path=Path(path), format=fmt, size_bytes=vsize or 1, virtual_size_bytes=vsize)


def test_normalize_digest_none() -> None:
    assert _normalize_digest(None) is None


def test_normalize_digest_bare_hex_lowercased_and_prefixed() -> None:
    assert _normalize_digest("  ABCDEF0123  ") == "sha256:abcdef0123"


def test_normalize_digest_passthrough_when_prefixed() -> None:
    assert _normalize_digest("sha256:deadbeef") == "sha256:deadbeef"


def test_verify_digest_match_no_raise() -> None:
    _verify_digest("sha256:x", "sha256:x", "http://u")  # no raise


def test_verify_digest_observed_none_is_noop() -> None:
    # A source that couldn't compute a digest (no oras layer, no declared
    # sha) verifies as a no-op rather than failing the flash.
    _verify_digest("sha256:x", None, "http://u")  # no raise


def test_verify_digest_mismatch_raises() -> None:
    with pytest.raises(FlashIntegrityError):
        _verify_digest("sha256:aaa", "sha256:bbb", "http://u")


def test_parse_gzip_listing_normal() -> None:
    out = (
        "         compressed        uncompressed  ratio uncompressed_name\n"
        "               1000                5000 -80.0% img\n"
    )
    assert _parse_gzip_listing(out) == 5000


def test_parse_gzip_listing_wrap_returns_none() -> None:
    # gzip stores uncompressed size mod 4 GiB; when the reported value is
    # smaller than the compressed bytes it wrapped and must be refused so
    # validate_plan doesn't fit a too-big image onto a too-small disk.
    out = "compressed uncompressed ratio name\n 5000 1000 -1% img\n"
    assert _parse_gzip_listing(out) is None


def test_parse_gzip_listing_garbage_returns_none() -> None:
    assert _parse_gzip_listing("") is None
    assert _parse_gzip_listing("not a table\n") is None


def test_parse_compressed_listing_zstd() -> None:
    listing = (
        "Frames  Skips  Compressed  Uncompressed  Ratio  Check  Filename\n"
        "     1      0    12.5 KiB     900.00 MiB  72.00  XXH64  img.zst\n"
    )
    assert _parse_compressed_listing(listing, header_prefix="Frames") == 900 * 1024**2


def test_parse_compressed_listing_xz() -> None:
    listing = (
        "Strms  Blocks   Compressed Uncompressed  Ratio  Check   Filename\n"
        "    1       1     10.0 MiB      2.0 GiB  0.005  CRC64   img.xz\n"
    )
    assert _parse_compressed_listing(listing, header_prefix="Strms") == 2 * 1024**3


def test_parse_compressed_listing_no_data_row_returns_none() -> None:
    assert _parse_compressed_listing("Frames Skips ...\n", header_prefix="Frames") is None


def test_sha256_file_matches_hashlib(tmp_path) -> None:
    data = b"pixie flash test bytes"
    p = tmp_path / "blob"
    p.write_bytes(data)
    assert _sha256_file(p) == "sha256:" + hashlib.sha256(data).hexdigest()


# ---- probe + plan + validate (pure; no root or real block device) -----


def test_probe_image_raw_img_reports_format_and_size(tmp_path) -> None:
    p = tmp_path / "disk.img"
    p.write_bytes(b"\0" * 4096)
    info = probe_image(p)
    assert info.format == "img"
    assert info.size_bytes == 4096
    assert info.virtual_size_bytes == 4096  # raw: on-disk size == bytes written


def test_probe_image_missing_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        probe_image(tmp_path / "nope.img")


def test_probe_image_unrecognized_extension_has_no_format(tmp_path) -> None:
    p = tmp_path / "mystery.bin"
    p.write_bytes(b"x")
    assert probe_image(p).format is None


def test_probe_target_missing_path(tmp_path) -> None:
    t = probe_target(tmp_path / "absent")
    assert not t.exists and not t.is_block_device and t.size_bytes is None


def test_probe_target_regular_file_is_not_a_block_device(tmp_path) -> None:
    p = tmp_path / "regular"
    p.write_bytes(b"x")
    t = probe_target(p)
    assert t.exists and not t.is_block_device


def test_validate_ok_when_image_fits_block_target() -> None:
    plan = make_plan(_img("d.img", "img", 100), _block_target(1000))
    assert validate_plan(plan) == []


def test_validate_rejects_unrecognized_format() -> None:
    errs = validate_plan(make_plan(_img("mystery.bin", None, None), _block_target(1000)))
    assert any("format not recognised" in e for e in errs)


def test_validate_rejects_tarball_with_extract_hint() -> None:
    errs = validate_plan(make_plan(_img("bundle.tar.gz", None, None), _block_target(1000)))
    assert any("tarball" in e and "Extract first" in e for e in errs)


def test_validate_rejects_missing_target() -> None:
    tgt = TargetInfo(
        path=Path("/dev/gone"),
        exists=False,
        is_block_device=False,
        size_bytes=None,
        mountpoints=[],
    )
    errs = validate_plan(make_plan(_img("d.img", "img", 1), tgt))
    assert any("does not exist" in e for e in errs)


def test_validate_rejects_non_block_target() -> None:
    tgt = TargetInfo(
        path=Path("/some/file"),
        exists=True,
        is_block_device=False,
        size_bytes=None,
        mountpoints=[],
    )
    errs = validate_plan(make_plan(_img("d.img", "img", 1), tgt))
    assert any("not a block device" in e for e in errs)


def test_validate_rejects_mounted_target() -> None:
    tgt = _block_target(1000, mountpoints=["/mnt/data"])
    errs = validate_plan(make_plan(_img("d.img", "img", 1), tgt))
    assert any("mounted partitions" in e for e in errs)


def test_validate_rejects_image_larger_than_target() -> None:
    errs = validate_plan(make_plan(_img("big.img", "img", 2000), _block_target(1000)))
    assert any("larger than target" in e for e in errs)


def test_make_plan_notes_unknown_virtual_size() -> None:
    img = ImageInfo(
        path=None,
        url="http://x/i.img.gz",
        format="img.gz",
        size_bytes=10,
        virtual_size_bytes=None,
    )
    plan = make_plan(img, _block_target(1000))
    assert any("size-fits-target check skipped" in n for n in plan.notes)
