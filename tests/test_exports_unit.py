"""Unit tests for the exports surface that DO NOT touch nbdkit.

Anything requiring a real nbdkit subprocess -- register + list +
delete + port allocation on a live NBD server -- lives in
``tests/integration/test_exports.py``, which runs against the actual
pixie container. These unit tests only cover surface that is
purely-Python-in-the-fastapi-process:

* Pydantic body validation.
* The MBR/GPT partition-sig heuristic on a synthetic file.
* Route-level auth checks (the write route rejects unauthed
  callers before ever reaching the supervisor).

Register/list/delete flows are DELIBERATELY not tested here. Stubbing
``subprocess.Popen`` to make them pass produces confidence in the
argv construction that doesn't survive contact with real nbdkit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_ADMIN_PASSWORD


def test_partition_sig_matches_boot_sector(tmp_path: Path) -> None:
    """A raw disk image with 0x55/0xAA at bytes 510-511 is treated
    as partitioned; anything else is not."""
    from pixie.exports._supervisor import file_looks_partitioned

    part = tmp_path / "part.img"
    buf = bytearray(2048)
    buf[510] = 0x55
    buf[511] = 0xAA
    part.write_bytes(bytes(buf))
    assert file_looks_partitioned(part) is True

    raw = tmp_path / "raw.img"
    raw.write_bytes(b"\0" * 2048)
    assert file_looks_partitioned(raw) is False

    tiny = tmp_path / "tiny.img"
    tiny.write_bytes(b"\x55\xaa")
    assert file_looks_partitioned(tiny) is False

    missing = tmp_path / "does-not-exist.img"
    assert file_looks_partitioned(missing) is False


def test_register_export_requires_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Write route rejects unauthed callers before reaching the
    supervisor."""
    monkeypatch.setenv("PIXIE_ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    monkeypatch.setenv("PIXIE_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PIXIE_NBD_BIND", "127.0.0.1")
    from pixie.web.main import create_app

    with TestClient(create_app()) as c:
        r = c.post("/exports", json={"name": "x", "content_sha256": "b" * 64})
        assert r.status_code == 401


def test_register_export_rejects_bad_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The name allowlist prevents nbdkit -e argv smuggling."""
    monkeypatch.setenv("PIXIE_ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    monkeypatch.setenv("PIXIE_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PIXIE_NBD_BIND", "127.0.0.1")
    from pixie.web.main import create_app

    with TestClient(create_app()) as c:
        c.post("/ui/login", data={"password": TEST_ADMIN_PASSWORD})
        r = c.post(
            "/exports",
            json={"name": "has spaces", "content_sha256": "b" * 64},
        )
        assert r.status_code == 400


def test_register_export_rejects_missing_blob(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Register before Fetch: 400 with the exact operator hint the UI
    shows."""
    monkeypatch.setenv("PIXIE_ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    monkeypatch.setenv("PIXIE_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PIXIE_NBD_BIND", "127.0.0.1")
    from pixie.web.main import create_app

    with TestClient(create_app()) as c:
        c.post("/ui/login", data={"password": TEST_ADMIN_PASSWORD})
        r = c.post(
            "/exports",
            json={"name": "missing", "content_sha256": "a" * 64},
        )
        assert r.status_code == 400
        assert "Fetch the catalog entry first" in r.json()["detail"]


def test_delete_missing_export_returns_404(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PIXIE_ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    monkeypatch.setenv("PIXIE_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PIXIE_NBD_BIND", "127.0.0.1")
    from pixie.web.main import create_app

    with TestClient(create_app()) as c:
        c.post("/ui/login", data={"password": TEST_ADMIN_PASSWORD})
        r = c.delete("/exports/nope")
        assert r.status_code == 404
