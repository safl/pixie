"""Plain-English narration of what a machine will do on its next PXE.

Rendered server-side into the machine detail page's preview panel so
an operator sees a real sentence on page load, not a bare placeholder
that only fills in after the client-side JS observes a bind-form
change. The client-side ``MODE_PREVIEWS`` table in
``machine_detail.html`` mirrors this text so the two agree on any
subsequent update; keeping the sentences in sync is a manual matter
until we route the JS through a JSON endpoint that reads this module.

Shape: ``bind_preview_text`` takes the fields the bind form observes
(``boot_mode``, ``image_name``, ``disk_label``, ``overlay_profile``)
and returns a single sentence. Missing prerequisites are called out
in-line so the operator sees "pick an image above" rather than a
sentence with a bare ``{IMAGE}`` placeholder.
"""

from __future__ import annotations

_IMAGE_MODES: frozenset[str] = frozenset({"nbdboot", "pixie-flash-once", "pixie-flash-always"})
_FLASH_MODES: frozenset[str] = frozenset({"pixie-flash-once", "pixie-flash-always"})

_MODE_PREVIEWS: dict[str, str] = {
    "ipxe-exit": (
        "Pixie exits the iPXE chain. The BIOS boot order picks the "
        "next bootable device on this target."
    ),
    "pixie-inventory": (
        "Pixie boots its live env; the pixie CLI posts disk + NIC "
        "inventory back, then the target reboots to firmware."
    ),
    "pixie-tui": (
        "Pixie boots its live env into an interactive TUI. An operator "
        "drives the wizard on the target's console."
    ),
    "pixie-flash-once": (
        "Pixie boots its live env, which writes {IMAGE} to disk "
        "(serial: {DISK}), then the target reboots to firmware."
    ),
    "pixie-flash-always": (
        "Pixie boots its live env, which re-writes {IMAGE} to disk "
        "(serial: {DISK}) on every PXE. Any local changes are lost."
    ),
    "nbdboot_ephemeral": (
        "Pixie streams {IMAGE} over NBD; root is an overlay-on-tmpfs "
        "of the image. Nothing writes back to the source."
    ),
    "nbdboot_persist": (
        "Pixie streams {IMAGE} over NBD onto this target's "
        "{PROFILE} qcow2 overlay. System changes on the target "
        "survive reboots; other machines are untouched."
    ),
}


def bind_preview_text(
    *,
    boot_mode: str,
    image_name: str,
    disk_label: str,
    overlay_profile: str,
) -> str:
    """Return the plain-English preview sentence for the given bind."""
    if not boot_mode:
        return "Pick a boot mode above to see what happens."

    if boot_mode == "nbdboot":
        template_key = "nbdboot_persist" if overlay_profile else "nbdboot_ephemeral"
    else:
        template_key = boot_mode

    text = _MODE_PREVIEWS.get(template_key, boot_mode)

    if boot_mode in _IMAGE_MODES and not image_name:
        return "Pick an image above; this mode needs one."
    text = text.replace("{IMAGE}", image_name) if image_name else text

    if boot_mode in _FLASH_MODES:
        text = text.replace("{DISK}", disk_label or "-not picked-")

    if overlay_profile:
        text = text.replace("{PROFILE}", overlay_profile)

    return text
