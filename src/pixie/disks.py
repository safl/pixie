"""Block-device discovery via ``lsblk``.

Pure-data module: returns plain dicts so the result can be JSON-serialised
or tabulated by ``pixie`` without further translation.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

# Columns we ask ``lsblk`` for. NAME and PATH are both requested because
# loop/ram devices sometimes lack PATH.
_LSBLK_COLS = "NAME,PATH,SIZE,TYPE,VENDOR,MODEL,SERIAL,RM,RO,MOUNTPOINTS,TRAN"

# Top-level types we surface. Partitions are a child of "disk" and are
# not reported as separate entries in the default output.
_INTERESTING_TYPES = {"disk"}


def list_disks() -> list[dict[str, Any]]:
    """Return interesting block devices on the local system.

    Shells out to ``lsblk -J`` and filters to top-level disks (drops
    loop, ram, rom, etc.). Each entry is a plain dict with stable keys.
    """
    proc = subprocess.run(
        ["lsblk", "-J", "-o", _LSBLK_COLS],
        capture_output=True,
        text=True,
        check=True,
        # Bound the call so a stuck IO subsystem (failing disk
        # responding slowly to udev queries) can't hang ``pixie``
        # indefinitely. 10s is generous; healthy lsblk returns
        # in <100ms on every box I've tested.
        timeout=10,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        # lsblk exited 0 but emitted non-JSON. ``check=True`` covers a
        # non-zero exit, but a zero-exit-with-truncated/empty-stdout
        # (seen on cut-down busybox lsblk builds) would otherwise raise
        # an uncaught ``ValueError`` and crash disk selection. Surface
        # it as a SubprocessError so the callers that already guard
        # lsblk failures (the TUI disk picker, the CLI) degrade to "no
        # disks discoverable" instead of tracing back.
        raise subprocess.SubprocessError(f"lsblk returned unparseable JSON: {exc}") from exc
    devices: list[dict[str, Any]] = payload.get("blockdevices", [])

    out: list[dict[str, Any]] = []
    for d in devices:
        if d.get("type") not in _INTERESTING_TYPES:
            continue
        out.append(
            {
                "path": d.get("path") or f"/dev/{d['name']}",
                "size": d.get("size"),
                "type": d.get("type"),
                "vendor": _strip_or_none(d.get("vendor")),
                "model": _strip_or_none(d.get("model")),
                # Some USB enclosures / vendor-firmware report
                # serials with trailing whitespace; strip for
                # consistency with vendor / model. ``pixie`` in
                # auto-flash mode matches the plan's
                # ``target_disk_serial`` against this value
                # exactly, so the same strip on both ends keeps
                # the gate working when the inventory side and
                # the flash-time side agree on the canonical form.
                "serial": _strip_or_none(d.get("serial")),
                "tran": d.get("tran"),
                "removable": bool(d.get("rm")),
                "readonly": bool(d.get("ro")),
                "mountpoints": [m for m in (d.get("mountpoints") or []) if m],
            }
        )
    return out


def _strip_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
