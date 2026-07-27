"""Shared audit-event emission for a machine bind.

Both the JSON API (``machines/_routes.py``) and the HTML form
(``web/main.py`` ``ui_machines_bind``) bind a machine through
``upsert_binding``; this keeps their event trail identical instead of
one path emitting and the other going silent. ``machine.bound`` fires on
a fresh MAC or a row that was only auto-registered (an uncommitted mode
with no bound image); ``machine.binding.changed`` fires when the mode or
image actually shifted from a real prior operator bind.
"""

from __future__ import annotations

from typing import Any

from pixie.events._kinds import MACHINE_BINDING_CHANGED, MACHINE_BOUND
from pixie.machines._store import UNCOMMITTED_BOOT_MODES


def emit_bind_event(log: Any, previous: Any, row: Any) -> None:
    """Emit the bound / binding-changed event for a completed bind.

    ``log`` is the events log (or ``None`` -- a no-op then). ``previous``
    is the row snapshot from before the bind (``None`` if the MAC was
    new); ``row`` is the freshly-upserted row.
    """
    if log is None:
        return
    details: dict[str, Any] = {"boot_mode": row.boot_mode}
    if row.image_content_sha256:
        details["image_content_sha256"] = row.image_content_sha256
    was_bound = previous is not None and (
        bool(previous.image_content_sha256) or previous.boot_mode not in UNCOMMITTED_BOOT_MODES
    )
    changed = previous is not None and (
        previous.boot_mode != row.boot_mode
        or previous.image_content_sha256 != row.image_content_sha256
    )
    if previous is not None and was_bound and changed:
        details["previous_boot_mode"] = previous.boot_mode
        if previous.image_content_sha256:
            details["previous_image_content_sha256"] = previous.image_content_sha256
        log.emit(
            MACHINE_BINDING_CHANGED,
            subject_kind="machine",
            subject_id=row.mac,
            summary=f"{row.mac}: {previous.boot_mode} -> {row.boot_mode}",
            details=details,
        )
    else:
        log.emit(
            MACHINE_BOUND,
            subject_kind="machine",
            subject_id=row.mac,
            summary=f"{row.mac} -> {row.boot_mode}",
            details=details,
        )
