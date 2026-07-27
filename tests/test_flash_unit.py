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

import pytest

from pixie.flash import (
    FlashIntegrityError,
    _normalize_digest,
    _parse_compressed_listing,
    _parse_gzip_listing,
    _sha256_file,
    _verify_digest,
)


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
