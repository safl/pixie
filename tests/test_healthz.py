"""``/healthz`` is open (container healthcheck reads it) and reports
the running version. Session-auth must NOT gate it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import pixie


def test_healthz_is_open_and_reports_version(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "pixie"
    assert body["version"] == pixie.__version__


def test_root_redirects_to_ui(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/"
