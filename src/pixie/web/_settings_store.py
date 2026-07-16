"""Operator-overridable settings: a thin key-value store over the
``settings`` table in state.db.

Two knobs today, both about how pixie renders timestamps to the
operator: display timezone (``KEY_DISPLAY_TZ``) and strftime format
(``KEY_DATETIME_FORMAT``). Pixie stores timestamps in UTC ISO-8601
inside the DB and normalises on render, so a Settings change is a
pure display flip -- no data migration.

Resolution order for both keys is override (this table) -> env var
-> built-in default. The Settings form persists overrides so they
survive a container restart without a systemd-unit edit; an env
override in ``envvars`` still wins over a stored override so a
compose-file deploy pins behaviour deterministically.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# One shared table for every future key -> value. ``updated_at`` is
# free debugging telemetry; the Settings page shows it next to each
# overridden row.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Display timezone for every timestamp pixie-web renders. An IANA
# zone name (``UTC``, ``Europe/Copenhagen``, ``America/New_York``).
# Invalid values raise :class:`SettingValueError` at resolve time so
# the form rejects them BEFORE persisting -- a bad row in state.db
# would come from an out-of-band write and warrants a loud failure,
# not a silent UTC fallback.
KEY_DISPLAY_TZ = "display.timezone"
ENV_DISPLAY_TZ = "PIXIE_DISPLAY_TZ"
DEFAULT_DISPLAY_TZ = "UTC"

# strftime pattern applied to every timestamp after the timezone
# normalisation. Default matches bty's operator-facing "human ISO"
# shape. The form accepts any strftime-parseable string; validation
# runs the pattern through :func:`datetime.strftime` at set time so
# a bogus ``%Q`` doesn't wait until the next page-render to blow up.
KEY_DATETIME_FORMAT = "display.datetime_format"
ENV_DATETIME_FORMAT = "PIXIE_DATETIME_FORMAT"
DEFAULT_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S %Z"


class SettingValueError(ValueError):
    """A stored override failed validation at resolve time. The
    Settings page surfaces the message as an inline error instead of
    500'ing the request."""


class SettingsStore:
    """Thin sqlite3 wrapper over the ``settings`` table."""

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

    def get(self, key: str) -> str | None:
        """Return the stored override for ``key``, or ``None`` if
        unset. Does NOT fall through to env / default; use the
        ``resolve_*`` helpers for that."""
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_value(self, key: str, value: str) -> None:
        """Upsert ``key`` = ``value``."""
        now = datetime.now(UTC).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                               updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

    def clear(self, key: str) -> None:
        """Remove any override for ``key`` (revert to env / default)."""
        with self._conn() as conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    def updated_at(self, key: str) -> str | None:
        """When ``key``'s override was last written, or ``None``."""
        with self._conn() as conn:
            row = conn.execute("SELECT updated_at FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["updated_at"])

    def resolve_display_timezone(self) -> ZoneInfo:
        """Effective timezone: override -> ``$PIXIE_DISPLAY_TZ`` -> UTC."""
        raw = self.get(KEY_DISPLAY_TZ) or (os.environ.get(ENV_DISPLAY_TZ) or "").strip()
        if not raw:
            return ZoneInfo(DEFAULT_DISPLAY_TZ)
        try:
            return ZoneInfo(raw)
        except ZoneInfoNotFoundError as exc:
            raise SettingValueError(
                f"{KEY_DISPLAY_TZ}={raw!r} is not a known IANA timezone "
                f"(expected e.g. 'UTC', 'Europe/Copenhagen', 'America/New_York')"
            ) from exc

    def resolve_datetime_format(self) -> str:
        """Effective strftime pattern: override -> env -> default."""
        return (
            self.get(KEY_DATETIME_FORMAT)
            or (os.environ.get(ENV_DATETIME_FORMAT) or "").strip()
            or DEFAULT_DATETIME_FORMAT
        )


def parse_iso_utc(raw: str) -> datetime | None:
    """Parse the ISO-8601 strings pixie writes to state.db. Accepts a
    trailing ``Z`` or an explicit offset; a naive string is assumed to
    be UTC (matches how the stores stamp ``now_iso()``). Returns
    ``None`` on any parse failure so the render path can fall back to
    the raw string rather than 500."""
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def format_ts(raw: str, store: SettingsStore) -> str:
    """Render a stored ISO-8601 timestamp per the current settings.
    Falls back to the raw string on a parse failure (better an
    unformatted-but-visible timestamp than an empty cell) or on a
    :class:`SettingValueError` from the timezone resolver (the
    Settings page surfaces the error separately)."""
    dt = parse_iso_utc(raw)
    if dt is None:
        return raw
    try:
        tz = store.resolve_display_timezone()
    except SettingValueError:
        return raw
    fmt = store.resolve_datetime_format()
    try:
        return dt.astimezone(tz).strftime(fmt)
    except ValueError:
        return raw
