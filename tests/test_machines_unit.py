"""Pure-Python unit tests for the machines module.

The end-to-end flows (discovery upsert on /pxe/<mac>, ramboot plan
rendering with a live nbdkit) live in ``tests/integration/``. These
tests cover surface that never touches a subprocess.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pixie.machines._store import BOOT_MODES, BadMac, normalise_mac
from tests.conftest import TEST_ADMIN_PASSWORD


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


def _authed(client: TestClient) -> TestClient:
    client.post("/ui/login", data={"password": TEST_ADMIN_PASSWORD})
    return client


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
