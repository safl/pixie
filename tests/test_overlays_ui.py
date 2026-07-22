"""Overlay management surface: view-model classification + the
``/ui/overlays`` page, live-refresh JSON, single Reset, and bulk Prune.

The view-model unit tests drive :func:`build_overlay_views` directly
against real stores (with tiny catalog/NBD stubs) so the state
classification is pinned without the full app. The route tests exercise
the wiring through the authed TestClient.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from pixie.exports._store import ExportsStore, Overlay, OverlaysStore
from pixie.machines._store import MachinesStore
from pixie.web._overlays import (
    STATE_ACTIVE,
    STATE_IDLE,
    STATE_MISSING,
    STATE_ORPHANED,
    build_overlay_views,
    overlay_totals,
)
from tests.conftest import authed

_SHA = "a" * 64


class _StubEntry:
    def __init__(self, name: str, content_sha256: str, size_bytes: int) -> None:
        self.name = name
        self.content_sha256 = content_sha256
        self.size_bytes = size_bytes


class _StubCatalog:
    def __init__(self, entries: list[_StubEntry]) -> None:
        self._entries = entries

    def list_entries(self) -> list[_StubEntry]:
        return list(self._entries)


class _StubNbd:
    """Only the two methods the view-model + reset touch."""

    def __init__(self, ports: dict[str, int] | None = None) -> None:
        self._ports = ports or {}

    def port_for(self, name: str) -> int | None:
        return self._ports.get(name)

    def terminate(self, name: str) -> bool:
        return self._ports.pop(name, None) is not None


def _stores(tmp_path: Path) -> tuple[OverlaysStore, MachinesStore]:
    db = tmp_path / "state.db"
    ExportsStore(db)  # creates the exports + overlays tables
    return OverlaysStore(db), MachinesStore(db)


def _touch_qcow2(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 8192)


def test_classifies_active_idle_orphaned_missing(tmp_path: Path) -> None:
    overlays, machines = _stores(tmp_path)
    ov_dir = tmp_path / "overlays"

    # active: machine bound to nbdboot on this (image, profile) + file present
    active_path = ov_dir / "active.qcow2"
    _touch_qcow2(active_path)
    machines.upsert_binding(
        "aa:aa:aa:aa:aa:aa",
        boot_mode="nbdboot",
        image_content_sha256=_SHA,
        overlay_profile="prod",
    )
    overlays.upsert(Overlay("aa:aa:aa:aa:aa:aa", _SHA, "prod", str(active_path)))

    # idle: machine exists but its current binding is a different profile
    idle_path = ov_dir / "idle.qcow2"
    _touch_qcow2(idle_path)
    overlays.upsert(Overlay("aa:aa:aa:aa:aa:aa", _SHA, "scratch", str(idle_path)))

    # orphaned: no machine row for this MAC, file present
    orphan_path = ov_dir / "orphan.qcow2"
    _touch_qcow2(orphan_path)
    overlays.upsert(Overlay("cc:cc:cc:cc:cc:cc", _SHA, "prod", str(orphan_path)))

    # missing: row points at a qcow2 that is gone
    overlays.upsert(Overlay("dd:dd:dd:dd:dd:dd", _SHA, "prod", str(ov_dir / "gone.qcow2")))

    catalog = _StubCatalog([_StubEntry("ubuntu", _SHA, 4_000_000_000)])
    views = build_overlay_views(
        overlays=overlays, machines=machines, catalog=catalog, nbd=_StubNbd()
    )
    by_state = {(v.mac, v.profile): v.state for v in views}
    assert by_state[("aa:aa:aa:aa:aa:aa", "prod")] == STATE_ACTIVE
    assert by_state[("aa:aa:aa:aa:aa:aa", "scratch")] == STATE_IDLE
    assert by_state[("cc:cc:cc:cc:cc:cc", "prod")] == STATE_ORPHANED
    assert by_state[("dd:dd:dd:dd:dd:dd", "prod")] == STATE_MISSING

    # base-image join: name + virtual size resolved from the catalog
    active = next(v for v in views if v.state == STATE_ACTIVE)
    assert active.image_name == "ubuntu"
    assert active.base_bytes == 4_000_000_000
    assert active.used_bytes > 0  # allocated blocks for the 8 KiB file


def test_running_flag_from_nbd_supervisor(tmp_path: Path) -> None:
    overlays, machines = _stores(tmp_path)
    path = tmp_path / "ov.qcow2"
    _touch_qcow2(path)
    ov = Overlay("aa:aa:aa:aa:aa:aa", _SHA, "prod", str(path))
    overlays.upsert(ov)
    from pixie.pxe._renderer import _overlay_export_name

    nbd = _StubNbd({_overlay_export_name(ov): 10815})
    views = build_overlay_views(
        overlays=overlays, machines=machines, catalog=_StubCatalog([]), nbd=nbd
    )
    assert views[0].running is True
    assert views[0].nbd_port == 10815


def test_totals_and_reclaimable(tmp_path: Path) -> None:
    overlays, machines = _stores(tmp_path)
    ov_dir = tmp_path / "overlays"
    p = ov_dir / "present.qcow2"
    _touch_qcow2(p)
    # orphaned (no machine row) + missing (file gone) -> both reclaimable
    overlays.upsert(Overlay("aa:aa:aa:aa:aa:aa", _SHA, "prod", str(p)))
    overlays.upsert(Overlay("bb:bb:bb:bb:bb:bb", _SHA, "prod", str(ov_dir / "gone.qcow2")))
    views = build_overlay_views(
        overlays=overlays, machines=machines, catalog=_StubCatalog([]), nbd=_StubNbd()
    )
    totals = overlay_totals(views)
    assert totals.count == 2
    assert totals.reclaimable == 2  # orphaned + missing are both reclaimable


# ---------- route tests ---------------------------------------------


def test_ui_overlays_page_renders_empty(client: TestClient) -> None:
    c = authed(client)
    r = c.get("/ui/overlays")
    assert r.status_code == 200
    assert "No overlays yet" in r.text


def test_ui_overlays_requires_auth(client: TestClient) -> None:
    r = client.get("/ui/overlays", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_ui_overlays_shows_row_and_live_json(client: TestClient) -> None:
    c = authed(client)
    state = client.app.state
    path = Path(state.overlays_dir) / "aa" / "ov.qcow2"
    _touch_qcow2(path)
    state.machines_store.upsert_binding(
        "aa:aa:aa:aa:aa:aa",
        boot_mode="nbdboot",
        image_content_sha256=_SHA,
        overlay_profile="prod",
    )
    state.overlays_store.upsert(Overlay("aa:aa:aa:aa:aa:aa", _SHA, "prod", str(path)))

    r = c.get("/ui/overlays")
    assert r.status_code == 200
    assert "aa:aa:aa:aa:aa:aa" in r.text
    assert "prod" in r.text
    assert "Active" in r.text  # active-state badge

    j = c.get("/ui/overlays-live.json").json()
    key = "aa:aa:aa:aa:aa:aa|" + _SHA + "|prod"
    assert key in j["rows"]
    assert j["rows"][key]["state"] == STATE_ACTIVE
    assert j["totals"]["count"] == 1


def test_ui_overlays_reset_deletes_file_and_row(client: TestClient) -> None:
    c = authed(client)
    state = client.app.state
    path = Path(state.overlays_dir) / "aa" / "ov.qcow2"
    _touch_qcow2(path)
    state.overlays_store.upsert(Overlay("aa:aa:aa:aa:aa:aa", _SHA, "prod", str(path)))

    r = c.post(
        "/ui/overlays/reset",
        data={"mac": "aa:aa:aa:aa:aa:aa", "image_sha": _SHA, "profile": "prod"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/overlays"
    assert not path.exists()
    assert state.overlays_store.get("aa:aa:aa:aa:aa:aa", _SHA, "prod") is None


def test_ui_overlays_reset_requires_auth(client: TestClient) -> None:
    r = client.post(
        "/ui/overlays/reset",
        data={"mac": "aa:aa:aa:aa:aa:aa", "image_sha": _SHA, "profile": "prod"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_ui_overlays_prune_reclaims_only_junk(client: TestClient) -> None:
    c = authed(client)
    state = client.app.state
    ov_dir = Path(state.overlays_dir)

    # active: bound machine + file -> KEEP
    active_path = ov_dir / "active.qcow2"
    _touch_qcow2(active_path)
    state.machines_store.upsert_binding(
        "aa:aa:aa:aa:aa:aa",
        boot_mode="nbdboot",
        image_content_sha256=_SHA,
        overlay_profile="prod",
    )
    state.overlays_store.upsert(Overlay("aa:aa:aa:aa:aa:aa", _SHA, "prod", str(active_path)))

    # idle: machine exists, non-current profile + file -> KEEP
    idle_path = ov_dir / "idle.qcow2"
    _touch_qcow2(idle_path)
    state.overlays_store.upsert(Overlay("aa:aa:aa:aa:aa:aa", _SHA, "scratch", str(idle_path)))

    # orphaned: no machine, file present -> PRUNE
    orphan_path = ov_dir / "orphan.qcow2"
    _touch_qcow2(orphan_path)
    state.overlays_store.upsert(Overlay("cc:cc:cc:cc:cc:cc", _SHA, "prod", str(orphan_path)))

    # missing: file gone -> PRUNE
    gone = str(ov_dir / "gone.qcow2")
    state.overlays_store.upsert(Overlay("dd:dd:dd:dd:dd:dd", _SHA, "prod", gone))

    r = c.post("/ui/overlays/prune", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/overlays"

    remaining = {(o.mac, o.profile) for o in state.overlays_store.list_all()}
    assert remaining == {("aa:aa:aa:aa:aa:aa", "prod"), ("aa:aa:aa:aa:aa:aa", "scratch")}
    assert active_path.exists()
    assert idle_path.exists()
    assert not orphan_path.exists()
