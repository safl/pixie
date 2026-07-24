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

# An overlay is a globally-named writable volume over ONE base image.
# Identity is its ``alias`` (globally unique), NOT the machine: the
# qcow2 at ``qcow2_path`` has ``backing_file`` pointing at the catalog
# blob for ``image_sha`` and holds every write made through it. Exactly
# one machine may hold exclusive write at a time -- ``attached_mac``
# (empty = free) records which; qemu-nbd's qcow2 image-lock is the
# backstop. Separate table from ``exports`` because the ephemeral
# (nbdkit, per-content) and persistent (qemu-nbd, per-alias) paths have
# different identity + lifecycle.
_OVERLAYS_SCHEMA = """
CREATE TABLE IF NOT EXISTS overlays (
    alias         TEXT PRIMARY KEY,
    image_sha     TEXT NOT NULL,
    qcow2_path    TEXT NOT NULL,
    attached_mac  TEXT NOT NULL DEFAULT '',
    nbd_port      INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'idle',
    error         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    last_boot_at  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_overlays_image_sha
    ON overlays(image_sha);
CREATE INDEX IF NOT EXISTS idx_overlays_attached_mac
    ON overlays(attached_mac);
"""


def _migrate_overlays_schema(conn: sqlite3.Connection) -> None:
    """Re-key the pre-alias ``overlays`` table (PK
    ``(mac, image_sha, profile)``) to the alias-keyed shape. For each
    old row a globally-unique ``alias = <profile>-<mac_slug>`` is minted
    (dedup on the rare collision) and ``attached_mac`` is seeded with the
    row's old ``mac`` so the machine that owned it keeps its exclusive
    hold. The qcow2 ``qcow2_path`` is left untouched -- no large files
    move during migration. Idempotent: a table that already has an
    ``alias`` column, or no overlays table at all, is left alone."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(overlays)").fetchall()}
    if not cols or "alias" in cols:
        return
    old_rows = conn.execute("SELECT * FROM overlays").fetchall()
    conn.execute("ALTER TABLE overlays RENAME TO overlays_pre_alias")
    conn.executescript(_OVERLAYS_SCHEMA)
    seen: set[str] = set()
    for r in old_rows:
        mac_slug = str(r["mac"]).replace(":", "-")
        base = f"{r['profile']}-{mac_slug}"
        alias = base
        n = 1
        while alias in seen:
            n += 1
            alias = f"{base}-{n}"
        seen.add(alias)
        conn.execute(
            """
            INSERT INTO overlays (
                alias, image_sha, qcow2_path, attached_mac,
                nbd_port, status, error, created_at, last_boot_at
            ) VALUES (?, ?, ?, ?, 0, 'idle', '', ?, ?)
            """,
            (
                alias,
                r["image_sha"],
                r["qcow2_path"],
                r["mac"],
                r["created_at"],
                r["last_boot_at"],
            ),
        )
    conn.execute("DROP TABLE overlays_pre_alias")


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
            _migrate_overlays_schema(conn)
            conn.executescript(_OVERLAYS_SCHEMA)

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
    """A globally-named writable volume over one base image.

    Identity is the ``alias`` (globally unique), not a machine: the
    qcow2 at ``qcow2_path`` has ``backing_file`` pointing at the catalog
    blob for ``image_sha`` and holds every write made through it.
    ``attached_mac`` records the single machine currently holding
    exclusive write ("" = free); a qemu-nbd subprocess serves it while
    ``status == 'running'``. Deleting the row is the operator's "Reset"
    action -- the caller unlinks the qcow2 too."""

    alias: str
    image_sha: str
    qcow2_path: str
    attached_mac: str = ""
    nbd_port: int = 0
    status: str = "idle"  # "idle" | "running" | "error"
    error: str = ""
    created_at: str = field(default_factory=now_iso)
    last_boot_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "alias": self.alias,
            "image_sha": self.image_sha,
            "qcow2_path": self.qcow2_path,
            "attached_mac": self.attached_mac,
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
    """Repository over the alias-keyed ``overlays`` table.

    Same DB path as :class:`ExportsStore` + :class:`CatalogStore`. The
    table is created + migrated by :meth:`ExportsStore._ensure_schema`
    (constructed first at startup), so this is a thin data-access
    sibling."""

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
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM overlays ORDER BY alias").fetchall()
        return [_row_to_overlay(r) for r in rows]

    def get(self, alias: str) -> Overlay | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM overlays WHERE alias = ?", (alias,)).fetchone()
        return _row_to_overlay(row) if row else None

    def list_for_image(self, image_sha: str) -> list[Overlay]:
        """Every overlay over this base image. Drives the bind-form
        picker (which aliases a machine could attach for this image)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM overlays WHERE image_sha = ? ORDER BY alias", (image_sha,)
            ).fetchall()
        return [_row_to_overlay(r) for r in rows]

    def upsert(self, ov: Overlay) -> None:
        with _DB_WRITE_LOCK, self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO overlays (
                    alias, image_sha, qcow2_path, attached_mac,
                    nbd_port, status, error, created_at, last_boot_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ov.alias,
                    ov.image_sha,
                    ov.qcow2_path,
                    ov.attached_mac,
                    ov.nbd_port,
                    ov.status,
                    ov.error,
                    ov.created_at,
                    ov.last_boot_at,
                ),
            )

    def delete(self, alias: str) -> bool:
        with _DB_WRITE_LOCK, self._conn() as conn:
            cur = conn.execute("DELETE FROM overlays WHERE alias = ?", (alias,))
            return cur.rowcount > 0

    def update_runtime(self, alias: str, *, nbd_port: int, status: str, error: str = "") -> None:
        """Refresh transient port + status after a qemu-nbd spawn."""
        with _DB_WRITE_LOCK, self._conn() as conn:
            conn.execute(
                "UPDATE overlays SET nbd_port = ?, status = ?, error = ? WHERE alias = ?",
                (nbd_port, status, error, alias),
            )

    def touch_last_boot(self, alias: str) -> None:
        with _DB_WRITE_LOCK, self._conn() as conn:
            conn.execute("UPDATE overlays SET last_boot_at = ? WHERE alias = ?", (now_iso(), alias))

    def attach(self, alias: str, mac: str) -> None:
        """Record ``mac`` as the exclusive writer of ``alias``.
        Single-writer is enforced at the call site (bind route + renderer
        refuse a hand-off to a different mac); this just records it."""
        with _DB_WRITE_LOCK, self._conn() as conn:
            conn.execute("UPDATE overlays SET attached_mac = ? WHERE alias = ?", (mac, alias))

    def detach(self, alias: str) -> None:
        """Release the writer hold on ``alias`` (attached_mac -> '')."""
        with _DB_WRITE_LOCK, self._conn() as conn:
            conn.execute("UPDATE overlays SET attached_mac = '' WHERE alias = ?", (alias,))

    def detach_mac(self, mac: str, *, keep: str = "") -> None:
        """Release every alias ``mac`` holds except ``keep`` -- called
        when a machine rebinds away so a stale hold can't block another
        machine from attaching."""
        with _DB_WRITE_LOCK, self._conn() as conn:
            conn.execute(
                "UPDATE overlays SET attached_mac = '' WHERE attached_mac = ? AND alias != ?",
                (mac, keep),
            )


def _row_to_overlay(row: sqlite3.Row) -> Overlay:
    return Overlay(
        alias=row["alias"],
        image_sha=row["image_sha"],
        qcow2_path=row["qcow2_path"],
        attached_mac=row["attached_mac"],
        nbd_port=row["nbd_port"] or 0,
        status=row["status"],
        error=row["error"],
        created_at=row["created_at"],
        last_boot_at=row["last_boot_at"],
    )
