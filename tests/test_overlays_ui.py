"""Overlay management surface: view-model classification + the
``/ui/overlays`` page, live-refresh JSON, single Reset, and bulk Prune.

An overlay is a globally-unique named writable volume over ONE base
image (alias is the identity, not a machine). The view-model unit tests
drive :func:`build_overlay_views` directly against real stores (with tiny
catalog/NBD stubs) so the state classification is pinned without the full
app. The route tests exercise the wiring through the authed TestClient.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from pixie.exports._store import ExportsStore, Overlay, OverlaysStore
from pixie.machines._store import MachinesStore
from pixie.web._overlays import (
    STATE_FREE,
    STATE_HELD,
    STATE_MISSING,
    STATE_ORPHANED,
    STATE_PENDING,
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
        boot_mode="nbdboot-overlay",
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

    # missing: row points at a qcow2 that is gone AFTER a prior boot
    # (last_boot_at set -> genuine data loss, reclaimable).
    overlays.upsert(
        Overlay(
            "lost",
            _SHA,
            str(ov_dir / "gone.qcow2"),
            attached_mac="dd:dd:dd:dd:dd:dd",
            last_boot_at="2026-07-01T00:00:00Z",
        )
    )

    # pending: reserved alias, no qcow2 yet, never booted -- the file is
    # lazy-created on the first nbdboot, so this is benign (NOT missing).
    overlays.upsert(Overlay("newborn", _SHA, str(ov_dir / "newborn.qcow2")))

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
    assert by_state["newborn"] == STATE_PENDING

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
    # orphaned (attached to a dead MAC) + missing (file gone after a
    # prior boot) -> reclaimable.
    overlays.upsert(Overlay("ghost", _SHA, str(p), attached_mac="cc:cc:cc:cc:cc:cc"))
    overlays.upsert(
        Overlay(
            "lost",
            _SHA,
            str(ov_dir / "gone.qcow2"),
            attached_mac="dd:dd:dd:dd:dd:dd",
            last_boot_at="2026-07-01T00:00:00Z",
        )
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


def test_pending_overlay_is_not_reclaimable(tmp_path: Path) -> None:
    """A reserved alias with no qcow2 yet + never booted is PENDING, not
    missing -- its file is lazy-created on first nbdboot, so Prune must
    leave it alone (unlike a booted-then-lost 'missing' overlay)."""
    overlays, machines = _stores(tmp_path)
    # No file on disk, no attached machine, last_boot_at defaults to "".
    overlays.upsert(Overlay("newborn", _SHA, str(tmp_path / "overlays" / "newborn.qcow2")))
    views = build_overlay_views(
        overlays=overlays, machines=machines, catalog=_StubCatalog([]), nbd=_StubNbd()
    )
    assert views[0].state == STATE_PENDING
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
        boot_mode="nbdboot-overlay",
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
        "/ui/overlays/delete",
        data={"alias": "prod"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/overlays"
    assert not path.exists()
    assert state.overlays_store.get("prod") is None


def test_ui_overlays_reset_requires_auth(client: TestClient) -> None:
    r = client.post(
        "/ui/overlays/delete",
        data={"alias": "prod"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def _seed_fetched_image(state: object, name: str = "ubuntu") -> None:
    """Seed a catalog image with a real base blob on disk so the create
    route's qcow2 materialization has a backing file."""
    from pixie.catalog._schema import CatalogEntry

    state.catalog_store.upsert(  # type: ignore[attr-defined]
        CatalogEntry(name=name, src=f"https://x/{name}.img.gz", format="img.gz")
    )
    state.catalog_store.mark_fetched(name, content_sha256=_SHA, size_bytes=1024)  # type: ignore[attr-defined]
    blob = state.catalog_store.blob_path(_SHA)  # type: ignore[attr-defined]
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"\0" * 1024)


def _stub_create_qcow2(monkeypatch: object) -> None:
    """Replace the real ``qemu-img create`` shell-out with a stub that
    just touches the target file, so the create-route unit tests exercise
    the route logic without depending on qemu-img (absent in the lint +
    typecheck + pytest CI job; the real call is covered in integration)."""

    def _fake(qcow2_path: Path, base_path: Path, *, size_bytes: int | None = None) -> None:
        Path(qcow2_path).parent.mkdir(parents=True, exist_ok=True)
        Path(qcow2_path).write_bytes(b"")

    monkeypatch.setattr(  # type: ignore[attr-defined]
        "pixie.exports._supervisor.NbdServer.create_qcow2", staticmethod(_fake)
    )


def test_ui_overlays_create_materializes_qcow2_and_row(
    client: TestClient, monkeypatch: object
) -> None:
    """Create makes the overlay explicit: a real qcow2 on disk (no
    ``pending`` limbo) + a row over the chosen image + an OVERLAY_CREATED
    event. This is the ONLY path that brings an overlay into being."""
    c = authed(client)
    state = client.app.state
    _seed_fetched_image(state)
    _stub_create_qcow2(monkeypatch)

    r = c.post(
        "/ui/overlays/create",
        data={"alias": "fresh", "image_content_sha256": _SHA},
        follow_redirects=False,
    )
    assert r.status_code == 303
    ov = state.overlays_store.get("fresh")
    assert ov is not None
    assert ov.image_sha == _SHA
    assert Path(ov.qcow2_path).is_file()  # materialized now, not lazy
    kinds = [e.kind for e in state.events_log.list(limit=50)]
    assert "overlay.created" in kinds


def test_ui_overlays_create_rejects_dup_bad_and_unfetched(
    client: TestClient, monkeypatch: object
) -> None:
    """Create refuses a duplicate alias, a malformed alias, and an image
    whose blob is not on disk -- each leaves no new overlay behind."""
    c = authed(client)
    state = client.app.state
    _seed_fetched_image(state)
    _stub_create_qcow2(monkeypatch)

    # First create wins.
    assert (
        c.post(
            "/ui/overlays/create",
            data={"alias": "dup", "image_content_sha256": _SHA},
            follow_redirects=False,
        ).status_code
        == 303
    )
    # Duplicate alias is refused; still exactly one overlay.
    c.post("/ui/overlays/create", data={"alias": "dup", "image_content_sha256": _SHA})
    assert len(state.overlays_store.list_all()) == 1

    # Malformed alias -> no new row.
    c.post("/ui/overlays/create", data={"alias": "../evil", "image_content_sha256": _SHA})
    assert state.overlays_store.get("../evil") is None
    assert len(state.overlays_store.list_all()) == 1

    # Unfetched image (valid-looking sha, no blob on disk) -> no new row.
    c.post("/ui/overlays/create", data={"alias": "nofetch", "image_content_sha256": "b" * 64})
    assert state.overlays_store.get("nofetch") is None
    assert len(state.overlays_store.list_all()) == 1


def test_render_overlay_missing_file_is_unavailable_never_creates(
    client: TestClient, monkeypatch: object
) -> None:
    """A machine on nbdboot-overlay whose overlay row exists but whose
    qcow2 was deleted out of band renders an 'unavailable' plan naming
    the missing file -- the renderer NEVER re-creates the overlay or
    emits overlay.created. Overlays are born only on the Overlays page."""
    import types

    c = authed(client)
    state = client.app.state
    missing = Path(state.overlays_dir) / "ghost.qcow2"
    assert not missing.exists()
    state.overlays_store.upsert(Overlay("ghost", _SHA, str(missing)))
    state.machines_store.upsert_binding(
        "aa:bb:cc:dd:ee:99",
        boot_mode="nbdboot-overlay",
        image_content_sha256=_SHA,
        overlay_alias="ghost",
    )
    # Let the bundle/blob resolution succeed so the render reaches the
    # overlay-file check (bundle staging is an integration concern).
    fake = (types.SimpleNamespace(content_sha256="b" * 64), Path("/nonexistent-blob"))
    monkeypatch.setattr(  # type: ignore[attr-defined]
        state.pxe_renderer, "_resolve_bundle_and_blob", lambda _sha: fake
    )

    body = c.get("/pxe/aa:bb:cc:dd:ee:99").text
    assert "missing on disk" in body
    # The renderer did NOT materialize the overlay, and emitted no
    # overlay.created event.
    assert not missing.exists()
    kinds = [e.kind for e in state.events_log.list(limit=50)]
    assert "overlay.created" not in kinds


def test_ui_overlays_create_requires_auth(client: TestClient) -> None:
    r = client.post(
        "/ui/overlays/create",
        data={"alias": "x", "image_content_sha256": _SHA},
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
        boot_mode="nbdboot-overlay",
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

    # missing: file gone after a prior boot (last_boot_at set) -> PRUNE
    gone = str(ov_dir / "gone.qcow2")
    state.overlays_store.upsert(
        Overlay(
            "lost",
            _SHA,
            gone,
            attached_mac="dd:dd:dd:dd:dd:dd",
            last_boot_at="2026-07-01T00:00:00Z",
        )
    )

    r = c.post("/ui/overlays/prune", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/overlays"

    remaining = {o.alias for o in state.overlays_store.list_all()}
    assert remaining == {"prod", "spare"}
    assert held_path.exists()
    assert free_path.exists()
    assert not orphan_path.exists()


def test_machine_detail_shows_held_elsewhere_alias_disabled(client: TestClient) -> None:
    """The overlay picker surfaces an alias held by ANOTHER machine as a
    disabled option (with the holder MAC), so the operator sees it exists
    but is single-writer-locked -- rather than it being hidden and the
    operator trying to create a duplicate that gets rejected."""
    from pixie.catalog._schema import CatalogEntry

    c = authed(client)
    state = client.app.state
    state.catalog_store.upsert(
        CatalogEntry(name="ubuntu", src="https://x/u.img.gz", format="img.gz")
    )
    state.catalog_store.mark_fetched("ubuntu", content_sha256=_SHA, size_bytes=42)
    # Machine A (no row needed) holds alias "prod" over that image.
    path = Path(state.overlays_dir) / "prod.qcow2"
    _touch_qcow2(path)
    state.overlays_store.upsert(Overlay("prod", _SHA, str(path), attached_mac="aa:aa:aa:aa:aa:aa"))
    # Machine B binds nbdboot + the same image, so its picker lists the
    # overlays over that image.
    state.machines_store.upsert_binding(
        "bb:bb:bb:bb:bb:bb", boot_mode="nbdboot-overlay", image_content_sha256=_SHA
    )

    body = c.get("/ui/machines/bb:bb:bb:bb:bb:bb").text
    opt = re.search(r'<option[^>]*value="prod"[^>]*>', body)
    assert opt is not None and "disabled" in opt.group(0)
    assert "held by aa:aa:aa:aa:aa:aa" in body


# ---------- overlay sizing: create-with-size, grow, snapshots ----------


def test_ui_overlays_create_with_size_records_virtual_size(
    client: TestClient, monkeypatch: object
) -> None:
    """A size on Create provisions the qcow2 at that virtual size (passed
    through to qemu-img) and records it on the row."""
    c = authed(client)
    state = client.app.state
    _seed_fetched_image(state)
    captured: dict[str, object] = {}

    def _fake(qcow2_path: Path, base_path: Path, *, size_bytes: int | None = None) -> None:
        captured["size_bytes"] = size_bytes
        Path(qcow2_path).parent.mkdir(parents=True, exist_ok=True)
        Path(qcow2_path).write_bytes(b"")

    monkeypatch.setattr(  # type: ignore[attr-defined]
        "pixie.exports._supervisor.NbdServer.create_qcow2", staticmethod(_fake)
    )
    r = c.post(
        "/ui/overlays/create",
        data={"alias": "big", "image_content_sha256": _SHA, "size_gib": "64"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert captured["size_bytes"] == 64 * 1024**3
    assert state.overlays_store.get("big").size_bytes == 64 * 1024**3


def _stub_qcow2_size(monkeypatch: object, *, current: int, resized: list[int]) -> None:
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "pixie.exports._supervisor.NbdServer.qcow2_virtual_size",
        staticmethod(lambda p: current),
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "pixie.exports._supervisor.NbdServer.resize_qcow2",
        staticmethod(lambda p, n: resized.append(n)),
    )


def test_ui_overlays_grow_updates_size_and_emits(client: TestClient, monkeypatch: object) -> None:
    """Grow on a free overlay resizes the qcow2, records the new size, and
    emits overlay.resized."""
    c = authed(client)
    state = client.app.state
    _seed_fetched_image(state)
    state.overlays_store.upsert(
        Overlay("g", _SHA, str(Path(state.overlays_dir) / "g.qcow2"), size_bytes=8 * 1024**3)
    )
    resized: list[int] = []
    _stub_qcow2_size(monkeypatch, current=8 * 1024**3, resized=resized)
    r = c.post("/ui/overlays/grow", data={"alias": "g", "size_gib": "32"}, follow_redirects=False)
    assert r.status_code == 303
    assert resized == [32 * 1024**3]
    assert state.overlays_store.get("g").size_bytes == 32 * 1024**3
    assert "overlay.resized" in [e.kind for e in state.events_log.list(limit=50)]


def test_ui_overlays_grow_refuses_shrink_and_bound(client: TestClient, monkeypatch: object) -> None:
    """Grow refuses a smaller-than-current size and a bound overlay -- no
    resize call, size unchanged in both cases."""
    c = authed(client)
    state = client.app.state
    _seed_fetched_image(state)
    state.overlays_store.upsert(
        Overlay("free", _SHA, str(Path(state.overlays_dir) / "free.qcow2"), size_bytes=32 * 1024**3)
    )
    resized: list[int] = []
    _stub_qcow2_size(monkeypatch, current=32 * 1024**3, resized=resized)
    c.post("/ui/overlays/grow", data={"alias": "free", "size_gib": "16"})  # shrink
    assert resized == []
    assert state.overlays_store.get("free").size_bytes == 32 * 1024**3
    state.overlays_store.upsert(
        Overlay(
            "bound",
            _SHA,
            str(Path(state.overlays_dir) / "bound.qcow2"),
            attached_mac="aa:bb:cc:dd:ee:ff",
            size_bytes=8 * 1024**3,
        )
    )
    c.post("/ui/overlays/grow", data={"alias": "bound", "size_gib": "64"})  # bound
    assert resized == []
    assert state.overlays_store.get("bound").size_bytes == 8 * 1024**3


def test_ui_overlays_snapshot_ops(client: TestClient, monkeypatch: object) -> None:
    """Snapshot create/revert/delete dispatch to the right qemu-img op on a
    free overlay + emit overlay.snapshotted; a bad name and a bound overlay
    are refused (no op)."""
    c = authed(client)
    state = client.app.state
    _seed_fetched_image(state)
    state.overlays_store.upsert(Overlay("s", _SHA, str(Path(state.overlays_dir) / "s.qcow2")))
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "pixie.exports._supervisor.NbdServer.snapshot_create",
        staticmethod(lambda p, n: calls.append(("create", n))),
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "pixie.exports._supervisor.NbdServer.snapshot_apply",
        staticmethod(lambda p, n: calls.append(("revert", n))),
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "pixie.exports._supervisor.NbdServer.snapshot_delete",
        staticmethod(lambda p, n: calls.append(("delete", n))),
    )
    c.post("/ui/overlays/snapshot/create", data={"alias": "s", "snapshot": "snap1"})
    c.post("/ui/overlays/snapshot/revert", data={"alias": "s", "snapshot": "snap1"})
    c.post("/ui/overlays/snapshot/delete", data={"alias": "s", "snapshot": "snap1"})
    assert calls == [("create", "snap1"), ("revert", "snap1"), ("delete", "snap1")]
    assert "overlay.snapshotted" in [e.kind for e in state.events_log.list(limit=50)]
    c.post("/ui/overlays/snapshot/create", data={"alias": "s", "snapshot": "../evil"})  # bad name
    assert len(calls) == 3
    state.overlays_store.upsert(
        Overlay(
            "sb",
            _SHA,
            str(Path(state.overlays_dir) / "sb.qcow2"),
            attached_mac="aa:bb:cc:dd:ee:ff",
        )
    )
    c.post("/ui/overlays/snapshot/create", data={"alias": "sb", "snapshot": "ok"})  # bound
    assert len(calls) == 3


def test_overlay_size_bytes_roundtrip_and_migration(tmp_path: Path) -> None:
    """size_bytes round-trips through upsert/get/update_size, and a
    pre-sizing overlays table (no size_bytes column) is migrated to add it
    with a 0 default without dropping the existing row."""
    import sqlite3

    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """CREATE TABLE overlays (
            alias TEXT PRIMARY KEY, image_sha TEXT NOT NULL, qcow2_path TEXT NOT NULL,
            attached_mac TEXT NOT NULL DEFAULT '', nbd_port INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'idle', error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, last_boot_at TEXT NOT NULL DEFAULT '');"""
    )
    conn.execute(
        "INSERT INTO overlays (alias, image_sha, qcow2_path, created_at) VALUES (?, ?, ?, ?)",
        ("old", _SHA, "/x/old.qcow2", "2026-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    ExportsStore(db)  # runs the migration (ALTER TABLE ADD COLUMN size_bytes)
    store = OverlaysStore(db)
    got = store.get("old")
    assert got is not None and got.size_bytes == 0  # migrated row survives, defaults 0
    store.upsert(Overlay("new", _SHA, "/x/new.qcow2", size_bytes=42))
    assert store.get("new").size_bytes == 42
    store.update_size("new", 99)
    assert store.get("new").size_bytes == 99
