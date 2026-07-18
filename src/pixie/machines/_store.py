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
from collections.abc import Generator, Sequence
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
        "pixie-tui",
        "nbdboot",
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
    labels                 TEXT NOT NULL DEFAULT '',
    target_disk_serial     TEXT NOT NULL DEFAULT '',
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

# Boot modes that write to the target disk. Binding these requires a
# ``target_disk_serial`` chosen from the machine's inventory, so the
# live env's flash pipeline has a concrete destination.
_FLASH_MODES: frozenset[str] = frozenset({"pixie-flash-once", "pixie-flash-always"})

# Bty's shape: alphanumeric-leading, alphanumeric + space + . _ - inside,
# 64 chars max per label, 16 labels max per machine. Matches the CSS-safe
# subset so a label can render as a ``.badge`` without escaping surprises.
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._\-]{0,63}$")
_LABEL_LIMIT = 16


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Additive column adds for existing state.db files. Idempotent."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(machines)").fetchall()}
    if "inventory_json" not in cols:
        conn.execute("ALTER TABLE machines ADD COLUMN inventory_json TEXT NOT NULL DEFAULT ''")
    if "inventory_at" not in cols:
        conn.execute("ALTER TABLE machines ADD COLUMN inventory_at TEXT NOT NULL DEFAULT ''")
    if "labels" not in cols:
        conn.execute("ALTER TABLE machines ADD COLUMN labels TEXT NOT NULL DEFAULT ''")
    if "target_disk_serial" not in cols:
        conn.execute("ALTER TABLE machines ADD COLUMN target_disk_serial TEXT NOT NULL DEFAULT ''")
    # Retired 2026-07 pre-1.0: sanboot_drive was carried on the bind
    # form as an iPXE ``sanboot`` BIOS drive slug, but pixie never
    # actually rendered it into any iPXE template -- the ipxe-exit
    # plan just does ``exit`` and hands off to firmware boot order.
    # Dropped from the schema (no back-compat shim); the column is
    # gone from fresh state.dbs and stays as an unused column on
    # existing ones. SQLite is forgiving of unread columns.
    # Renamed 2026-07: ``boot_mode='ramboot'`` -> ``'nbdboot'``. The
    # earlier name evoked "loads root into RAM" which is not what
    # this mode does -- it's a netboot that mounts the root over
    # NBD. "ramboot" was a pixie-ism (via bty-media -> pixie-media),
    # not a nosi convention, so the full rename lives entirely
    # inside our own build chain: kernel cmdline says
    # ``boot=nbdboot``, ``/scripts/nbdboot`` (pixie-media) is the
    # mountroot() driver, wire status tokens are ``nbdboot.*``.
    # Nosi bundles inherit the script at bake time; a coordinated
    # pixie + pixie-media + nosi release ships them together.
    # Migrate silently -- an existing state.db from before the
    # rename still resolves.
    conn.execute("UPDATE machines SET boot_mode = 'nbdboot' WHERE boot_mode = 'ramboot'")


def parse_labels(raw: str) -> list[str]:
    """Split a comma-separated label string into a normalised list.
    Whitespace-only tokens are dropped; duplicates are folded to a
    single occurrence in first-seen order. Raises :class:`ValueError`
    on any token that fails :data:`_LABEL_RE` or when the count
    exceeds :data:`_LABEL_LIMIT`."""
    out: list[str] = []
    seen: set[str] = set()
    for tok in (raw or "").split(","):
        s = tok.strip()
        if not s:
            continue
        if not _LABEL_RE.match(s):
            raise ValueError(
                f"label {s!r} must be alphanumeric-leading + a-z/0-9/space/._- (max 64 chars)"
            )
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    if len(out) > _LABEL_LIMIT:
        raise ValueError(f"at most {_LABEL_LIMIT} labels per machine (got {len(out)})")
    return out


@dataclass
class Machine:
    mac: str
    boot_mode: str = DEFAULT_BOOT_MODE
    image_content_sha256: str = ""
    labels: list[str] = field(default_factory=list)
    target_disk_serial: str = ""
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
        if self.labels:
            out["labels"] = list(self.labels)
        if self.target_disk_serial:
            out["target_disk_serial"] = self.target_disk_serial
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
        labels: Sequence[str] | None = None,
        target_disk_serial: str = "",
    ) -> Machine:
        """Operator-driven write: set boot mode + optional image ref.
        Creates the row if it doesn't exist; preserves discovery
        telemetry (``discovered_at``, ``last_seen_*``) on update.

        ``labels`` is a pre-parsed list (caller runs :func:`parse_labels`
        so form + JSON paths share the validator).
        ``target_disk_serial`` is the disk serial the live env's flash
        pipeline matches at flash time -- an operator picks it from
        the machine's reported inventory.
        """
        canon = normalise_mac(mac)
        if boot_mode not in BOOT_MODES:
            raise ValueError(f"unknown boot_mode {boot_mode!r}; valid: {sorted(BOOT_MODES)}")
        if image_content_sha256 and not re.match(r"^[0-9a-f]{64}$", image_content_sha256):
            raise ValueError("image_content_sha256 must be 64 lowercase hex chars")
        labels_json = _labels_to_json(list(labels or []))
        target_serial = (target_disk_serial or "").strip()

        now = now_iso()
        with _DB_WRITE_LOCK, self._conn() as conn:
            existing = conn.execute("SELECT * FROM machines WHERE mac = ?", (canon,)).fetchone()
            # Flash modes need to know which disk to overwrite. The live
            # env matches ``target_disk_serial`` against currently-attached
            # disks at flash time; a bind that doesn't name one would
            # either fall through to "flash /dev/sda blindly" (dangerous)
            # or refuse (silent no-op). Reject early so the operator sees
            # a 422 pointing at the missing prerequisite, and require the
            # value be one of the serials on the machine's stored
            # inventory so a hand-typed sha doesn't sneak past. New /
            # never-inventoried MACs are told to run pixie-inventory
            # first.
            if boot_mode in _FLASH_MODES:
                inv_disks = _inventory_disk_serials(existing)
                if not inv_disks:
                    raise ValueError(
                        f"boot_mode={boot_mode!r} requires a target_disk_serial, "
                        "but this machine has no inventory yet. "
                        "Bind boot_mode=pixie-inventory + power-cycle first."
                    )
                if not target_serial:
                    raise ValueError(
                        f"boot_mode={boot_mode!r} requires target_disk_serial "
                        f"(one of: {sorted(inv_disks)})"
                    )
                if target_serial not in inv_disks:
                    raise ValueError(
                        f"target_disk_serial={target_serial!r} is not in this "
                        f"machine's inventory (known: {sorted(inv_disks)})"
                    )
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO machines (
                        mac, boot_mode, image_content_sha256,
                        labels, target_disk_serial,
                        discovered_at, last_seen_at, last_seen_ip, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?)
                    """,
                    (
                        canon,
                        boot_mode,
                        image_content_sha256,
                        labels_json,
                        target_serial,
                        now,
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE machines
                    SET boot_mode = ?, image_content_sha256 = ?,
                        labels = ?, target_disk_serial = ?,
                        updated_at = ?
                    WHERE mac = ?
                    """,
                    (
                        boot_mode,
                        image_content_sha256,
                        labels_json,
                        target_serial,
                        now,
                        canon,
                    ),
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

    def set_labels(self, mac: str, labels: Sequence[str]) -> Machine:
        """Store operator-supplied labels against the row. Creates the
        row on first contact (mirrors ``touch_seen`` + ``set_inventory``)
        so an operator tagging a machine before it has PXE'd works.

        ``labels`` is a pre-parsed list -- callers run
        :func:`parse_labels` so the form + JSON paths share the
        validator. Passing an empty list CLEARS the labels."""
        canon = normalise_mac(mac)
        now = now_iso()
        labels_json = _labels_to_json(list(labels or []))
        with _DB_WRITE_LOCK, self._conn() as conn:
            existing = conn.execute("SELECT * FROM machines WHERE mac = ?", (canon,)).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO machines (
                        mac, boot_mode, image_content_sha256,
                        labels, discovered_at, last_seen_at, last_seen_ip, updated_at
                    ) VALUES (?, ?, '', ?, ?, ?, '', ?)
                    """,
                    (canon, DEFAULT_BOOT_MODE, labels_json, now, now, now),
                )
            else:
                conn.execute(
                    "UPDATE machines SET labels = ?, updated_at = ? WHERE mac = ?",
                    (labels_json, now, canon),
                )
            row = conn.execute("SELECT * FROM machines WHERE mac = ?", (canon,)).fetchone()
        return _row(row)

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


def _inventory_disk_serials(row: sqlite3.Row | None) -> set[str]:
    """Pull the set of disk serials off a stored inventory blob. Used
    to check ``target_disk_serial`` against the machine's reported
    hardware at bind time. Returns an empty set when the row is
    missing, the inventory blob is empty / malformed, or none of the
    disks report a serial."""
    import json as _json

    if row is None:
        return set()
    with contextlib.suppress(IndexError, KeyError):
        raw = row["inventory_json"] or ""
        if not raw:
            return set()
        try:
            parsed = _json.loads(raw)
        except ValueError:
            return set()
        if not isinstance(parsed, dict):
            return set()
        disks = parsed.get("disks") or []
        if not isinstance(disks, list):
            return set()
        out: set[str] = set()
        for d in disks:
            if not isinstance(d, dict):
                continue
            serial = str(d.get("serial") or "").strip()
            if serial:
                out.add(serial)
        return out
    return set()


def _labels_to_json(labels: list[str]) -> str:
    """Serialise a validated label list to a JSON array string. Empty
    list -> ``''`` so the DB stores a single trivially-checkable form."""
    import json as _json

    return _json.dumps(list(labels)) if labels else ""


def _labels_from_json(raw: str) -> list[str]:
    """Parse a stored labels JSON string. Malformed values fall back to
    an empty list -- the migration path may leave a legacy row with
    unexpected shape and pre-1.0 pixie tolerates it rather than 500'ing
    the machines page."""
    import json as _json

    if not raw:
        return []
    try:
        parsed = _json.loads(raw)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x) for x in parsed if isinstance(x, str)]


def _row(r: sqlite3.Row) -> Machine:
    import json as _json

    inv: dict[str, Any] = {}
    inv_at = ""
    labels: list[str] = []
    target_serial = ""
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
    with contextlib.suppress(IndexError, KeyError):
        labels = _labels_from_json(r["labels"] or "")
    with contextlib.suppress(IndexError, KeyError):
        target_serial = r["target_disk_serial"] or ""
    return Machine(
        mac=r["mac"],
        boot_mode=r["boot_mode"],
        image_content_sha256=r["image_content_sha256"],
        labels=labels,
        target_disk_serial=target_serial,
        inventory=inv,
        inventory_at=inv_at,
        discovered_at=r["discovered_at"],
        last_seen_at=r["last_seen_at"],
        last_seen_ip=r["last_seen_ip"],
        updated_at=r["updated_at"],
    )
