"""Shared pytest fixtures.

A per-test FastAPI TestClient so session cookies don't leak between
tests: each test gets a freshly constructed app + client + isolated
password env.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

# Constant admin password for tests. Set BEFORE importing the app so
# the env-var read at ``check_password`` time picks it up.
TEST_ADMIN_PASSWORD = "test-pw"


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
