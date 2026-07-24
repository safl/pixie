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

**Persistent overlays** flip a target from ephemeral to dev mode
without changing anything else about the bind. On the machine detail
page, the `Overlay alias` field is blank by default (ephemeral,
unchanged behaviour) or names a persistent overlay volume (e.g.
`simon`, `karl`, `ci-with-nvme-tools`). A non-blank alias attaches a
qcow2 volume with the image's base blob as `backing_file`, served by
`qemu-nbd` at a dedicated port. The target mounts the NBD device
read-write; system-level changes (apt-installed packages,
hardware-specific config, kernel modules) land on the qcow2 and
survive reboots.

An overlay is a **globally-unique named volume over one base image**,
not a per-machine file. See [](#overlays-are-volumes-not-per-machine-files)
below for how the alias, its base image, and single-writer access
work.

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

## Overlays are volumes, not per-machine files

An overlay is a **globally-unique named writable volume** layered over
exactly one base image. The alias is the identity, not the machine:
`data/overlays/<alias>.qcow2`, a single qcow2 whose `backing_file` is
the image's base blob. There is no `(mac, image, profile)` key and no
per-machine directory; two machines cannot mint independent files
under the same name, because the name is the volume.

**The alias implies its image.** Attaching an *existing* alias binds
the machine to that overlay's base image; you don't re-pick the image
on the bind form, the volume already knows it. Attaching a *new* alias
takes the image you selected and lays a fresh qcow2 over it. Because
the alias carries the image, moving a volume between machines is just
re-binding the alias; the base image follows.

**Single-writer, enforced in the app.** At most one machine holds an
alias at a time. Binding an alias already attached to another machine
is refused ("overlay `<alias>` held by `<mac>`; detach first") on both
the operator form and the `PUT /machines/{mac}` API, before any qcow2
is touched. The qemu qcow2 lock is the backstop; the app-level check
is the operator-facing guard. Rebinding a machine to ephemeral, to a
different alias, or to a non-`nbdboot` mode releases the hold it had.

**Managing volumes.** The Overlays page (`/ui/overlays`) lists every
volume with its state: *serving* (a live machine is bound to it and
`qemu-nbd` is up), *held* (attached to a machine, nothing serving),
*free* (unattached, kept for a future bind), *orphaned* (attached to a
MAC with no machine row), or *missing* (the qcow2 is gone). **Reset**
tears down `qemu-nbd` and unlinks one volume's qcow2 so the next boot
lazy-creates it fresh from the base; **Prune** reclaims the junk
states (orphaned + missing) in bulk and deliberately leaves free
volumes alone.

## Bindings

A binding is what pixie serves a MAC next time it PXEs: a boot mode,
plus (for the modes that need one) an image, plus (for `nbdboot`) an
optional overlay alias. A fresh MAC auto-registers as `ipxe-exit` with
no image on first contact; the machine detail page is where an
operator promotes it. Saving the bind form persists all three fields
together, and the plan renderer reads them per PXE request: it resolves
the image's netboot bundle, resolves the alias to its overlay (and
base image), ensures the NBD export is up, and renders the plan. A
binding that can't be satisfied (no image bound, bundle not fetched,
alias held elsewhere) degrades to an `unavailable` plan whose comment
names the reason, rather than serving a broken boot.

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
