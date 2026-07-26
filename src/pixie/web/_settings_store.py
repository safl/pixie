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
import re
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
# normalisation. Default is a plain ISO-ish ``YYYY-MM-DD HH:MM:SS`` in
# 24-hour time, no timezone suffix (the tz is an operator setting, not
# something every row needs to repeat). The form accepts any
# strftime-parseable string; validation runs the pattern through
# :func:`datetime.strftime` at set time so a bogus ``%Q`` doesn't wait
# until the next page-render to blow up.
KEY_DATETIME_FORMAT = "display.datetime_format"
ENV_DATETIME_FORMAT = "PIXIE_DATETIME_FORMAT"
DEFAULT_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Extra tokens appended verbatim to the pixie-live-env kernel
# cmdline (see pixie.pxe._routes._render_context + the
# pixie-live-env.j2 template). Empty by default; the docstring for
# the resolver + docs/src/hardware-quirks.md carry the known-good
# values for boards we've hit in the field. The form accepts any
# whitespace-separated tokens; newlines are rejected at set time
# because they'd break the single-line iPXE ``kernel`` directive.
KEY_LIVE_ENV_EXTRA_CMDLINE = "live_env.extra_cmdline"
ENV_LIVE_ENV_EXTRA_CMDLINE = "PIXIE_LIVE_ENV_EXTRA_CMDLINE"

# A value that is nothing but an unexpanded shell placeholder --
# ``${VAR}`` / ``${VAR:-default}`` / ``${VAR:+x}`` -- which is what a
# compose runner that doesn't expand ``${VAR:-}`` (podman-compose)
# leaks into the container env. Such a value is treated as "unset" so
# it never reaches a kernel cmdline.
_UNEXPANDED_ENV_PLACEHOLDER = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(:[-+?=]?[^}]*)?\}$")

# Disk-image content sha the live-env boot modes
# (pixie-inventory / -tui / -flash-once / -flash-always) boot over
# EPHEMERAL nbdboot. When set (and the named image + its netboot bundle
# are fetched) the renderer streams this image over NBD -- proven on
# hardware to bring the NIC up via dracut where the retired squashfs
# live-boot hung in the initramfs on some boards. Empty (the default)
# means those modes degrade to the unavailable plan. Resolution: DB
# override -> env -> empty. A value that is not 64 lowercase hex chars
# is treated as unset so a fat-fingered setting degrades to unavailable
# cleanly.
KEY_LIVE_ENV_IMAGE_SHA = "live_env.image_sha"
ENV_LIVE_ENV_IMAGE_SHA = "PIXIE_LIVE_ENV_IMAGE_SHA"

# 64 lowercase hex chars: a catalog entry's ``content_sha256``.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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

    def resolve_live_env_extra_cmdline(self) -> str:
        """Effective live-env cmdline tail: override -> env -> empty.

        Known-good values by hardware (docs/src/hardware-quirks.md carries
        the canonical list):

        - GIGABYTE MC12-LE0 (Ryzen server board, BIOS F06+):
          ``pci=realloc=on,nocrs`` -- BIOS ROM BAR overlap defect
          leaves no MMIO space for the Intel i210 NICs; ``nocrs``
          tells Linux to ignore ACPI's PCI root windows + realloc
          rebalances every BAR into the (now larger) usable window.
          Without the workaround the live env boots with no network,
          live-boot never fetches the squashfs, and boot hangs
          silently.

        Empty when both DB override + env are unset -- the template
        expects a string and skips the append when it's empty.

        Defends against a compose runner that fails to expand
        ``${VAR:-}``: podman-compose passes the unset default through
        LITERALLY, so the container env holds the string
        ``${PIXIE_LIVE_ENV_EXTRA_CMDLINE:-}``. A value that is nothing
        but an unexpanded ``${...}`` placeholder is treated as empty so
        it can't leak onto every live-env / nbdboot kernel cmdline."""
        override = self.get(KEY_LIVE_ENV_EXTRA_CMDLINE)
        if override:
            return override
        env = (os.environ.get(ENV_LIVE_ENV_EXTRA_CMDLINE) or "").strip()
        if env and not _UNEXPANDED_ENV_PLACEHOLDER.match(env):
            return env
        return ""

    def resolve_live_env_image_sha(self) -> str:
        """Effective live-env disk-image content sha: DB override -> env
        -> empty.

        When non-empty (and the named image + its netboot bundle are
        fetched) the live-env boot modes nbdboot this image ephemerally
        instead of fetching the Debian live-boot squashfs. Empty is the
        default and keeps the squashfs path, so an existing deploy that
        never sets this is untouched.

        A value that is not 64 lowercase hex chars is treated as unset:
        the renderer then falls back to the squashfs path rather than
        degrading every live-env boot to the unavailable plan. The
        Settings / Live env form validates the shape at set time, so a
        bad value can only reach here from a stray env-var."""
        override = self.get(KEY_LIVE_ENV_IMAGE_SHA)
        if override and _SHA256_RE.match(override.strip()):
            return override.strip()
        env = (os.environ.get(ENV_LIVE_ENV_IMAGE_SHA) or "").strip()
        if env and _SHA256_RE.match(env):
            return env
        return ""


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
