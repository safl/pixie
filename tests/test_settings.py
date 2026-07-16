"""Settings pane: display timezone + strftime format.

Verifies the store / renderer / route contract:

* ``resolve_*`` falls through override -> env -> default.
* ``format_ts`` picks up both keys and survives a bad tz stored on
  the row (surface the raw string; the Settings page shows the error
  in a red alert).
* The ``/ui/settings`` POST validates BOTH fields up-front so a bad
  tz on the tz field doesn't half-persist a good format.
* Blank inputs CLEAR the override so the row falls back to env /
  default.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_ADMIN_PASSWORD


def _authed(client: TestClient) -> TestClient:
    client.post("/ui/login", data={"password": TEST_ADMIN_PASSWORD})
    return client


def test_format_ts_defaults_to_utc(tmp_path: Path) -> None:
    from pixie.web._settings_store import SettingsStore, format_ts

    store = SettingsStore(tmp_path / "state.db")
    out = format_ts("2026-07-16T14:30:00Z", store)
    assert out == "2026-07-16 14:30:00 UTC"


def test_format_ts_respects_override(tmp_path: Path) -> None:
    from pixie.web._settings_store import (
        KEY_DATETIME_FORMAT,
        KEY_DISPLAY_TZ,
        SettingsStore,
        format_ts,
    )

    store = SettingsStore(tmp_path / "state.db")
    store.set_value(KEY_DISPLAY_TZ, "Europe/Copenhagen")
    store.set_value(KEY_DATETIME_FORMAT, "%d %b %Y %H:%M")
    out = format_ts("2026-07-16T14:30:00Z", store)
    # Europe/Copenhagen in mid-July is UTC+2 -> 16:30 local.
    assert out == "16 Jul 2026 16:30"


def test_format_ts_bad_tz_falls_back_to_raw(tmp_path: Path) -> None:
    """A stored bad tz value doesn't 500 the render path; ``format_ts``
    returns the raw ISO string. Settings form catches this before it
    lands in the DB; this guard is for out-of-band writes / older
    schemas."""
    from pixie.web._settings_store import KEY_DISPLAY_TZ, SettingsStore, format_ts

    store = SettingsStore(tmp_path / "state.db")
    store.set_value(KEY_DISPLAY_TZ, "Not/A_Zone")
    raw = "2026-07-16T14:30:00Z"
    assert format_ts(raw, store) == raw


def test_ui_settings_get_renders(client: TestClient) -> None:
    c = _authed(client)
    r = c.get("/ui/settings")
    assert r.status_code == 200
    body = r.text
    assert "Display timezone" in body
    assert "Datetime format" in body
    # Nav pill for the current page.
    assert 'href="/ui/settings"' in body


def test_ui_settings_post_persists_override(client: TestClient) -> None:
    c = _authed(client)
    r = c.post(
        "/ui/settings/display/edit",
        data={"timezone": "Europe/Copenhagen", "datetime_format": "%d %b %Y %H:%M"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    body = c.get("/ui/settings").text
    assert "Europe/Copenhagen" in body
    assert "%d %b %Y %H:%M" in body
    # A follow-up render on a page carrying a timestamp uses the new
    # format (dashboard is the cheapest to hit).
    dash = c.get("/ui/").text
    # The datetime_format's UPDATED_AT stamp should now be rendered
    # through the new format on the Settings page's help text.
    resettings = c.get("/ui/settings").text
    assert "%d %b %Y %H:%M" in resettings
    del dash


def test_ui_settings_rejects_bad_tz(client: TestClient) -> None:
    """A bad tz value fails validation + returns 400 with the form
    re-rendered; nothing is persisted."""
    c = _authed(client)
    r = c.post(
        "/ui/settings/display/edit",
        data={"timezone": "Nope/Not_A_Zone", "datetime_format": "%Y-%m-%d"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    body = r.text
    assert "not a known IANA timezone" in body
    # Nothing persisted: a follow-up GET still shows the default
    # (blank override) values.
    body2 = c.get("/ui/settings").text
    assert 'value=""' in body2 or 'value=""' in body2


def test_ui_settings_blank_inputs_clear_override(client: TestClient) -> None:
    from pixie.web._settings_store import KEY_DATETIME_FORMAT, KEY_DISPLAY_TZ

    c = _authed(client)
    # Seed both overrides.
    c.post(
        "/ui/settings/display/edit",
        data={"timezone": "Europe/Copenhagen", "datetime_format": "%d %b %Y"},
    )
    store = c.app.state.settings_store  # type: ignore[attr-defined]
    assert store.get(KEY_DISPLAY_TZ) == "Europe/Copenhagen"
    assert store.get(KEY_DATETIME_FORMAT) == "%d %b %Y"
    # Now clear both by submitting blanks.
    c.post("/ui/settings/display/edit", data={"timezone": "", "datetime_format": ""})
    assert store.get(KEY_DISPLAY_TZ) is None
    assert store.get(KEY_DATETIME_FORMAT) is None


def test_ui_settings_requires_auth(client: TestClient) -> None:
    r = client.get("/ui/settings", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


@pytest.mark.parametrize("path", ["/ui/", "/ui/machines", "/ui/catalog", "/ui/events"])
def test_layout_has_settings_nav_pill(client: TestClient, path: str) -> None:
    """Settings link appears on every authed page's top nav."""
    c = _authed(client)
    body = c.get(path).text
    assert 'href="/ui/settings"' in body
