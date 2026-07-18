# Boot modes

Every machine pixie has seen is bound to exactly one of six boot
modes. On the target's next PXE request, pixie renders an iPXE plan
whose shape depends on the mode. The bind form on the machine detail
page picks the mode by clicking a card; the JSON API accepts the same
tokens.

## The six modes

### `ipxe-exit` (default)

Pixie exits the iPXE chain. The target's BIOS boot order picks the
next bootable device. This is the safe default for a fresh MAC:
pixie won't reflash a machine or attempt to netboot it until an
operator has explicitly bound it.

### `pixie-inventory`

The target boots pixie's own live env. The `pixie` CLI collects
disk + NIC + lshw output and POSTs it to `/pxe/<mac>/inventory`,
then the target reboots to firmware. Prerequisite for
`pixie-flash-*`: without inventory, pixie doesn't know which disk
serial to hand the flash pipeline.

### `pixie-tui`

The target boots pixie's live env into an interactive TUI. An
operator drives the wizard on the target's own console (or IP-KVM /
serial-over-LAN). Useful for one-off image picks that don't need
recording on pixie's side.

### `pixie-flash-once`

The target boots pixie's live env, which writes the bound image to
the picked disk serial, then reboots to firmware. Pixie flips the
binding to `ipxe-exit` after a successful flash so the next PXE
lands the target on its freshly-flashed local disk. Requires
inventory + a target disk serial.

### `pixie-flash-always`

Same as `pixie-flash-once` but pixie leaves the binding on
`pixie-flash-always` after the flash. Every PXE re-flashes the
image; any local changes between reboots are discarded. Useful
for CI reflash loops where the target is expected to boot from a
known-clean image each cycle.

### `nbdboot`

The target boots the image's OWN kernel (extracted from the sibling
netboot bundle) and mounts the image over NBD. Root is an
overlay-on-tmpfs: writes go to RAM, nothing propagates back to the
source blob. Multiple targets can nbdboot the same image
simultaneously because they each get their own overlay.

## Prerequisites at a glance

| Mode | Needs image | Needs inventory | Needs target disk |
|------|-------------|-----------------|-------------------|
| `ipxe-exit` | no | no | no |
| `pixie-inventory` | no | no | no |
| `pixie-tui` | no | no | no |
| `pixie-flash-once` | yes | yes | yes |
| `pixie-flash-always` | yes | yes | yes |
| `nbdboot` | yes (with sibling netboot bundle) | no | no |

The bind form on the machine detail page enforces these prerequisites
client-side; the server enforces them again with a 422 on the
`PUT /machines/{mac}` endpoint.

## Extra kernel cmdline

Every mode that boots pixie's live env or the nbdboot chain accepts
extra kernel-cmdline tokens. Deploy-wide default:
`PIXIE_LIVE_ENV_EXTRA_CMDLINE` in the container env. Per-machine
override on the bind form: the `Extra kernel cmdline` text field.
A non-blank per-machine value fully overrides the deploy-wide default
for that ONE machine, so hardware quirks stay scoped to the target
that needs them. See [](hardware-quirks.md) for known-good tokens.
