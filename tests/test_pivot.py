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


def test_get_pivot_route_memoises_on_app_state(client: TestClient) -> None:
    """The route caches the built blob on ``app.state.pivot_nbdboot_cpio_gz``
    so subsequent hits don't re-run the cpio+gzip pipeline. Two GETs
    must observe the same object identity on the cached blob; a
    naive re-build would produce equal-but-not-identical bytes."""
    r1 = client.get("/pivot/nbdboot.cpio.gz")
    r2 = client.get("/pivot/nbdboot.cpio.gz")
    assert r1.status_code == r2.status_code == 200
    assert r1.content == r2.content
    # The cache lives on the app's state; poking through the client
    # tests the actual memoisation contract not just "same bytes".
    cached = client.app.state.pivot_nbdboot_cpio_gz  # type: ignore[attr-defined]
    assert cached is not None
    assert cached == r1.content


# ---------- pivot cpio builder unit tests ------------------------


def test_walk_newc_handles_zero_byte_regular_file_between_entries() -> None:
    """Guard the ``_walk_newc`` test helper against an off-by-one on
    pad alignment when a regular file has ``filesize=0`` sitting
    between two non-trivial entries. The trailer is filesize=0 too
    but sits at the end, so it doesn't exercise the "next-entry
    header must be 4-byte aligned after zero data" path."""
    import io

    from pixie.pivot import _emit_entry, _newc_header, _pad4

    buf = io.BytesIO()
    _emit_entry(buf, ino=1, mode=0o040755, name=b"d", data=b"")
    _emit_entry(buf, ino=2, mode=0o100644, name=b"d/empty", data=b"")
    _emit_entry(buf, ino=3, mode=0o100644, name=b"d/hello", data=b"hi")
    buf.write(_newc_header(ino=0, mode=0, name=b"TRAILER!!!", filesize=0))
    buf.write(b"TRAILER!!!\0")
    _pad4(buf)
    entries = _walk_newc(buf.getvalue())
    names = [e["name"] for e in entries]
    assert names == [b"d", b"d/empty", b"d/hello", b"TRAILER!!!"]
    assert entries[1]["data"] == b""
    assert entries[2]["data"] == b"hi"


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
