"""Plan renderer: machine + catalog -> iPXE script.

Owns the boot-mode dispatch table and the nbdboot resolution:

* ``ipxe-exit``     -> ``ipxe/exit.j2`` unconditionally.
* ``nbdboot``       -> walk catalog[image_sha] -> netboot_src -> catalog[netboot_src]
  to find the netboot-bundle catalog entry, use its ``content_sha256``
  as the artifacts key, ensure an NBD export exists for the disk-image
  blob, and render ``ipxe/nbdboot.j2`` with the resolved fields. If any
  step fails (no bound image, netboot bundle not fetched, NBD spawn
  refused) the renderer emits the ``unavailable.j2`` template with the
  reason baked into the plan comment.

The renderer is pure (no side effects) apart from the NBD spawn --
which is idempotent per (name, blob_path), so a spurious render does
not accumulate exports.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from pixie.catalog._schema import CatalogEntry
from pixie.catalog._store import CatalogStore
from pixie.exports._store import Export, ExportsStore
from pixie.exports._supervisor import NbdServer
from pixie.machines._store import LIVE_ENV_MODES, Machine

_log = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "web" / "_templates" / "ipxe"


# NBD name = ``pixie-`` + short sha. Short enough to type at a
# ``nbdinfo`` prompt, unique-per-content across machines.
def _export_name_for(image_sha: str) -> str:
    return f"pixie-{image_sha[:12]}.img"


DEFAULT_OVERLAY_SIZE = "10G"

# The pixie-* live-env boot modes are the store's
# :data:`LIVE_ENV_MODES` set; imported rather than re-authored so a
# new mode added to the store's :data:`BOOT_MODES` frontier can't
# fall through here silently (which used to render as
# ``unknown boot_mode``, indistinguishable from an unbounded
# operator typo).


@dataclass
class RenderContext:
    """Everything the renderer needs to produce a plan for one MAC."""

    host: str
    port: int
    nbd_host: str
    overlay_size: str = DEFAULT_OVERLAY_SIZE
    # Extra tokens appended verbatim to the pixie-live-env kernel
    # cmdline. Operator-set via PIXIE_LIVE_ENV_EXTRA_CMDLINE.
    # Intended for hardware-specific workarounds -- e.g. the
    # GIGABYTE MC12-LE0 needs pci=nommconf to bring up its Intel
    # i210 NICs under kernel 6.12 -- without a live-env rebake.
    # Empty by default; the template shims it in unconditionally so
    # no-op is a legal value.
    extra_cmdline: str = ""


class PlanRenderer:
    """Assemble iPXE plans. Constructed once at app-startup; called
    per ``GET /pxe/<mac>``."""

    def __init__(
        self,
        *,
        catalog: CatalogStore,
        exports: ExportsStore,
        nbd: NbdServer,
        live_env_dir: Path | None = None,
    ) -> None:
        self._catalog = catalog
        self._exports = exports
        self._nbd = nbd
        # Where the netboot-pc bake's vmlinuz + initrd + squashfs are
        # staged on disk. When set + the three files exist, the
        # ``pixie-*`` boot modes chain into the live env; otherwise
        # they fall back to the ``unavailable`` plan so a bound
        # target does not silently boot nothing.
        self._live_env_dir = live_env_dir
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=select_autoescape(disabled_extensions=("j2",)),
            keep_trailing_newline=True,
        )

    def _live_env_ready(self) -> bool:
        """True iff the netboot-pc artifacts are staged on disk under
        ``self._live_env_dir``. Called per-render so an operator
        dropping the files in without a pixie restart takes effect
        on the next PXE hit."""
        if self._live_env_dir is None:
            return False
        return all(
            (self._live_env_dir / name).is_file() for name in ("vmlinuz", "initrd", "live.squashfs")
        )

    def render(self, machine: Machine, ctx: RenderContext) -> str:
        mode = machine.boot_mode
        if mode == "ipxe-exit":
            return self._env.get_template("exit.j2").render(mac=machine.mac)
        if mode == "nbdboot":
            return self._render_nbdboot(machine, ctx)
        if mode in LIVE_ENV_MODES:
            if not self._live_env_ready():
                # netboot-pc bake artifacts have not been staged on
                # this deploy yet; degrade to the readable
                # unavailable plan so a bound target lands on a
                # legible screen instead of a bty-media initrd.
                return self._unavailable(
                    machine,
                    f"boot_mode={mode!r} needs pixie live-env media; "
                    f"stage vmlinuz+initrd+squashfs under $PIXIE_LIVE_ENV_DIR",
                )
            return self._env.get_template("pixie-live-env.j2").render(
                mac=machine.mac,
                boot_mode=mode,
                host=ctx.host,
                port=ctx.port,
                extra_cmdline=ctx.extra_cmdline,
            )
        # Unknown mode: refuse loudly rather than falling through.
        return self._env.get_template("unavailable.j2").render(
            mac=machine.mac,
            reason=f"unknown boot_mode {mode!r}",
            reason_slug="unknown-boot-mode",
        )

    def render_bootstrap(self, ctx: RenderContext) -> str:
        return self._env.get_template("bootstrap.j2").render(host=ctx.host, port=ctx.port)

    # ---------- nbdboot resolution ---------------------------------

    def _render_nbdboot(self, machine: Machine, ctx: RenderContext) -> str:
        image_sha = machine.image_content_sha256
        if not image_sha:
            return self._unavailable(
                machine, "no image bound; set image_content_sha256 to a fetched entry"
            )

        disk_entry = self._catalog_entry_by_sha(image_sha)
        if disk_entry is None:
            return self._unavailable(
                machine,
                f"no catalog entry with content_sha256={image_sha[:12]}; re-fetch it",
            )
        if not disk_entry.netboot_src:
            return self._unavailable(
                machine,
                f"catalog entry {disk_entry.name!r} has no netboot_src; "
                "advertise a sibling bundle before selecting nbdboot",
            )
        bundle_entry = self._catalog.get_entry_by_src(disk_entry.netboot_src)
        if bundle_entry is None:
            return self._unavailable(
                machine,
                f"netboot_src {disk_entry.netboot_src} has no catalog entry",
            )
        if not bundle_entry.content_sha256:
            return self._unavailable(
                machine,
                f"netboot bundle {bundle_entry.name!r} not fetched yet",
            )
        artifact_dir = self._catalog.artifact_dir(bundle_entry.content_sha256)
        if not (artifact_dir / "manifest.json").is_file():
            return self._unavailable(
                machine,
                f"netboot bundle {bundle_entry.name!r} not unpacked "
                f"(manifest.json missing under artifacts/{bundle_entry.content_sha256[:12]})",
            )

        # Blob for the disk image must exist too; that's what the NBD
        # export streams over the wire.
        blob = self._catalog.blob_path(image_sha)
        if not blob.is_file():
            return self._unavailable(
                machine,
                f"disk image {disk_entry.name!r} blob missing on disk; re-fetch it",
            )

        # Ensure an NBD export for this content is running. Idempotent
        # per name+blob.
        export_name = _export_name_for(image_sha)
        try:
            port = self._ensure_export(export_name, image_sha, blob)
        except RuntimeError as exc:
            return self._unavailable(
                machine, f"nbdkit refused to start for export {export_name!r}: {exc}"
            )

        return self._env.get_template("nbdboot.j2").render(
            mac=machine.mac,
            host=ctx.host,
            port=ctx.port,
            nbd_host=ctx.nbd_host,
            nbd_port=port,
            nbd_name=export_name,
            bundle_sha=bundle_entry.content_sha256,
            overlay_size=ctx.overlay_size,
        )

    def _unavailable(self, machine: Machine, reason: str) -> str:
        _log.info("pxe %s unavailable: %s", machine.mac, reason)
        slug = reason.split(";", 1)[0].replace(" ", "-").lower()[:60]
        return self._env.get_template("unavailable.j2").render(
            mac=machine.mac,
            reason=reason,
            reason_slug=slug,
        )

    def _catalog_entry_by_sha(self, sha: str) -> CatalogEntry | None:
        for e in self._catalog.list_entries():
            if e.content_sha256 == sha:
                return e
        return None

    def _ensure_export(self, name: str, image_sha: str, blob: Path) -> int:
        """Idempotent: register an export in the store + spawn nbdkit
        if not already running; return its port."""
        row = self._exports.get(name)
        if row is None:
            self._exports.upsert(Export(name=name, content_sha256=image_sha))
        port = self._nbd.spawn(name, blob)
        self._exports.update_runtime(name, nbd_port=port, status="running", error="")
        return port
