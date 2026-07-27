# Booting the usbboot `.iso`

Pixie ships a bootable `.iso` (`pixie-usbboot-pc-x86_64.iso`, a GitHub
release asset) that carries the pixie live env on removable media
instead of over the network. It's the same flash / inventory / operator
wizard as the netboot live env, for when a target isn't wired into
pixie's DHCP chain, has no PXE, or you just want an image-on-a-stick.

It's an Arch Linux live image (built with `mkarchiso`); the kernel +
full linux-firmware carry the NIC/GPU drivers in-tree, so no per-driver
DKMS. It boots on BIOS and UEFI, from a USB stick or from a CD/virtual
CD.

```{note}
The live env runs from a RAM-backed overlay, so the target needs **at
least 2 GiB of RAM** to boot the ~1.3 GiB image. Any real server or PC
has plenty; a memory-constrained box may not reach the wizard. And
**Secure Boot must be disabled** (see [](deployment.md#dhcp-handoff)) —
the image isn't signed for it.
```

## Mount via BMC virtual media (Redfish / PiKVM / JetKVM)

The headline path: mount the `.iso` as a virtual CD through the target's
BMC and boot it, no physical media. The exact clicks vary by BMC, but
the shape is the same:

1. Host the `.iso` somewhere the BMC can reach over HTTP (or upload it
   through the BMC's own web UI). For Redfish `InsertMedia`, the server
   must speak HTTP/1.1 with byte-range support — a plain
   `python -m http.server` (HTTP/1.0, no ranges) fails the mount on some
   BMCs; use nginx / darkhttpd or the BMC's upload.
2. Attach it as **Virtual Media -> CD/DVD** (Redfish:
   `VirtualMedia.InsertMedia` on the manager's CD slot).
3. Set one-time boot to the virtual CD (UEFI) and power-cycle.

It boots straight into the pixie live env on the console. Proven on
Supermicro (needs the OOB/DCMS license for virtual media) and any
PiKVM / JetKVM that exposes a virtual CD.

## Write it to a USB stick

`dd` the image to a stick (BIOS + UEFI bootable, isohybrid):

```
dd if=pixie-usbboot-pc-x86_64.iso of=/dev/sdX bs=4M oflag=direct status=progress
```

Or open it in Balena Etcher / Rufus (DD mode) / Raspberry Pi Imager, or
drop it onto a Ventoy stick alongside other rescue ISOs.

## The `PIXIE_IMGS` partition

The `.iso` carries a writable exFAT partition labelled `PIXIE_IMGS`
(32 MiB at bake time, auto-grown to fill the stick on first boot). Drop
image files (`.qcow2` / `.img.gz` / `.img` / `.iso`) onto it — or a
`catalog.toml` + a `pixie-images/` folder — from any host, and the live
env mounts it at `/var/lib/pixie/images` and offers those images in the
wizard. It's the offline equivalent of a fetched catalog. When booted
via a loop-mount shim (Ventoy, some BMCs) that hides the internal
partition, the live env instead scans the surrounding stick for the same
`pixie-images/` layout.

## What you get on boot

The live env drops the operator into the pixie TUI on tty1 (Alt+F2..F6
are root shells for diagnostics; ssh is up as `root` / `pixie` on a
trusted segment). Two modes:

- **Local / USB.** No `pixie.server` on the kernel cmdline: the wizard
  scans the local image-root (the `PIXIE_IMGS` partition or the seed
  catalog) and walks you through catalog -> image -> disk -> flash ->
  reboot.
- **Server-driven.** When chained from a pixie server's iPXE plan with
  `pixie.server=` + `pixie.mac=`, the same live env fetches
  `<server>/pxe/<mac>/plan` and does what the binding says (auto-flash,
  interactive, inventory, or exit).

See [](concepts.md) for how images + the live env fit together, and
[](boot-modes.md) for the flash / inventory / nbdboot modes.
