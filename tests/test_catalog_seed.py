"""Curated bundled catalog + first-start seeding.

The bundled ``catalog.toml`` is a curated subset of the nosi catalog
(the nbdboot-tested distros + a few flash-only images) plus pixie's own
live-env image; the app seeds it into a fresh (empty) catalog once,
gated on ``PIXIE_SEED_CATALOG`` and a one-shot settings marker.
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


def test_bundled_catalog_netboot_refs_resolve() -> None:
    entries = parse_catalog_toml(bundled_catalog_bytes())
    images = [e for e in entries if e.is_bindable()]
    bundles = [e for e in entries if not e.is_bindable()]
    assert images and bundles
    bundle_srcs = {b.src for b in bundles}
    # An image MAY be flash-only (no netboot bundle -- e.g. freebsd), but
    # any image that names a bundle must resolve to one in the catalog.
    for img in images:
        if img.netboot_src:
            assert img.netboot_src in bundle_srcs, f"{img.name} netboot_src is dangling"
    # No orphan bundle: every bundle is referenced by at least one image.
    # Bundles may be shared (arch-headless-netboot is used by both
    # ``nosi arch-headless`` and ``pixie-live-env``).
    referenced = {img.netboot_src for img in images if img.netboot_src}
    for b in bundles:
        assert b.src in referenced, f"orphan bundle {b.name}"


def test_bundled_catalog_matches_the_supported_images() -> None:
    entries = parse_catalog_toml(bundled_catalog_bytes())
    images = {e.name for e in entries if e.is_bindable()}
    assert images == {
        "nosi debian-13-headless",
        "nosi ubuntu-2404-headless",
        "nosi ubuntu-2604-headless",
        "nosi fedora-44-headless",
        # nbdboot-capable via the shared arch-headless netboot bundle.
        "nosi arch-headless",
        # flash-only (no netboot bundle published).
        "nosi freebsd-14-headless",
        "nosi freebsd-15-headless",
        # pixie's own live-env image (arch-headless + injected CLI/service).
        "pixie-live-env",
    }


def test_default_catalog_url_points_at_pixie_not_nosi() -> None:
    assert "safl/pixie/releases" in DEFAULT_CATALOG_URL
    assert "nosi" not in DEFAULT_CATALOG_URL


# ---------- seeding behaviour ----------------------------------------


def test_seed_populates_empty_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    app = _build_app(monkeypatch, tmp_path, seed="1")
    entries = app.state.catalog_store.list_entries()
    assert len(entries) == 13
    assert app.state.settings_store.get(CATALOG_SEEDED_KEY) == "1"


def test_seed_disabled_leaves_catalog_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _build_app(monkeypatch, tmp_path, seed="0")
    assert app.state.catalog_store.list_entries() == []
    assert app.state.settings_store.get(CATALOG_SEEDED_KEY) in (None, "")


def test_seed_is_one_shot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    app1 = _build_app(monkeypatch, tmp_path, seed="1")
    assert len(app1.state.catalog_store.list_entries()) == 13
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


def test_catalog_page_has_fetch_latest_nosi_button(client: TestClient) -> None:
    """The Catalog page offers a one-click 'Fetch latest catalog' that
    imports the full upstream nosi catalog (separate from the pixie
    default in the URL bar)."""
    from pixie.catalog import NOSI_CATALOG_URL

    c = authed(client)
    body = c.get("/ui/catalog").text
    assert "Fetch latest catalog" in body
    assert NOSI_CATALOG_URL in body
    assert "safl/nosi" in NOSI_CATALOG_URL
