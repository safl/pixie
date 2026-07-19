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

-- One row per (mac, image_sha, profile) triple; the qcow2 file at
-- ``qcow2_path`` has ``backing_file`` pointing at the catalog blob
-- for ``image_sha`` and holds every write the machine has made under
-- that profile. Separate from the ``exports`` table above because the
-- ephemeral (nbdkit) and persistent (qemu-nbd) paths have different
-- identity (per-content vs per-triple) and lifecycle (respawn on
-- restart vs create-on-first-bind).
CREATE TABLE IF NOT EXISTS overlays (
    mac           TEXT NOT NULL,
    image_sha     TEXT NOT NULL,
    profile       TEXT NOT NULL,
    qcow2_path    TEXT NOT NULL,
    nbd_port      INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'idle',
    error         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    last_boot_at  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (mac, image_sha, profile)
);
CREATE INDEX IF NOT EXISTS idx_overlays_mac
    ON overlays(mac);
CREATE INDEX IF NOT EXISTS idx_overlays_image_sha
    ON overlays(image_sha);
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


@dataclass
class Overlay:
    """One persistent per-machine qcow2 overlay for ``boot_mode=nbdboot``.

    Identity is the ``(mac, image_sha, profile)`` triple. The qcow2 at
    ``qcow2_path`` has ``backing_file`` pointing at the catalog blob
    for ``image_sha``; a qemu-nbd subprocess serves it while the row's
    ``status == 'running'``. Deleting the row is the operator's
    "Reset overlay" action -- the caller unlinks the qcow2 too."""

    mac: str
    image_sha: str
    profile: str
    qcow2_path: str
    nbd_port: int = 0
    status: str = "idle"  # "idle" | "running" | "error"
    error: str = ""
    created_at: str = field(default_factory=now_iso)
    last_boot_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "mac": self.mac,
            "image_sha": self.image_sha,
            "profile": self.profile,
            "qcow2_path": self.qcow2_path,
            "status": self.status,
            "created_at": self.created_at,
        }
        if self.nbd_port:
            out["nbd_port"] = self.nbd_port
        if self.error:
            out["error"] = self.error
        if self.last_boot_at:
            out["last_boot_at"] = self.last_boot_at
        return out


class OverlaysStore:
    """Repository over the ``overlays`` table on the shared state.db.

    Same DB path as :class:`ExportsStore` + :class:`CatalogStore`.
    Kept as a sibling class rather than a method-set on ExportsStore
    so the ephemeral vs persistent lifecycles read as distinct
    concerns at the call site."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    @contextlib.contextmanager
    def _conn(self) -> Generator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def list_all(self) -> list[Overlay]:
        """Every overlay row across every (mac, image, profile) triple.
        Named ``list_all`` (not ``list``) so ``list[Overlay]`` in
        sibling method signatures below resolves to the built-in
        rather than the shadowed method."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM overlays ORDER BY mac, image_sha, profile"
            ).fetchall()
        return [_row_to_overlay(r) for r in rows]

    def get(self, mac: str, image_sha: str, profile: str) -> Overlay | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM overlays WHERE mac = ? AND image_sha = ? AND profile = ?",
                (mac, image_sha, profile),
            ).fetchone()
        return _row_to_overlay(row) if row else None

    def list_for_machine_and_image(self, mac: str, image_sha: str) -> list[Overlay]:
        """Every profile this ``(mac, image_sha)`` combo has ever created.
        Drives the bind-form overlay picker."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM overlays WHERE mac = ? AND image_sha = ? ORDER BY profile",
                (mac, image_sha),
            ).fetchall()
        return [_row_to_overlay(r) for r in rows]

    def upsert(self, ov: Overlay) -> None:
        with _DB_WRITE_LOCK, self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO overlays (
                    mac, image_sha, profile, qcow2_path,
                    nbd_port, status, error, created_at, last_boot_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ov.mac,
                    ov.image_sha,
                    ov.profile,
                    ov.qcow2_path,
                    ov.nbd_port,
                    ov.status,
                    ov.error,
                    ov.created_at,
                    ov.last_boot_at,
                ),
            )

    def delete(self, mac: str, image_sha: str, profile: str) -> bool:
        with _DB_WRITE_LOCK, self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM overlays WHERE mac = ? AND image_sha = ? AND profile = ?",
                (mac, image_sha, profile),
            )
            return cur.rowcount > 0

    def update_runtime(
        self,
        mac: str,
        image_sha: str,
        profile: str,
        *,
        nbd_port: int,
        status: str,
        error: str = "",
    ) -> None:
        """Refresh transient port + status after a qemu-nbd spawn."""
        with _DB_WRITE_LOCK, self._conn() as conn:
            conn.execute(
                """
                UPDATE overlays
                SET nbd_port = ?, status = ?, error = ?
                WHERE mac = ? AND image_sha = ? AND profile = ?
                """,
                (nbd_port, status, error, mac, image_sha, profile),
            )

    def touch_last_boot(self, mac: str, image_sha: str, profile: str) -> None:
        """Record the timestamp of the most recent plan render for this
        overlay. Used by the UI's "last used" column + future auto-GC."""
        with _DB_WRITE_LOCK, self._conn() as conn:
            conn.execute(
                """
                UPDATE overlays
                SET last_boot_at = ?
                WHERE mac = ? AND image_sha = ? AND profile = ?
                """,
                (now_iso(), mac, image_sha, profile),
            )


def _row_to_overlay(row: sqlite3.Row) -> Overlay:
    return Overlay(
        mac=row["mac"],
        image_sha=row["image_sha"],
        profile=row["profile"],
        qcow2_path=row["qcow2_path"],
        nbd_port=row["nbd_port"] or 0,
        status=row["status"],
        error=row["error"],
        created_at=row["created_at"],
        last_boot_at=row["last_boot_at"],
    )
