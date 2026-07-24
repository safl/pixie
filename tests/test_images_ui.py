"""Images rollup: group fetched disk content by sha, join machines +
exports + overlays + boot bundle, and render the /ui/images page.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from pixie.catalog._schema import CatalogEntry
from pixie.catalog._store import CatalogStore
from pixie.exports._store import Export, ExportsStore, Overlay, OverlaysStore
from pixie.machines._store import MachinesStore
from pixie.pxe._renderer import _overlay_export_name
from pixie.web._images import build_image_views
from tests.conftest import authed

_SHA = "a" * 64  # disk image
_BSHA = "b" * 64  # netboot bundle
_BUNDLE_SRC = "oras://ghcr.io/safl/nosi/x-netboot:latest"


class _StubNbd:
    def __init__(self, ports: dict[str, int] | None = None) -> None:
        self._ports = ports or {}

    def port_for(self, name: str) -> int | None:
        return self._ports.get(name)

    def terminate(self, name: str) -> bool:
        return self._ports.pop(name, None) is not None


def _seed(tmp: Path):
    catalog = CatalogStore(tmp)
    ExportsStore(catalog.db_path)  # creates exports + overlays tables
    exports = ExportsStore(catalog.db_path)
    overlays = OverlaysStore(catalog.db_path)
    machines = MachinesStore(catalog.db_path)
    # disk image entry (fetched) + its netboot bundle
    catalog.upsert(
        CatalogEntry(
            name="ubuntu",
            src="oras://ghcr.io/safl/nosi/x:latest",
            format="img.gz",
            content_sha256=_SHA,
            netboot_src=_BUNDLE_SRC,
        )
    )
    catalog.upsert(
        CatalogEntry(name="ubuntu-netboot", src=_BUNDLE_SRC, format="tar.gz", content_sha256=_BSHA)
    )
    # a second catalog entry resolving to the SAME disk sha (alias)
    catalog.upsert(
        CatalogEntry(
            name="ubuntu-alias", src="https://x/y.img.gz", format="img.gz", content_sha256=_SHA
        )
    )
    # on-disk artifacts: a disk blob + the bundle's unpacked manifest
    blob = catalog.blob_path(_SHA)
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"\x00" * 4096)
    adir = catalog.artifact_dir(_BSHA)
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "manifest.json").write_text("{}")
    (adir / "vmlinuz").write_bytes(b"K")
    return catalog, exports, overlays, machines


def test_build_image_views_groups_and_rolls_up(tmp_path: Path) -> None:
    catalog, exports, overlays, machines = _seed(tmp_path)
    machines.upsert_binding("aa:aa:aa:aa:aa:aa", boot_mode="nbdboot", image_content_sha256=_SHA)
    ov = Overlay("aa:aa:aa:aa:aa:aa", _SHA, "prod", str(tmp_path / "ov.qcow2"))
    (tmp_path / "ov.qcow2").write_bytes(b"\x00" * 2048)
    overlays.upsert(ov)
    exports.upsert(Export(name="exp1", content_sha256=_SHA))
    nbd = _StubNbd({"exp1": 10809, _overlay_export_name(ov): 10818})

    views = build_image_views(
        catalog=catalog, exports=exports, overlays=overlays, machines=machines, nbd=nbd
    )
    assert len(views) == 1  # both disk entries collapse to one image
    im = views[0]
    assert im.sha == _SHA
    assert im.names == ["ubuntu", "ubuntu-alias"]  # alias folded in
    assert im.boot_present is True  # bundle manifest on disk
    assert im.nbdboot_capable is True
    assert im.machines_count == 1
    assert im.export_running is True and im.export_ports == [10809]
    assert im.overlays_total == 1 and im.overlays_running == 1
    assert im.usage_count == 3  # 1 machine + 1 export + 1 overlay
    assert im.in_use is True
    assert im.disk_bytes > 0 and im.boot_bytes > 0


def test_unused_image_is_reclaimable(tmp_path: Path) -> None:
    catalog, exports, overlays, machines = _seed(tmp_path)
    views = build_image_views(
        catalog=catalog, exports=exports, overlays=overlays, machines=machines, nbd=_StubNbd()
    )
    assert len(views) == 1
    assert views[0].usage_count == 0
    assert views[0].in_use is False


def test_unfetched_entries_are_not_images(tmp_path: Path) -> None:
    catalog = CatalogStore(tmp_path)
    ExportsStore(catalog.db_path)
    catalog.upsert(CatalogEntry(name="pending", src="oras://x/y:latest", format="img.gz"))
    views = build_image_views(
        catalog=catalog,
        exports=ExportsStore(catalog.db_path),
        overlays=OverlaysStore(catalog.db_path),
        machines=MachinesStore(catalog.db_path),
        nbd=_StubNbd(),
    )
    assert views == []  # no content_sha256 -> not materialised


# ---------- route ----------------------------------------------------


def test_ui_images_requires_auth(client: TestClient) -> None:
    r = client.get("/ui/images", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_ui_images_empty(client: TestClient) -> None:
    c = authed(client)
    r = c.get("/ui/images")
    assert r.status_code == 200
    assert "No fetched images yet" in r.text


def test_ui_images_renders_image_row(client: TestClient) -> None:
    c = authed(client)
    state = client.app.state
    state.catalog_store.upsert(
        CatalogEntry(name="ubuntu", src="oras://x/y:latest", format="img.gz", content_sha256=_SHA)
    )
    blob = state.catalog_store.blob_path(_SHA)
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"\x00" * 4096)
    state.machines_store.upsert_binding(
        "aa:aa:aa:aa:aa:aa", boot_mode="nbdboot", image_content_sha256=_SHA
    )
    body = c.get("/ui/images").text
    assert "ubuntu" in body
    assert _SHA[:12] in body
    assert "bound" in body  # the machine usage


def test_orphan_blob_is_surfaced(tmp_path: Path) -> None:
    catalog = CatalogStore(tmp_path)
    ExportsStore(catalog.db_path)
    # a blob on disk with NO catalog entry
    orphan = "c" * 64
    ob = catalog.blob_path(orphan)
    ob.parent.mkdir(parents=True, exist_ok=True)
    ob.write_bytes(b"\x00" * 8192)
    views = build_image_views(
        catalog=catalog,
        exports=ExportsStore(catalog.db_path),
        overlays=OverlaysStore(catalog.db_path),
        machines=MachinesStore(catalog.db_path),
        nbd=_StubNbd(),
    )
    assert len(views) == 1
    assert views[0].sha == orphan
    assert views[0].orphan is True
    assert views[0].usage_count == 0
    assert views[0].disk_bytes > 0


def test_ui_image_detail_renders(client: TestClient) -> None:
    c = authed(client)
    state = client.app.state
    state.catalog_store.upsert(
        CatalogEntry(name="ubuntu", src="oras://x/y:latest", format="img.gz", content_sha256=_SHA)
    )
    blob = state.catalog_store.blob_path(_SHA)
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"\x00" * 4096)
    body = c.get(f"/ui/images/{_SHA}").text
    assert _SHA in body
    assert "Delete image" in body  # the GC danger zone


def test_ui_image_delete_gc_unused(client: TestClient) -> None:
    c = authed(client)
    state = client.app.state
    state.catalog_store.upsert(
        CatalogEntry(name="ubuntu", src="oras://x/y:latest", format="img.gz", content_sha256=_SHA)
    )
    blob = state.catalog_store.blob_path(_SHA)
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"\x00" * 4096)
    (blob.parent / "rootfs.raw").write_bytes(b"\x00" * 2048)  # the file per-entry delete leaks

    r = c.post(f"/ui/images/{_SHA}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/images"
    assert not blob.parent.exists()  # whole sha dir gone (blob + rootfs.raw)
    assert state.catalog_store.get_entry("ubuntu").content_sha256 == ""  # sha cleared


def test_ui_image_delete_refuses_while_in_use(client: TestClient) -> None:
    c = authed(client)
    state = client.app.state
    state.catalog_store.upsert(
        CatalogEntry(name="ubuntu", src="oras://x/y:latest", format="img.gz", content_sha256=_SHA)
    )
    blob = state.catalog_store.blob_path(_SHA)
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"\x00" * 4096)
    state.machines_store.upsert_binding(
        "aa:aa:aa:aa:aa:aa", boot_mode="nbdboot", image_content_sha256=_SHA
    )
    c.post(f"/ui/images/{_SHA}/delete", follow_redirects=False)
    assert blob.exists()  # refused: a machine still depends on it
    assert state.catalog_store.get_entry("ubuntu").content_sha256 == _SHA
