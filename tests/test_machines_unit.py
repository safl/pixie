"""Pure-Python unit tests for the machines module.

The end-to-end flows (discovery upsert on /pxe/<mac>, nbdboot plan
rendering with a live nbdkit) live in ``tests/integration/``. These
tests cover surface that never touches a subprocess.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pixie.machines._store import BOOT_MODES, BadMac, MachinesStore, normalise_mac
from tests.conftest import authed as _authed


def test_machines_migration_derives_overlay_alias_from_profile(tmp_path: Path) -> None:
    """A pre-re-model machines row (``overlay_profile`` set,
    ``overlay_alias`` empty) has its alias derived on the next store
    open, using the SAME ``<profile>-<mac_slug>`` rule the overlays-table
    migration mints, so the machine keeps pointing at its qcow2."""
    import sqlite3

    db = tmp_path / "state.db"
    # Hand-build a pre-re-model machines table: it has ``overlay_profile``
    # but NOT ``overlay_alias`` (the column the migration adds).
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE machines (
            mac                    TEXT PRIMARY KEY,
            boot_mode              TEXT NOT NULL DEFAULT 'ipxe-exit',
            image_content_sha256   TEXT NOT NULL DEFAULT '',
            labels                 TEXT NOT NULL DEFAULT '',
            target_disk_serial     TEXT NOT NULL DEFAULT '',
            extra_cmdline          TEXT NOT NULL DEFAULT '',
            overlay_profile        TEXT NOT NULL DEFAULT '',
            inventory_json         TEXT NOT NULL DEFAULT '',
            inventory_at           TEXT NOT NULL DEFAULT '',
            discovered_at          TEXT NOT NULL,
            last_seen_at           TEXT NOT NULL,
            last_seen_ip           TEXT NOT NULL DEFAULT '',
            updated_at             TEXT NOT NULL
        );
        INSERT INTO machines (mac, boot_mode, image_content_sha256, overlay_profile,
            discovered_at, last_seen_at, updated_at)
        VALUES ('aa:bb:cc:dd:ee:00', 'nbdboot', 'a', 'safl', 'x', 'x', 'x');
        """
    )
    conn.commit()
    conn.close()

    # Open: the additive migration adds overlay_alias + backfills it.
    store = MachinesStore(db)
    row = store.get("aa:bb:cc:dd:ee:00")
    assert row is not None
    assert row.overlay_alias == "safl-aa-bb-cc-dd-ee-00"
    # The nbdboot-split migration also fires: this row carried an overlay
    # (profile -> alias backfilled), so it becomes nbdboot-overlay.
    assert row.boot_mode == "nbdboot-overlay"


def test_machines_migration_splits_nbdboot_by_overlay(tmp_path: Path) -> None:
    """The single ``nbdboot`` mode splits into two: a row with a named
    overlay becomes ``nbdboot-overlay``, a blank-overlay row becomes
    ``nbdboot-ephemeral``. One-time forward migration on store open."""
    import sqlite3

    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE machines (
            mac                    TEXT PRIMARY KEY,
            boot_mode              TEXT NOT NULL DEFAULT 'ipxe-exit',
            image_content_sha256   TEXT NOT NULL DEFAULT '',
            labels                 TEXT NOT NULL DEFAULT '',
            target_disk_serial     TEXT NOT NULL DEFAULT '',
            extra_cmdline          TEXT NOT NULL DEFAULT '',
            overlay_profile        TEXT NOT NULL DEFAULT '',
            overlay_alias          TEXT NOT NULL DEFAULT '',
            inventory_json         TEXT NOT NULL DEFAULT '',
            inventory_at           TEXT NOT NULL DEFAULT '',
            discovered_at          TEXT NOT NULL,
            last_seen_at           TEXT NOT NULL,
            last_seen_ip           TEXT NOT NULL DEFAULT '',
            updated_at             TEXT NOT NULL
        );
        INSERT INTO machines (mac, boot_mode, overlay_alias,
            discovered_at, last_seen_at, updated_at)
        VALUES ('aa:bb:cc:dd:ee:01', 'nbdboot', 'held', 'x', 'x', 'x'),
               ('aa:bb:cc:dd:ee:02', 'nbdboot', '',     'x', 'x', 'x');
        """
    )
    conn.commit()
    conn.close()

    store = MachinesStore(db)
    assert store.get("aa:bb:cc:dd:ee:01").boot_mode == "nbdboot-overlay"  # type: ignore[union-attr]
    assert store.get("aa:bb:cc:dd:ee:02").boot_mode == "nbdboot-ephemeral"  # type: ignore[union-attr]


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
                "nbdboot-ephemeral",
                "nbdboot-overlay",
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
        json={"boot_mode": "legacy-tui"},
    )
    assert r.status_code == 422


def test_put_machine_rejects_bad_content_sha(client: TestClient) -> None:
    r = _authed(client).put(
        "/machines/aa:bb:cc:dd:ee:03",
        json={"boot_mode": "nbdboot-ephemeral", "image_content_sha256": "not-a-sha"},
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


def test_put_machine_persists_labels_target_serial(client: TestClient) -> None:
    """Extended binding fields round-trip through the JSON PUT + GET
    pair and land on ``Machine.to_dict()``. Seeds an inventory with a
    matching disk serial so the flash-mode guard passes.

    Labels ride the bind body for API convenience; the UI bind form
    no longer offers them (edited on their own row on the machine
    detail page). ``sanboot_drive`` is retired: pixie never rendered
    it into an iPXE plan, targets rely on the firmware boot order."""
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
            "target_disk_serial": "S679NX0R123456",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["labels"] == ["rack-3", "gmktec-g5"]
    assert body["target_disk_serial"] == "S679NX0R123456"
    assert "sanboot_drive" not in body

    row = c.get("/machines/aa:bb:cc:dd:ee:20").json()
    assert row["labels"] == ["rack-3", "gmktec-g5"]
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
    """ipxe-exit / nbdboot-ephemeral / pixie-inventory / pixie-tui do not
    touch the target disk; binding them without an inventory succeeds."""
    c = _authed(client)
    for mode in ("ipxe-exit", "nbdboot-ephemeral", "pixie-inventory", "pixie-tui"):
        r = c.put(
            f"/machines/aa:bb:cc:dd:ee:{ord(mode[0]):02x}",
            json={"boot_mode": mode},
        )
        assert r.status_code == 200, f"{mode} unexpectedly rejected: {r.text}"


def test_put_machine_ignores_retired_sanboot_drive(client: TestClient) -> None:
    """The ``sanboot_drive`` field was retired: pixie never rendered
    it into any iPXE plan, targets rely on the firmware boot order.
    A JSON PUT with the (unknown) key is accepted without error, and
    nothing sanboot-related lands on the row."""
    c = _authed(client)
    r = c.put(
        "/machines/aa:bb:cc:dd:ee:21",
        json={"boot_mode": "ipxe-exit", "sanboot_drive": "0x80"},
    )
    assert r.status_code == 200
    assert "sanboot_drive" not in r.json()


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


def test_ui_machines_bind_form_persists_boot_mode(client: TestClient) -> None:
    """UI bind form persists boot_mode. Labels are edited via their
    own row (see /ui/machines/{mac}/labels/edit); sanboot_drive is
    retired. Extra keys posted here are silently ignored."""
    c = _authed(client)
    r = c.post(
        "/ui/machines/bind",
        data={
            "mac": "aa:bb:cc:dd:ee:23",
            "boot_mode": "ipxe-exit",
            "sanboot_drive": "0x80",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    row = c.get("/machines/aa:bb:cc:dd:ee:23").json()
    assert row["boot_mode"] == "ipxe-exit"
    assert "sanboot_drive" not in row
    # No labels supplied on the bind form -> row has no labels.
    assert "labels" not in row


def test_bind_overlay_selects_existing_and_round_trips(client: TestClient) -> None:
    """nbdboot-overlay attaches an EXISTING overlay (created out of band),
    persists the alias, and holds the single-writer lock. Switching the
    machine to nbdboot-ephemeral releases the hold and clears the alias."""
    c = _authed(client)
    state = client.app.state
    from pixie.exports._store import Overlay

    # The overlay must already exist -- the bind never creates it.
    state.overlays_store.upsert(Overlay("simon", "a" * 64, "/tmp/simon.qcow2"))
    mac = "aa:bb:cc:dd:ee:2a"
    r = c.put(
        f"/machines/{mac}",
        json={
            "boot_mode": "nbdboot-overlay",
            "overlay_alias": "simon",
        },
    )
    assert r.status_code == 200
    row = c.get(f"/machines/{mac}").json()
    assert row["overlay_alias"] == "simon"
    ov = state.overlays_store.get("simon")
    assert ov is not None
    assert ov.image_sha == "a" * 64  # implied by the overlay
    assert ov.attached_mac == mac

    # Switch to ephemeral: overlay_alias clears + the hold is released.
    r = c.put(
        f"/machines/{mac}",
        json={
            "boot_mode": "nbdboot-ephemeral",
            "image_content_sha256": "a" * 64,
        },
    )
    assert r.status_code == 200
    row2 = c.get(f"/machines/{mac}").json()
    assert "overlay_alias" not in row2
    assert state.overlays_store.get("simon").attached_mac == ""  # type: ignore[union-attr]


def test_bind_overlay_alias_implies_base_image(client: TestClient) -> None:
    """Attaching an existing overlay overrides the image dropdown: the
    machine binds the overlay's base image, not whatever sha was sent."""
    c = _authed(client)
    state = client.app.state
    from pixie.exports._store import Overlay

    state.overlays_store.upsert(Overlay("shared", "b" * 64, "/tmp/shared.qcow2"))
    mac = "aa:bb:cc:dd:ee:2d"
    r = c.put(
        f"/machines/{mac}",
        json={
            # A different sha is sent; the overlay's base image wins.
            "boot_mode": "nbdboot-overlay",
            "image_content_sha256": "c" * 64,
            "overlay_alias": "shared",
        },
    )
    assert r.status_code == 200
    row = c.get(f"/machines/{mac}").json()
    assert row["overlay_alias"] == "shared"
    assert row["image_content_sha256"] == "b" * 64  # implied by the overlay


def test_bind_overlay_alias_single_writer_rejected(client: TestClient) -> None:
    """An overlay already held by a DIFFERENT machine is single-writer-
    locked: a second machine attaching it is rejected (422) and no bind
    lands."""
    c = _authed(client)
    state = client.app.state
    from pixie.exports._store import Overlay

    state.overlays_store.upsert(
        Overlay("held", "a" * 64, "/tmp/held.qcow2", attached_mac="aa:bb:cc:dd:ee:01")
    )
    other = "aa:bb:cc:dd:ee:02"
    r = c.put(
        f"/machines/{other}",
        json={
            "boot_mode": "nbdboot-overlay",
            "overlay_alias": "held",
        },
    )
    assert r.status_code == 422
    assert "held by" in r.json()["detail"]
    # The hold did not move.
    assert state.overlays_store.get("held").attached_mac == "aa:bb:cc:dd:ee:01"  # type: ignore[union-attr]


def test_bind_overlay_nonexistent_alias_rejected_never_creates(client: TestClient) -> None:
    """The core contract of the split: selecting an overlay never CREATES
    it. Binding nbdboot-overlay to an alias that does not exist is
    rejected (422), and no overlay row is conjured."""
    c = _authed(client)
    state = client.app.state
    r = c.put(
        "/machines/aa:bb:cc:dd:ee:2c",
        json={
            "boot_mode": "nbdboot-overlay",
            "image_content_sha256": "a" * 64,
            "overlay_alias": "ghost",
        },
    )
    assert r.status_code == 422
    assert "does not exist" in r.json()["detail"]
    assert state.overlays_store.list_all() == []


def test_bind_overlay_alias_rejects_bad_chars(client: TestClient) -> None:
    """A malformed alias (``..`` or a slash) is refused before any lookup,
    with no overlay row written."""
    c = _authed(client)
    state = client.app.state
    r = c.put(
        "/machines/aa:bb:cc:dd:ee:2b",
        json={
            "boot_mode": "nbdboot-overlay",
            "overlay_alias": "../etc/passwd",
        },
    )
    assert r.status_code == 422
    # No bogus overlay row was created.
    assert state.overlays_store.list_all() == []


def test_bind_overlay_without_alias_rejected(client: TestClient) -> None:
    """nbdboot-overlay requires an overlay; binding it with none is
    rejected rather than silently falling back to ephemeral."""
    c = _authed(client)
    r = c.put(
        "/machines/aa:bb:cc:dd:ee:2e",
        json={"boot_mode": "nbdboot-overlay"},
    )
    assert r.status_code == 422
    assert "requires an overlay" in r.json()["detail"]


def test_ui_labels_edit_form_persists_and_independent_of_bind(
    client: TestClient,
) -> None:
    """POST /ui/machines/{mac}/labels/edit persists labels without
    touching boot_mode / image / target_disk_serial. A subsequent
    bind form POST leaves those labels intact."""
    c = _authed(client)
    mac = "aa:bb:cc:dd:ee:29"

    # 1. Set labels on a machine before any bind.
    r = c.post(
        f"/ui/machines/{mac}/labels/edit",
        data={"labels": "rack-3, noisy"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    row = c.get(f"/machines/{mac}").json()
    assert row["labels"] == ["rack-3", "noisy"]

    # 2. A follow-up bind form POST (which now has no labels field)
    # must leave the operator's tags alone.
    c.post(
        "/ui/machines/bind",
        data={"mac": mac, "boot_mode": "pixie-tui"},
        follow_redirects=False,
    )
    row2 = c.get(f"/machines/{mac}").json()
    assert row2["boot_mode"] == "pixie-tui"
    assert row2["labels"] == ["rack-3", "noisy"]

    # 3. Blank labels input CLEARS the labels.
    c.post(f"/ui/machines/{mac}/labels/edit", data={"labels": ""})
    row3 = c.get(f"/machines/{mac}").json()
    assert "labels" not in row3


def test_ui_labels_edit_rejects_malformed_label(client: TestClient) -> None:
    """A label that violates :data:`_LABEL_RE` (leading punctuation,
    chars outside ``[A-Za-z0-9 ._-]``, over 64 chars) is rejected with a
    flash + 303 back to the machine page -- NOT a raw 400 JSON, which
    would eject the operator out of the HTML UI. State must not apply."""
    c = _authed(client)
    mac = "aa:bb:cc:dd:ee:30"

    r = c.post(
        f"/ui/machines/{mac}/labels/edit",
        data={"labels": "@bad"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/ui/machines/{mac}"
    # State must not have partially applied: the machine row still
    # has no labels field.
    row = c.get(f"/machines/{mac}").json()
    assert "labels" not in row


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


def test_pxe_plan_nbdboot_without_bound_image_falls_back(client: TestClient) -> None:
    """Binding nbdboot without a fetched image renders the
    ``unavailable`` template with the reason baked in the comment;
    the target does NOT boot a mismatched kernel."""
    c = _authed(client)
    mac = "de:ad:be:ef:00:01"
    c.put(f"/machines/{mac}", json={"boot_mode": "nbdboot-ephemeral"})
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


def test_touch_seen_defaults_to_pixie_inventory(tmp_path: Path) -> None:
    """A freshly-discovered MAC auto-registers with the default boot
    mode -- now pixie-inventory (non-destructive + useful), not the old
    ipxe-exit no-op."""
    store = MachinesStore(tmp_path / "state.db")
    m = store.touch_seen("aa:bb:cc:dd:ee:01")
    assert m.boot_mode == "pixie-inventory"


def test_touch_seen_honours_default_and_rejects_unknown(tmp_path: Path) -> None:
    store = MachinesStore(tmp_path / "state.db")
    assert store.touch_seen("aa:bb:cc:dd:ee:02", default_boot_mode="ipxe-exit").boot_mode == (
        "ipxe-exit"
    )
    # An unknown value can't seed an unrenderable mode on every new MAC.
    assert store.touch_seen("aa:bb:cc:dd:ee:03", default_boot_mode="bogus").boot_mode == (
        "pixie-inventory"
    )


def test_resolve_default_boot_mode_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from pixie.web.main import _resolve_default_boot_mode

    monkeypatch.delenv("PIXIE_DEFAULT_BOOT_MODE", raising=False)
    assert _resolve_default_boot_mode() == "pixie-inventory"
    monkeypatch.setenv("PIXIE_DEFAULT_BOOT_MODE", "ipxe-exit")
    assert _resolve_default_boot_mode() == "ipxe-exit"
    monkeypatch.setenv("PIXIE_DEFAULT_BOOT_MODE", "bogus")
    assert _resolve_default_boot_mode() == "pixie-inventory"


def test_inventory_post_keeps_mode_renderer_serves_exit(client: TestClient) -> None:
    """Inventory is one-shot, but the boot_mode is NOT auto-flipped: it
    stays ``pixie-inventory`` (operator intent). The renderer serves an
    exit plan for a pixie-inventory machine that already reported, so a
    PXE-first box neither re-inventories + boot-loops nor has its mode
    changed behind the operator's back."""
    mac = "aa:bb:cc:dd:ee:f0"
    client.get(f"/pxe/{mac}")  # discovery -> pixie-inventory default
    assert client.get(f"/machines/{mac}").json()["boot_mode"] == "pixie-inventory"

    r = client.post(
        f"/pxe/{mac}/inventory", json={"lshw": {"x": 1}, "disks": [{"path": "/dev/sda"}]}
    )
    assert r.status_code == 204
    # Mode is unchanged (no auto-flip), and the inventory is kept.
    assert client.get(f"/machines/{mac}").json()["boot_mode"] == "pixie-inventory"
    assert client.get(f"/machines/{mac}/inventory").json()["inventory"]["disks"]
    # The renderer now serves the exit plan (its comment carries the
    # ipxe-exit marker) so the box boots its local disk, not inventory.
    assert "boot_mode=ipxe-exit" in client.get(f"/pxe/{mac}").text


def test_re_inventory_clears_inventory_and_reserves_the_pass(client: TestClient) -> None:
    """The Re-inventory action drops the stored inventory so the machine
    re-runs the pixie-inventory pass on its next PXE (no longer the exit
    plan), while its boot_mode stays pixie-inventory."""
    c = _authed(client)
    mac = "aa:bb:cc:dd:ee:f3"
    client.get(f"/pxe/{mac}")
    client.post(f"/pxe/{mac}/inventory", json={"lshw": {}, "disks": [{"path": "/dev/sda"}]})
    assert "boot_mode=ipxe-exit" in client.get(f"/pxe/{mac}").text  # exit while inventory present

    r = c.post("/ui/machines/re-inventory", data={"mac": mac}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get(f"/machines/{mac}/inventory").status_code == 404  # inventory dropped
    assert client.get(f"/machines/{mac}").json()["boot_mode"] == "pixie-inventory"  # mode kept
    # No longer the exit plan: it re-runs inventory (or degrades to
    # unavailable when no live env is staged) -- either way not exit.
    assert "boot_mode=ipxe-exit" not in client.get(f"/pxe/{mac}").text


def test_ui_machine_delete_emits_event_and_releases_overlay_hold(client: TestClient) -> None:
    """Deleting via the HTML form now takes the SAME path as the JSON
    API: it emits machine.deleted (the UI used to skip it) and releases
    any overlay single-writer hold the machine held (so a deleted MAC
    can't orphan an overlay)."""
    from pixie.exports._store import Overlay

    c = _authed(client)
    state = client.app.state
    mac = "aa:bb:cc:dd:ee:5a"
    state.overlays_store.upsert(Overlay("held5a", "a" * 64, "/tmp/held5a.qcow2", attached_mac=mac))
    state.machines_store.upsert_binding(
        mac, boot_mode="nbdboot-overlay", image_content_sha256="a" * 64, overlay_alias="held5a"
    )

    r = c.post("/ui/machines/delete", data={"mac": mac}, follow_redirects=False)
    assert r.status_code == 303
    assert state.machines_store.get(mac) is None
    assert state.overlays_store.get("held5a").attached_mac == ""  # type: ignore[union-attr]
    kinds = [e.kind for e in state.events_log.list(limit=50)]
    assert "machine.deleted" in kinds


def test_inventory_post_does_not_flip_a_non_inventory_mode(client: TestClient) -> None:
    """A machine an operator put on, say, nbdboot-ephemeral is left alone
    -- only pixie-inventory is one-shot."""
    c = _authed(client)
    mac = "aa:bb:cc:dd:ee:f1"
    c.put(f"/machines/{mac}", json={"boot_mode": "nbdboot-ephemeral"})
    client.post(f"/pxe/{mac}/inventory", json={"lshw": {}, "disks": []})
    assert client.get(f"/machines/{mac}").json()["boot_mode"] == "nbdboot-ephemeral"


def test_session_secret_is_stable_and_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session signing key must be stable across create_app() calls
    (a fresh key each time logs every operator out on every restart).
    It persists under the state dir and an env override wins."""
    from pixie.web.main import _resolve_session_secret

    monkeypatch.delenv("PIXIE_SESSION_SECRET", raising=False)
    first = _resolve_session_secret(tmp_path)
    assert first  # non-empty
    assert _resolve_session_secret(tmp_path) == first  # stable via the persisted file
    assert (tmp_path / "session_secret").read_text(encoding="utf-8").strip() == first

    monkeypatch.setenv("PIXIE_SESSION_SECRET", "operator-supplied-key")
    assert _resolve_session_secret(tmp_path) == "operator-supplied-key"  # env wins


def test_ui_form_missing_field_redirects_not_json(client: TestClient) -> None:
    """A missing required Form field on a /ui/* POST comes back as a 303
    redirect (with a flash), not a raw 422 JSON that ejects the operator
    out of the HTML UI."""
    c = _authed(client)
    r = c.post(
        "/ui/machines/aa:bb:cc:dd:ee:42/labels/edit",
        data={},  # missing the required ``labels`` field
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/")
