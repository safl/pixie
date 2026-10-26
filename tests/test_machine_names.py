"""Machine names: every row carries a unique, hostname-style name that
discovery assigns and an operator can change, and every ``/machines``
route accepts it in place of the MAC."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pixie.machines._store import DuplicateName, MachinesStore, validate_name
from tests.conftest import authed as _authed

MAC = "d8:5e:d3:6f:e3:cd"


@pytest.mark.parametrize(
    ("raw", "want"),
    [("epyc-02", "epyc-02"), ("  EPYC-02 ", "epyc-02"), ("a", "a"), ("x" * 63, "x" * 63)],
)
def test_validate_name_accepts_and_lowercases_host_labels(raw: str, want: str) -> None:
    assert validate_name(raw) == want


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "-epyc",
        "epyc-",
        "epyc.lab",
        "epyc_02",
        "x" * 64,
        # MAC-shaped names would make a name-or-MAC ref ambiguous.
        "d85ed36fe3cd",
        "d8-5e-d3-6f-e3-cd",
    ],
)
def test_validate_name_rejects_non_host_labels_and_macs(raw: str) -> None:
    with pytest.raises(ValueError):
        validate_name(raw)


def test_discovery_assigns_mac_derived_name(tmp_path: Path) -> None:
    store = MachinesStore(tmp_path / "state.db")
    assert store.touch_seen(MAC).name == "machine-d85ed36fe3cd"
    # Every other lazy row-create door names the row the same way.
    assert store.set_labels("aa:bb:cc:dd:ee:01", ["x"]).name == "machine-aabbccddee01"
    assert store.set_inventory("aa:bb:cc:dd:ee:02", {}).name == "machine-aabbccddee02"
    bound = store.upsert_binding("aa:bb:cc:dd:ee:03", boot_mode="ipxe-exit")
    assert bound.name == "machine-aabbccddee03"


def test_default_name_steps_around_a_hand_picked_collision(tmp_path: Path) -> None:
    store = MachinesStore(tmp_path / "state.db")
    store.set_name("aa:bb:cc:dd:ee:01", "machine-d85ed36fe3cd")
    assert store.touch_seen(MAC).name == "machine-d85ed36fe3cd-2"


def test_rename_and_resolve_by_name_or_mac(tmp_path: Path) -> None:
    store = MachinesStore(tmp_path / "state.db")
    store.touch_seen(MAC)
    store.set_name(MAC, "Warp")
    assert store.resolve("warp").mac == MAC
    assert store.resolve("WARP").mac == MAC
    assert store.resolve(MAC.upper()).name == "warp"
    assert store.resolve("nope") is None
    # A rename survives discovery bumps.
    assert store.touch_seen(MAC, ip="10.20.30.80").name == "warp"


def test_rename_refuses_a_name_held_by_another_machine(tmp_path: Path) -> None:
    store = MachinesStore(tmp_path / "state.db")
    store.set_name("aa:bb:cc:dd:ee:01", "epyc-02")
    with pytest.raises(DuplicateName, match="aa:bb:cc:dd:ee:01"):
        store.set_name(MAC, "EPYC-02")
    # Re-applying a machine's own name is not a collision.
    assert store.set_name("aa:bb:cc:dd:ee:01", "epyc-02").name == "epyc-02"


def test_migration_backfills_names_and_enforces_uniqueness(tmp_path: Path) -> None:
    """A state.db from before names gets one per row on the next open,
    and the unique index then holds at the SQL level too."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE machines (
            mac                    TEXT PRIMARY KEY,
            boot_mode              TEXT NOT NULL DEFAULT 'ipxe-exit',
            image_content_sha256   TEXT NOT NULL DEFAULT '',
            discovered_at          TEXT NOT NULL,
            last_seen_at           TEXT NOT NULL,
            last_seen_ip           TEXT NOT NULL DEFAULT '',
            updated_at             TEXT NOT NULL
        );
        INSERT INTO machines (mac, discovered_at, last_seen_at, updated_at)
        VALUES ('aa:bb:cc:dd:ee:01', 'x', 'x', 'x'), ('aa:bb:cc:dd:ee:02', 'x', 'x', 'x');
        """
    )
    conn.commit()
    conn.close()

    store = MachinesStore(db)
    assert [m.name for m in store.list()] == ["machine-aabbccddee01", "machine-aabbccddee02"]
    conn = sqlite3.connect(str(db))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE machines SET name = 'machine-aabbccddee01'")
    conn.close()


def test_api_get_bind_and_delete_by_name(client: TestClient) -> None:
    c = _authed(client)
    c.put(f"/machines/{MAC}", json={"boot_mode": "ipxe-exit"})
    assert c.get(f"/machines/{MAC}").json()["name"] == "machine-d85ed36fe3cd"

    r = c.put(f"/machines/{MAC}/name", json={"name": "warp"})
    assert r.status_code == 200
    assert r.json()["name"] == "warp"
    assert c.get("/machines/warp").json()["mac"] == MAC

    r = c.put("/machines/warp", json={"boot_mode": "pixie-tui"})
    assert r.status_code == 200
    assert c.get(f"/machines/{MAC}").json()["boot_mode"] == "pixie-tui"

    assert c.delete("/machines/warp").status_code == 204
    assert c.get(f"/machines/{MAC}").status_code == 404


def test_api_rename_errors(client: TestClient) -> None:
    assert client.put(f"/machines/{MAC}/name", json={"name": "warp"}).status_code == 401
    c = _authed(client)
    c.put("/machines/aa:bb:cc:dd:ee:01/name", json={"name": "warp"})
    assert c.put(f"/machines/{MAC}/name", json={"name": "warp"}).status_code == 409
    assert c.put(f"/machines/{MAC}/name", json={"name": "bad.name"}).status_code == 422
    assert c.put("/machines/nope/name", json={"name": "x"}).status_code == 404


def test_ui_rename_and_detail_by_name(client: TestClient) -> None:
    c = _authed(client)
    c.put(f"/machines/{MAC}", json={"boot_mode": "ipxe-exit"})
    r = c.post(f"/ui/machines/{MAC}/name/edit", data={"name": "warp"}, follow_redirects=False)
    assert r.status_code == 303
    page = c.get("/ui/machines/warp")
    assert page.status_code == 200
    assert MAC in page.text
    assert "warp" in c.get("/ui/machines").text

    # A rejected rename flashes back to the page and keeps the old name.
    r = c.post(f"/ui/machines/{MAC}/name/edit", data={"name": "-bad"}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get(f"/machines/{MAC}").json()["name"] == "warp"


def test_rows_from_an_older_pixie_without_names_do_not_collide(tmp_path: Path) -> None:
    """After a downgrade, the older pixie inserts machines without a
    name. The unique index must let several in, and the next upgraded
    start names them."""
    db = tmp_path / "state.db"
    MachinesStore(db)
    conn = sqlite3.connect(str(db))
    for mac in ("aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"):
        conn.execute(
            "INSERT INTO machines (mac, discovered_at, last_seen_at, updated_at) "
            "VALUES (?, 'x', 'x', 'x')",
            (mac,),
        )
    conn.commit()
    conn.close()
    assert [m.name for m in MachinesStore(db).list()] == [
        "machine-aabbccddee01",
        "machine-aabbccddee02",
    ]
