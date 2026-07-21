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
