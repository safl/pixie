"""Machines table + typed row.

One row per MAC. The two operator-writable fields are ``boot_mode``
and ``image_content_sha256``; the rest are discovery + telemetry.
Discovery is a side-effect of ``GET /pxe/<mac>`` -- the routes
module upserts a row (or bumps ``last_seen_at`` on an existing one)
before rendering the plan.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
import threading
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pixie._util import now_iso

_DB_WRITE_LOCK = threading.Lock()

# Boot modes pixie renders a plan for. The set is closed on purpose:
# an unknown mode on a row would silently fall through to the default
# and confuse an operator staring at ``GET /machines/<mac>``. Mirrors
# bty's tuple minus ``bty-tui`` (whose live-env driver has not been
# ported yet). ``pixie-flash-*`` + ``pixie-inventory`` chain into
# pixie's own live env; the renderer currently emits an
# ``unavailable`` plan for them so a bound target boots into a
# readable "live env not yet baked" screen rather than kernel-panicking
# on a bty-media initrd.
BOOT_MODES: frozenset[str] = frozenset(
    {
        "ipxe-exit",
        "pixie-flash-once",
        "pixie-flash-always",
        "pixie-inventory",
        "ramboot",
    }
)
DEFAULT_BOOT_MODE = "ipxe-exit"

# Normalise MAC to lower-case colon form. Rejects anything else.
_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


class BadMac(ValueError):
    """The provided MAC failed the canonical-form check."""


def normalise_mac(mac: str) -> str:
    """Fold ``mac`` to ``aa:bb:cc:dd:ee:ff`` form + lowercase. Accepts
    the same MAC in ``AA-BB-CC-DD-EE-FF`` or ``aabbccddeeff`` shapes;
    raises :class:`BadMac` on anything unparseable.
    """
    raw = (mac or "").strip().lower().replace("-", ":").replace(".", ":")
    if ":" not in raw and len(raw) == 12:
        raw = ":".join(raw[i : i + 2] for i in range(0, 12, 2))
    if not _MAC_RE.match(raw):
        raise BadMac(f"invalid mac: {mac!r}")
    return raw


_SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    mac                    TEXT PRIMARY KEY,
    boot_mode              TEXT NOT NULL DEFAULT 'ipxe-exit',
    image_content_sha256   TEXT NOT NULL DEFAULT '',
    inventory_json         TEXT NOT NULL DEFAULT '',
    inventory_at           TEXT NOT NULL DEFAULT '',
    discovered_at          TEXT NOT NULL,
    last_seen_at           TEXT NOT NULL,
    last_seen_ip           TEXT NOT NULL DEFAULT '',
    updated_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_machines_image_content_sha
    ON machines(image_content_sha256);
"""


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Additive column adds for existing state.db files. Idempotent."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(machines)").fetchall()}
    if "inventory_json" not in cols:
        conn.execute("ALTER TABLE machines ADD COLUMN inventory_json TEXT NOT NULL DEFAULT ''")
    if "inventory_at" not in cols:
        conn.execute("ALTER TABLE machines ADD COLUMN inventory_at TEXT NOT NULL DEFAULT ''")


@dataclass
class Machine:
    mac: str
    boot_mode: str = DEFAULT_BOOT_MODE
    image_content_sha256: str = ""
    inventory: dict[str, Any] = field(default_factory=dict)
    inventory_at: str = ""
    discovered_at: str = field(default_factory=now_iso)
    last_seen_at: str = field(default_factory=now_iso)
    last_seen_ip: str = ""
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "mac": self.mac,
            "boot_mode": self.boot_mode,
            "discovered_at": self.discovered_at,
            "last_seen_at": self.last_seen_at,
            "updated_at": self.updated_at,
        }
        if self.image_content_sha256:
            out["image_content_sha256"] = self.image_content_sha256
        if self.last_seen_ip:
            out["last_seen_ip"] = self.last_seen_ip
        if self.inventory:
            out["inventory_at"] = self.inventory_at
            out["inventory"] = self.inventory
        return out


class MachinesStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            _migrate_schema(conn)

    @contextlib.contextmanager
    def _conn(self) -> Generator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def list(self) -> list[Machine]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM machines ORDER BY mac").fetchall()
        return [_row(r) for r in rows]

    def get(self, mac: str) -> Machine | None:
        canon = normalise_mac(mac)
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM machines WHERE mac = ?", (canon,)).fetchone()
        return _row(row) if row else None

    def upsert_binding(
        self,
        mac: str,
        *,
        boot_mode: str,
        image_content_sha256: str = "",
    ) -> Machine:
        """Operator-driven write: set boot mode + optional image ref.
        Creates the row if it doesn't exist; preserves discovery
        telemetry (``discovered_at``, ``last_seen_*``) on update."""
        canon = normalise_mac(mac)
        if boot_mode not in BOOT_MODES:
            raise ValueError(f"unknown boot_mode {boot_mode!r}; valid: {sorted(BOOT_MODES)}")
        if image_content_sha256 and not re.match(r"^[0-9a-f]{64}$", image_content_sha256):
            raise ValueError("image_content_sha256 must be 64 lowercase hex chars")

        now = now_iso()
        with _DB_WRITE_LOCK, self._conn() as conn:
            existing = conn.execute("SELECT * FROM machines WHERE mac = ?", (canon,)).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO machines (
                        mac, boot_mode, image_content_sha256,
                        discovered_at, last_seen_at, last_seen_ip, updated_at
                    ) VALUES (?, ?, ?, ?, ?, '', ?)
                    """,
                    (canon, boot_mode, image_content_sha256, now, now, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE machines
                    SET boot_mode = ?, image_content_sha256 = ?, updated_at = ?
                    WHERE mac = ?
                    """,
                    (boot_mode, image_content_sha256, now, canon),
                )
            row = conn.execute("SELECT * FROM machines WHERE mac = ?", (canon,)).fetchone()
        return _row(row)

    def touch_seen(self, mac: str, *, ip: str = "") -> Machine:
        """Discovery-side write: create-or-update ``last_seen_at`` +
        optionally ``last_seen_ip``. Does NOT touch operator fields."""
        canon = normalise_mac(mac)
        now = now_iso()
        with _DB_WRITE_LOCK, self._conn() as conn:
            existing = conn.execute("SELECT * FROM machines WHERE mac = ?", (canon,)).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO machines (
                        mac, boot_mode, image_content_sha256,
                        discovered_at, last_seen_at, last_seen_ip, updated_at
                    ) VALUES (?, ?, '', ?, ?, ?, ?)
                    """,
                    (canon, DEFAULT_BOOT_MODE, now, now, ip, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE machines
                    SET last_seen_at = ?, last_seen_ip = ?, updated_at = ?
                    WHERE mac = ?
                    """,
                    (now, ip or existing["last_seen_ip"], now, canon),
                )
            row = conn.execute("SELECT * FROM machines WHERE mac = ?", (canon,)).fetchone()
        return _row(row)

    def delete(self, mac: str) -> bool:
        canon = normalise_mac(mac)
        with _DB_WRITE_LOCK, self._conn() as conn:
            cur = conn.execute("DELETE FROM machines WHERE mac = ?", (canon,))
            return cur.rowcount > 0

    def set_inventory(self, mac: str, inventory: dict[str, Any]) -> Machine:
        """Store a fresh inventory blob against the machine row. Creates
        the row on first contact (mirrors touch_seen shape) so a bare
        POST from a live env's first boot lands correctly."""
        import json as _json

        canon = normalise_mac(mac)
        now = now_iso()
        blob = _json.dumps(inventory)
        with _DB_WRITE_LOCK, self._conn() as conn:
            existing = conn.execute("SELECT * FROM machines WHERE mac = ?", (canon,)).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO machines (
                        mac, boot_mode, image_content_sha256,
                        inventory_json, inventory_at,
                        discovered_at, last_seen_at, last_seen_ip, updated_at
                    ) VALUES (?, ?, '', ?, ?, ?, ?, '', ?)
                    """,
                    (canon, DEFAULT_BOOT_MODE, blob, now, now, now, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE machines
                    SET inventory_json = ?, inventory_at = ?, updated_at = ?
                    WHERE mac = ?
                    """,
                    (blob, now, now, canon),
                )
            row = conn.execute("SELECT * FROM machines WHERE mac = ?", (canon,)).fetchone()
        return _row(row)


def _row(r: sqlite3.Row) -> Machine:
    import json as _json

    inv: dict[str, Any] = {}
    inv_at = ""
    # New columns post-migration; older schema pre-v0.9 may not
    # have them yet. sqlite3.Row raises IndexError on missing keys.
    with contextlib.suppress(IndexError, KeyError):
        raw = r["inventory_json"] or ""
        if raw:
            try:
                parsed = _json.loads(raw)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                inv = parsed
    with contextlib.suppress(IndexError, KeyError):
        inv_at = r["inventory_at"] or ""
    return Machine(
        mac=r["mac"],
        boot_mode=r["boot_mode"],
        image_content_sha256=r["image_content_sha256"],
        inventory=inv,
        inventory_at=inv_at,
        discovered_at=r["discovered_at"],
        last_seen_at=r["last_seen_at"],
        last_seen_ip=r["last_seen_ip"],
        updated_at=r["updated_at"],
    )
