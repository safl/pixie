"""One-click live-env setup: POST /ui/live-env/setup fetches the
pixie-live-env image + its netboot bundle, then selects the image
(sets live_env.image_sha). Progress polls on /ui/live-env/setup-state.json.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_ADMIN_PASSWORD, authed


def _wait_setup(client: TestClient, want: str, timeout: float = 5.0) -> dict:
    """Poll the setup-state until it reaches ``want`` (or times out)."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = client.get("/ui/live-env/setup-state.json").json()
        if last.get("state") == want:
            return last
        time.sleep(0.05)
    return last


def test_setup_no_image_entry_errors(client: TestClient) -> None:
    """With no ``pixie-live-env`` entry in the catalog, the setup action
    records a readable error rather than running a doomed fetch."""
    c = authed(client)
    r = c.post("/ui/live-env/setup", follow_redirects=False)
    assert r.status_code == 303
    s = c.get("/ui/live-env/setup-state.json").json()
    assert s["state"] == "error"
    assert "pixie-live-env" in s["error"]
    # The page renders the not-ready state: the one-click button + the
    # error surfaced.
    page = c.get("/ui/live-env").text
    assert "Set up live env" in page
    assert "/ui/live-env/setup" in page


def test_setup_no_bundle_errors(client: TestClient) -> None:
    """A pixie-live-env image whose netboot_src does not resolve to a
    catalog entry errors out before fetching."""
    from pixie.catalog._schema import CatalogEntry

    c = authed(client)
    c.app.state.catalog_store.upsert(  # type: ignore[attr-defined]
        CatalogEntry(
            name="pixie-live-env",
            src="https://x/pixie-live-env.img.gz",
            format="img.gz",
            netboot_src="oras://x/no-such-bundle:latest",
        )
    )
    r = c.post("/ui/live-env/setup", follow_redirects=False)
    assert r.status_code == 303
    s = c.get("/ui/live-env/setup-state.json").json()
    assert s["state"] == "error"
    assert "netboot bundle" in s["error"]


def test_setup_fetches_and_selects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: setup fetches the bundle + image and sets
    live_env.image_sha to the fetched image's content sha. The fetcher
    is stubbed (mark the entry fetched + return a result) so the test
    exercises the orchestration, not a real download."""
    monkeypatch.setenv("PIXIE_ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)

    image_sha = "a" * 64
    bundle_sha = "b" * 64

    class _Result:
        def __init__(self, sha: str) -> None:
            self.content_sha256 = sha
            self.size_bytes = 10

    def _fake_fetch(entry, store, progress=None):
        sha = image_sha if getattr(entry, "format", "") == "img.gz" else bundle_sha
        store.mark_fetched(entry.name, content_sha256=sha, size_bytes=10)
        if progress is not None:
            progress({"phase": "downloading", "bytes_downloaded": 5, "total_bytes": 10})
        return _Result(sha)

    import pixie.catalog._fetcher as _fetcher

    monkeypatch.setattr(_fetcher, "fetch", _fake_fetch)

    from pixie.catalog._schema import CatalogEntry
    from pixie.web.main import create_app

    app = create_app()
    with TestClient(app) as client:
        c = authed(client)
        store = app.state.catalog_store
        bundle_src = "oras://x/pixie-live-env-bundle:latest"
        store.upsert(
            CatalogEntry(name="nosi arch-headless netboot bundle", src=bundle_src, format="tar.gz")
        )
        store.upsert(
            CatalogEntry(
                name="pixie-live-env",
                src="https://x/pixie-live-env.img.gz",
                format="img.gz",
                netboot_src=bundle_src,
            )
        )

        r = c.post("/ui/live-env/setup", follow_redirects=False)
        assert r.status_code == 303
        done = _wait_setup(c, "done")
        assert done["state"] == "done", done
        assert done["image_sha"] == image_sha
        # The image is now selected.
        assert app.state.settings_store.resolve_live_env_image_sha() == image_sha
        # And the page reports ready.
        assert "ready" in c.get("/ui/live-env").text
        # Regression: the live-env setup drives the SAME catalog
        # fetch_states the Catalog pane polls, so both fetched entries
        # carry a pill (previously the pane showed no status during a
        # live-env setup because it ran its own separate fetch path).
        fs = app.state.fetch_states
        assert fs["pixie-live-env"]["state"] == "done"
        assert fs["nosi arch-headless netboot bundle"]["state"] == "done"
