"""Session-cookie auth: login flips the session flag, logout clears it,
protected routes 401 without a session, and the browser UI redirects
unauthed viewers to /ui/login instead of showing a JSON 401.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import TEST_ADMIN_PASSWORD


def test_api_ping_401_without_session(client: TestClient) -> None:
    """The session-gated ping route rejects an unauthed caller."""
    r = client.get("/api/ping")
    assert r.status_code == 401
    assert r.json()["detail"] == "login required"


def test_ui_dashboard_redirects_unauthed_browser(client: TestClient) -> None:
    """The UI variant redirects to /ui/login instead of a JSON 401 so
    a fresh operator lands on the login form."""
    r = client.get("/ui/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_login_wrong_password_rerenders_with_error(client: TestClient) -> None:
    r = client.post(
        "/ui/login",
        data={"password": "not-the-password"},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert "Invalid password" in r.text


def test_login_correct_password_sets_session_cookie(client: TestClient) -> None:
    """``POST /ui/login`` with the correct password 303s to /ui/ and
    ships a ``pixie-token`` Set-Cookie."""
    r = client.post(
        "/ui/login",
        data={"password": TEST_ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/"
    # httpx captures Set-Cookie into the client's cookie jar; grep for
    # the exact cookie name we ship.
    assert "pixie-token" in client.cookies


def test_authed_api_ping_returns_pong(client: TestClient) -> None:
    """Once logged in, ``/api/ping`` returns 200. Proves session-cookie
    auth roundtrips end-to-end via the shared TestClient jar."""
    client.post("/ui/login", data={"password": TEST_ADMIN_PASSWORD})
    r = client.get("/api/ping")
    assert r.status_code == 200
    assert r.json()["pong"] is True


def test_authed_ui_dashboard_renders(client: TestClient) -> None:
    client.post("/ui/login", data={"password": TEST_ADMIN_PASSWORD})
    r = client.get("/ui/")
    assert r.status_code == 200
    assert "pixie" in r.text


def test_logout_clears_session(client: TestClient) -> None:
    """``POST /ui/logout`` invalidates the session; subsequent
    protected requests 401 (or redirect for UI)."""
    client.post("/ui/login", data={"password": TEST_ADMIN_PASSWORD})
    r = client.post("/ui/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"
    r2 = client.get("/api/ping")
    assert r2.status_code == 401
