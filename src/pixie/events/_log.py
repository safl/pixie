"""Events table + append/list operations.

Events are (kind, subject_kind, subject_id, summary, details, ts)
tuples. ``kind`` is a dotted string (e.g. ``catalog.fetch.started``)
that operators grep on. ``subject_kind`` + ``subject_id`` scope the
event to a specific resource: ``machine``/<mac>, ``export``/<name>,
``entry``/<catalog-name>.

All time in ISO-8601 UTC (``now_iso`` from the shared util).
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pixie._util import now_iso
from pixie.events._kinds import KNOWN_EVENT_KINDS

_DB_WRITE_LOCK = threading.Lock()


class UnknownEventKind(ValueError):
    """Raised by :meth:`EventsLog.emit` when the caller passed a
    ``kind`` string not in :data:`pixie.events.KNOWN_EVENT_KINDS`.

    Every action pixie takes must land in the event log with a
    well-defined identifier from ``pixie.events._kinds``; the closed
    set is enforced (not advisory) so a new mutation site cannot slip
    through without an operator seeing it. Add the constant to
    ``_kinds.py`` first, then wire the call site.
    """


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    kind          TEXT NOT NULL,
    subject_kind  TEXT NOT NULL DEFAULT '',
    subject_id    TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    details_json  TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_subject
    ON events(subject_kind, subject_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


@dataclass
class Event:
    ts: str
    kind: str
    subject_kind: str = ""
    subject_id: str = ""
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "ts": self.ts,
            "kind": self.kind,
            "summary": self.summary,
        }
        if self.subject_kind:
            out["subject_kind"] = self.subject_kind
        if self.subject_id:
            out["subject_id"] = self.subject_id
        if self.details:
            out["details"] = self.details
        return out


class EventsLog:
    """Repository over the ``events`` table. Append-only + query."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextlib.contextmanager
    def _conn(self) -> Generator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def emit(
        self,
        kind: str,
        *,
        subject_kind: str = "",
        subject_id: str = "",
        summary: str = "",
        details: dict[str, Any] | None = None,
    ) -> Event:
        """Append one row.

        ``kind`` MUST be one of the constants declared in
        :mod:`pixie.events._kinds`; anything else raises
        :class:`UnknownEventKind`. Every pixie mutation carries an
        event log entry with a well-defined identifier, and the
        closed set is enforced so a new mutation site cannot slip in
        without an operator noticing. Register the constant in
        ``_kinds.py`` first, then wire the call site.
        """
        if kind not in KNOWN_EVENT_KINDS:
            raise UnknownEventKind(
                f"event kind {kind!r} is not registered in "
                f"pixie.events._kinds.KNOWN_EVENT_KINDS. Add a constant "
                f"there first (with a docstring) before emitting from a "
                f"new site."
            )
        row = Event(
            ts=now_iso(),
            kind=kind,
            subject_kind=subject_kind,
            subject_id=subject_id,
            summary=summary,
            details=details or {},
        )
        with _DB_WRITE_LOCK, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO events (ts, kind, subject_kind, subject_id, summary, details_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row.ts,
                    row.kind,
                    row.subject_kind,
                    row.subject_id,
                    row.summary,
                    json.dumps(row.details),
                ),
            )
            row.id = int(cur.lastrowid) if cur.lastrowid is not None else None
        return row

    def list(
        self,
        *,
        kind: str = "",
        subject_kind: str = "",
        subject_id: str = "",
        since_id: int = 0,
        limit: int = 100,
    ) -> list[Event]:
        """Reverse-chronological (newest first) with optional filters.

        ``since_id`` is exclusive (>) so operators can build a poll
        loop off ``max(id)`` without dedup. Filters are ANDed; empty
        strings match anything.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if subject_kind:
            clauses.append("subject_kind = ?")
            params.append(subject_kind)
        if subject_id:
            clauses.append("subject_id = ?")
            params.append(subject_id)
        if since_id > 0:
            clauses.append("id > ?")
            params.append(since_id)
        sql = "SELECT * FROM events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row(r) for r in rows]


def _row(r: sqlite3.Row) -> Event:
    try:
        details = json.loads(r["details_json"] or "{}")
        if not isinstance(details, dict):
            details = {}
    except json.JSONDecodeError:
        details = {}
    return Event(
        id=r["id"],
        ts=r["ts"],
        kind=r["kind"],
        subject_kind=r["subject_kind"],
        subject_id=r["subject_id"],
        summary=r["summary"],
        details=details,
    )
