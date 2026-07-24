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
netboot bundle) and mounts the image over NBD. By default, root is an
overlay-on-tmpfs: writes go to RAM, nothing propagates back to the
source blob. Multiple targets can nbdboot the same image
simultaneously because they each get their own overlay.

**Persistent overlays** flip a single target from ephemeral to dev
mode without changing anything else about the bind. An overlay is a
named writable volume over one base image, not something a machine
owns: its identity is a globally-unique `alias`, and the base image is
implied by the alias. On the machine detail page, the `Overlay` field
is blank by default (ephemeral, unchanged behaviour), or you pick a
free alias, or create a new one (a name plus the base image). The
overlay is a qcow2 with the image's base blob as `backing_file`,
served by `qemu-nbd` at a dedicated port; the target mounts it
read-write, so system-level changes (apt-installed packages,
hardware-specific config, kernel modules) land on the qcow2 and
survive reboots.

An overlay is single-writer: at most one machine may hold an alias at
a time. Binding a machine to an alias another machine already holds is
rejected in the app ("held by `<mac>`; detach first"), with
qemu-nbd's qcow2 image-lock as the backstop. Storage is a flat
`data/overlays/<alias>.qcow2`. Manage overlays on the **Overlays**
page: it keys on the alias, shows an `Attached to` column (a MAC or
`free`), and classifies each as serving / held / free / orphaned /
missing. The **Reset** button tears down `qemu-nbd`, unlinks the
qcow2, and lets the next boot lazy-create a fresh overlay from the
base; **Prune** reclaims only the orphaned (holder gone) + missing
(file gone) ones, leaving a `free` alias (a deliberate keep) alone.

Because the alias is decoupled from the machine, an overlay can be
handed to a different target: detach it from one machine, bind another
to the same alias, and the volume moves with its accumulated writes.

**Kexec into a locally-installed kernel.** The netboot bundle owns
the kernel and initrd pixie serves. Installing `linux-image-*` on the
target's persistent overlay writes files to `/boot` but the next
power-cycle refetches pixie's kernel and those files sit unused.
`kexec` bridges that gap: the netboot kernel comes up, then the
operator runs
`kexec -l /boot/vmlinuz-<v> --initrd=/boot/initrd.img-<v>
--reuse-cmdline && systemctl kexec` to switch to the local kernel
without going through firmware. Every netboot-shipping nosi variant
now bakes `kexec-tools` in; see nosi's [Custom kernel under netboot
(kexec)](https://safl.github.io/nosi/kexec.html) for the full
workflow. Recovery from a bad kernel is a power-cycle back to
pixie's kernel; nothing kexec's automatically, so a broken install
never becomes a boot loop.

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
