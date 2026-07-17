"""GET /pxe/{mac}/plan JSON endpoint + renderer live-env branch.

Two contracts covered here:

- The JSON plan the LIVE-ENV pixie CLI reads after boot returns the
  right ``mode`` for each ``boot_mode`` (pixie-inventory -> inventory,
  pixie-tui -> interactive, pixie-flash-* -> interactive until the
  target-disk field lands, ipxe-exit / ramboot / unknown -> exit).
- The renderer's ``pixie-*`` branch degrades to ``unavailable`` when
  no live-env artifacts are staged, and renders the ``pixie-live-env``
  iPXE chain when they are.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import authed as _authed


def _seed_machine(client: TestClient, mac: str, boot_mode: str) -> None:
    """Prime the machine store with a specific boot_mode. Uses the
    JSON API so tests do not depend on the /ui/machines/bind form.
    Flash modes get a seed inventory + target_disk_serial to satisfy
    the bind-time guard; other modes bind unconditionally."""
    c = _authed(client)
    if boot_mode in ("pixie-flash-once", "pixie-flash-always"):
        c.post(
            f"/pxe/{mac}/inventory",
            json={"disks": [{"path": "/dev/sda", "serial": f"SN-{mac}"}]},
        )
        r = c.put(
            f"/machines/{mac}",
            json={"boot_mode": boot_mode, "target_disk_serial": f"SN-{mac}"},
        )
    else:
        r = c.put(f"/machines/{mac}", json={"boot_mode": boot_mode})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "boot_mode,expected_mode",
    [
        ("pixie-inventory", "inventory"),
        ("pixie-tui", "interactive"),
        ("pixie-flash-once", "interactive"),
        ("pixie-flash-always", "interactive"),
        ("ipxe-exit", "exit"),
        ("ramboot", "interactive"),
    ],
)
def test_plan_json_maps_boot_mode(client: TestClient, boot_mode: str, expected_mode: str) -> None:
    _seed_machine(client, "aa:bb:cc:dd:ee:10", boot_mode)
    r = client.get("/pxe/aa:bb:cc:dd:ee:10/plan")
    assert r.status_code == 200
    assert r.json() == {"mode": expected_mode}


def test_plan_json_unknown_mac_returns_exit(client: TestClient) -> None:
    """A GET /plan without a prior discovery hit (no machine row) is
    unusual but valid; the CLI should still get a well-formed response
    so its inventory-auto-post path can fire and its wizard code does
    not KeyError."""
    r = client.get("/pxe/aa:bb:cc:dd:ee:11/plan")
    assert r.status_code == 200
    assert r.json() == {"mode": "exit"}


def test_plan_json_rejects_bad_mac(client: TestClient) -> None:
    r = client.get("/pxe/not-a-mac/plan")
    assert r.status_code == 400


def test_ipxe_plan_pixie_inventory_no_live_env_dir_falls_back(client: TestClient) -> None:
    """With no netboot-pc artifacts staged, ``boot_mode=pixie-inventory``
    must degrade to the readable ``unavailable`` plan rather than
    emitting a chain the target cannot fetch."""
    _seed_machine(client, "aa:bb:cc:dd:ee:12", "pixie-inventory")
    r = client.get("/pxe/aa:bb:cc:dd:ee:12")
    assert r.status_code == 200
    body = r.text
    # unavailable.j2 emits ``exit`` (unloads iPXE, firmware moves on)
    # + a reason comment naming the missing live-env media.
    assert "exit" in body
    assert "pixie live-env media" in body
    # A live-env chain would carry boot=live in the kernel line; the
    # fallback must NOT emit that.
    assert "boot=live" not in body


def test_ipxe_plan_pixie_inventory_with_staged_artifacts_renders_live_env(
    client: TestClient, tmp_path: Path
) -> None:
    """Once the three artifact files are on disk under the
    live-env dir the renderer emits the Debian live-boot chain."""
    live_env = client.app.state.live_env_dir  # type: ignore[attr-defined]
    assert isinstance(live_env, Path)
    live_env.mkdir(parents=True, exist_ok=True)
    for name in ("vmlinuz", "initrd", "live.squashfs"):
        (live_env / name).write_bytes(b"stub")
    try:
        _seed_machine(client, "aa:bb:cc:dd:ee:13", "pixie-inventory")
        r = client.get("/pxe/aa:bb:cc:dd:ee:13")
        assert r.status_code == 200
        body = r.text
        assert "boot=live" in body
        assert "/boot/pixie-live-env/vmlinuz" in body
        assert "/boot/pixie-live-env/initrd" in body
        assert "/boot/pixie-live-env/live.squashfs" in body
        assert "pixie.mac=aa:bb:cc:dd:ee:13" in body
    finally:
        for name in ("vmlinuz", "initrd", "live.squashfs"):
            (live_env / name).unlink(missing_ok=True)
