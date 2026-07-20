"""Unit tests for ``pixie.web._bind_preview.bind_preview_text``.

The function is the single-source-of-truth for the plain-English
sentence rendered under "On the next PXE from this target" on the
machine detail page. Regressions here silently degrade the operator
UI to a bare hyphen (the pre-fix state), so we lock the shape of
every branch: empty mode, exit modes, image-needing modes with and
without an image, flash modes with and without a disk, and the two
nbdboot flavours (ephemeral vs persistent-overlay).
"""

from __future__ import annotations

from pixie.web._bind_preview import bind_preview_text


def _call(**kwargs: str) -> str:
    defaults: dict[str, str] = {
        "boot_mode": "",
        "image_name": "",
        "disk_label": "",
        "overlay_profile": "",
    }
    defaults.update(kwargs)
    return bind_preview_text(**defaults)


def test_blank_boot_mode_shows_pick_prompt() -> None:
    assert _call(boot_mode="") == "Pick a boot mode above to see what happens."


def test_ipxe_exit_needs_no_image() -> None:
    assert "BIOS boot order" in _call(boot_mode="ipxe-exit")


def test_pixie_inventory_narrates_the_flow() -> None:
    text = _call(boot_mode="pixie-inventory")
    assert "posts disk + NIC inventory back" in text


def test_pixie_tui_names_the_console() -> None:
    text = _call(boot_mode="pixie-tui")
    assert "interactive TUI" in text


def test_flash_once_without_image_prompts_for_pick() -> None:
    text = _call(boot_mode="pixie-flash-once")
    assert "Pick an image above" in text


def test_flash_once_with_image_and_disk_interpolates() -> None:
    text = _call(
        boot_mode="pixie-flash-once",
        image_name="nosi ubuntu-2604-headless",
        disk_label="/dev/disk/by-id/foo",
    )
    assert "nosi ubuntu-2604-headless" in text
    assert "/dev/disk/by-id/foo" in text


def test_flash_always_without_disk_flags_it() -> None:
    text = _call(boot_mode="pixie-flash-always", image_name="nosi debian-13-headless")
    assert "-not picked-" in text


def test_nbdboot_ephemeral_says_overlay_on_tmpfs() -> None:
    text = _call(boot_mode="nbdboot", image_name="nosi ubuntu-2604-headless")
    assert "overlay-on-tmpfs" in text


def test_nbdboot_persist_names_the_profile() -> None:
    text = _call(
        boot_mode="nbdboot",
        image_name="nosi ubuntu-2604-headless",
        overlay_profile="simon",
    )
    assert "simon" in text
    assert "qcow2 overlay" in text
    assert "overlay-on-tmpfs" not in text


def test_nbdboot_without_image_prompts_for_pick_even_persist() -> None:
    text = _call(boot_mode="nbdboot", overlay_profile="simon")
    assert "Pick an image above" in text


def test_unknown_mode_falls_back_to_the_mode_string() -> None:
    text = _call(boot_mode="frobnicate-mode")
    assert text == "frobnicate-mode"
