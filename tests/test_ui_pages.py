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
