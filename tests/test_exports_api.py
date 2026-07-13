"""Exports HTTP API + supervisor integration.

Uses a fake ``nbdkit`` binary (``python3 -c "import time; time.sleep(...)"``)
so CI + local runs don't need the real nbdkit installed. The subprocess
still binds a real TCP port through nbdkit's argv, which the fake
faithfully ignores; the port allocator only reserves ports via
``socket.bind``, so a running fake process holds no port. We assert on
the store state (name / status / port slot claimed) rather than on
NBD-protocol traffic.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_ADMIN_PASSWORD

# Fake "nbdkit" -- a Python one-liner that sleeps forever + prints its
# argv to stderr. The supervisor's _SPAWN_STARTUP_GRACE gives the fake
# time to reach the sleep before poll() runs, so the spawn appears live.
_FAKE_NBDKIT = f"{sys.executable}"


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, state_dir: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("PIXIE_ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    monkeypatch.setenv("PIXIE_DATA_DIR", str(state_dir))
    # Bind to loopback so parallel test runs on 0.0.0.0 don't clash.
    monkeypatch.setenv("PIXIE_NBD_BIND", "127.0.0.1")
    # Use a high port_base so we avoid the real nbdmux on the dev
    # box (10809) or bty-web on the test box (8080-8082).
    monkeypatch.setenv("PIXIE_NBD_PORT_BASE", "20809")
    # Stub the nbdkit binary with a python sleep loop.
    monkeypatch.setenv("PIXIE_NBDKIT_BIN", _FAKE_NBDKIT)
    from pixie.web.main import create_app

    app = create_app()
    with TestClient(app) as c:
        c.post("/ui/login", data={"password": TEST_ADMIN_PASSWORD})
        try:
            yield c
        finally:
            # Ensure no lingering fake children.
            app.state.nbd_server.stop()


def _install_blob(state_dir: Path, sha: str, body: bytes = b"tiny-blob-bytes") -> Path:
    """Simulate a fetched catalog blob at ``<state_dir>/blobs/<sha>/blob``.

    The exports registration requires the blob file to exist on disk
    (the routes reject the register otherwise). Tests bypass the fetch
    pipeline and lay the file down directly.
    """
    blob = state_dir / "blobs" / sha / "blob"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(body)
    return blob


def _mock_nbdkit_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rewrite the supervisor's Popen so the argv it renders for
    ``nbdkit`` becomes a python sleep-forever loop with the same argv
    for logging. Keeps the port allocator + status tracking honest
    without depending on nbdkit itself."""
    import subprocess

    real_popen = subprocess.Popen

    def _stub_popen(argv: list[str], *a: object, **kw: object) -> object:
        # Replace nbdkit-specific argv with a python sleep-forever.
        return real_popen(
            [sys.executable, "-c", "import time; time.sleep(3600)"],
            *a,
            **kw,
        )

    monkeypatch.setattr("pixie.exports._supervisor.subprocess.Popen", _stub_popen)


# ---------- routes: register / list / delete ---------------------------


def test_register_export_404_when_blob_missing(client: TestClient) -> None:
    r = client.post(
        "/exports",
        json={"name": "missing", "content_sha256": "b" * 64},
    )
    assert r.status_code == 400
    assert "Fetch the catalog entry first" in r.json()["detail"]


def test_register_export_requires_session(monkeypatch: pytest.MonkeyPatch, state_dir: Path) -> None:
    """No session cookie -> 401 on the write route."""
    monkeypatch.setenv("PIXIE_ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    monkeypatch.setenv("PIXIE_DATA_DIR", str(state_dir))
    monkeypatch.setenv("PIXIE_NBD_BIND", "127.0.0.1")
    monkeypatch.setenv("PIXIE_NBD_PORT_BASE", "20909")
    from pixie.web.main import create_app

    with TestClient(create_app()) as c:
        r = c.post(
            "/exports",
            json={"name": "x", "content_sha256": "b" * 64},
        )
        assert r.status_code == 401


def test_register_export_bad_name_returns_400(client: TestClient) -> None:
    """Names must match the allowlist so nbdkit's -e param stays sane."""
    r = client.post(
        "/exports",
        json={"name": "has spaces", "content_sha256": "b" * 64},
    )
    assert r.status_code == 400


def test_register_and_list_export(
    client: TestClient, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_nbdkit_argv(monkeypatch)
    sha = "a" * 64
    _install_blob(state_dir, sha)

    r = client.post(
        "/exports",
        json={"name": "tiny.img", "content_sha256": sha},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "tiny.img"
    assert body["content_sha256"] == sha
    assert body["nbd_port"] >= 20809
    assert body["status"] == "running"

    entries = client.get("/exports").json()["exports"]
    assert [e["name"] for e in entries] == ["tiny.img"]
    assert entries[0]["status"] == "running"


def test_register_conflict_returns_409(
    client: TestClient, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_nbdkit_argv(monkeypatch)
    sha = "a" * 64
    _install_blob(state_dir, sha)

    assert client.post("/exports", json={"name": "dup", "content_sha256": sha}).status_code == 201
    r = client.post("/exports", json={"name": "dup", "content_sha256": sha})
    assert r.status_code == 409


def test_delete_export_removes_and_kills(
    client: TestClient, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_nbdkit_argv(monkeypatch)
    sha = "c" * 64
    _install_blob(state_dir, sha)

    client.post("/exports", json={"name": "goner", "content_sha256": sha})
    r = client.delete("/exports/goner")
    assert r.status_code == 204
    assert client.get("/exports").json()["exports"] == []


def test_delete_missing_returns_404(client: TestClient) -> None:
    r = client.delete("/exports/nope")
    assert r.status_code == 404


# ---------- supervisor unit tests --------------------------------------


def test_supervisor_port_allocation_and_termination(
    monkeypatch: pytest.MonkeyPatch, state_dir: Path
) -> None:
    """The supervisor gives distinct ports to concurrent exports and
    frees them on terminate."""
    from pixie.exports._supervisor import NbdServer

    _mock_nbdkit_argv(monkeypatch)
    blob1 = _install_blob(state_dir, "1" * 64, b"blob-1")
    blob2 = _install_blob(state_dir, "2" * 64, b"blob-2")

    srv = NbdServer(port_base=21009, bind="127.0.0.1", nbdkit_bin=_FAKE_NBDKIT)
    try:
        p1 = srv.spawn("a", blob1)
        p2 = srv.spawn("b", blob2)
        assert p1 != p2
        assert srv.is_running("a") is True
        assert srv.is_running("b") is True
        assert srv.port_for("a") == p1

        # Idempotent: second spawn of same name returns the same port.
        assert srv.spawn("a", blob1) == p1

        # Terminate frees the slot.
        assert srv.terminate("a") is True
        assert srv.is_running("a") is False
        assert srv.port_for("a") is None
    finally:
        srv.stop()


def test_supervisor_spawn_missing_blob_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from pixie.exports._supervisor import NbdServer

    srv = NbdServer(port_base=21109, bind="127.0.0.1", nbdkit_bin=_FAKE_NBDKIT)
    try:
        with pytest.raises(RuntimeError, match="does not exist"):
            srv.spawn("x", Path("/nonexistent/blob"))
    finally:
        srv.stop()


def test_supervisor_file_looks_partitioned(state_dir: Path) -> None:
    from pixie.exports._supervisor import file_looks_partitioned

    part = state_dir / "part.img"
    part.parent.mkdir(parents=True, exist_ok=True)
    buf = bytearray(2048)
    buf[510] = 0x55
    buf[511] = 0xAA
    part.write_bytes(bytes(buf))
    assert file_looks_partitioned(part) is True

    raw = state_dir / "raw.img"
    raw.write_bytes(b"\0" * 2048)
    assert file_looks_partitioned(raw) is False

    tiny = state_dir / "tiny.img"
    tiny.write_bytes(b"\x55\xaa")  # too small to have the sig at 510-511
    assert file_looks_partitioned(tiny) is False
