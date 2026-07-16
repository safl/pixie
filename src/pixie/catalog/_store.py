"""Catalog store: SQLite-backed metadata + content-addressed blobs
and artifacts on the filesystem.

One ``state.db`` under ``<state_dir>`` holds every catalog entry;
blobs land at ``<state_dir>/blobs/<content_sha256>/blob`` and the
tar.gz-unpacked netboot artifacts at
``<state_dir>/artifacts/<content_sha256>/{vmlinuz,initrd,manifest.json}``.

Both paths are content-addressed: the same bytes served under
different catalog names share on-disk storage, and an entry's blob
URL (``/b/<sha>/<name>``) never changes across renames as long as the
content is stable. This is a deliberate departure from withcache's
URL-addressed store; pixie is an operator-curated library, not a
URL->bytes cache.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Generator
from pathlib import Path

from pixie._util import now_iso
from pixie.catalog._schema import CatalogEntry

_DB_WRITE_LOCK = threading.Lock()

# SQLite schema. Migrations for pixie land as ``CREATE TABLE IF NOT
# EXISTS`` at ``open_store`` time, per bty's pattern. Pre-1.0: if a
# schema change is required and the migration would be complex, we
# rotate ``state.db`` -> ``state.db.<oldver>.<ts>.bak`` and start
# clean rather than write migration SQL. (Documented ergonomics
# tradeoff -- Cf. bty v0.25.0.)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_entries (
    name           TEXT PRIMARY KEY,
    src            TEXT NOT NULL,
    format         TEXT NOT NULL,
    arch           TEXT NOT NULL DEFAULT '',
    description    TEXT NOT NULL DEFAULT '',
    netboot_src    TEXT NOT NULL DEFAULT '',
    content_sha256 TEXT NOT NULL DEFAULT '',
    size_bytes     INTEGER NOT NULL DEFAULT 0,
    fetched_at     TEXT NOT NULL DEFAULT '',
    added_at       TEXT NOT NULL,
    extra_json     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_catalog_entries_src ON catalog_entries(src);
CREATE INDEX IF NOT EXISTS idx_catalog_entries_content_sha
    ON catalog_entries(content_sha256);
"""


class CatalogStore:
    """Thin repository over ``state.db`` for catalog rows.

    Blob + artifact storage on the filesystem is siblings of the DB
    (``<state_dir>/{blobs,artifacts}/``); the store owns their
    directory-shape but does NOT own the fetch pipeline that fills
    them -- that lives in :mod:`pixie.catalog._fetcher`.
    """

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "state.db"
        self.blobs_dir = self.state_dir / "blobs"
        self.artifacts_dir = self.state_dir / "artifacts"
        self.blobs_dir.mkdir(exist_ok=True)
        self.artifacts_dir.mkdir(exist_ok=True)
        self._ensure_schema()

    # ---------- schema init ---------------------------------------

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

    # ---------- entry CRUD ----------------------------------------

    def list_entries(self) -> list[CatalogEntry]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM catalog_entries ORDER BY LOWER(name)").fetchall()
        return [_row_to_entry(r) for r in rows]

    def get_entry(self, name: str) -> CatalogEntry | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM catalog_entries WHERE name = ?", (name,)).fetchone()
        return _row_to_entry(row) if row else None

    def get_entry_by_src(self, src: str) -> CatalogEntry | None:
        """Look up an entry by its ``src`` URL. This is the pairing key
        for ``netboot_src`` cross-reference: pixie resolves a
        disk-image entry's netboot bundle by matching src, never by
        name."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM catalog_entries WHERE src = ?", (src,)).fetchone()
        return _row_to_entry(row) if row else None

    def upsert(self, entry: CatalogEntry) -> None:
        """Insert or update an entry by name. Uses INSERT OR REPLACE so
        editing an entry doesn't leave orphan rows; a full row write
        is cheap at catalog sizes."""
        import json as _json

        with _DB_WRITE_LOCK, self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO catalog_entries (
                    name, src, format, arch, description, netboot_src,
                    content_sha256, size_bytes, fetched_at, added_at,
                    extra_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.name,
                    entry.src,
                    entry.format,
                    entry.arch,
                    entry.description,
                    entry.netboot_src,
                    entry.content_sha256,
                    entry.size_bytes,
                    entry.fetched_at,
                    entry.added_at,
                    _json.dumps(entry.extra),
                ),
            )

    def delete(self, name: str) -> bool:
        """Remove an entry by name. Returns True iff a row was removed.
        Blob + artifact bytes are NOT deleted here; they may be shared
        by other entries with the same content_sha256. GC (refcount
        walk) lives on a caller in the routes layer."""
        with _DB_WRITE_LOCK, self._conn() as conn:
            cur = conn.execute("DELETE FROM catalog_entries WHERE name = ?", (name,))
            return cur.rowcount > 0

    def mark_unfetched(self, name: str) -> None:
        """Reverse of :meth:`mark_fetched`: clear the row's fetched
        fields (content sha, size, timestamp) without deleting the
        row itself. Used by the "delete blob but keep the entry"
        path so a subsequent Fetch re-runs the pipeline instead of
        needing the operator to re-Import the URL."""
        with _DB_WRITE_LOCK, self._conn() as conn:
            conn.execute(
                """
                UPDATE catalog_entries
                SET content_sha256 = '', size_bytes = 0, fetched_at = ''
                WHERE name = ?
                """,
                (name,),
            )

    def mark_fetched(
        self,
        name: str,
        *,
        content_sha256: str,
        size_bytes: int,
    ) -> None:
        """Post-fetch update: record content sha + size + timestamp.
        Failure (no matching row) is silent because the fetch pipeline
        may race with a concurrent delete; the fetched blob is
        content-addressed so it's not orphaned in a bad way."""
        with _DB_WRITE_LOCK, self._conn() as conn:
            conn.execute(
                """
                UPDATE catalog_entries
                SET content_sha256 = ?, size_bytes = ?, fetched_at = ?
                WHERE name = ?
                """,
                (content_sha256, size_bytes, now_iso(), name),
            )

    # ---------- content-addressed storage paths -------------------

    def blob_path(self, content_sha256: str) -> Path:
        """Where a fetched disk-image blob lives on disk."""
        return self.blobs_dir / content_sha256 / "blob"

    def artifact_dir(self, content_sha256: str) -> Path:
        """Where an unpacked netboot bundle's files live on disk."""
        return self.artifacts_dir / content_sha256

    def artifact_path(self, content_sha256: str, filename: str) -> Path:
        return self.artifact_dir(content_sha256) / filename

    def content_shas_in_use(self) -> set[str]:
        """The union of content_sha256 values referenced by any
        catalog row. Used to compute GC candidates."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT content_sha256 FROM catalog_entries WHERE content_sha256 != ''"
            ).fetchall()
        return {str(r["content_sha256"]) for r in rows}


def _row_to_entry(row: sqlite3.Row) -> CatalogEntry:
    import json as _json

    try:
        extra = _json.loads(row["extra_json"] or "{}")
        if not isinstance(extra, dict):
            extra = {}
    except _json.JSONDecodeError:
        extra = {}

    return CatalogEntry(
        name=row["name"],
        src=row["src"],
        format=row["format"],
        arch=row["arch"],
        description=row["description"],
        netboot_src=row["netboot_src"],
        content_sha256=row["content_sha256"],
        size_bytes=row["size_bytes"] or 0,
        fetched_at=row["fetched_at"],
        added_at=row["added_at"],
        extra=extra,
    )
