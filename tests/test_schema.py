"""Catalog schema: parse + serialize round-trip, plus the loose-parse
guard on the legacy ``netboot_ref`` field.
"""

from __future__ import annotations

import logging

import pytest

from pixie.catalog._schema import CatalogEntry, parse_catalog_toml, serialise_catalog

_NOSI_SHAPE = b"""
version = 1

[[images]]
name = "nosi ubuntu-2604-headless (x86_64, 2026.W28)"
src = "oras://ghcr.io/safl/nosi/ubuntu-2604-headless:2026.W28"
format = "img.gz"
arch = "x86_64"
description = "Ubuntu 26.04 LTS (resolute) headless."
netboot_src = "oras://ghcr.io/safl/nosi/ubuntu-2604-headless-netboot:2026.W28"

[[images]]
name = "nosi ubuntu-2604-headless netboot bundle (x86_64, 2026.W28)"
src = "oras://ghcr.io/safl/nosi/ubuntu-2604-headless-netboot:2026.W28"
format = "tar.gz"
arch = "x86_64"
"""


def test_parse_and_serialise_roundtrip_preserves_known_fields() -> None:
    entries = parse_catalog_toml(_NOSI_SHAPE)
    assert len(entries) == 2

    disk = entries[0]
    bundle = entries[1]
    assert disk.format == "img.gz"
    assert disk.is_bindable() is True
    assert disk.netboot_src.endswith(":2026.W28")
    assert bundle.format == "tar.gz"
    assert bundle.is_bindable() is False

    out = serialise_catalog(entries)
    assert b"netboot_src = " in out
    # Serialise emits only pixie's canonical shape (no netboot_ref
    # sneaks in via the extra dict).
    assert b"netboot_ref" not in out


def test_legacy_netboot_ref_is_ignored_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Older nosi tags shipped ``netboot_ref = <name-string>``. Pixie
    reads them, logs a warning, and drops the field on write."""
    caplog.set_level(logging.WARNING, logger="pixie.catalog._schema")
    toml = b"""
version = 1

[[images]]
name = "legacy"
src = "https://example.com/legacy.img.gz"
format = "img.gz"
netboot_ref = "legacy netboot bundle (x86_64, 2025.W01)"
"""
    entries = parse_catalog_toml(toml)
    assert len(entries) == 1
    assert entries[0].netboot_src == ""
    # Serialise doesn't round-trip the legacy field.
    out = serialise_catalog(entries)
    assert b"netboot_ref" not in out
    assert any("netboot_ref" in rec.message for rec in caplog.records)


def test_parse_drops_incomplete_rows(caplog: pytest.LogCaptureFixture) -> None:
    """Rows missing name/src/format are unusable; the parser drops
    them and warns rather than raising."""
    caplog.set_level(logging.WARNING, logger="pixie.catalog._schema")
    toml = b"""
version = 1

[[images]]
name = "ok"
src = "https://example.com/x.img.gz"
format = "img.gz"

[[images]]
name = "no-src"
format = "img.gz"

[[images]]
src = "https://example.com/y.img.gz"
format = "img.gz"
"""
    entries = parse_catalog_toml(toml)
    assert [e.name for e in entries] == ["ok"]
    assert sum("missing name/src/format" in r.message for r in caplog.records) == 2


def test_to_dict_shape() -> None:
    e = CatalogEntry(
        name="tiny",
        src="https://example.com/tiny.img.gz",
        format="img.gz",
        arch="x86_64",
        description="hello",
        netboot_src="https://example.com/tiny-netboot.tar.gz",
    )
    d = e.to_dict()
    assert d["name"] == "tiny"
    assert d["fetched"] is False
    assert d["bindable"] is True
    assert d["netboot_src"].endswith("tar.gz")
    # size_bytes / content_sha256 / fetched_at not surfaced pre-fetch.
    assert "size_bytes" not in d
    assert "content_sha256" not in d


def test_unknown_keys_are_round_tripped_via_extra() -> None:
    """Future nosi fields survive a pixie that doesn't know them."""
    toml = b"""
version = 1

[[images]]
name = "fut"
src = "https://example.com/fut.img.gz"
format = "img.gz"
future_field = "some-value"
another = "here"
"""
    entries = parse_catalog_toml(toml)
    assert entries[0].extra == {"future_field": "some-value", "another": "here"}
    out = serialise_catalog(entries).decode()
    assert 'future_field = "some-value"' in out
    assert 'another = "here"' in out
