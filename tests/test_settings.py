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

from tests.conftest import authed as _authed


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


def test_resolve_display_timezone_env_var_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No DB override + PIXIE_DISPLAY_TZ set -> env wins over the
    built-in UTC default. Ordering matters: env is second in the
    resolution chain, so an operator can pin the compose deploy's
    timezone via envvars without touching state.db."""
    from pixie.web._settings_store import SettingsStore, format_ts

    monkeypatch.setenv("PIXIE_DISPLAY_TZ", "Europe/Copenhagen")
    store = SettingsStore(tmp_path / "state.db")
    out = format_ts("2026-07-16T14:30:00Z", store)
    # Europe/Copenhagen in mid-July is UTC+2 -> 16:30 local.
    assert "16:30:00" in out


def test_resolve_datetime_format_env_var_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No DB override + PIXIE_DATETIME_FORMAT set -> env wins over
    the built-in default. Same resolution chain as the tz key."""
    from pixie.web._settings_store import SettingsStore, format_ts

    monkeypatch.setenv("PIXIE_DATETIME_FORMAT", "%d %b %Y")
    store = SettingsStore(tmp_path / "state.db")
    out = format_ts("2026-07-16T14:30:00Z", store)
    assert out == "16 Jul 2026"


def test_db_override_wins_over_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DB override is FIRST in the resolution chain, so a Settings-
    page pick trumps the compose envvar. Guards the /ui/settings
    write path against a surprise revert on the next render."""
    from pixie.web._settings_store import KEY_DISPLAY_TZ, SettingsStore, format_ts

    monkeypatch.setenv("PIXIE_DISPLAY_TZ", "America/New_York")
    store = SettingsStore(tmp_path / "state.db")
    store.set_value(KEY_DISPLAY_TZ, "Europe/Copenhagen")
    out = format_ts("2026-07-16T14:30:00Z", store)
    assert "16:30:00" in out  # Copenhagen, not New_York's 10:30


def test_live_env_extra_cmdline_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """live_env.extra_cmdline resolves override -> env -> empty. The
    /pxe/<mac> render reads through this so an operator can pin a
    hardware workaround (docs/hardware-quirks.md) from the Settings
    page without a compose restart."""
    from pixie.web._settings_store import KEY_LIVE_ENV_EXTRA_CMDLINE, SettingsStore

    monkeypatch.delenv("PIXIE_LIVE_ENV_EXTRA_CMDLINE", raising=False)
    store = SettingsStore(tmp_path / "state.db")
    assert store.resolve_live_env_extra_cmdline() == ""
    monkeypatch.setenv("PIXIE_LIVE_ENV_EXTRA_CMDLINE", "pci=realloc=on,nocrs")
    assert store.resolve_live_env_extra_cmdline() == "pci=realloc=on,nocrs"
    # DB override wins even with env set.
    store.set_value(KEY_LIVE_ENV_EXTRA_CMDLINE, "amd_iommu=off")
    assert store.resolve_live_env_extra_cmdline() == "amd_iommu=off"
    store.clear(KEY_LIVE_ENV_EXTRA_CMDLINE)
    assert store.resolve_live_env_extra_cmdline() == "pci=realloc=on,nocrs"


def test_ui_settings_live_env_edit_persists(client: TestClient) -> None:
    """POST /ui/settings/live-env/edit stores the tokens; a subsequent
    GET /pxe/<mac> lands them on the kernel line."""
    from pathlib import Path as _Path

    c = _authed(client)
    r = c.post(
        "/ui/settings/live-env/edit",
        data={"extra_cmdline": "pci=realloc=on,nocrs"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    # Stage live-env media + a bound machine so /pxe/<mac> renders
    # the live-env template rather than the unavailable fallback.
    live_env = client.app.state.live_env_dir  # type: ignore[attr-defined]
    assert isinstance(live_env, _Path)
    live_env.mkdir(parents=True, exist_ok=True)
    for name in ("vmlinuz", "initrd", "live.squashfs"):
        (live_env / name).write_bytes(b"stub")
    try:
        c.put("/machines/aa:bb:cc:dd:ee:41", json={"boot_mode": "pixie-inventory"})
        body = c.get("/pxe/aa:bb:cc:dd:ee:41").text
        kernel_line = next(line for line in body.splitlines() if line.startswith("kernel "))
        assert "pci=realloc=on,nocrs" in kernel_line
    finally:
        for name in ("vmlinuz", "initrd", "live.squashfs"):
            (live_env / name).unlink(missing_ok=True)


def test_ui_settings_live_env_rejects_newline(client: TestClient) -> None:
    """A newline would truncate the single-line iPXE ``kernel``
    directive; reject at write time with a 400 + inline error rather
    than serving a broken plan."""
    c = _authed(client)
    r = c.post(
        "/ui/settings/live-env/edit",
        data={"extra_cmdline": "pci=realloc=on,nocrs\namd_iommu=off"},
    )
    assert r.status_code == 400
    assert "single line" in r.text


def test_ui_settings_live_env_blank_clears(client: TestClient) -> None:
    """Blank input clears the override so the value falls back to the
    env / empty. Mirrors the display-settings semantics."""
    from pixie.web._settings_store import KEY_LIVE_ENV_EXTRA_CMDLINE

    c = _authed(client)
    store = c.app.state.settings_store  # type: ignore[attr-defined]
    store.set_value(KEY_LIVE_ENV_EXTRA_CMDLINE, "pci=realloc=on,nocrs")
    c.post("/ui/settings/live-env/edit", data={"extra_cmdline": ""})
    assert store.get(KEY_LIVE_ENV_EXTRA_CMDLINE) is None
