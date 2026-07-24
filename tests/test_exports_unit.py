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


def test_overlays_store_round_trip(tmp_path: Path) -> None:
    """OverlaysStore CRUD: create -> get -> list -> update runtime ->
    attach/detach -> delete. Doesn't touch qemu-nbd; purely the SQL
    layer. Identity is the globally-unique ``alias``."""
    from pixie.exports._store import ExportsStore, Overlay, OverlaysStore

    db_path = tmp_path / "state.db"
    # ExportsStore materialises the schema (both tables); OverlaysStore
    # just opens connections against it.
    ExportsStore(db_path)
    store = OverlaysStore(db_path)

    assert store.list_all() == []
    assert store.get("simon") is None

    ov = Overlay(alias="simon", image_sha="a" * 64, qcow2_path="/tmp/simon.qcow2")
    store.upsert(ov)

    fetched = store.get("simon")
    assert fetched is not None
    assert fetched.alias == "simon"
    assert fetched.attached_mac == ""  # free until attached
    assert fetched.status == "idle"

    store.update_runtime("simon", nbd_port=10809, status="running", error="")
    row = store.get("simon")
    assert row is not None
    assert row.nbd_port == 10809

    # A second alias over the same base image coexists.
    store.upsert(Overlay(alias="karl", image_sha="a" * 64, qcow2_path="/tmp/karl.qcow2"))
    aliases = [o.alias for o in store.list_for_image("a" * 64)]
    assert aliases == ["karl", "simon"]  # alphabetical by alias

    # Single-writer bookkeeping: attach records the holder, detach frees
    # it, and detach_mac releases every alias a machine held bar one.
    store.attach("simon", "aa:bb:cc:dd:ee:00")
    store.attach("karl", "aa:bb:cc:dd:ee:00")
    assert store.get("simon").attached_mac == "aa:bb:cc:dd:ee:00"  # type: ignore[union-attr]
    store.detach_mac("aa:bb:cc:dd:ee:00", keep="simon")
    assert store.get("simon").attached_mac == "aa:bb:cc:dd:ee:00"  # type: ignore[union-attr]
    assert store.get("karl").attached_mac == ""  # type: ignore[union-attr]
    store.detach("simon")
    assert store.get("simon").attached_mac == ""  # type: ignore[union-attr]

    # Delete lands.
    assert store.delete("simon") is True
    assert store.get("simon") is None
    assert store.delete("simon") is False


def test_overlays_schema_migrates_pre_alias_rows(tmp_path: Path) -> None:
    """A pre-alias overlays table (PK ``(mac, image_sha, profile)``) is
    re-keyed to the alias shape on first :class:`ExportsStore` open: each
    old row mints ``alias = <profile>-<mac_slug>``, seeds ``attached_mac``
    with its old MAC, and keeps its qcow2 path. Collisions get a ``-N``
    suffix so the alias stays globally unique."""
    import sqlite3

    from pixie.exports._store import ExportsStore, OverlaysStore

    db_path = tmp_path / "state.db"
    # Hand-build the OLD table shape + rows, exactly as a pre-re-model
    # deploy would have them on disk.
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE overlays (
            mac          TEXT NOT NULL,
            image_sha    TEXT NOT NULL,
            profile      TEXT NOT NULL,
            qcow2_path   TEXT NOT NULL,
            nbd_port     INTEGER NOT NULL DEFAULT 0,
            status       TEXT NOT NULL DEFAULT 'idle',
            error        TEXT NOT NULL DEFAULT '',
            created_at   TEXT NOT NULL,
            last_boot_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (mac, image_sha, profile)
        );
        """
    )
    rows = [
        ("aa:bb:cc:dd:ee:00", "a" * 64, "safl", "/data/overlays/aa/safl.qcow2"),
        ("aa:bb:cc:dd:ee:01", "a" * 64, "safl", "/data/overlays/bb/safl.qcow2"),
        ("aa:bb:cc:dd:ee:02", "b" * 64, "scratch", "/data/overlays/cc/scratch.qcow2"),
    ]
    for mac, sha, profile, path in rows:
        conn.execute(
            "INSERT INTO overlays (mac, image_sha, profile, qcow2_path, created_at) "
            "VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z')",
            (mac, sha, profile, path),
        )
    conn.commit()
    conn.close()

    # First open runs the migration.
    ExportsStore(db_path)
    store = OverlaysStore(db_path)
    overlays = {o.alias: o for o in store.list_all()}

    # Two machines shared profile "safl": mac_slug keeps the aliases
    # distinct, no ``-N`` needed (the slugs already differ).
    assert overlays["safl-aa-bb-cc-dd-ee-00"].attached_mac == "aa:bb:cc:dd:ee:00"
    assert overlays["safl-aa-bb-cc-dd-ee-01"].attached_mac == "aa:bb:cc:dd:ee:01"
    assert overlays["scratch-aa-bb-cc-dd-ee-02"].image_sha == "b" * 64
    # qcow2 path is carried through untouched (no large-file move).
    assert overlays["safl-aa-bb-cc-dd-ee-00"].qcow2_path == "/data/overlays/aa/safl.qcow2"
    # Migration is idempotent: a second open leaves the alias table alone.
    ExportsStore(db_path)
    assert {o.alias for o in OverlaysStore(db_path).list_all()} == set(overlays)


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
