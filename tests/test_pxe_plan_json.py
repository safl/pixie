"""GET /pxe/{mac}/plan JSON endpoint + renderer live-env branch.

Two contracts covered here:

- The JSON plan the LIVE-ENV pixie CLI reads after boot returns the
  right ``mode`` for each ``boot_mode`` (pixie-inventory -> inventory,
  pixie-tui -> interactive, pixie-flash-* -> interactive until the
  target-disk field lands, ipxe-exit / nbdboot / unknown -> exit).
- The renderer's ``pixie-*`` branch degrades to ``unavailable`` when
  no live-env image is configured (or its bundle/blob is not fetched),
  and renders the ephemeral-nbdboot chain when it is.
"""

from __future__ import annotations

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
        ("nbdboot", "interactive"),
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


def test_ipxe_plan_pixie_inventory_no_image_degrades(client: TestClient) -> None:
    """With no live-env image configured, ``boot_mode=pixie-inventory``
    must degrade to the readable ``unavailable`` plan rather than
    emitting a chain the target cannot fetch."""
    _seed_machine(client, "aa:bb:cc:dd:ee:12", "pixie-inventory")
    r = client.get("/pxe/aa:bb:cc:dd:ee:12")
    assert r.status_code == 200
    body = r.text
    # unavailable.j2 emits ``exit`` (unloads iPXE, firmware moves on)
    # + a reason comment naming the missing live-env image.
    assert "exit" in body
    assert "needs a live-env image" in body
    # No live-env chain of any kind.
    assert "boot=live" not in body
    assert "boot=nbdboot" not in body


def _stage_live_env_image(client: TestClient, live_env_sha: str, bundle_sha: str) -> None:
    """Set up the on-disk state the nbdboot renderer needs for a
    configured live-env image: a fetched disk-image catalog entry whose
    ``netboot_src`` points at a fetched netboot-bundle entry, the
    unpacked bundle manifest under artifacts/, and the disk-image blob.
    Mirrors what the fetch pipeline lands so the renderer's resolution +
    on-disk checks pass."""
    from pixie.catalog._schema import CatalogEntry

    catalog = client.app.state.catalog_store  # type: ignore[attr-defined]
    bundle_src = "oras://x/pixie-live-env-bundle:latest"
    catalog.upsert(CatalogEntry(name="pixie-live-env bundle", src=bundle_src, format="tar.gz"))
    catalog.mark_fetched("pixie-live-env bundle", content_sha256=bundle_sha, size_bytes=10)
    catalog.upsert(
        CatalogEntry(
            name="pixie-live-env image",
            src="https://x/pixie-live-env.img",
            format="img",
            netboot_src=bundle_src,
        )
    )
    catalog.mark_fetched("pixie-live-env image", content_sha256=live_env_sha, size_bytes=20)
    # Unpacked bundle manifest + disk-image blob the renderer stats.
    art = catalog.artifact_dir(bundle_sha)
    art.mkdir(parents=True, exist_ok=True)
    (art / "manifest.json").write_text("{}")
    blob = catalog.blob_path(live_env_sha)
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"pixie-live-env-disk-image")


def test_ipxe_plan_live_env_image_renders_nbdboot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With PIXIE_LIVE_ENV_IMAGE_SHA set + the image + its netboot bundle
    fetched, a pixie-inventory machine renders an EPHEMERAL nbdboot plan
    (boot=nbdboot, pixie.nbd=, the live-env image's own kernel/initrd,
    pixie.server/pixie.mac so the on-target CLI phones home) instead of
    the Debian live-boot squashfs chain. The machine's bound mode still
    drives GET /pxe/<mac>/plan, so the CLI's dispatch is unchanged."""
    live_env_sha = "e" * 64
    bundle_sha = "f" * 64
    _authed(client)
    _stage_live_env_image(client, live_env_sha, bundle_sha)
    # Avoid a real nbdkit: the ephemeral export spawn returns a fixed
    # port. Everything else (row upsert, template render) is real.
    monkeypatch.setattr(
        client.app.state.nbd_server,  # type: ignore[attr-defined]
        "spawn",
        lambda name, blob: 10820,
    )
    monkeypatch.setenv("PIXIE_LIVE_ENV_IMAGE_SHA", live_env_sha)
    _seed_machine(client, "aa:bb:cc:dd:ee:40", "pixie-inventory")

    body = client.get("/pxe/aa:bb:cc:dd:ee:40").text
    # nbdboot chain, not the squashfs live-boot chain.
    assert "boot=nbdboot" in body
    assert "boot=live" not in body
    assert "fetch=" not in body
    # NBD wiring + the live-env image's own content-addressed kernel/initrd.
    assert "pixie.nbd=" in body
    assert f"/artifacts/{bundle_sha}/vmlinuz" in body
    assert f"/artifacts/{bundle_sha}/initrd" in body
    # Inspect the actual kernel cmdline (the template carries the string
    # "pixie.persist=1" in a comment, so check the kernel line only).
    kernel_line = next(line for line in body.splitlines() if line.startswith("kernel "))
    # Ephemeral: no persistent-overlay flag on the cmdline.
    assert "pixie.persist=1" not in kernel_line
    # Phone-home tokens the on-target pixie CLI reads.
    assert "pixie.mac=aa:bb:cc:dd:ee:40" in kernel_line
    assert "pixie.server=" in kernel_line
    # The JSON plan contract the CLI dispatches on is UNCHANGED.
    assert client.get("/pxe/aa:bb:cc:dd:ee:40/plan").json() == {"mode": "inventory"}


def test_ipxe_plan_live_env_appends_extra_cmdline(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PIXIE_LIVE_ENV_EXTRA_CMDLINE lets an operator pin hardware
    workarounds (e.g. pci=nommconf for the GIGABYTE MC12-LE0's Intel
    i210 NICs) without rebaking the live-env image. The tokens land on
    the live-env nbdboot kernel line verbatim, after the pixie.mac= /
    bty.mac= tail (last-token-wins); an empty env is a legal no-op."""
    live_env_sha = "e" * 64
    bundle_sha = "f" * 64
    _authed(client)
    _stage_live_env_image(client, live_env_sha, bundle_sha)
    monkeypatch.setattr(
        client.app.state.nbd_server,  # type: ignore[attr-defined]
        "spawn",
        lambda name, blob: 10820,
    )
    monkeypatch.setenv("PIXIE_LIVE_ENV_IMAGE_SHA", live_env_sha)
    _seed_machine(client, "aa:bb:cc:dd:ee:36", "pixie-inventory")
    # Empty extra-cmdline env -> no extra tokens injected.
    monkeypatch.setenv("PIXIE_LIVE_ENV_EXTRA_CMDLINE", "")
    body = client.get("/pxe/aa:bb:cc:dd:ee:36").text
    assert "pci=nommconf" not in body
    # With env -> tokens appended to the kernel line, still one line.
    monkeypatch.setenv("PIXIE_LIVE_ENV_EXTRA_CMDLINE", "pci=nommconf amd_iommu=off pcie_aspm=off")
    body = client.get("/pxe/aa:bb:cc:dd:ee:36").text
    kernel_line = next(line for line in body.splitlines() if line.startswith("kernel "))
    assert "pci=nommconf" in kernel_line
    assert "amd_iommu=off" in kernel_line
    assert "pcie_aspm=off" in kernel_line
    # Tokens land AFTER the bty.mac= tail (last-token-wins).
    assert kernel_line.index("pci=nommconf") > kernel_line.index("bty.mac=")


def test_ipxe_plan_live_env_image_configured_but_missing_degrades(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When PIXIE_LIVE_ENV_IMAGE_SHA names an image whose bundle/blob is
    NOT fetched, the live-env modes degrade to the readable unavailable
    plan (matching how nbdboot degrades) rather than booting nothing --
    a configured-but-broken live env is an operator error worth
    surfacing."""
    live_env_sha = "1" * 64
    monkeypatch.setenv("PIXIE_LIVE_ENV_IMAGE_SHA", live_env_sha)
    _seed_machine(client, "aa:bb:cc:dd:ee:42", "pixie-inventory")
    body = client.get("/pxe/aa:bb:cc:dd:ee:42").text
    assert "exit" in body
    assert "live-env image" in body
    assert "boot=live" not in body
    assert "boot=nbdboot" not in body


def test_pxe_first_contact_emits_machine_discovered_once(client: TestClient) -> None:
    """First GET /pxe/<mac> for an unknown MAC auto-registers it and
    emits ``machine.discovered`` exactly once; subsequent hits update
    ``last_seen`` but do NOT re-emit discovery."""
    mac = "aa:bb:cc:dd:ee:d1"
    log = client.app.state.events_log  # type: ignore[attr-defined]

    client.get(f"/pxe/{mac}")
    discovered = [e for e in log.list(limit=500) if e.kind == "machine.discovered"]
    assert len(discovered) == 1
    assert discovered[0].subject_id == mac
    # A fresh MAC auto-registers with the default boot mode, now
    # pixie-inventory (non-destructive + useful).
    assert discovered[0].details.get("boot_mode") == "pixie-inventory"

    client.get(f"/pxe/{mac}")  # second contact for the now-known MAC
    still = [e for e in log.list(limit=500) if e.kind == "machine.discovered"]
    assert len(still) == 1  # not re-emitted
