"""URL-state helpers for paginated + filterable HTML tables.

Every table page pixie shows (Catalog, Machines, Events) reads a
freeform search string + a page number off the query string, filters
the row set, and slices it before rendering. State lives in the URL
so a page is bookmarkable and survives a plain form re-submit.

Three helpers cover the surface:

- :func:`parse_pagination` reads ``?page=<N>&per_page=<N>``, clamps
  ``per_page`` to the dropdown values, computes the offset + limit,
  and returns a :class:`PageState` the template uses to render the
  page-number footer + "Showing X-Y of Z" line.
- :func:`filter_rows` applies a case-insensitive substring match to
  a caller-supplied set of attribute paths. Missing attributes /
  ``None`` values just fall through so a row with a blank field is
  never matched by a non-empty ``q``.
- :func:`build_query_string` merges + strips empties + emits a
  URL-encoded query for header / pagination links so an active
  ``?q=`` survives the click through to page 2.

No FastAPI / Starlette dependency: tests pass a plain dict.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# Page-size choices surfaced as the per-page dropdown. The first
# entry is the default. Kept small so the table fits a laptop
# without a 200-row page forcing manual scrolling.
PER_PAGE_CHOICES: tuple[int, ...] = (10, 25, 50, 100)
DEFAULT_PER_PAGE = PER_PAGE_CHOICES[1]  # 25

# Number of page buttons shown around the current page in the
# footer. ``window=2`` gives ``... 3 4 [5] 6 7 ...`` (the standard
# Bootstrap pagination shape).
_NUMBERED_WINDOW = 2


@dataclass(frozen=True)
class PageState:
    """Parsed ``?page=<N>&per_page=<N>`` + computed totals."""

    page: int  # 1-indexed; clamped to [1, last_page]
    per_page: int  # one of PER_PAGE_CHOICES
    total: int  # rows matching the (post-filter) input
    offset: int  # index of the first row on this page
    limit: int  # equals per_page

    @property
    def last_page(self) -> int:
        # An empty table still has page 1 so ``page X of 1`` reads
        # sensibly rather than ``X of 0``.
        if self.total <= 0:
            return 1
        return (self.total + self.per_page - 1) // self.per_page

    @property
    def first_row(self) -> int:
        """1-indexed row number of the first row on this page (0 if
        the table is empty)."""
        if self.total <= 0:
            return 0
        return self.offset + 1

    @property
    def last_row(self) -> int:
        """1-indexed row number of the last row on this page."""
        if self.total <= 0:
            return 0
        return min(self.offset + self.per_page, self.total)

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.last_page

    def numbered_pages(self) -> list[int]:
        """The page-number buttons to render in the footer.

        Up to ``2 * _NUMBERED_WINDOW + 1`` centred on the current
        page and clamped to ``[1, last_page]``. The template adds
        explicit Prev / Next / First / Last outside this window so
        very large tables don't grow a 50-button footer.
        """
        lo = max(1, self.page - _NUMBERED_WINDOW)
        hi = min(self.last_page, self.page + _NUMBERED_WINDOW)
        return list(range(lo, hi + 1))


def parse_pagination(
    params: Mapping[str, str],
    *,
    total: int,
    default_per_page: int = DEFAULT_PER_PAGE,
) -> PageState:
    """Parse ``?page=<N>&per_page=<N>``, clamp to sane values, return
    a :class:`PageState`. ``total`` is the post-filter row count."""
    raw_per = params.get("per_page") or ""
    try:
        per_candidate = int(raw_per)
    except ValueError:
        per_candidate = default_per_page
    per_page = per_candidate if per_candidate in PER_PAGE_CHOICES else default_per_page

    if total < 0:
        total = 0
    last_page = max(1, (total + per_page - 1) // per_page) if total > 0 else 1

    raw_page = params.get("page") or ""
    try:
        page_candidate = int(raw_page)
    except ValueError:
        page_candidate = 1
    page = max(1, min(last_page, page_candidate))

    offset = (page - 1) * per_page
    return PageState(page=page, per_page=per_page, total=total, offset=offset, limit=per_page)


def _resolve(row: Any, path: str) -> Any:
    """Best-effort attribute / mapping-key lookup along a dotted
    path. Returns ``None`` on any missing hop so callers can treat
    ``None`` uniformly.
    """
    cur: Any = row
    for part in path.split("."):
        if cur is None:
            return None
        cur = cur.get(part) if isinstance(cur, Mapping) else getattr(cur, part, None)
    return cur


def filter_rows(rows: Iterable[Any], q: str, *, fields: Iterable[str]) -> list[Any]:
    """Case-insensitive substring search across ``fields`` on each
    row. An empty ``q`` returns the input unchanged. ``fields`` is a
    tuple of dotted paths that :func:`_resolve` walks -- attributes
    on dataclasses, keys on dicts, mix-and-match.

    A row matches when the query appears (as a substring, lowercase)
    in ANY of the resolved field values. Numeric / non-string values
    are coerced with ``str()`` so a search on ``12345`` hits a
    ``size_bytes`` int.
    """
    q_clean = (q or "").strip().lower()
    if not q_clean:
        return list(rows)
    field_list = tuple(fields)
    kept: list[Any] = []
    for row in rows:
        for path in field_list:
            val = _resolve(row, path)
            if val is None:
                continue
            if q_clean in str(val).lower():
                kept.append(row)
                break
    return kept


def build_query_string(
    base: Mapping[str, str | None],
    overrides: Mapping[str, str | None] | None = None,
) -> str:
    """Merge ``base`` + ``overrides``, drop empty / None values, and
    return a URL-encoded query string suitable for pagination links.
    Caller wraps with ``"?"`` when the returned string is non-empty.

    ``None`` in ``overrides`` REMOVES the key (handy for "clear this
    filter" links). Empty-string values are dropped so links don't
    accumulate ``?q=&page=`` noise. Stable key order so two callers
    producing the same URL emit byte-identical strings.
    """
    merged: dict[str, str] = {}
    for k, v in base.items():
        if v:
            merged[k] = str(v)
    if overrides:
        for k, v in overrides.items():
            if v is None or v == "":
                merged.pop(k, None)
            else:
                merged[k] = str(v)
    return urllib.parse.urlencode(sorted(merged.items()))
