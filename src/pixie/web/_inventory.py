"""Normalise the raw ``lshw -json`` + ``lsblk`` inventory blob.

The wire payload the live env's ``pixie`` CLI POSTs to
``/pxe/<mac>/inventory`` (see ``pixie.tui._app.post_inventory`` and
``pixie.pxe._routes.pxe_inventory``) is verbatim
``{"lshw": <lshw -json output>, "disks": [<lsblk rows>]}``. There is
no pre-normalised ``cpu`` / ``memory`` / ``nics`` sub-dict on the
wire; ``pixie.machines._store.set_inventory`` stores the payload
as-is. This module walks the ``lshw`` tree at render time and pulls
out a summary shape the machine-detail template can render without
reaching into lshw's node structure itself.

Nothing here is persisted; the extraction runs fresh on every
``GET /ui/machines/<mac>`` render, which is fine at the few-kB size
of a typical ``lshw -json`` tree.

lshw shape assumptions (verified against real dmidecode-backed
output on physical x86 boards; a vendor whose firmware under-reports
SMBIOS type 4/17 records will just yield fewer fields, not a crash):

* CPU packages are ``class: processor`` nodes anywhere in the tree,
  each carrying ``configuration.cores`` / ``configuration.threads``
  (physical / logical count) and a current clock in ``size`` (Hz).
* The whole-system memory total is the ``class: memory`` node whose
  ``id`` is exactly ``"memory"`` (lshw also uses ``class: memory``
  for CPU cache levels, whose ``id`` starts with ``cache``, so the
  exact-id match is load-bearing).
* Per-DIMM records are that node's children, ``id`` starting with
  ``bank``. An empty slot has no ``size`` (or ``size: 0``) and a
  description containing ``[empty]``; lshw's DDR generation + speed
  live inside the free-text ``description`` field
  (e.g. ``"DIMM DDR4 Synchronous 3200 MHz (0.3 ns)"``), so those are
  regex-extracted rather than read off a dedicated key.
* BIOS/firmware info is the node whose ``id`` starts with
  ``"firmware"``.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

_DDR_RE = re.compile(r"\bDDR\d\b", re.IGNORECASE)
_MHZ_RE = re.compile(r"(\d+)\s*MHz", re.IGNORECASE)


def _iter_nodes(node: Any) -> Any:
    """Depth-first walk of an lshw node tree, including ``node``
    itself. lshw nests everything under ``children``; a leaf simply
    has no (or an empty) ``children`` list."""
    if not isinstance(node, dict):
        return
    yield node
    children = node.get("children")
    if isinstance(children, list):
        for child in children:
            yield from _iter_nodes(child)


def _find_all(root: Any, cls: str) -> list[dict[str, Any]]:
    return [n for n in _iter_nodes(root) if n.get("class") == cls]


def _find_by_id_prefix(root: Any, prefix: str) -> dict[str, Any] | None:
    for n in _iter_nodes(root):
        node_id = n.get("id")
        if isinstance(node_id, str) and node_id.startswith(prefix):
            return n
    return None


def humanize_bytes(n: int | float | None) -> str:
    """Auto-scaled byte count: ``17179869184`` -> ``"16 GiB"``.

    ``None`` or a negative value (a probe that couldn't determine a
    size) renders as ``"-"`` rather than raising.
    """
    if n is None or (isinstance(n, int | float) and n < 0):
        return "-"
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024 or unit == "PiB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} PiB"  # pragma: no cover -- unreachable, loop always returns


def humanize_hz(n: int | float | None) -> str:
    """Auto-scaled clock: ``2800000000`` -> ``"2.80 GHz"``."""
    if not n:
        return "-"
    ghz = float(n) / 1_000_000_000
    if ghz >= 0.1:
        return f"{ghz:.2f} GHz"
    mhz = float(n) / 1_000_000
    return f"{mhz:.0f} MHz"


def _extract_system(lshw: Any) -> dict[str, Any]:
    if not isinstance(lshw, dict):
        return {}
    out: dict[str, Any] = {}
    if lshw.get("product"):
        out["model"] = lshw["product"]
    if lshw.get("vendor"):
        out["vendor"] = lshw["vendor"]
    if lshw.get("serial"):
        out["serial"] = lshw["serial"]
    firmware = _find_by_id_prefix(lshw, "firmware")
    if firmware:
        version = firmware.get("version")
        date = firmware.get("date")
        vendor = firmware.get("vendor")
        bits = [b for b in (version, date) if b]
        if bits:
            out["firmware"] = " (".join(bits) + (")" if date else "")
        if vendor:
            out["firmware_vendor"] = vendor
    return out


def _cpu_arch(node: dict[str, Any]) -> str | None:
    capabilities = node.get("capabilities")
    if isinstance(capabilities, dict) and "x86-64" in capabilities:
        return "x86_64"
    width = node.get("width")
    if width:
        return f"{width}-bit"
    return None


def _extract_cpu(lshw: Any) -> dict[str, Any]:
    sockets: list[dict[str, Any]] = []
    for node in _find_all(lshw, "processor"):
        config = node.get("configuration") or {}
        cores = config.get("cores")
        threads = config.get("threads")
        socket: dict[str, Any] = {
            "model": node.get("product") or node.get("description"),
            "vendor": node.get("vendor"),
            "arch": _cpu_arch(node),
            "cores": int(cores) if cores is not None else None,
            "threads": int(threads) if threads is not None else None,
            "mhz_current": node.get("size"),
            "mhz_max": node.get("capacity"),
            "slot": node.get("slot") or node.get("physid"),
        }
        sockets.append(socket)

    total_cores = sum(s["cores"] for s in sockets if s["cores"])
    total_threads = sum(s["threads"] for s in sockets if s["threads"])
    return {
        "sockets": sockets,
        "total_cores": total_cores or None,
        "total_threads": total_threads or None,
    }


def _parse_dimm_type_speed(description: str) -> tuple[str | None, int | None]:
    """Pull ``("DDR4", 3200000000)`` out of an lshw DIMM description
    string, or ``(None, None)`` when it doesn't match the usual
    ``"DIMM DDR4 Synchronous 3200 MHz (0.3 ns)"`` shape."""
    ddr_match = _DDR_RE.search(description or "")
    mhz_match = _MHZ_RE.search(description or "")
    ddr = ddr_match.group(0).upper() if ddr_match else None
    speed_hz = int(mhz_match.group(1)) * 1_000_000 if mhz_match else None
    return ddr, speed_hz


def _extract_memory(lshw: Any) -> dict[str, Any]:
    mem_node = None
    for n in _iter_nodes(lshw):
        if n.get("class") == "memory" and n.get("id") == "memory":
            mem_node = n
            break

    if mem_node is None:
        return {"total_bytes": None, "dimms": None}

    total_bytes = mem_node.get("size")
    raw_dimms = [
        c
        for c in (mem_node.get("children") or [])
        if isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"].startswith("bank")
    ]

    if not raw_dimms:
        return {"total_bytes": total_bytes, "dimms": None}

    dimms: list[dict[str, Any]] = []
    for d in raw_dimms:
        size = d.get("size") or 0
        description = d.get("description") or ""
        product = str(d.get("product", "")).upper()
        empty = size <= 0 or "empty" in description.lower() or product == "NO DIMM"
        dimm_type, speed_hz = _parse_dimm_type_speed(description)
        form_factor = "SODIMM" if "sodimm" in description.lower() else "DIMM"
        dimms.append(
            {
                "slot": d.get("slot") or d.get("physid"),
                "size_bytes": None if empty else size,
                "speed_hz": None if empty else (d.get("clock") or speed_hz),
                "type": None if empty else dimm_type,
                "form_factor": form_factor,
                "empty": empty,
            }
        )

    if total_bytes is None:
        total_bytes = sum(d["size_bytes"] or 0 for d in dimms) or None

    populated = [d for d in dimms if not d["empty"]]
    dominant_type = None
    dominant_speed_hz = None
    if populated:
        type_counts = Counter(d["type"] for d in populated if d["type"])
        if type_counts:
            dominant_type = type_counts.most_common(1)[0][0]
        speed_counts = Counter(d["speed_hz"] for d in populated if d["speed_hz"])
        if speed_counts:
            dominant_speed_hz = speed_counts.most_common(1)[0][0]

    return {
        "total_bytes": total_bytes,
        "dimms": dimms,
        "slots_populated": len(populated),
        "slots_total": len(dimms),
        "dominant_type": dominant_type,
        "dominant_speed_hz": dominant_speed_hz,
    }


def _extract_nics(lshw: Any) -> list[dict[str, Any]]:
    nics: list[dict[str, Any]] = []
    for node in _find_all(lshw, "network"):
        config = node.get("configuration") or {}
        speed_hz = node.get("capacity") or node.get("size")
        nics.append(
            {
                "name": node.get("logicalname") or node.get("id"),
                "mac": node.get("serial"),
                "vendor": node.get("vendor"),
                "driver": config.get("driver"),
                "speed": humanize_hz(speed_hz) if speed_hz else None,
            }
        )
    return nics


def normalise_inventory(inventory: dict[str, Any] | None) -> dict[str, Any]:
    """Return a render-friendly summary of a raw inventory blob.

    ``inventory`` is the verbatim payload stored on ``Machine.inventory``
    (``{"lshw": ..., "disks": [...]}``); the return shape is
    ``{"system": {...}, "cpu": {...}, "memory": {...}, "nics": [...],
    "disks": [...]}``, each section defaulting to an empty
    dict/list when the source data doesn't have it. Safe to call on
    ``None`` or a payload with no ``lshw`` key -- everything degrades
    to empty sections rather than raising.
    """
    if not isinstance(inventory, dict):
        inventory = {}
    lshw = inventory.get("lshw")

    return {
        "system": _extract_system(lshw),
        "cpu": _extract_cpu(lshw),
        "memory": _extract_memory(lshw),
        "nics": _extract_nics(lshw),
        "disks": inventory.get("disks") or [],
    }
