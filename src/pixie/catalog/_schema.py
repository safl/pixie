"""Catalog entry schema + TOML parse/serialize.

A catalog entry captures what the operator staged and what pixie
learned about it after Fetch. Two entry shapes share the schema:

**Disk-image entry** (``format`` in ``BINDABLE_FORMATS``): flashable
onto a target disk and mountable over NBD for ramboot. May carry a
``netboot_src`` URL pointing at a sibling netboot-bundle entry.

**Netboot-bundle entry** (``format = "tar.gz"``): a build-time
extract of vmlinuz + initrd + manifest.json from a disk image, so
image-native ramboot serves the image's own kernel. Fetched entries
of this shape unpack into
``<state_dir>/artifacts/<content_sha256>/``.

Cross-references between the two shapes are by URL (``netboot_src``),
NOT by name. The name-based ``netboot_ref`` field that older nosi
tags shipped is accepted on read with a warning + dropped on write.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from typing import Any

from pixie._util import now_iso

_log = logging.getLogger(__name__)

# Formats that pixie can flash to a target disk (via the flash engine
# in a later PR) and serve over NBD for ramboot. Netboot bundles carry
# ``tar.gz`` and land in a different serving path (artifacts, not
# blobs).
BINDABLE_FORMATS: frozenset[str] = frozenset(
    {"img", "img.gz", "img.zst", "img.xz", "img.bz2", "qcow2"}
)

# Known scalar-string fields on a catalog entry. Anything else in the
# TOML gets dropped on write (silently); on read it's kept in
# ``extra`` so a future pixie can round-trip fields it doesn't yet
# know about.
_KNOWN_STR_KEYS: tuple[str, ...] = (
    "name",
    "src",
    "resolved_src",
    "format",
    "arch",
    "description",
    "netboot_src",
    "content_sha256",
    "fetched_at",
    "added_at",
)


@dataclass
class CatalogEntry:
    """One row in the operator's image library.

    ``name`` is the natural key + display label. ``src`` is the fetch
    URL (``oras://`` or ``https://``). ``format`` decides which post-
    fetch pipeline runs. ``netboot_src``, when set on a disk-image
    entry, is the URL of a sibling netboot-bundle entry -- pixie
    resolves it by URL match against the catalog's ``src`` field, no
    name-string comparison.

    Fields written by the operator at add time: ``name``, ``src``,
    ``format``, ``arch``, ``description``, ``netboot_src``. Fields
    populated by the fetch pipeline: ``content_sha256``,
    ``size_bytes``, ``fetched_at``. ``added_at`` is set at add time.
    """

    name: str
    src: str
    format: str
    arch: str = ""
    description: str = ""
    netboot_src: str = ""
    content_sha256: str = ""
    size_bytes: int = 0
    fetched_at: str = ""
    added_at: str = field(default_factory=now_iso)
    # Unknown TOML keys we round-trip so a future pixie schema addition
    # (or an operator-hand-edited catalog with fields we don't parse)
    # survives.
    extra: dict[str, Any] = field(default_factory=dict)

    def is_fetched(self) -> bool:
        return bool(self.content_sha256)

    def is_bindable(self) -> bool:
        """True iff the format is a disk image pixie can flash or
        serve over NBD. Netboot bundles return False."""
        return self.format in BINDABLE_FORMATS

    @property
    def bindable(self) -> bool:
        """Attribute-style alias of :meth:`is_bindable` so Jinja
        templates + ``getattr(entry, 'bindable', False)`` filters
        work without the ``()`` call. Same value; do not diverge."""
        return self.is_bindable()

    @property
    def fetched(self) -> bool:
        """Attribute-style alias of :meth:`is_fetched` for the same
        Jinja / getattr convenience as :attr:`bindable`."""
        return self.is_fetched()

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view. Skips zero-y fields so the JSON
        stays compact + easy to grep."""
        out: dict[str, Any] = {
            "name": self.name,
            "src": self.src,
            "format": self.format,
            "added_at": self.added_at,
        }
        for key in ("arch", "description", "netboot_src", "content_sha256", "fetched_at"):
            val = getattr(self, key)
            if val:
                out[key] = val
        if self.size_bytes:
            out["size_bytes"] = self.size_bytes
        out["fetched"] = self.is_fetched()
        out["bindable"] = self.is_bindable()
        if self.extra:
            out["extra"] = self.extra
        return out


def parse_catalog_toml(raw: bytes | str) -> list[CatalogEntry]:
    """Parse a nosi-shaped ``catalog.toml`` into ``CatalogEntry``
    objects. Skips rows that lack the required (``name``, ``src``,
    ``format``) triad.

    Backward-compat: an entry with ``netboot_ref = "<name-string>"``
    (older nosi tags) logs a warning and is dropped on the field --
    pixie does not resolve name-based cross-references. The rest of
    the row is kept; the entry just won't advertise a netboot
    sibling until nosi ships the ``netboot_src`` variant.
    """
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    doc = tomllib.loads(text)

    version = doc.get("version")
    if version not in (None, 1):
        _log.warning("catalog.toml: unknown version=%r; parsing anyway", version)

    entries: list[CatalogEntry] = []
    for row in doc.get("images", []):
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        src = str(row.get("src") or "").strip()
        fmt = str(row.get("format") or "").strip()
        if not (name and src and fmt):
            _log.warning("catalog.toml: dropping entry missing name/src/format: %r", row)
            continue

        # Loose-parse legacy netboot_ref: warn + drop.
        if row.get("netboot_ref"):
            _log.warning(
                "catalog.toml entry %r carries legacy 'netboot_ref' (name-string); "
                "pixie ignores it. Publish 'netboot_src' (URL) to advertise the "
                "sibling bundle.",
                name,
            )

        size_bytes = row.get("size_bytes") or 0
        try:
            size_int = int(size_bytes)
        except (TypeError, ValueError):
            size_int = 0

        entry = CatalogEntry(
            name=name,
            src=src,
            format=fmt,
            arch=str(row.get("arch") or ""),
            description=str(row.get("description") or ""),
            netboot_src=str(row.get("netboot_src") or ""),
            content_sha256=str(row.get("content_sha256") or row.get("sha256") or ""),
            size_bytes=size_int,
            fetched_at=str(row.get("fetched_at") or ""),
            added_at=str(row.get("added_at") or now_iso()),
        )
        # Stash anything we haven't consumed so serialize() can round-trip it.
        extra = {
            k: v
            for k, v in row.items()
            if k not in _KNOWN_STR_KEYS and k not in ("netboot_ref", "size_bytes", "sha256")
        }
        if extra:
            entry.extra = extra
        entries.append(entry)
    return entries


def serialise_catalog(entries: list[CatalogEntry]) -> bytes:
    """Serialise entries back to TOML matching the nosi shape.

    Hand-rolled emitter (no ``tomli_w`` dep): the schema is flat, and a
    conservative "known keys only" emitter beats surprising an operator
    with re-quoted strings from a general TOML writer.
    """
    lines: list[str] = ["version = 1", ""]
    for e in entries:
        lines.append("[[images]]")
        for key in (
            "name",
            "src",
            "format",
            "arch",
            "description",
            "netboot_src",
            "content_sha256",
            "fetched_at",
            "added_at",
        ):
            val = getattr(e, key, "") or ""
            if not val:
                continue
            escaped = str(val).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key} = "{escaped}"')
        if e.size_bytes:
            lines.append(f"size_bytes = {e.size_bytes}")
        for key, val in e.extra.items():
            # Only round-trip scalar strings/ints; anything else came
            # from an unusual source and we don't want to reformat it.
            if isinstance(val, str):
                escaped = val.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{key} = "{escaped}"')
            elif isinstance(val, int):
                lines.append(f"{key} = {val}")
        lines.append("")
    return "\n".join(lines).encode("utf-8")
