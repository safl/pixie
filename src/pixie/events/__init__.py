"""Append-only event log.

Every write path in pixie emits one row: fetch started, fetch failed,
machine bound, export registered, TFTP started. Rows live on the
shared ``state.db``; there is one bus for the whole process, not one
per module.

Consumers:

* Operator UI (``/ui/events``): scrollable timeline the operator
  glances at to see what pixie has done recently.
* JSON API (``GET /events``): programmatic access + filtering by
  subject/kind/since.

Rows are immutable; a mis-emitted event is not corrected in place, a
follow-up event is emitted describing the correction. Simpler audit
trail, no back-dated writes.

Hard-forked from bty's ``_events_log.py`` (see
``docs/audit.md#bty``) 2026-07-13.
"""

from __future__ import annotations

from pixie.events._kinds import KNOWN_EVENT_KINDS
from pixie.events._log import EventsLog, UnknownEventKind

__all__ = ["KNOWN_EVENT_KINDS", "EventsLog", "UnknownEventKind"]
