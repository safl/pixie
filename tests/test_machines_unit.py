"""Pure-Python unit tests for the machines module.

The end-to-end flows (discovery upsert on /pxe/<mac>, ramboot plan
rendering with a live nbdkit) live in ``tests/integration/``. These
tests cover surface that never touches a subprocess.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pixie.machines._store import BOOT_MODES, BadMac, normalise_mac
from tests.conftest import authed as _authed


def test_normalise_mac_accepts_all_common_shapes() -> None:
    assert normalise_mac("aa:bb:cc:dd:ee:ff") == "aa:bb:cc:dd:ee:ff"
    assert normalise_mac("AA:BB:CC:DD:EE:FF") == "aa:bb:cc:dd:ee:ff"
    assert normalise_mac("aa-bb-cc-dd-ee-ff") == "aa:bb:cc:dd:ee:ff"
    assert normalise_mac("AABBCCDDEEFF") == "aa:bb:cc:dd:ee:ff"


def test_normalise_mac_rejects_garbage() -> None:
    for bad in ("not-a-mac", "aa:bb:cc:dd:ee", "aabb-cc-dd-ee-ff", "gg:hh:ii:jj:kk:ll"):
        with pytest.raises(BadMac):
            normalise_mac(bad)


def test_boot_modes_is_the_locked_set() -> None:
    """The set is closed on purpose (see :mod:`pixie.machines._store`).
    If this test fails you added a mode; update the closed-set
    guarantee in the module docstring too."""
    assert (
        frozenset(
            {
                "ipxe-exit",
                "pixie-flash-once",
                "pixie-flash-always",
                "pixie-inventory",
                "pixie-tui",
                "ramboot",
            }
        )
        == BOOT_MODES
    )


def test_get_machine_404_before_discovery(client: TestClient) -> None:
    r = client.get("/machines/aa:bb:cc:dd:ee:00")
    assert r.status_code == 404


def test_put_machine_requires_session(client: TestClient) -> None:
    """No cookie -> 401 on the write route."""
    r = client.put(
        "/machines/aa:bb:cc:dd:ee:01",
        json={"boot_mode": "ipxe-exit"},
    )
    assert r.status_code == 401


def test_put_machine_rejects_bad_mac(client: TestClient) -> None:
    r = _authed(client).put("/machines/not-a-mac", json={"boot_mode": "ipxe-exit"})
    assert r.status_code == 400


def test_put_machine_rejects_unknown_boot_mode(client: TestClient) -> None:
    r = _authed(client).put(
        "/machines/aa:bb:cc:dd:ee:02",
        json={"boot_mode": "bty-tui"},
    )
    assert r.status_code == 422


def test_put_machine_rejects_bad_content_sha(client: TestClient) -> None:
    r = _authed(client).put(
        "/machines/aa:bb:cc:dd:ee:03",
        json={"boot_mode": "ramboot", "image_content_sha256": "not-a-sha"},
    )
    assert r.status_code == 422


def test_put_machine_ipxe_exit_roundtrip(client: TestClient) -> None:
    c = _authed(client)
    r = c.put(
        "/machines/aa:bb:cc:dd:ee:04",
        json={"boot_mode": "ipxe-exit"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["mac"] == "aa:bb:cc:dd:ee:04"
    assert body["boot_mode"] == "ipxe-exit"

    r2 = c.get("/machines/aa:bb:cc:dd:ee:04")
    assert r2.status_code == 200
    assert r2.json()["boot_mode"] == "ipxe-exit"


def test_put_machine_persists_labels_sanboot_target_serial(client: TestClient) -> None:
    """Extended binding fields round-trip through the JSON PUT + GET
    pair and land on ``Machine.to_dict()``. Seeds an inventory with a
    matching disk serial so the flash-mode guard passes."""
    c = _authed(client)
    c.post(
        "/pxe/aa:bb:cc:dd:ee:20/inventory",
        json={"disks": [{"path": "/dev/nvme0n1", "size": "1T", "serial": "S679NX0R123456"}]},
    )
    r = c.put(
        "/machines/aa:bb:cc:dd:ee:20",
        json={
            "boot_mode": "pixie-flash-once",
            "labels": ["rack-3", "gmktec-g5"],
            "sanboot_drive": "0x81",
            "target_disk_serial": "S679NX0R123456",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["labels"] == ["rack-3", "gmktec-g5"]
    assert body["sanboot_drive"] == "0x81"
    assert body["target_disk_serial"] == "S679NX0R123456"

    row = c.get("/machines/aa:bb:cc:dd:ee:20").json()
    assert row["labels"] == ["rack-3", "gmktec-g5"]
    assert row["sanboot_drive"] == "0x81"
    assert row["target_disk_serial"] == "S679NX0R123456"


def test_put_machine_flash_requires_inventory(client: TestClient) -> None:
    """Binding boot_mode=pixie-flash-once on a never-inventoried MAC
    is rejected with 422 pointing at the missing prerequisite."""
    c = _authed(client)
    r = c.put(
        "/machines/aa:bb:cc:dd:ee:24",
        json={"boot_mode": "pixie-flash-once", "target_disk_serial": "SN1"},
    )
    assert r.status_code == 422
    assert "no inventory" in r.text.lower()


def test_put_machine_flash_requires_target_disk_serial(client: TestClient) -> None:
    """Inventory reports a disk with a serial, but the bind omits
    target_disk_serial -> 422 listing the picks."""
    c = _authed(client)
    c.post(
        "/pxe/aa:bb:cc:dd:ee:25/inventory",
        json={"disks": [{"path": "/dev/sda", "serial": "SN-ABC"}]},
    )
    r = c.put(
        "/machines/aa:bb:cc:dd:ee:25",
        json={"boot_mode": "pixie-flash-always"},
    )
    assert r.status_code == 422
    assert "target_disk_serial" in r.text


def test_put_machine_flash_rejects_unknown_target_serial(client: TestClient) -> None:
    """Serial that doesn't match anything in the inventory -> 422 so
    a stale value doesn't slip through when disks were swapped."""
    c = _authed(client)
    c.post(
        "/pxe/aa:bb:cc:dd:ee:26/inventory",
        json={"disks": [{"path": "/dev/sda", "serial": "SN-KEEP"}]},
    )
    r = c.put(
        "/machines/aa:bb:cc:dd:ee:26",
        json={"boot_mode": "pixie-flash-once", "target_disk_serial": "SN-STALE"},
    )
    assert r.status_code == 422
    assert "not in this" in r.text.lower()


def test_put_machine_non_flash_modes_skip_disk_guard(client: TestClient) -> None:
    """ipxe-exit / ramboot / pixie-inventory / pixie-tui do not touch
    the target disk; binding them without an inventory succeeds."""
    c = _authed(client)
    for mode in ("ipxe-exit", "ramboot", "pixie-inventory", "pixie-tui"):
        r = c.put(
            f"/machines/aa:bb:cc:dd:ee:{ord(mode[0]):02x}",
            json={"boot_mode": mode},
        )
        assert r.status_code == 200, f"{mode} unexpectedly rejected: {r.text}"


def test_put_machine_rejects_bad_sanboot_drive(client: TestClient) -> None:
    """Malformed iPXE drive slug (not ``0x<hex1-2>``) returns 422."""
    c = _authed(client)
    r = c.put(
        "/machines/aa:bb:cc:dd:ee:21",
        json={"boot_mode": "ipxe-exit", "sanboot_drive": "80h"},
    )
    assert r.status_code == 422


def test_put_machine_rejects_bad_label(client: TestClient) -> None:
    """Labels reject anything outside the alnum-leading char set."""
    c = _authed(client)
    r = c.put(
        "/machines/aa:bb:cc:dd:ee:22",
        json={"boot_mode": "ipxe-exit", "labels": [" nope!bang"]},
    )
    assert r.status_code == 422


def test_parse_labels_dedupes_and_normalises() -> None:
    from pixie.machines._store import parse_labels

    out = parse_labels(" rack-3 , noisy,  rack-3 , gmktec-g5 ")
    assert out == ["rack-3", "noisy", "gmktec-g5"]


def test_parse_labels_enforces_count_limit() -> None:
    from pixie.machines._store import parse_labels

    with pytest.raises(ValueError, match="at most 16 labels"):
        parse_labels(", ".join(f"label{i}" for i in range(17)))


def test_ui_machines_bind_form_persists_extended_fields(client: TestClient) -> None:
    c = _authed(client)
    r = c.post(
        "/ui/machines/bind",
        data={
            "mac": "aa:bb:cc:dd:ee:23",
            "boot_mode": "ipxe-exit",
            "labels": "rack-3, noisy",
            "sanboot_drive": "0x80",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    body = c.get("/ui/machines").text
    assert "rack-3" in body
    assert "noisy" in body

    row = c.get("/machines/aa:bb:cc:dd:ee:23").json()
    assert row["labels"] == ["rack-3", "noisy"]
    assert row["sanboot_drive"] == "0x80"


def test_pxe_bootstrap_serves_ipxe_prefix(client: TestClient) -> None:
    """The bootstrap route never fails on a first contact (a fresh
    target has no machine row yet, and the bootstrap doesn't touch
    the machines table)."""
    r = client.get("/pxe-bootstrap.ipxe")
    assert r.status_code == 200
    assert r.text.startswith("#!ipxe")
    assert "/pxe/${net0/mac}" in r.text


def test_pxe_plan_ipxe_exit_default_for_new_mac(client: TestClient) -> None:
    """Discovery-side write: a MAC pixie has never seen before still
    gets a plan on the first hit (the mode is the default
    ``ipxe-exit`` so the plan is deterministic)."""
    mac = "de:ad:be:ef:00:00"
    r = client.get(f"/pxe/{mac}")
    assert r.status_code == 200
    assert r.text.startswith("#!ipxe")
    assert "exit" in r.text
    # The row now exists (discovery upsert).
    assert client.get(f"/machines/{mac}").status_code == 200


def test_pxe_plan_ramboot_without_bound_image_falls_back(client: TestClient) -> None:
    """Binding ramboot without a fetched image renders the
    ``unavailable`` template with the reason baked in the comment;
    the target does NOT boot a mismatched kernel."""
    c = _authed(client)
    mac = "de:ad:be:ef:00:01"
    c.put(f"/machines/{mac}", json={"boot_mode": "ramboot"})
    r = c.get(f"/pxe/{mac}")
    assert r.status_code == 200
    assert r.text.startswith("#!ipxe")
    assert "exit" in r.text
    assert "no image bound" in r.text


def test_ui_machines_live_reflects_current_row_shape(client: TestClient) -> None:
    """The /ui/machines-live.json endpoint returns a dict keyed by MAC
    with the fields the JS refresh needs: boot_mode, image sha, labels,
    last_seen_at (raw + display), inventory count, has_lshw."""
    c = _authed(client)
    # Seed a row via discovery + a bind + an inventory post.
    mac = "aa:bb:cc:dd:ee:fe"
    c.get(f"/pxe/{mac}")  # discovery
    c.put(f"/machines/{mac}", json={"boot_mode": "ipxe-exit", "labels": ["rack-9"]})
    c.post(
        f"/pxe/{mac}/inventory",
        json={"disks": [{"path": "/dev/sda", "serial": "SN"}], "lshw": {"class": "system"}},
    )
    r = c.get("/ui/machines-live.json")
    assert r.status_code == 200
    body = r.json()
    row = body[mac]
    assert row["boot_mode"] == "ipxe-exit"
    assert row["labels"] == ["rack-9"]
    assert row["disks_count"] == 1
    assert row["has_lshw"] is True
    assert row["last_seen_at"]  # raw ISO
    assert row["last_seen_at_display"]  # server-formatted


def test_ui_machines_live_requires_auth(client: TestClient) -> None:
    r = client.get("/ui/machines-live.json", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"
