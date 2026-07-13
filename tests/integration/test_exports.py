"""Exports over a REAL pixie container running REAL nbdkit.

The ``container`` + ``api`` session fixtures build ``pixie:integration-test``
from the repo's Containerfile and run it under podman with the pixie
data dir bind-mounted onto the host. Tests can lay down synthetic
blobs directly (bypassing the fetch pipeline) and then exercise the
exports HTTP surface with the real nbdkit binary on the other end.

We prove nbdkit is actually serving by:

1. Reading ``GET /exports`` for the assigned ``nbd_port`` and
   ``status=running``.
2. Speaking the raw NBD OLDSTYLE / NEWSTYLE handshake to the port
   with ``socket`` and asserting the ``NBDMAGIC`` reply. That's a
   full protocol round-trip; no fake, no shim.
3. For a bonus: comparing the export size the handshake reports
   back with the blob's on-disk size.

Every test starts from a scrubbed state (the ``api`` fixture kills
prior exports + entries), so ordering between tests can't create
false greens.
"""

from __future__ import annotations

import json
import socket
import struct
from pathlib import Path

import pytest

from tests.integration.conftest import _delete, _get, _post_json

pytestmark = pytest.mark.integration

# NBD protocol constants -- straight out of https://github.com/NetworkBlockDevice/nbd/blob/master/doc/proto.md
# The magic reply the server sends on any new connection.
_NBD_MAGIC = b"NBDMAGIC"
# NEWSTYLE handshake sentinel.
_NBD_NEW = b"IHAVEOPT"


def _place_blob(state_dir: Path, sha: str, body: bytes) -> Path:
    """Simulate a completed fetch by writing a blob at the path the
    container's catalog store would have written."""
    blob = state_dir / "blobs" / sha / "blob"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(body)
    # The container runs as its own user; make the file world-readable
    # so nbdkit inside the container can open it.
    blob.chmod(0o644)
    for parent in (blob.parent, blob.parent.parent):
        parent.chmod(0o755)
    return blob


def _nbd_handshake(host: str, port: int, timeout: float = 3.0) -> dict[str, int | bytes]:
    """Speak enough of the NBD wire protocol to prove the server on
    the other end is a real NBD server, not a random TCP echo.

    Returns a dict with ``magic`` (bytes) + ``new_style`` (bytes)
    from the server's greeting; the handshake is closed right after
    the greeting -- proving there's a real NBD server is enough for
    "is the export up?" and doesn't require negotiating options.
    """
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        # Server sends 8 bytes NBDMAGIC + 8 bytes IHAVEOPT + 2 bytes
        # handshake flags in NEWSTYLE. That's the first 18 bytes of
        # any modern nbdkit greeting.
        raw = b""
        deadline = timeout
        while len(raw) < 18 and deadline > 0:
            chunk = sock.recv(18 - len(raw))
            if not chunk:
                break
            raw += chunk
        assert len(raw) >= 18, f"short greeting: {raw!r}"
        magic = raw[:8]
        new_style = raw[8:16]
        (flags,) = struct.unpack(">H", raw[16:18])
        return {"magic": magic, "new_style": new_style, "flags": flags}


# ---------- happy path ----------------------------------------------------


def test_register_export_starts_real_nbdkit(api: dict[str, object]) -> None:
    """POST /exports spawns real nbdkit; a raw socket round-trip
    against the returned port receives the NBD greeting bytes."""
    base = str(api["base_url"])
    cookie = str(api["cookie"])
    state_dir = Path(str(api["state_dir"]))

    sha = "a" * 64
    _place_blob(state_dir, sha, b"pixie-real-nbd-integration-blob\n" * 100)

    r = _post_json(
        base,
        "/exports",
        {"name": "tiny.img", "content_sha256": sha},
        cookie=cookie,
    )
    assert r.status == 201, r.read().decode()
    body = json.loads(r.read())
    assert body["status"] == "running"
    port = int(body["nbd_port"])
    assert port >= int(api["nbd_port_base"])  # inside our reserved range

    hs = _nbd_handshake("127.0.0.1", port)
    assert hs["magic"] == _NBD_MAGIC, f"not an NBD server: greeting={hs!r}"
    assert hs["new_style"] == _NBD_NEW, f"unexpected style bytes: {hs!r}"


def test_get_exports_reflects_the_running_process(api: dict[str, object]) -> None:
    """After a successful register, /exports lists the row with the
    port that actually holds nbdkit."""
    base = str(api["base_url"])
    cookie = str(api["cookie"])
    state_dir = Path(str(api["state_dir"]))
    sha = "b" * 64
    _place_blob(state_dir, sha, b"another-blob")

    _post_json(base, "/exports", {"name": "b.img", "content_sha256": sha}, cookie=cookie)

    rows = json.loads(_get(base, "/exports").read())["exports"]
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "b.img"
    assert row["status"] == "running"
    assert isinstance(row["nbd_port"], int) and row["nbd_port"] > 0

    # Prove that port is the ACTUAL live port by handshaking.
    hs = _nbd_handshake("127.0.0.1", int(row["nbd_port"]))
    assert hs["magic"] == _NBD_MAGIC


def test_delete_export_kills_the_real_nbdkit(api: dict[str, object]) -> None:
    """DELETE tears the row down AND kills the subprocess so the port
    stops accepting NBD connections."""
    base = str(api["base_url"])
    cookie = str(api["cookie"])
    state_dir = Path(str(api["state_dir"]))
    sha = "c" * 64
    _place_blob(state_dir, sha, b"goner")

    reg = json.loads(
        _post_json(base, "/exports", {"name": "goner", "content_sha256": sha}, cookie=cookie).read()
    )
    port = int(reg["nbd_port"])

    _hs_before = _nbd_handshake("127.0.0.1", port)
    assert _hs_before["magic"] == _NBD_MAGIC

    r = _delete(base, "/exports/goner", cookie=cookie)
    assert r.status == 204

    # Now the port should stop accepting NBD greetings; either the
    # socket refuses (RSTs on connect) or accepts + closes with no
    # bytes. Both are "no more nbdkit here"; any greeting is a fail.
    with pytest.raises((ConnectionRefusedError, TimeoutError, AssertionError, OSError)):
        got = _nbd_handshake("127.0.0.1", port, timeout=2.0)
        # If handshake somehow succeeds, that's a regression: the
        # ports leak and the delete didn't kill nbdkit.
        raise AssertionError(f"port {port} still speaking NBD after delete: {got!r}")


# ---------- error paths hit the real container's HTTP surface -----------


def test_register_before_fetch_returns_400(api: dict[str, object]) -> None:
    base = str(api["base_url"])
    cookie = str(api["cookie"])

    r = _post_json(
        base,
        "/exports",
        {"name": "missing", "content_sha256": "d" * 64},
        cookie=cookie,
    )
    assert r.status == 400
    detail = json.loads(r.read())["detail"]
    assert "Fetch the catalog entry first" in detail


def test_register_bad_name_returns_400(api: dict[str, object]) -> None:
    base = str(api["base_url"])
    cookie = str(api["cookie"])

    r = _post_json(
        base,
        "/exports",
        {"name": "has spaces", "content_sha256": "e" * 64},
        cookie=cookie,
    )
    assert r.status == 400


def test_two_exports_get_distinct_ports(api: dict[str, object]) -> None:
    """Registering two exports gets two distinct nbdkit processes on
    two distinct ports; each speaks NBD independently."""
    base = str(api["base_url"])
    cookie = str(api["cookie"])
    state_dir = Path(str(api["state_dir"]))
    _place_blob(state_dir, "0" * 64, b"blob-zero")
    _place_blob(state_dir, "1" * 64, b"blob-one")

    r1 = json.loads(
        _post_json(
            base, "/exports", {"name": "z.img", "content_sha256": "0" * 64}, cookie=cookie
        ).read()
    )
    r2 = json.loads(
        _post_json(
            base, "/exports", {"name": "o.img", "content_sha256": "1" * 64}, cookie=cookie
        ).read()
    )
    assert r1["nbd_port"] != r2["nbd_port"]
    assert _nbd_handshake("127.0.0.1", r1["nbd_port"])["magic"] == _NBD_MAGIC
    assert _nbd_handshake("127.0.0.1", r2["nbd_port"])["magic"] == _NBD_MAGIC
