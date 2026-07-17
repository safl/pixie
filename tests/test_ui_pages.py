"""Operator UI pages: /ui/exports and /ui/machines shape.

Renders each page for both an empty catalog + a populated one and
asserts the operator sees what they should. No integration container
here; the routes are pure Jinja + store reads.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import authed as _authed


def test_dashboard_renders_nav_when_authed(client: TestClient) -> None:
    c = _authed(client)
    r = c.get("/ui/")
    assert r.status_code == 200
    body = r.text
    assert 'href="/ui/catalog"' in body
    assert 'href="/ui/machines"' in body


def test_login_page_has_no_nav(client: TestClient) -> None:
    """Nav is gated on ``authed`` so a fresh viewer sees only the
    login form."""
    r = client.get("/ui/login")
    assert r.status_code == 200
    body = r.text
    assert 'href="/ui/catalog"' not in body
    assert 'href="/ui/machines"' not in body


def test_ui_exports_redirects_to_catalog(client: TestClient) -> None:
    """Exports merged into the Catalog view; the /ui/exports URL is
    kept as a 308 redirect so any operator bookmark still lands."""
    c = _authed(client)
    r = c.get("/ui/exports", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "/ui/catalog"


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


def test_ui_catalog_and_machines_require_auth(client: TestClient) -> None:
    for path in ("/ui/catalog", "/ui/machines"):
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
    # image sha select has an option with the current sha value
    # (may or may not be pre-selected depending on whether the sha
    # corresponds to a fetched catalog entry; the test just seeded
    # a machine binding with an arbitrary sha, no catalog entry).
    assert sha in body
    # form action posts to the same /ui/machines/bind route the list
    # page uses; the hidden MAC field is included so operators can't
    # accidentally bind a different one
    assert 'action="/ui/machines/bind"' in body
    assert 'name="mac" value="aa:bb:cc:dd:ee:04"' in body


def test_ui_machine_detail_image_picker_has_boot_mode_gate_markup(
    client: TestClient,
) -> None:
    """Image picker carries the JS-driven boot-mode gate: the wrapping
    div is tagged ``data-policy-relevant`` with the modes that consume
    an image, the select carries ``data-ramboot-gate``, and each option
    reflects the entry's fetched state via ``data-ramboot-ready``. The
    JS itself is browser-side; this guards the markup contract."""
    from pixie.catalog._schema import CatalogEntry

    c = _authed(client)
    catalog = c.app.state.catalog_store  # type: ignore[attr-defined]
    catalog.upsert(CatalogEntry(name="ready-img", src="https://x/ready.img.gz", format="img.gz"))
    catalog.mark_fetched("ready-img", content_sha256="a" * 64, size_bytes=42)
    catalog.upsert(CatalogEntry(name="staged-img", src="https://x/staged.img.gz", format="img.gz"))

    c.put("/machines/aa:bb:cc:dd:ee:06", json={"boot_mode": "ramboot"})
    body = c.get("/ui/machines/aa:bb:cc:dd:ee:06").text

    assert 'data-policy-relevant="pixie-flash-once pixie-flash-always ramboot"' in body
    assert 'data-ramboot-gate="1"' in body
    assert 'data-ramboot-ready="true"' in body
    assert 'data-ramboot-ready="false"' in body
    assert "-- not fetched" in body


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
    body = c.get("/ui/catalog").text
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
    body = c.get("/ui/catalog").text
    assert "badge text-bg-danger" in body
    assert "connect timed out" in body
    assert "Retry" in body


def test_ui_events_filter_by_kind(client: TestClient) -> None:
    """``?kind=<slug>`` narrows the log to rows whose kind matches; a
    ``kind`` value that isn't in the closed registry is ignored (i.e.
    behaves like no filter) so a stale bookmark doesn't render an
    empty page for no reason. The dropdown carries the KNOWN_EVENT_KINDS
    set as options."""
    from pixie.events._kinds import (
        AUTH_LOGIN_SUCCEEDED,
        CATALOG_ENTRY_ADDED,
        KNOWN_EVENT_KINDS,
    )

    c = _authed(client)
    log = c.app.state.events_log  # type: ignore[attr-defined]
    log.emit(CATALOG_ENTRY_ADDED, subject_kind="entry", subject_id="a", summary="added a")
    log.emit(CATALOG_ENTRY_ADDED, subject_kind="entry", subject_id="b", summary="added b")

    body_all = c.get("/ui/events").text
    assert "added a" in body_all and "added b" in body_all

    body_narrowed = c.get(f"/ui/events?kind={CATALOG_ENTRY_ADDED}").text
    assert "added a" in body_narrowed and "added b" in body_narrowed
    # login-success rows are not in the narrowed slice
    assert AUTH_LOGIN_SUCCEEDED not in body_narrowed.split("<tbody>")[1].split("</tbody>")[0]

    # Kind dropdown rendered with the full closed set.
    for k in list(KNOWN_EVENT_KINDS)[:3]:
        assert f'value="{k}"' in body_all

    # A bogus ?kind= is silently dropped: page still lists both rows.
    body_bogus = c.get("/ui/events?kind=not.a.real.kind").text
    assert "added a" in body_bogus and "added b" in body_bogus


def test_ui_events_filter_by_subject_kind(client: TestClient) -> None:
    from pixie.events._kinds import (
        AUTH_LOGIN_SUCCEEDED,
        CATALOG_ENTRY_ADDED,
        MACHINE_BOUND,
    )

    c = _authed(client)
    log = c.app.state.events_log  # type: ignore[attr-defined]
    log.emit(CATALOG_ENTRY_ADDED, subject_kind="entry", subject_id="e1", summary="added e1")
    log.emit(MACHINE_BOUND, subject_kind="machine", subject_id="aa:bb", summary="bound m1")
    log.emit(AUTH_LOGIN_SUCCEEDED, summary="login")

    body = c.get("/ui/events?subject_kind=machine").text
    assert "bound m1" in body
    # A tbody-scoped assert so nav / dropdown labels don't confuse
    # the check.
    tbody = body.split("<tbody>")[1].split("</tbody>")[0]
    assert "added e1" not in tbody
    assert "login" not in tbody


def test_ui_events_confirms_on_destructive_forms(client: TestClient) -> None:
    """Sanity: destructive buttons carry an ``onsubmit=confirm`` gate."""
    c = _authed(client)
    # Seed one machine + one catalog entry so the delete buttons render.
    c.post("/ui/machines/bind", data={"mac": "aa:bb:cc:dd:ee:07", "boot_mode": "ipxe-exit"})
    c.post(
        "/catalog/entries",
        json={"name": "tiny", "src": "https://example.invalid/t.img", "format": "img.gz"},
    )
    machines = c.get("/ui/machines").text
    catalog = c.get("/ui/catalog").text
    assert 'onsubmit="return confirm' in machines
    assert 'onsubmit="return confirm' in catalog


def test_ui_machine_detail_renders_inventory_sections(client: TestClient) -> None:
    """The machine detail's inventory pane renders each section
    (system / cpu / memory / nics / disks) present on the posted
    payload; sections not in the payload are simply absent."""
    c = _authed(client)
    payload = {
        "system": {"vendor": "GMKtec", "model": "NUCBOX G5", "serial": "SN123"},
        "cpu": {"model": "Intel N100", "cores": 4, "threads": 4, "arch": "x86_64"},
        "memory": {"total": "16GiB", "modules": 1},
        "nics": [
            {"name": "enp1s0", "mac": "aa:bb:cc:dd:ee:08", "vendor": "Realtek", "driver": "r8125"}
        ],
        "disks": [{"path": "/dev/nvme0n1", "size": "1T", "vendor": "Samsung"}],
    }
    r = c.post("/pxe/aa:bb:cc:dd:ee:08/inventory", json=payload)
    assert r.status_code == 204
    body = c.get("/ui/machines/aa:bb:cc:dd:ee:08").text
    assert "GMKtec" in body
    assert "NUCBOX G5" in body
    assert "Intel N100" in body
    assert "16GiB" in body
    assert "enp1s0" in body
    assert "r8125" in body
    assert "Samsung" in body


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
    assert r.headers["location"] == "/ui/catalog"


def test_ui_dashboard_live_returns_stats(client: TestClient) -> None:
    """/ui/dashboard-live.json echoes the same stats the dashboard
    HTML renders so a poll updates counts in place."""
    c = _authed(client)
    # Seed a machine + a catalog entry so the counts aren't all zero.
    c.get("/pxe/aa:bb:cc:dd:ee:aa")  # discovery
    c.post(
        "/catalog/entries",
        json={"name": "seed", "src": "https://example.invalid/s.img", "format": "img.gz"},
    )
    r = c.get("/ui/dashboard-live.json")
    assert r.status_code == 200
    body = r.json()
    assert body["machines_total"] >= 1
    assert body["catalog_total"] >= 1
    for k in ("machines_bound", "catalog_fetched", "exports_running"):
        assert k in body


def test_ui_events_live_returns_recent_events(client: TestClient) -> None:
    """/ui/events-live.json returns the most recent events, respects
    since_ts (only rows strictly newer), and clamps limit."""
    from pixie.events._kinds import CATALOG_ENTRY_ADDED

    c = _authed(client)
    log = c.app.state.events_log  # type: ignore[attr-defined]
    log.emit(CATALOG_ENTRY_ADDED, subject_kind="entry", subject_id="e1", summary="e1")
    r = c.get("/ui/events-live.json?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert "events" in body
    kinds = [e["kind"] for e in body["events"]]
    assert CATALOG_ENTRY_ADDED in kinds
    # since_ts trims: use a future stamp -> no rows returned.
    r2 = c.get("/ui/events-live.json?since_ts=2999-01-01T00:00:00Z")
    assert r2.json()["events"] == []


def test_ui_live_endpoints_require_auth(client: TestClient) -> None:
    for path in ("/ui/dashboard-live.json", "/ui/events-live.json"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/ui/login"
