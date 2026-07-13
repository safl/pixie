"""Stdlib-only helpers shared across pixie modules.

Keep this module import-cheap (no fastapi / jinja pulls) so the CLI +
fetch worker can import it before the web layer boots.
"""

from __future__ import annotations

from datetime import UTC, datetime

CHUNK = 64 * 1024  # 64 KiB streaming block for download + tar extract


def now_iso() -> str:
    """UTC ISO-8601 timestamp with second precision + trailing Z. Written
    directly into catalog + event rows without further formatting."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def human_size(n: int) -> str:
    """Bytes to short human string. 1024-based (KiB/MiB/GiB/TiB)."""
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if f < 1024 or unit == "TiB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TiB"


def parse_size(s: str | int) -> int:
    """Parse '0', '1024', '50M', '20G', '1.5T' into bytes (1024-based)."""
    s = str(s).strip()
    if not s:
        return 0
    units = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    if s[-1].upper() in units:
        return int(float(s[:-1]) * units[s[-1].upper()])
    return int(s)
