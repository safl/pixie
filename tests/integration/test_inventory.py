"""Inventory collection E2E: live-env TUI helpers -> real containerized
pixie server -> stored on the machine row + event emitted.

The TUI's ``_auto_post_inventory`` calls three library functions in
sequence when the live env boots:

  1. ``pixie.disks.list_disks()``       -> shells out to ``lsblk -J``
  2. ``pixie.tui._app.collect_lshw()``  -> shells out to ``lshw -json``
  3. ``pixie.tui._app.post_inventory()`` -> POSTs the merged blob

This file exercises that exact call chain against the real
containerized pixie server (from the shared ``container`` fixture).
No stubs; no mocks; no in-process TestClient. If lsblk or lshw is
missing on the test host we fall back to a synthetic payload so
the wire contract is still exercised.

Skip conditions live in ``conftest.py::_skip_reason`` -- podman not
on PATH is the only implicit skip; the tests themselves have no
extra prerequisites.
"""

from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from typing import Any

import pytest

from pixie import disks
from pixie.tui._app import collect_lshw, post_inventory
from tests.integration.conftest import _get, _post_json

pytestmark = pytest.mark.integration

_TEST_MAC = "aa:bb:cc:dd:ee:99"


def _synthetic_disks() -> list[dict[str, Any]]:
    """Fallback payload when ``lsblk`` is missing on the test host.
    Matches the shape :func:`pixie.disks.list_disks` returns so a
    downstream ``/ui/machines`` consumer can't tell the difference."""
    return [
        {
            "path": "/dev/sda",
            "size": "512G",
            "type": "disk",
            "vendor": "SYNTHETIC",
            "model": "TEST-DISK",
            "serial": "0xdeadbeef",
            "rm": False,
            "ro": False,
            "tran": "sata",
        }
    ]


def _real_disks_or_synthetic() -> list[dict[str, Any]]:
    if shutil.which("lsblk") is None:
        return _synthetic_disks()
    try:
        return disks.list_disks()
    except Exception:
        return _synthetic_disks()


def test_post_inventory_stores_and_reads_back(container: dict[str, object]) -> None:
    """Full E2E: call the same ``post_inventory`` helper the live env's
    TUI calls, hit the real containerized pixie, then read it back
    over ``GET /machines/<mac>/inventory``."""
    base_url = str(container["base_url"])
    disks_payload = _real_disks_or_synthetic()
    lshw_payload = collect_lshw() if shutil.which("lshw") else None

    # Real HTTP POST from an in-process caller to the containerized
    # pixie -- same code path the live env uses.
    post_inventory(base_url, _TEST_MAC, disks_payload, lshw=lshw_payload)

    resp = _get(base_url, f"/machines/{_TEST_MAC}/inventory")
    assert resp.status == 200
    body = json.loads(resp.read())

    assert body["mac"] == _TEST_MAC
    assert body["inventory_at"], "server should stamp inventory_at on POST"
    inv = body["inventory"]
    assert inv["disks"] == disks_payload
    if lshw_payload is not None:
        assert inv["lshw"] == lshw_payload
    else:
        # Live envs without lshw omit the field; server should not
        # invent one.
        assert "lshw" not in inv


def test_post_inventory_emits_event(container: dict[str, object]) -> None:
    """The server-side POST handler emits ``machine.inventory.updated``
    with ``disks_count`` + ``has_lshw`` details."""
    base_url = str(container["base_url"])
    disks_payload = _real_disks_or_synthetic()
    lshw_payload = {"synthetic": True, "class": "system"}

    post_inventory(base_url, _TEST_MAC, disks_payload, lshw=lshw_payload)

    resp = _get(base_url, "/events", cookie="")
    events = json.loads(resp.read())["events"]
    inv_events = [
        e
        for e in events
        if e["kind"] == "machine.inventory.updated" and e["subject_id"] == _TEST_MAC
    ]
    assert inv_events, "expected a machine.inventory.updated event"
    latest = inv_events[0]
    details = latest.get("details") or {}
    assert details.get("disks_count") == len(disks_payload)
    assert details.get("has_lshw") is True


def test_post_inventory_creates_machine_row_on_first_contact(
    container: dict[str, object],
) -> None:
    """A live env can POST inventory before its first ``GET /pxe/<mac>``;
    the server must upsert (not 404) so the row exists after the POST."""
    base_url = str(container["base_url"])
    fresh_mac = "aa:bb:cc:dd:ee:11"

    # Confirm the row isn't there yet.
    machines = json.loads(_get(base_url, "/machines").read())["machines"]
    assert not any(m["mac"] == fresh_mac for m in machines), "test mac leaked between runs"

    disks_payload = _real_disks_or_synthetic()
    post_inventory(base_url, fresh_mac, disks_payload)

    # Row visible on the machines index...
    machines = json.loads(_get(base_url, "/machines").read())["machines"]
    assert any(m["mac"] == fresh_mac for m in machines), "row should be upserted by POST"

    # ...and the inventory read-back works.
    resp = _get(base_url, f"/machines/{fresh_mac}/inventory")
    assert resp.status == 200
    body = json.loads(resp.read())
    assert body["inventory"]["disks"] == disks_payload


def test_get_inventory_404_when_none_posted(container: dict[str, object]) -> None:
    """A row that exists (via /pxe/<mac> discovery) but has no inventory
    yet returns 404 on GET /machines/<mac>/inventory. Ensures the empty
    inventory dict on a fresh row is not confused for stored data."""
    base_url = str(container["base_url"])
    new_mac = "aa:bb:cc:dd:ee:22"

    # Discovery-side upsert without an inventory POST.
    _get(base_url, f"/pxe/{new_mac}")

    req = urllib.request.Request(f"{base_url}/machines/{new_mac}/inventory", method="GET")
    try:
        urllib.request.urlopen(req, timeout=5.0)
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    else:
        pytest.fail("expected 404 for a machine without inventory")


def test_bad_mac_rejected(container: dict[str, object]) -> None:
    """Unparseable MACs return 400 rather than crashing the store."""
    base_url = str(container["base_url"])
    resp = _post_json(base_url, "/pxe/not-a-mac/inventory", {"disks": []})
    assert getattr(resp, "code", getattr(resp, "status", 0)) == 400


def test_bad_body_rejected(container: dict[str, object]) -> None:
    """A non-object JSON body is rejected."""
    base_url = str(container["base_url"])
    # Send a bare list; the server insists on an object envelope so
    # future keys (``firmware``, ``bios``, ...) don't collide with a
    # positional interpretation.
    body = json.dumps(["not", "an", "object"]).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/pxe/{_TEST_MAC}/inventory",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=5.0)
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    else:
        pytest.fail("expected 400 for a non-object JSON body")


def test_second_post_replaces_first(container: dict[str, object]) -> None:
    """Live envs re-post inventory on each boot; the second POST should
    replace, not append (nothing merges lshw fragments)."""
    base_url = str(container["base_url"])

    first_disks = [{"path": "/dev/sda", "size": "1T", "type": "disk"}]
    second_disks = [
        {"path": "/dev/nvme0n1", "size": "2T", "type": "disk"},
        {"path": "/dev/sda", "size": "1T", "type": "disk"},
    ]

    post_inventory(base_url, _TEST_MAC, first_disks)
    post_inventory(base_url, _TEST_MAC, second_disks)

    body = json.loads(_get(base_url, f"/machines/{_TEST_MAC}/inventory").read())
    assert body["inventory"]["disks"] == second_disks
