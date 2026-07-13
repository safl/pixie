"""Exports table + typed row.

An export row is name + content_sha256 (points at the catalog blob) +
transient runtime fields (nbd_port, status). The row is persistent so
a restart can respawn the NBD supervisor without operator help; the
port + status are refreshed at spawn time.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pixie._util import now_iso

_DB_WRITE_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS exports (
    name           TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL,
    nbd_port       INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'idle',
    error          TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exports_content_sha
    ON exports(content_sha256);
"""


@dataclass
class Export:
    """One registered NBD export."""

    name: str
    content_sha256: str
    nbd_port: int = 0
    status: str = "idle"  # "idle" | "running" | "error"
    error: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "content_sha256": self.content_sha256,
            "nbd_port": self.nbd_port or None,
            "status": self.status,
            "error": self.error or None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ExportsStore:
    """Repository over the ``exports`` table on the shared state.db.

    Shares the state.db with :class:`pixie.catalog.CatalogStore`;
    pixie's design is one state.db per deploy. Callers pass the DB
    path in explicitly so tests can point at a fresh tmpdir.
    """

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

    # ---------- CRUD ------------------------------------------------

    def list(self) -> list[Export]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM exports ORDER BY name").fetchall()
        return [_row_to_export(r) for r in rows]

    def get(self, name: str) -> Export | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM exports WHERE name = ?", (name,)).fetchone()
        return _row_to_export(row) if row else None

    def upsert(self, export: Export) -> None:
        with _DB_WRITE_LOCK, self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO exports (
                    name, content_sha256, nbd_port, status, error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    export.name,
                    export.content_sha256,
                    export.nbd_port,
                    export.status,
                    export.error,
                    export.created_at,
                    now_iso(),
                ),
            )

    def delete(self, name: str) -> bool:
        with _DB_WRITE_LOCK, self._conn() as conn:
            cur = conn.execute("DELETE FROM exports WHERE name = ?", (name,))
            return cur.rowcount > 0

    def update_runtime(
        self,
        name: str,
        *,
        nbd_port: int,
        status: str,
        error: str = "",
    ) -> None:
        """Refresh the transient fields (port + status + error) after
        a spawn attempt. Silent on missing row (raced with a delete)."""
        with _DB_WRITE_LOCK, self._conn() as conn:
            conn.execute(
                """
                UPDATE exports
                SET nbd_port = ?, status = ?, error = ?, updated_at = ?
                WHERE name = ?
                """,
                (nbd_port, status, error, now_iso(), name),
            )


def _row_to_export(row: sqlite3.Row) -> Export:
    return Export(
        name=row["name"],
        content_sha256=row["content_sha256"],
        nbd_port=row["nbd_port"] or 0,
        status=row["status"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
