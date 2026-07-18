"""Pivot overlay: cpio builder + HTTP serve.

pixie ships the nbdboot pivot script here rather than baking it into
nosi's netboot bundle. Downstream targets load the overlay after
the base initrd; Linux merges same-path entries so
``/scripts/nbdboot`` from the overlay wins.

Contract verified below:

- ``build_pivot_cpio()`` produces a well-formed newc-cpio: magic +
  the two entries + TRAILER!!! at the end.
- ``build_pivot_cpio_gz()`` produces bytes decompressible with the
  stock ``gunzip`` handler.
- ``GET /pivot/nbdboot.cpio.gz`` serves the gz bytes with the
  right content-type + a stable ETag (byte-identical builds).
- HEAD is served for probe-friendliness (same shape as ``/b/``).
"""

from __future__ import annotations

import gzip

from fastapi.testclient import TestClient


def test_cpio_starts_with_newc_magic() -> None:
    from pixie.pivot import build_pivot_cpio

    raw = build_pivot_cpio()
    assert raw.startswith(b"070701"), raw[:20]


def test_cpio_contains_scripts_nbdboot_entry_and_trailer() -> None:
    """Parse the newc entries by walking the 110-byte-header + name +
    pad + data + pad shape and assert the expected sequence: a
    ``scripts`` directory, a ``scripts/nbdboot`` regular file with
    the source-tree script bytes, and a ``TRAILER!!!`` sentinel."""
    from pixie.pivot import NBDBOOT_SCRIPT, build_pivot_cpio

    raw = build_pivot_cpio()
    entries = _walk_newc(raw)
    names = [e["name"] for e in entries]
    assert names == [b"scripts", b"scripts/nbdboot", b"TRAILER!!!"]
    nbdboot = entries[1]
    # scripts/nbdboot's payload is the exact bytes from the source-tree
    # ``pivot/nbdboot`` file; renaming a variable in the script must
    # flow through untouched.
    assert nbdboot["data"] == NBDBOOT_SCRIPT
    # Mode: regular file (0o100000) + 0755 permissions.
    assert nbdboot["mode"] == 0o100755


def test_cpio_gz_roundtrips_through_gunzip() -> None:
    from pixie.pivot import build_pivot_cpio, build_pivot_cpio_gz

    gz = build_pivot_cpio_gz()
    assert gz.startswith(b"\x1f\x8b")  # gzip magic
    round_tripped = gzip.decompress(gz)
    assert round_tripped == build_pivot_cpio()


def test_cpio_gz_is_deterministic_across_builds() -> None:
    """The kernel + iPXE don't care, but a stable byte output lets
    caches and content-hashers (a future CDN, a distro packager)
    treat the overlay as immutable per-pixie-release. mtime=0 on
    every entry + on the gzip header pins this."""
    from pixie.pivot import build_pivot_cpio_gz

    assert build_pivot_cpio_gz() == build_pivot_cpio_gz()


def test_get_pivot_route_serves_the_overlay(client: TestClient) -> None:
    from pixie.pivot import build_pivot_cpio_gz

    r = client.get("/pivot/nbdboot.cpio.gz")
    assert r.status_code == 200
    # Content-type mirrors the ``/b/`` convention (raw archive).
    assert r.headers["content-type"] in ("application/gzip", "application/octet-stream")
    assert r.content == build_pivot_cpio_gz()


def test_head_pivot_route_returns_length_no_body(client: TestClient) -> None:
    """HEAD support: iPXE + the pixie CLI probe URLs with HEAD before
    committing to a fetch; a 405 there stalls the boot chain (same
    class as the ``/b/`` fix)."""
    from pixie.pivot import build_pivot_cpio_gz

    r = client.head("/pivot/nbdboot.cpio.gz")
    assert r.status_code == 200
    assert r.content == b""
    assert int(r.headers["content-length"]) == len(build_pivot_cpio_gz())


# ---------- test helper: minimal newc-cpio walker ----------


def _walk_newc(raw: bytes) -> list[dict]:
    """Return one dict per newc-cpio entry: {name, mode, data}. Not a
    general-purpose parser -- only enough to verify our builder."""
    entries: list[dict] = []
    off = 0
    while off < len(raw):
        header = raw[off : off + 110]
        assert header[:6] == b"070701", f"bad magic at offset {off}"
        mode = int(header[14:22], 16)
        filesize = int(header[54:62], 16)
        namesize = int(header[94:102], 16)
        name_start = off + 110
        name_end = name_start + namesize - 1  # strip trailing NUL
        name = raw[name_start:name_end]
        data_start = _pad4_align(name_start + namesize)
        data_end = data_start + filesize
        data = raw[data_start:data_end]
        entries.append({"name": name, "mode": mode, "data": data})
        next_off = _pad4_align(data_end)
        # Trailer sentinel terminates the archive.
        if name == b"TRAILER!!!":
            break
        off = next_off
    return entries


def _pad4_align(n: int) -> int:
    over = n & 3
    return n + (4 - over) if over else n
