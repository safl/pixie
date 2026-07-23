"""Curated bundled catalog + first-start seeding.

The bundled ``catalog.toml`` is a strict subset of the nosi catalog
restricted to netboot-capable images; the app seeds it into a fresh
(empty) catalog once, gated on ``PIXIE_SEED_CATALOG`` and a one-shot
settings marker.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pixie.catalog import (
    CATALOG_SEEDED_KEY,
    DEFAULT_CATALOG_URL,
    bundled_catalog_bytes,
)
from pixie.catalog._schema import CatalogEntry, parse_catalog_toml
from pixie.catalog._store import CatalogStore
from tests.conftest import authed


def _build_app(monkeypatch: pytest.MonkeyPatch, data_dir: Path, *, seed: str):
    monkeypatch.setenv("PIXIE_SEED_CATALOG", seed)
    monkeypatch.setenv("PIXIE_ADMIN_PASSWORD", "test-pw")
    monkeypatch.setenv("PIXIE_DATA_DIR", str(data_dir))
    from pixie.web.main import create_app

    return create_app()


# ---------- the bundled catalog is a valid netboot-only subset -------


def test_bundled_catalog_is_netboot_only_subset() -> None:
    entries = parse_catalog_toml(bundled_catalog_bytes())
    images = [e for e in entries if e.is_bindable()]
    bundles = [e for e in entries if not e.is_bindable()]
    # Every disk image cross-references a bundle, and every bundle is
    # referenced -- i.e. no image without a netboot artifact, and no
    # orphan bundle.
    assert images and bundles
    bundle_srcs = {b.src for b in bundles}
    for img in images:
        assert img.netboot_src, f"{img.name} has no netboot_src"
        assert img.netboot_src in bundle_srcs, f"{img.name} netboot_src is dangling"
    assert len(images) == len(bundles)


def test_bundled_catalog_matches_the_four_supported_images() -> None:
    entries = parse_catalog_toml(bundled_catalog_bytes())
    images = {e.name for e in entries if e.is_bindable()}
    assert images == {
        "nosi debian-13-headless",
        "nosi ubuntu-2404-headless",
        "nosi ubuntu-2604-headless",
        "nosi fedora-44-headless",
    }


def test_default_catalog_url_points_at_pixie_not_nosi() -> None:
    assert "safl/pixie/releases" in DEFAULT_CATALOG_URL
    assert "nosi" not in DEFAULT_CATALOG_URL


# ---------- seeding behaviour ----------------------------------------


def test_seed_populates_empty_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    app = _build_app(monkeypatch, tmp_path, seed="1")
    entries = app.state.catalog_store.list_entries()
    assert len(entries) == 8
    assert app.state.settings_store.get(CATALOG_SEEDED_KEY) == "1"


def test_seed_disabled_leaves_catalog_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _build_app(monkeypatch, tmp_path, seed="0")
    assert app.state.catalog_store.list_entries() == []
    assert app.state.settings_store.get(CATALOG_SEEDED_KEY) in (None, "")


def test_seed_is_one_shot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    app1 = _build_app(monkeypatch, tmp_path, seed="1")
    assert len(app1.state.catalog_store.list_entries()) == 8
    # Operator curates the catalog down to nothing.
    for e in list(app1.state.catalog_store.list_entries()):
        app1.state.catalog_store.delete(e.name)
    assert app1.state.catalog_store.list_entries() == []
    # A restart (fresh app, same data dir) must NOT re-seed.
    app2 = _build_app(monkeypatch, tmp_path, seed="1")
    assert app2.state.catalog_store.list_entries() == []


def test_seed_does_not_pollute_existing_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Pre-populate a catalog (upgrade from a pre-seed pixie) with no marker.
    store = CatalogStore(tmp_path)
    store.upsert(CatalogEntry(name="my custom image", src="https://x/y.img.gz", format="img.gz"))
    app = _build_app(monkeypatch, tmp_path, seed="1")
    names = {e.name for e in app.state.catalog_store.list_entries()}
    assert names == {"my custom image"}  # curated set NOT added on top
    # marker set so the check is skipped next start
    assert app.state.settings_store.get(CATALOG_SEEDED_KEY) == "1"


# ---------- the import form defaults to pixie's catalog --------------


def test_import_form_prefills_pixie_default(client: TestClient) -> None:
    c = authed(client)
    body = c.get("/ui/catalog").text
    assert DEFAULT_CATALOG_URL in body
