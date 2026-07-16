"""Operator UI pages: /ui/exports and /ui/machines shape.

Renders each page for both an empty catalog + a populated one and
asserts the operator sees what they should. No integration container
here; the routes are pure Jinja + store reads.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import TEST_ADMIN_PASSWORD


def _authed(client: TestClient) -> TestClient:
    client.post("/ui/login", data={"password": TEST_ADMIN_PASSWORD})
    return client


def test_dashboard_renders_nav_when_authed(client: TestClient) -> None:
    c = _authed(client)
    r = c.get("/ui/")
    assert r.status_code == 200
    body = r.text
    assert 'href="/ui/exports"' in body
    assert 'href="/ui/machines"' in body


def test_login_page_has_no_nav(client: TestClient) -> None:
    """Nav is gated on ``authed`` so a fresh viewer sees only the
    login form."""
    r = client.get("/ui/login")
    assert r.status_code == 200
    body = r.text
    assert 'href="/ui/exports"' not in body
    assert 'href="/ui/machines"' not in body


def test_ui_exports_empty(client: TestClient) -> None:
    c = _authed(client)
    r = c.get("/ui/exports")
    assert r.status_code == 200
    assert "No exports yet" in r.text


def test_ui_machines_empty(client: TestClient) -> None:
    c = _authed(client)
    r = c.get("/ui/machines")
    assert r.status_code == 200
    assert "No machines yet" in r.text


def test_ui_machines_bind_form_creates_row(client: TestClient) -> None:
    """Form POST binds a machine + returns the redirect + subsequent
    /ui/machines lists the row."""
    c = _authed(client)
    r = c.post(
        "/ui/machines/bind",
        data={"mac": "aa:bb:cc:dd:ee:00", "boot_mode": "ipxe-exit"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    body = c.get("/ui/machines").text
    assert "aa:bb:cc:dd:ee:00" in body


def test_ui_machines_bind_form_bad_mac_silently_redirects(client: TestClient) -> None:
    """A garbage MAC does NOT 500 the form -- it just returns to the
    machines page without creating a row. A field-error flash channel
    lands in a follow-up PR."""
    c = _authed(client)
    r = c.post(
        "/ui/machines/bind",
        data={"mac": "not-a-mac", "boot_mode": "ipxe-exit"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    body = c.get("/ui/machines").text
    assert "not-a-mac" not in body


def test_ui_exports_and_machines_require_auth(client: TestClient) -> None:
    for path in ("/ui/exports", "/ui/machines"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/ui/login"


def test_ui_machine_detail_unknown_mac_redirects(client: TestClient) -> None:
    """A detail request for a MAC pixie hasn't seen falls through to
    the list -- consistent with how catalog + exports pages handle
    missing entries."""
    c = _authed(client)
    r = c.get("/ui/machines/aa:bb:cc:dd:ee:ff", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/machines"


def test_ui_machine_detail_bad_mac_redirects(client: TestClient) -> None:
    c = _authed(client)
    r = c.get("/ui/machines/not-a-mac", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/machines"


def test_ui_machine_detail_renders_row_without_inventory(client: TestClient) -> None:
    """A bound-but-uninventoried machine shows telemetry + the
    'no inventory yet' hint."""
    c = _authed(client)
    c.post(
        "/ui/machines/bind",
        data={"mac": "aa:bb:cc:dd:ee:01", "boot_mode": "ipxe-exit"},
    )
    r = c.get("/ui/machines/aa:bb:cc:dd:ee:01")
    assert r.status_code == 200
    body = r.text
    assert "aa:bb:cc:dd:ee:01" in body
    assert "ipxe-exit" in body
    assert "No inventory has been posted" in body


def test_ui_machine_detail_renders_stored_inventory(client: TestClient) -> None:
    """A POSTed inventory shows up on the detail page: per-disk table
    row + the lshw <details> block."""
    c = _authed(client)
    payload = {
        "disks": [
            {
                "path": "/dev/nvme0n1",
                "size": "1T",
                "vendor": "Samsung",
                "model": "PM9A1",
                "serial": "S679NX0R123456",
                "tran": "nvme",
            }
        ],
        "lshw": {"class": "system", "product": "test-model"},
    }
    r = c.post("/pxe/aa:bb:cc:dd:ee:02/inventory", json=payload)
    assert r.status_code == 204
    body = c.get("/ui/machines/aa:bb:cc:dd:ee:02").text
    assert "/dev/nvme0n1" in body
    assert "Samsung" in body
    assert "PM9A1" in body
    # lshw <details>: the tojson filter should render the class/product
    # pair somewhere in the pre block.
    assert '"class"' in body
    assert "test-model" in body


def test_ui_machine_detail_bind_form_prefills_current_binding(client: TestClient) -> None:
    """The detail page carries an edit form pre-populated with the
    machine's current boot_mode + image_content_sha256."""
    c = _authed(client)
    sha = "a" * 64
    c.put(
        "/machines/aa:bb:cc:dd:ee:04",
        json={"boot_mode": "ramboot", "image_content_sha256": sha},
    )
    body = c.get("/ui/machines/aa:bb:cc:dd:ee:04").text
    # boot_mode select is pre-selected to ramboot
    assert 'value="ramboot" selected' in body
    # image sha input has the current value
    assert f'value="{sha}"' in body
    # form action posts to the same /ui/machines/bind route the list
    # page uses; the hidden MAC field is included so operators can't
    # accidentally bind a different one
    assert 'action="/ui/machines/bind"' in body
    assert 'name="mac" value="aa:bb:cc:dd:ee:04"' in body


def test_ui_machine_detail_lists_recent_events(client: TestClient) -> None:
    """Detail page shows a filtered event history for the machine
    (subject_kind=machine, subject_id=<mac>)."""
    c = _authed(client)
    r = c.put("/machines/aa:bb:cc:dd:ee:05", json={"boot_mode": "ipxe-exit"})
    assert r.status_code == 200
    body = c.get("/ui/machines/aa:bb:cc:dd:ee:05").text
    assert "Recent events" in body
    assert "machine.bound" in body


def test_ui_machines_list_shows_inventory_summary(client: TestClient) -> None:
    """Per-row summary column indicates disk count + whether lshw
    was included."""
    c = _authed(client)
    c.post(
        "/pxe/aa:bb:cc:dd:ee:03/inventory",
        json={"disks": [{"path": "/dev/sda"}, {"path": "/dev/sdb"}]},
    )
    body = c.get("/ui/machines").text
    assert "2 disks" in body


def test_ui_dashboard_shows_fetching_pill_when_fetch_state_is_fetching(
    client: TestClient,
) -> None:
    """After ``POST /ui/catalog/fetch`` (or an equivalent
    /catalog/entries/<name>/fetch) the entry's fetch_state is
    'fetching'; the dashboard should render a pill + a disabled
    'Fetching' button so operators don't spam the fetch verb."""
    c = _authed(client)
    c.post(
        "/catalog/entries",
        json={
            "name": "tiny",
            "src": "https://example.invalid/tiny.img.gz",
            "format": "img.gz",
        },
    )
    # Directly poke fetch_states so we don't have to wait for a real
    # HTTP fetch to reach the 'fetching' state.
    c.app.state.fetch_states["tiny"] = {  # type: ignore[attr-defined]
        "state": "fetching",
        "started_at": "2026-07-14T00:00:00Z",
        "error": None,
    }
    body = c.get("/ui/").text
    assert "fetching" in body
    assert "badge text-bg-primary" in body
    assert "disabled" in body


def test_ui_dashboard_shows_error_pill_with_retry_when_fetch_failed(
    client: TestClient,
) -> None:
    """A prior fetch that hit an error surfaces as a pill + the
    button flips to 'Retry' so the operator can try again."""
    c = _authed(client)
    c.post(
        "/catalog/entries",
        json={
            "name": "broken",
            "src": "https://example.invalid/broken.img.gz",
            "format": "img.gz",
        },
    )
    c.app.state.fetch_states["broken"] = {  # type: ignore[attr-defined]
        "state": "error",
        "started_at": "2026-07-14T00:00:00Z",
        "error": "download failed: connect timed out",
    }
    body = c.get("/ui/").text
    assert "badge text-bg-danger" in body
    assert "connect timed out" in body
    assert "Retry" in body


def test_ui_exports_delete_removes_missing_export_silently(client: TestClient) -> None:
    """A DELETE for an unknown export is 303 (not 500). The catalog
    tab does the same for missing entries; consistent shape."""
    c = _authed(client)
    r = c.post(
        "/ui/exports/delete",
        data={"name": "nope"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/exports"
