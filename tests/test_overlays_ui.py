"""Overlay management surface: view-model classification + the
``/ui/overlays`` page, live-refresh JSON, single Reset, and bulk Prune.

An overlay is a globally-unique named writable volume over ONE base
image (alias is the identity, not a machine). The view-model unit tests
drive :func:`build_overlay_views` directly against real stores (with tiny
catalog/NBD stubs) so the state classification is pinned without the full
app. The route tests exercise the wiring through the authed TestClient.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from pixie.exports._store import ExportsStore, Overlay, OverlaysStore
from pixie.machines._store import MachinesStore
from pixie.web._overlays import (
    STATE_FREE,
    STATE_HELD,
    STATE_MISSING,
    STATE_ORPHANED,
    STATE_SERVING,
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


def test_classifies_serving_held_free_orphaned_missing(tmp_path: Path) -> None:
    overlays, machines = _stores(tmp_path)
    ov_dir = tmp_path / "overlays"

    # serving: attached to a live machine bound to it + a running nbd port.
    serving_path = ov_dir / "serving.qcow2"
    _touch_qcow2(serving_path)
    machines.upsert_binding(
        "aa:aa:aa:aa:aa:aa",
        boot_mode="nbdboot",
        image_content_sha256=_SHA,
        overlay_alias="prod",
    )
    serving = Overlay("prod", _SHA, str(serving_path), attached_mac="aa:aa:aa:aa:aa:aa")
    overlays.upsert(serving)

    # held: attached to a live machine, file present, but nothing serving.
    held_path = ov_dir / "held.qcow2"
    _touch_qcow2(held_path)
    overlays.upsert(Overlay("scratch", _SHA, str(held_path), attached_mac="aa:aa:aa:aa:aa:aa"))

    # free: unattached, file present.
    free_path = ov_dir / "free.qcow2"
    _touch_qcow2(free_path)
    overlays.upsert(Overlay("spare", _SHA, str(free_path)))

    # orphaned: attached to a MAC with no machine row, file present.
    orphan_path = ov_dir / "orphan.qcow2"
    _touch_qcow2(orphan_path)
    overlays.upsert(Overlay("ghost", _SHA, str(orphan_path), attached_mac="cc:cc:cc:cc:cc:cc"))

    # missing: row points at a qcow2 that is gone.
    overlays.upsert(
        Overlay("lost", _SHA, str(ov_dir / "gone.qcow2"), attached_mac="dd:dd:dd:dd:dd:dd")
    )

    from pixie.pxe._renderer import _overlay_export_name

    nbd = _StubNbd({_overlay_export_name(serving): 10809})
    catalog = _StubCatalog([_StubEntry("ubuntu", _SHA, 4_000_000_000)])
    views = build_overlay_views(overlays=overlays, machines=machines, catalog=catalog, nbd=nbd)
    by_state = {v.alias: v.state for v in views}
    assert by_state["prod"] == STATE_SERVING
    assert by_state["scratch"] == STATE_HELD
    assert by_state["spare"] == STATE_FREE
    assert by_state["ghost"] == STATE_ORPHANED
    assert by_state["lost"] == STATE_MISSING

    # base-image join: name + virtual size resolved from the catalog.
    prod = next(v for v in views if v.alias == "prod")
    assert prod.image_name == "ubuntu"
    assert prod.base_bytes == 4_000_000_000
    assert prod.used_bytes > 0  # allocated blocks for the 8 KiB file
    assert prod.is_active is True  # backs the machine's current binding


def test_running_flag_from_nbd_supervisor(tmp_path: Path) -> None:
    overlays, machines = _stores(tmp_path)
    path = tmp_path / "ov.qcow2"
    _touch_qcow2(path)
    ov = Overlay("prod", _SHA, str(path))
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
    # orphaned (attached to a dead MAC) + missing (file gone) -> reclaimable.
    overlays.upsert(Overlay("ghost", _SHA, str(p), attached_mac="cc:cc:cc:cc:cc:cc"))
    overlays.upsert(
        Overlay("lost", _SHA, str(ov_dir / "gone.qcow2"), attached_mac="dd:dd:dd:dd:dd:dd")
    )
    views = build_overlay_views(
        overlays=overlays, machines=machines, catalog=_StubCatalog([]), nbd=_StubNbd()
    )
    totals = overlay_totals(views)
    assert totals.count == 2
    assert totals.reclaimable == 2  # orphaned + missing are both reclaimable


def test_free_overlay_is_not_reclaimable(tmp_path: Path) -> None:
    """A free (unattached) overlay is a deliberate keep for a future
    bind -- Prune must leave it alone."""
    overlays, machines = _stores(tmp_path)
    p = tmp_path / "spare.qcow2"
    _touch_qcow2(p)
    overlays.upsert(Overlay("spare", _SHA, str(p)))
    views = build_overlay_views(
        overlays=overlays, machines=machines, catalog=_StubCatalog([]), nbd=_StubNbd()
    )
    assert views[0].state == STATE_FREE
    assert views[0].reclaimable is False
    assert overlay_totals(views).reclaimable == 0


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
    path = Path(state.overlays_dir) / "prod.qcow2"
    _touch_qcow2(path)
    state.machines_store.upsert_binding(
        "aa:aa:aa:aa:aa:aa",
        boot_mode="nbdboot",
        image_content_sha256=_SHA,
        overlay_alias="prod",
    )
    state.overlays_store.upsert(Overlay("prod", _SHA, str(path), attached_mac="aa:aa:aa:aa:aa:aa"))

    r = c.get("/ui/overlays")
    assert r.status_code == 200
    assert "aa:aa:aa:aa:aa:aa" in r.text
    assert "prod" in r.text
    assert "Held" in r.text  # attached but nothing serving

    j = c.get("/ui/overlays-live.json").json()
    assert "prod" in j["rows"]
    assert j["rows"]["prod"]["state"] == STATE_HELD
    assert j["totals"]["count"] == 1


def test_ui_overlays_reset_deletes_file_and_row(client: TestClient) -> None:
    c = authed(client)
    state = client.app.state
    path = Path(state.overlays_dir) / "prod.qcow2"
    _touch_qcow2(path)
    state.overlays_store.upsert(Overlay("prod", _SHA, str(path)))

    r = c.post(
        "/ui/overlays/reset",
        data={"alias": "prod"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/overlays"
    assert not path.exists()
    assert state.overlays_store.get("prod") is None


def test_ui_overlays_reset_requires_auth(client: TestClient) -> None:
    r = client.post(
        "/ui/overlays/reset",
        data={"alias": "prod"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_ui_overlays_prune_reclaims_only_junk(client: TestClient) -> None:
    c = authed(client)
    state = client.app.state
    ov_dir = Path(state.overlays_dir)

    # held: bound machine + file -> KEEP
    held_path = ov_dir / "prod.qcow2"
    _touch_qcow2(held_path)
    state.machines_store.upsert_binding(
        "aa:aa:aa:aa:aa:aa",
        boot_mode="nbdboot",
        image_content_sha256=_SHA,
        overlay_alias="prod",
    )
    state.overlays_store.upsert(
        Overlay("prod", _SHA, str(held_path), attached_mac="aa:aa:aa:aa:aa:aa")
    )

    # free: unattached + file -> KEEP
    free_path = ov_dir / "spare.qcow2"
    _touch_qcow2(free_path)
    state.overlays_store.upsert(Overlay("spare", _SHA, str(free_path)))

    # orphaned: attached to a dead MAC, file present -> PRUNE
    orphan_path = ov_dir / "ghost.qcow2"
    _touch_qcow2(orphan_path)
    state.overlays_store.upsert(
        Overlay("ghost", _SHA, str(orphan_path), attached_mac="cc:cc:cc:cc:cc:cc")
    )

    # missing: file gone -> PRUNE
    gone = str(ov_dir / "gone.qcow2")
    state.overlays_store.upsert(Overlay("lost", _SHA, gone, attached_mac="dd:dd:dd:dd:dd:dd"))

    r = c.post("/ui/overlays/prune", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/overlays"

    remaining = {o.alias for o in state.overlays_store.list_all()}
    assert remaining == {"prod", "spare"}
    assert held_path.exists()
    assert free_path.exists()
    assert not orphan_path.exists()
