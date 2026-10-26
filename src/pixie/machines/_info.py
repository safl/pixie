"""``GET /machines/{ref}/info``: everything pixie knows about one
machine, in one machine-readable answer, for tools that show machines
without walking pixie's raw tables.

OPEN, like the other machine read routes. The inventory comes out
already summarised (see :func:`pixie.web._inventory.normalise_inventory`)
so a client never has to walk an ``lshw`` tree. Unknown values are left
out rather than sent as null, which keeps the answer representable as
TOML as well as JSON and YAML.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from pixie.images import bindable_image_names
from pixie.machines._store import BOOT_MODE_META, IMAGE_BOOT_MODES, MachinesStore
from pixie.web._inventory import normalise_inventory

router = APIRouter()

_MODE_LABELS = {mode: meta["short"] for mode, meta in BOOT_MODE_META}


def _prune(value: Any) -> Any:
    """Drop ``None`` and empty containers, recursively."""
    if isinstance(value, dict):
        pruned = {k: _prune(v) for k, v in value.items()}
        return {k: v for k, v in pruned.items() if v not in (None, {}, [])}
    if isinstance(value, list):
        return [p for p in (_prune(v) for v in value) if p not in (None, {}, [])]
    return value


@router.get("/machines/{ref}/info")
def machine_info(request: Request, ref: str) -> dict[str, Any]:
    store: MachinesStore = request.app.state.machines_store
    m = store.resolve(ref)
    if m is None:
        raise HTTPException(status_code=404, detail=f"no machine {ref!r}")
    binding: dict[str, Any] = {
        "boot_mode": m.boot_mode,
        "boot_mode_label": _MODE_LABELS.get(m.boot_mode, m.boot_mode),
    }
    if m.boot_mode in IMAGE_BOOT_MODES and m.image_content_sha256:
        names = bindable_image_names(request.app.state.catalog_store)
        binding["image"] = {
            "sha256": m.image_content_sha256,
            "name": names.get(m.image_content_sha256),
        }
    if m.boot_mode == "nbdboot-overlay":
        binding["overlay"] = m.overlay_alias
    info: dict[str, Any] = {
        "name": m.name,
        "mac": m.mac,
        "labels": m.labels,
        "binding": binding,
        "discovered_at": m.discovered_at,
        "last_seen": {"at": m.last_seen_at, "pxe_ip": m.last_seen_ip or None},
    }
    if m.inventory:
        info["inventory"] = {"taken_at": m.inventory_at, **normalise_inventory(m.inventory)}
    pruned: dict[str, Any] = _prune(info)
    return pruned
