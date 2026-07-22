"""View-model + stats for the persistent-overlay management surface.

The ``overlays`` table (see :class:`pixie.exports._store.OverlaysStore`)
holds one row per ``(mac, image_sha, profile)`` triple, each pointing at
a qcow2 on pixie's data volume. The per-machine bind form only ever
shows ONE machine's profiles, so an operator watching disk pressure
across the fleet has no aggregate view. This module joins each overlay
row against the machine registry (is this the target's *current*
binding?), the catalog (what base image does it wrap?), the NBD
supervisor (is a qemu-nbd serving it right now?), and the qcow2 on disk
(how much has it actually grown to?) so the Overlays page can render one
honest row per overlay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pixie.catalog._store import CatalogStore
from pixie.exports._store import Overlay, OverlaysStore
from pixie.exports._supervisor import NbdServer
from pixie.machines._store import MachinesStore
from pixie.pxe._renderer import _overlay_export_name

# State classification. ``active`` = backs the machine's next boot;
# ``idle`` = kept but not the current binding; ``missing`` = a row whose
# qcow2 is gone from disk; ``orphaned`` = a qcow2 for a machine pixie no
# longer tracks. Only ``missing`` + ``orphaned`` are auto-reclaimable --
# an ``idle`` overlay is a deliberate keep for a future rebind, so Prune
# leaves it alone.
STATE_ACTIVE = "active"
STATE_IDLE = "idle"
STATE_MISSING = "missing"
STATE_ORPHANED = "orphaned"
RECLAIMABLE_STATES = frozenset({STATE_MISSING, STATE_ORPHANED})

# Sort buckets: live first, deliberate keeps next, junk last. Biggest
# consumer first within a bucket so the operator's eye lands on what is
# both live and heavy before what is reclaimable.
_STATE_ORDER = {STATE_ACTIVE: 0, STATE_IDLE: 1, STATE_ORPHANED: 2, STATE_MISSING: 3}


def overlay_key(mac: str, image_sha: str, profile: str) -> str:
    """Stable per-overlay DOM/JSON key. ``|`` cannot appear in a MAC, a
    hex sha, or the profile allowlist, so it round-trips cleanly and the
    live-refresh JS can address each row without ambiguity."""
    return f"{mac}|{image_sha}|{profile}"


@dataclass
class OverlayView:
    """One overlay row joined against machine + catalog + NBD + disk."""

    mac: str
    image_sha: str
    profile: str
    qcow2_path: str
    export_name: str
    created_at: str
    last_boot_at: str
    file_exists: bool
    used_bytes: int
    apparent_bytes: int
    mtime: str
    running: bool
    nbd_port: int
    machine_exists: bool
    machine_labels: list[str] = field(default_factory=list)
    is_active: bool = False
    image_name: str = ""
    base_bytes: int = 0
    state: str = STATE_IDLE

    @property
    def key(self) -> str:
        return overlay_key(self.mac, self.image_sha, self.profile)

    @property
    def reclaimable(self) -> bool:
        return self.state in RECLAIMABLE_STATES

    def to_json(self) -> dict[str, Any]:
        """The subset the live-refresh poll needs to update in place."""
        return {
            "key": self.key,
            "used_bytes": self.used_bytes,
            "apparent_bytes": self.apparent_bytes,
            "mtime": self.mtime,
            "running": self.running,
            "nbd_port": self.nbd_port,
            "state": self.state,
            "file_exists": self.file_exists,
        }


def _stat_file(path: Path) -> tuple[bool, int, int, str]:
    """``(exists, allocated_bytes, apparent_bytes, mtime_iso)``.

    A growing qcow2's *allocated* size (``st_blocks * 512``) is the
    honest "space consumed" figure -- ``st_size`` is the apparent length,
    which over-counts a sparse/COW file. Missing or unreadable returns
    all-zero + an empty mtime so the row still renders (as ``missing``).
    ``mtime`` is emitted in the same ``...Z`` shape as ``now_iso`` so the
    ``fmt_ts`` filter / ``format_ts`` render it in the operator's tz.
    """
    try:
        st = path.stat()
    except OSError:
        return (False, 0, 0, "")
    allocated = int(getattr(st, "st_blocks", 0)) * 512
    mtime = datetime.fromtimestamp(st.st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (True, allocated, int(st.st_size), mtime)


def build_overlay_view(
    ov: Overlay,
    *,
    machines: MachinesStore,
    image_names: dict[str, str],
    image_sizes: dict[str, int],
    nbd: NbdServer,
) -> OverlayView:
    """Enrich one :class:`Overlay` row into an :class:`OverlayView`."""
    export_name = _overlay_export_name(ov)
    exists, used, apparent, mtime = _stat_file(Path(ov.qcow2_path))
    port = nbd.port_for(export_name)
    machine = machines.get(ov.mac)
    is_active = bool(
        machine is not None
        and machine.boot_mode == "nbdboot"
        and machine.image_content_sha256 == ov.image_sha
        and machine.overlay_profile == ov.profile
    )
    if not exists:
        state = STATE_MISSING
    elif machine is None:
        state = STATE_ORPHANED
    elif is_active:
        state = STATE_ACTIVE
    else:
        state = STATE_IDLE
    return OverlayView(
        mac=ov.mac,
        image_sha=ov.image_sha,
        profile=ov.profile,
        qcow2_path=ov.qcow2_path,
        export_name=export_name,
        created_at=ov.created_at,
        last_boot_at=ov.last_boot_at,
        file_exists=exists,
        used_bytes=used,
        apparent_bytes=apparent,
        mtime=mtime,
        running=port is not None,
        nbd_port=port or 0,
        machine_exists=machine is not None,
        machine_labels=list(machine.labels) if machine is not None else [],
        is_active=is_active,
        image_name=image_names.get(ov.image_sha, ""),
        base_bytes=image_sizes.get(ov.image_sha, 0),
        state=state,
    )


def build_overlay_views(
    *,
    overlays: OverlaysStore,
    machines: MachinesStore,
    catalog: CatalogStore,
    nbd: NbdServer,
) -> list[OverlayView]:
    """Every overlay, enriched + sorted for the Overlays page."""
    entries = catalog.list_entries()
    image_names = {e.content_sha256: e.name for e in entries if e.content_sha256}
    image_sizes = {
        e.content_sha256: int(getattr(e, "size_bytes", 0) or 0) for e in entries if e.content_sha256
    }
    views = [
        build_overlay_view(
            ov,
            machines=machines,
            image_names=image_names,
            image_sizes=image_sizes,
            nbd=nbd,
        )
        for ov in overlays.list_all()
    ]
    views.sort(key=lambda v: (_STATE_ORDER.get(v.state, 9), -v.used_bytes, v.mac, v.profile))
    return views


@dataclass
class OverlayTotals:
    """Fleet-wide roll-up shown in the summary strip + polled live."""

    count: int
    used_bytes: int
    running: int
    active: int
    reclaimable: int
    reclaimable_bytes: int

    def to_json(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "used_bytes": self.used_bytes,
            "running": self.running,
            "active": self.active,
            "reclaimable": self.reclaimable,
            "reclaimable_bytes": self.reclaimable_bytes,
        }


def overlay_totals(views: list[OverlayView]) -> OverlayTotals:
    return OverlayTotals(
        count=len(views),
        used_bytes=sum(v.used_bytes for v in views),
        running=sum(1 for v in views if v.running),
        active=sum(1 for v in views if v.state == STATE_ACTIVE),
        reclaimable=sum(1 for v in views if v.reclaimable),
        reclaimable_bytes=sum(v.used_bytes for v in views if v.reclaimable),
    )
