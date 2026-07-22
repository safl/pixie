"""Events log: append + filter + emit-from-route roundtrip.

The events log is a stdlib sqlite repository over the shared
``state.db``; every unit test in this file gets a fresh tmpdir so
concurrent runs cannot collide.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from pixie.events import EventsLog
from tests.conftest import authed as _authed


def test_emit_and_list_roundtrip(tmp_path: Path) -> None:
    log = EventsLog(tmp_path / "state.db")
    log.emit("catalog.entry.added", subject_kind="entry", subject_id="tiny", summary="tiny")
    log.emit(
        "machine.bound",
        subject_kind="machine",
        subject_id="aa:bb:cc:dd:ee:ff",
        summary="aa:bb:cc:dd:ee:ff -> nbdboot",
        details={"boot_mode": "nbdboot"},
    )
    rows = log.list()
    # Newest-first ordering.
    assert [r.kind for r in rows] == ["machine.bound", "catalog.entry.added"]
    # Details round-trip through JSON.
    assert rows[0].details == {"boot_mode": "nbdboot"}


def test_filter_by_kind(tmp_path: Path) -> None:
    log = EventsLog(tmp_path / "state.db")
    for i in range(3):
        log.emit("catalog.fetch.started", subject_kind="entry", subject_id=f"e{i}")
    log.emit("machine.bound", subject_kind="machine", subject_id="mac")
    fetches = log.list(kind="catalog.fetch.started")
    assert len(fetches) == 3
    assert all(f.kind == "catalog.fetch.started" for f in fetches)


def test_filter_by_subject(tmp_path: Path) -> None:
    log = EventsLog(tmp_path / "state.db")
    log.emit("machine.discovered", subject_kind="machine", subject_id="mac-1")
    log.emit("machine.bound", subject_kind="machine", subject_id="mac-2")
    log.emit("catalog.entry.added", subject_kind="entry", subject_id="e")
    machine_1 = log.list(subject_kind="machine", subject_id="mac-1")
    assert len(machine_1) == 1
    assert machine_1[0].kind == "machine.discovered"


def test_since_id_is_exclusive(tmp_path: Path) -> None:
    log = EventsLog(tmp_path / "state.db")
    e1 = log.emit("catalog.entry.added", subject_kind="entry", subject_id="e1")
    e2 = log.emit("catalog.entry.added", subject_kind="entry", subject_id="e2")
    tail = log.list(since_id=e1.id or 0)
    assert [e.id for e in tail] == [e2.id]


def test_limit_capped(tmp_path: Path) -> None:
    log = EventsLog(tmp_path / "state.db")
    for i in range(1500):
        log.emit("catalog.entry.added", subject_kind="entry", subject_id=f"e{i}")
    # Explicit limit=2000 clamps to 1000.
    rows = log.list(limit=2000)
    assert len(rows) == 1000


def test_add_catalog_entry_route_emits_event(client: TestClient) -> None:
    """The route event dispatcher writes through to the log."""
    c = _authed(client)
    r = c.post(
        "/catalog/entries",
        json={
            "name": "tiny",
            "src": "https://example.com/tiny.img.gz",
            "format": "img.gz",
        },
    )
    assert r.status_code == 201
    body = c.get("/events").json()["events"]
    assert body, "expected at least one event"
    added = [e for e in body if e["kind"] == "catalog.entry.added"]
    assert added
    assert added[0]["subject_id"] == "tiny"


def test_delete_catalog_entry_route_emits_event(client: TestClient) -> None:
    c = _authed(client)
    c.post(
        "/catalog/entries",
        json={
            "name": "gone",
            "src": "https://example.com/gone.img.gz",
            "format": "img.gz",
        },
    )
    c.delete("/catalog/entries", params={"name": "gone"})
    kinds = [e["kind"] for e in c.get("/events").json()["events"]]
    assert "catalog.entry.deleted" in kinds


def test_machine_bind_emits_event(client: TestClient) -> None:
    c = _authed(client)
    r = c.put(
        "/machines/aa:bb:cc:dd:ee:99",
        json={"boot_mode": "ipxe-exit"},
    )
    assert r.status_code == 200
    bound = [e for e in c.get("/events").json()["events"] if e["kind"] == "machine.bound"]
    assert bound
    assert bound[0]["subject_id"] == "aa:bb:cc:dd:ee:99"
    assert bound[0]["details"]["boot_mode"] == "ipxe-exit"


def test_events_route_open_read(client: TestClient) -> None:
    """No session cookie -> still 200 (read routes are open by
    design; on-call operators curl from a workstation)."""
    r = client.get("/events")
    assert r.status_code == 200
    assert "events" in r.json()


def test_events_route_filtering(client: TestClient) -> None:
    c = _authed(client)
    c.post(
        "/catalog/entries",
        json={
            "name": "a",
            "src": "https://example.com/a.img.gz",
            "format": "img.gz",
        },
    )
    c.put("/machines/aa:bb:cc:dd:ee:aa", json={"boot_mode": "ipxe-exit"})
    only_bound = c.get("/events", params={"kind": "machine.bound"}).json()["events"]
    assert only_bound
    assert all(e["kind"] == "machine.bound" for e in only_bound)


def test_ui_events_page_renders(client: TestClient) -> None:
    c = _authed(client)
    r = c.get("/ui/events")
    assert r.status_code == 200
    assert "Events" in r.text


def test_ui_events_requires_auth(client: TestClient) -> None:
    r = client.get("/ui/events", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_clear_removes_all_events(tmp_path: Path) -> None:
    log = EventsLog(tmp_path / "state.db")
    for i in range(4):
        log.emit("catalog.fetch.started", subject_kind="entry", subject_id=f"e{i}")
    assert log.clear() == 4
    assert log.list() == []


def test_ui_events_clear_wipes_and_leaves_marker(client: TestClient) -> None:
    c = _authed(client)  # the login itself emits auth.login.succeeded
    r = c.post("/ui/events/clear", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/events"
    # The log is wiped except for the events.cleared marker the route drops.
    assert "events.cleared" in c.get("/ui/events").text


def test_ui_events_ack_honours_next_and_guards_open_redirect(client: TestClient) -> None:
    c = _authed(client)
    r = c.post("/ui/events/ack", data={"next": "/ui/events"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/events"
    # A non-/ui/ next is refused (no open redirect) and falls back to /ui/.
    r2 = c.post("/ui/events/ack", data={"next": "https://evil.test/"}, follow_redirects=False)
    assert r2.headers["location"] == "/ui/"


def test_ui_events_clear_requires_auth(client: TestClient) -> None:
    r = client.post("/ui/events/clear", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"
