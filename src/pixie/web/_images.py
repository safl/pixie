"""Image-centric rollup: the materialised entity behind a fetched
catalog source.

A **Catalog** entry is a *source* (a URL you can fetch). The moment you
fetch a disk-image entry, its bytes land on disk under a content sha --
that content IS the **Image**, a distinct entity from the catalog row
(multiple catalog rows can resolve to one image sha). An image has
derived forms (the raw disk + rootfs.raw, and a boot bundle =
vmlinuz + initrd resolved via ``netboot_src``) and live *usages*:
machines bound to it, an ephemeral NBD export (nbdkit), and per-machine
persistent overlays (qemu-nbd). Every usage keys off the same disk
``content_sha256``, so this module is a group-by-sha with a footprint +
usage-count rollup. Those counts are also the refcount that makes a
safe "delete image / GC blob" possible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pixie.catalog._schema import CatalogEntry
from pixie.catalog._store import CatalogStore
from pixie.exports._store import ExportsStore, OverlaysStore
from pixie.exports._supervisor import NbdServer
from pixie.machines._store import MachinesStore
from pixie.pxe._renderer import _overlay_export_name


def _dir_allocated_bytes(directory: Path) -> int:
    """Sum of *allocated* bytes (``st_blocks * 512``) of the files
    directly under ``directory`` -- the honest on-disk footprint of a
    blob/artifact/overlay dir. Missing dir -> 0."""
    total = 0
    try:
        for p in directory.iterdir():
            if p.is_file():
                try:
                    total += int(getattr(p.stat(), "st_blocks", 0)) * 512
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _file_allocated_bytes(path: Path) -> int:
    try:
        return int(getattr(path.stat(), "st_blocks", 0)) * 512
    except OSError:
        return 0


@dataclass
class ImageMachineUse:
    """One machine binding pointing at this image."""

    mac: str
    boot_mode: str
    overlay_profile: str = ""
    labels: list[str] = field(default_factory=list)


@dataclass
class ImageView:
    """A fetched image (disk content sha) with its derived footprint +
    every live usage rolled up."""

    sha: str
    names: list[str]  # catalog-entry names that resolve to this sha
    disk_bytes: int  # blobs/<sha>/ (raw disk + rootfs.raw)
    boot_present: bool  # the netboot bundle (vmlinuz+initrd) is fetched
    boot_bundle_name: str
    boot_bytes: int  # artifacts/<bundle_sha>/
    machines: list[ImageMachineUse]
    export_ports: list[int]  # running ephemeral nbdkit exports of this image
    overlays_total: int
    overlays_running: int  # qemu-nbd currently serving
    overlays_bytes: int

    @property
    def primary_name(self) -> str:
        return self.names[0] if self.names else self.sha[:12]

    @property
    def machines_count(self) -> int:
        return len(self.machines)

    @property
    def export_running(self) -> bool:
        return bool(self.export_ports)

    @property
    def footprint_bytes(self) -> int:
        return self.disk_bytes + self.boot_bytes + self.overlays_bytes

    @property
    def usage_count(self) -> int:
        """Total live dependencies -- the refcount. 0 == safe to GC."""
        return self.machines_count + len(self.export_ports) + self.overlays_total

    @property
    def in_use(self) -> bool:
        return self.usage_count > 0

    @property
    def nbdboot_capable(self) -> bool:
        return self.boot_present


def build_image_views(
    *,
    catalog: CatalogStore,
    exports: ExportsStore,
    overlays: OverlaysStore,
    machines: MachinesStore,
    nbd: NbdServer,
) -> list[ImageView]:
    """Every fetched disk image, grouped by content sha, with its
    footprint + machine / export / overlay usage rolled up."""
    entries = catalog.list_entries()
    by_src = {e.src: e for e in entries if e.src}

    groups: dict[str, list[CatalogEntry]] = {}
    for e in entries:
        if e.is_bindable() and e.content_sha256:
            groups.setdefault(e.content_sha256, []).append(e)

    all_machines = machines.list()
    all_exports = exports.list()
    all_overlays = overlays.list_all()

    views: list[ImageView] = []
    for sha, ents in groups.items():
        disk_bytes = _dir_allocated_bytes(catalog.blob_path(sha).parent)

        # Boot form: any of these entries' ``netboot_src`` -> the bundle
        # entry -> its unpacked artifacts dir.
        boot_present = False
        boot_name = ""
        boot_bytes = 0
        for e in ents:
            if not e.netboot_src:
                continue
            bundle = by_src.get(e.netboot_src) or catalog.get_entry_by_src(e.netboot_src)
            if bundle is None or not bundle.content_sha256:
                continue
            adir = catalog.artifact_dir(bundle.content_sha256)
            if (adir / "manifest.json").is_file() or (adir / "vmlinuz").is_file():
                boot_present = True
                boot_name = bundle.name
                boot_bytes = _dir_allocated_bytes(adir)
                break

        mach = [
            ImageMachineUse(m.mac, m.boot_mode, m.overlay_profile, list(m.labels))
            for m in all_machines
            if m.image_content_sha256 == sha
        ]
        export_ports = sorted(
            p for x in all_exports if x.content_sha256 == sha if (p := nbd.port_for(x.name))
        )
        ovs = [o for o in all_overlays if o.image_sha == sha]
        ov_running = sum(1 for o in ovs if nbd.port_for(_overlay_export_name(o)) is not None)
        ov_bytes = sum(_file_allocated_bytes(Path(o.qcow2_path)) for o in ovs)

        views.append(
            ImageView(
                sha=sha,
                names=sorted(e.name for e in ents),
                disk_bytes=disk_bytes,
                boot_present=boot_present,
                boot_bundle_name=boot_name,
                boot_bytes=boot_bytes,
                machines=mach,
                export_ports=export_ports,
                overlays_total=len(ovs),
                overlays_running=ov_running,
                overlays_bytes=ov_bytes,
            )
        )

    # Heaviest first, so the disk-pressure + most-used images lead.
    views.sort(key=lambda v: (-v.footprint_bytes, v.primary_name))
    return views
