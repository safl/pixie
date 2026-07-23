"""Shared pytest fixtures.

A per-test FastAPI TestClient so session cookies don't leak between
tests: each test gets a freshly constructed app + client + isolated
password env.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

# Constant admin password for tests. Set BEFORE importing the app so
# the env-var read at ``check_password`` time picks it up.
TEST_ADMIN_PASSWORD = "test-pw"

# Force the fetch pipeline's curl transport to fail fast for tests.
# Real deployments override with the module defaults (10 retries at
# 5 s spacing); an in-process pytest run wants a deterministically-
# broken mock server to raise inside a second, not after 50 s of
# curl's retry backoff. Set at import time (module-level) so pytest
# collection + subprocess spawns both see it.
os.environ.setdefault("PIXIE_FETCH_RETRY", "0")
os.environ.setdefault("PIXIE_FETCH_RETRY_DELAY", "0")
os.environ.setdefault("PIXIE_FETCH_RETRY_MAX_TIME", "5")

# Keep the unit suite's catalog empty by default: the app seeds the
# bundled curated catalog on first start otherwise, which every
# empty-catalog / count assertion would trip on. The seed feature has
# its own test that flips this back on explicitly.
os.environ.setdefault("PIXIE_SEED_CATALOG", "0")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PIXIE_ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    from pixie.web.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


def authed(client: TestClient) -> TestClient:
    """POST /ui/login with the shared TEST_ADMIN_PASSWORD, return the
    same client so callers chain ``authed(client).get(...)``. Every
    test module was reimplementing this three-liner; consolidate."""
    client.post("/ui/login", data={"password": TEST_ADMIN_PASSWORD})
    return client
