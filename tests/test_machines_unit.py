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
    row = MachinesStore(db).get("aa:bb:cc:dd:ee:00")
    assert row is not None
    assert row.overlay_alias == "safl-aa-bb-cc-dd-ee-00"


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
                "nbdboot",
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
        json={"boot_mode": "nbdboot", "image_content_sha256": "not-a-sha"},
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
    """ipxe-exit / nbdboot / pixie-inventory / pixie-tui do not touch
    the target disk; binding them without an inventory succeeds."""
    c = _authed(client)
    for mode in ("ipxe-exit", "nbdboot", "pixie-inventory", "pixie-tui"):
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


def test_bind_overlay_alias_round_trips(client: TestClient) -> None:
    """PUT /machines/{mac} with overlay_alias creates the overlay over
    the selected base image, persists the alias on the row, and holds
    the single-writer lock; the ephemeral (blank alias) case is the
    default and releases the hold."""
    c = _authed(client)
    state = client.app.state
    mac = "aa:bb:cc:dd:ee:2a"
    r = c.put(
        f"/machines/{mac}",
        json={
            "boot_mode": "nbdboot",
            "image_content_sha256": "a" * 64,
            "overlay_alias": "simon",
        },
    )
    assert r.status_code == 200
    row = c.get(f"/machines/{mac}").json()
    assert row["overlay_alias"] == "simon"
    # The overlay row exists over the selected image and this MAC holds it.
    ov = state.overlays_store.get("simon")
    assert ov is not None
    assert ov.image_sha == "a" * 64
    assert ov.attached_mac == mac

    # Blank overlay_alias clears the field (round-trip absent) + frees it.
    r = c.put(
        f"/machines/{mac}",
        json={
            "boot_mode": "nbdboot",
            "image_content_sha256": "a" * 64,
            "overlay_alias": "",
        },
    )
    assert r.status_code == 200
    row2 = c.get(f"/machines/{mac}").json()
    assert "overlay_alias" not in row2
    assert state.overlays_store.get("simon").attached_mac == ""  # type: ignore[union-attr]


def test_bind_overlay_alias_implies_base_image(client: TestClient) -> None:
    """Attaching an EXISTING alias overrides the image dropdown: the
    machine binds the overlay's base image, not whatever sha was sent."""
    c = _authed(client)
    state = client.app.state
    from pixie.exports._store import Overlay

    state.overlays_store.upsert(Overlay("shared", "b" * 64, "/tmp/shared.qcow2"))
    mac = "aa:bb:cc:dd:ee:2d"
    r = c.put(
        f"/machines/{mac}",
        json={
            # A different (even blank-ish) sha is sent; the alias wins.
            "boot_mode": "nbdboot",
            "image_content_sha256": "c" * 64,
            "overlay_alias": "shared",
        },
    )
    assert r.status_code == 200
    row = c.get(f"/machines/{mac}").json()
    assert row["overlay_alias"] == "shared"
    assert row["image_content_sha256"] == "b" * 64  # implied by the alias


def test_bind_overlay_alias_single_writer_rejected(client: TestClient) -> None:
    """An alias already held by a DIFFERENT machine is single-writer-
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
            "boot_mode": "nbdboot",
            "image_content_sha256": "a" * 64,
            "overlay_alias": "held",
        },
    )
    assert r.status_code == 422
    assert "held by" in r.json()["detail"]
    # The hold did not move.
    assert state.overlays_store.get("held").attached_mac == "aa:bb:cc:dd:ee:01"  # type: ignore[union-attr]


def test_bind_overlay_alias_rejects_bad_chars(client: TestClient) -> None:
    """A new alias lands on disk as ``data/overlays/<alias>.qcow2``, so a
    name with ``..`` or a slash could escape the tree. Reject before any
    row or path is written."""
    c = _authed(client)
    state = client.app.state
    r = c.put(
        "/machines/aa:bb:cc:dd:ee:2b",
        json={
            "boot_mode": "nbdboot",
            "image_content_sha256": "a" * 64,
            "overlay_alias": "../etc/passwd",
        },
    )
    assert r.status_code == 422
    # No bogus overlay row was created.
    assert state.overlays_store.list_all() == []


def test_ui_bind_form_maps_overlay_alias_new_to_new_name(
    client: TestClient,
) -> None:
    """The bind form select carries a magic ``__new`` value that tells
    the handler to pull the alias name from the sibling
    ``overlay_alias_new`` text field. Exercise the merge so an operator
    using the picker's create-new flow lands the fresh alias without a
    JSON round-trip."""
    c = _authed(client)
    mac = "aa:bb:cc:dd:ee:2c"
    r = c.post(
        "/ui/machines/bind",
        data={
            "mac": mac,
            "boot_mode": "nbdboot",
            "image_content_sha256": "a" * 64,
            "overlay_alias": "__new",
            "overlay_alias_new": "karl",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    row = c.get(f"/machines/{mac}").json()
    assert row["overlay_alias"] == "karl"


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
    chars outside ``[A-Za-z0-9 ._-]``, over 64 chars) surfaces as
    400 with the parser's message in ``detail``. Silent-suppress
    was the previous shape and made "why did my label edit not
    take" a real debug ratdance on the operator side."""
    c = _authed(client)
    mac = "aa:bb:cc:dd:ee:30"

    r = c.post(
        f"/ui/machines/{mac}/labels/edit",
        data={"labels": "@bad"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "alphanumeric-leading" in r.json()["detail"]
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
    c.put(f"/machines/{mac}", json={"boot_mode": "nbdboot"})
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
