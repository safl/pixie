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


def _seed_flash_bound_machine(client: TestClient, mac: str, mode: str, serial: str) -> str:
    """Bind ``mac`` to ``mode`` with a fetched image + matching disk
    serial so plan JSON returns mode=flash. Returns the content sha
    the machine is bound to. Reused by the flash-plan tests."""
    from pixie.catalog._schema import CatalogEntry

    c = _authed(client)
    catalog = c.app.state.catalog_store  # type: ignore[attr-defined]
    catalog.upsert(CatalogEntry(name="ready", src="https://x/ready.img.gz", format="img.gz"))
    sha = "a" * 64
    catalog.mark_fetched("ready", content_sha256=sha, size_bytes=42)
    c.post(f"/pxe/{mac}/inventory", json={"disks": [{"path": "/dev/sda", "serial": serial}]})
    r = c.put(
        f"/machines/{mac}",
        json={
            "boot_mode": mode,
            "image_content_sha256": sha,
            "target_disk_serial": serial,
        },
    )
    assert r.status_code == 200, r.text
    return sha


def test_plan_json_returns_flash_for_pixie_flash_once(client: TestClient) -> None:
    """A pixie-flash-once bind with image + target serial resolves to
    mode=flash with image URL + target_disk_serial + name +
    disk_image_sha; the pixie CLI auto-flashes without touching the
    interactive wizard.

    The catalog entry's format is ``img.gz`` but pixie's fetcher
    decompresses ``img.gz`` at fetch time, so the blob on disk is
    raw ``img``. The plan advertises ``img`` accordingly -- shipping
    ``img.gz`` here would send the live-env CLI into gunzip-on-raw-
    bytes and the flash never completes."""
    mac = "aa:bb:cc:dd:ee:30"
    sha = _seed_flash_bound_machine(client, mac, "pixie-flash-once", "SN-1")
    r = client.get(f"/pxe/{mac}/plan")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "flash"
    assert body["target_disk_serial"] == "SN-1"
    assert body["name"] == "ready"
    assert body["disk_image_sha"] == sha
    # img.gz + img.zst + img.xz all normalise to "img" (fetcher
    # decompressed to blob; wire bytes are raw). Plain "img" +
    # "tar.gz" round-trip untouched.
    assert body["format"] == "img"
    assert body["image"].startswith("http://")
    assert body["image"].endswith(f"/b/{sha}/ready")


def test_plan_json_flash_format_passes_through_uncompressed(client: TestClient) -> None:
    """A plain ``img`` catalog entry ships as ``img`` in the plan;
    no server-side normalisation kicks in for already-uncompressed
    formats (or ``tar.gz`` bundles, which the flash pipeline
    understands as-is)."""
    from pixie.catalog._schema import CatalogEntry

    c = _authed(client)
    mac = "aa:bb:cc:dd:ee:35"
    catalog = c.app.state.catalog_store  # type: ignore[attr-defined]
    catalog.upsert(CatalogEntry(name="raw-img", src="https://x/y.img", format="img"))
    sha = "d" * 64
    catalog.mark_fetched("raw-img", content_sha256=sha, size_bytes=1)
    c.post(f"/pxe/{mac}/inventory", json={"disks": [{"path": "/dev/sda", "serial": "SN-r"}]})
    c.put(
        f"/machines/{mac}",
        json={
            "boot_mode": "pixie-flash-once",
            "image_content_sha256": sha,
            "target_disk_serial": "SN-r",
        },
    )
    body = client.get(f"/pxe/{mac}/plan").json()
    assert body["format"] == "img"


def test_plan_json_flash_url_quotes_entry_name(client: TestClient) -> None:
    """nosi's published entry names carry spaces + parens ("nosi
    debian-13-headless (x86_64, 2026.W29)"). The plan-JSON image URL
    must URL-quote the name so the live env's ``urllib.request``
    parser doesn't drop the path segment; leaving raw whitespace in
    the URL was observed in CI to make the CLI HEAD the bare host,
    which pixie 405s and the auto-flash stalls."""
    from pixie.catalog._schema import CatalogEntry

    c = _authed(client)
    mac = "aa:bb:cc:dd:ee:34"
    catalog = c.app.state.catalog_store  # type: ignore[attr-defined]
    catalog.upsert(
        CatalogEntry(
            name="nosi debian-13-headless (x86_64, 2026.W29)",
            src="oras://x/y:z",
            format="img.gz",
        )
    )
    sha = "c" * 64
    catalog.mark_fetched(
        "nosi debian-13-headless (x86_64, 2026.W29)", content_sha256=sha, size_bytes=1
    )
    c.post(f"/pxe/{mac}/inventory", json={"disks": [{"path": "/dev/sda", "serial": "SN-x"}]})
    c.put(
        f"/machines/{mac}",
        json={
            "boot_mode": "pixie-flash-once",
            "image_content_sha256": sha,
            "target_disk_serial": "SN-x",
        },
    )
    body = client.get(f"/pxe/{mac}/plan").json()
    # Path segment carries no raw whitespace or unescaped parens; the
    # decoded ``name`` field still reads normally for logging.
    assert " " not in body["image"], body["image"]
    assert "(" not in body["image"], body["image"]
    assert body["image"].endswith(
        "/b/" + sha + "/nosi%20debian-13-headless%20%28x86_64%2C%202026.W29%29"
    )
    assert body["name"] == "nosi debian-13-headless (x86_64, 2026.W29)"


def test_plan_json_flash_falls_back_when_image_missing(client: TestClient) -> None:
    """A pixie-flash-* bind without an image_content_sha256 falls
    back to interactive so the operator can pick manually."""
    from pixie.catalog._schema import CatalogEntry

    c = _authed(client)
    mac = "aa:bb:cc:dd:ee:31"
    catalog = c.app.state.catalog_store  # type: ignore[attr-defined]
    catalog.upsert(CatalogEntry(name="ready", src="https://x/r.img.gz", format="img.gz"))
    catalog.mark_fetched("ready", content_sha256="b" * 64, size_bytes=1)
    c.post(f"/pxe/{mac}/inventory", json={"disks": [{"path": "/dev/sda", "serial": "SN-x"}]})
    # bind flash-once WITHOUT image_content_sha256 -- machine record
    # accepts it (only target_disk_serial guarded); plan should back
    # off to interactive rather than build a broken flash payload.
    c.put(
        f"/machines/{mac}",
        json={"boot_mode": "pixie-flash-once", "target_disk_serial": "SN-x"},
    )
    r = client.get(f"/pxe/{mac}/plan")
    assert r.json() == {"mode": "interactive"}


def test_status_done_flips_pixie_flash_once_to_ipxe_exit(client: TestClient) -> None:
    """After the live env's pixie CLI POSTs status=done, a
    pixie-flash-once bind flips to ipxe-exit so the target's next
    PXE boot lands on the disk without re-flashing."""
    mac = "aa:bb:cc:dd:ee:32"
    _seed_flash_bound_machine(client, mac, "pixie-flash-once", "SN-2")
    # Confirm the bind pre-check.
    assert client.get(f"/machines/{mac}").json()["boot_mode"] == "pixie-flash-once"

    r = client.post(f"/pxe/{mac}/status", json={"status": "done"})
    assert r.status_code == 204
    assert client.get(f"/machines/{mac}").json()["boot_mode"] == "ipxe-exit"


def test_status_done_leaves_pixie_flash_always_alone(client: TestClient) -> None:
    """pixie-flash-always is meant to re-flash every boot; a status
    done stays on the same mode so the next boot re-arms."""
    mac = "aa:bb:cc:dd:ee:33"
    _seed_flash_bound_machine(client, mac, "pixie-flash-always", "SN-3")
    client.post(f"/pxe/{mac}/status", json={"status": "done"})
    assert client.get(f"/machines/{mac}").json()["boot_mode"] == "pixie-flash-always"


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
