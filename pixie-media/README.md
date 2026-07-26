# pixie-media

Source content for the pixie media images. Two variants:

- **USB live image** (`VARIANT=usbboot-pc`) - bootable USB carrying the
  pixie runtime + a writable exFAT `PIXIE_IMGS` partition for pre-built
  images, for ad-hoc flash-install of images. Built via Debian's
  live-build (`iso-hybrid` output); shipped gzip-compressed as
  `pixie-usbboot-pc-x86_64.iso.gz` (Etcher / Rufus / Raspberry Pi
  Imager all decompress `.gz` natively; xz tripped Etcher's bundled
  handler regardless of preset).
- **Raspberry-Pi USB flasher** (`VARIANT=usbboot-rpi`) - arm64 image that
  boots a Pi 4 / CM4 / Pi 5 / CM5 from a USB stick into the same pixie
  TUI as `usbboot-pc`, sized for in-situ flashing of local eMMC / NVMe.
  Unlike the x86 variants this is NOT a live image: it customizes the
  official Raspberry Pi OS Lite (arm64) image in place (download +
  loop-mount + chroot), so it inherits every Pi kernel + `bcm*.dtb`
  (incl. the CM5 / CM5IO device trees) + firmware and boots every
  supported board with no per-board branching. Shipped gzip-compressed
  as `pixie-usbboot-rpi-arm64.img.gz`.

This directory holds the **content** baked into the images: the rootfs
trees that live-build folds in and the live-build config tree. The cijoe
**orchestration** (configs, tasks, scripts) that consumes this
content lives at `cijoe/` at the repo root.

Operators drive everything via the top-level Makefile:
`make build VARIANT=usbboot-pc|usbboot-rpi`.

## Layout

- `auxiliary/cloudinit-metadata.meta` - shared cloud-init metadata.
- `rootfs/common/` - files baked into every variant.
- `live-build/` - live-build config tree used by the x86 usbboot-pc
  variant. The ``PIXIE_VARIANT`` env var selects the shape:
  ``usbboot-pc`` -> amd64 iso-hybrid. (``usbboot-rpi`` does not use live-build; it reuses this tree's
  ``includes.chroot/`` + ``config/hooks/`` inside an RPiOS chroot.)

## Pipeline

From the repo root:

```
make build VARIANT=usbboot-pc|usbboot-rpi
```

dispatches to one of two cijoe task files. The Makefile picks the
right one based on the variant:

- `usbboot-pc` -> `cijoe tasks/usbboot-pc.yaml`. Drives Debian's `live-build`
  with `PIXIE_VARIANT=usbboot-pc` selecting `iso-hybrid` output, then
  post-processes the pre-built ISO to append a writable exFAT
  `PIXIE_IMGS` partition (`sfdisk --append`, `losetup -fP`,
  `mkfs.exfat`) and gzip-compresses it. Output is
  `pixie-usbboot-pc-x86_64.iso.gz`. No QEMU full-system bake.

- `usbboot-rpi` -> `cijoe tasks/usbboot-rpi.yaml`. Does NOT use live-build.
  `scripts/rpios_image_build.py` downloads Raspberry Pi OS Lite
  (arm64) on a native arm64 builder, grows the root for headroom,
  loop-mounts + chroots, installs the pixie runtime + flash tooling,
  drops in this tree's `includes.chroot/` and runs the pixie
  `config/hooks/` verbatim, masks RPiOS's first-boot user wizard so
  the box boots straight into the pixie TUI, then gzips the raw image.
  Output is `pixie-usbboot-rpi-arm64.img.gz`. Operator dd's it to a USB
  stick and boots a Pi 4 / CM4 / Pi 5 / CM5 from it.

The x86 variants stage the pixie-lab wheel via `pixie_wheel_stage` into
the live-build chroot includes, then drive live-build via
`live_build` (usbboot-pc additionally runs `usb_iso_build` for the
exFAT `PIXIE_IMGS` post-processing). usbboot-rpi also runs
`pixie_wheel_stage` first, then `rpios_image_build` consumes the same
staged wheel inside the RPiOS chroot.

## Build prerequisites

All three variants (live-build):
- `live-build` (`sudo apt install live-build`)
- `debootstrap`, `squashfs-tools`, `xorriso` (pulled in by
  `live-build`'s recommends, or install explicitly)
- `exfatprogs` for the usbboot-pc post-build PIXIE_IMGS exFAT step
  (`mkfs.exfat`)
- `xz-utils` for compressing the final usbboot-pc artifact (always
  present on Ubuntu/Debian; listed for completeness)
- `uv` for `pixie_wheel_stage` to build the pixie-lab wheel; install
  with `pipx install uv` if needed
- Passwordless `sudo` - live-build's chroot operations are
  privileged; CI runners have NOPASSWD by default

All variants:
- `cijoe` (install via `make media-deps`, which runs `pipx install cijoe`)

## Output

usbboot-pc:
- `~/system_imaging/disk/pixie-usbboot-pc-x86_64.iso.gz` - final artifact.
  Open in Balena Etcher / Raspberry Pi Imager / Rufus DD-mode
  (those tools decompress `.gz` natively), or pipe via CLI:
  `gunzip -d --stdout pixie-usbboot-pc-x86_64.iso.gz | sudo dd of=/dev/sdX bs=4M`.
  Decompress to `.iso` first (`gunzip ...`) before dropping onto a
  Ventoy stick; Ventoy doesn't auto-decompress.

## Status

Both variants ship on every tagged release at
[the GitHub releases page](https://github.com/safl/pixie/releases).
The end-to-end PXE chain test (``make test-pxe``) gates each release
on usbboot-pc building cleanly and the chain working end to end. Most operators never run this build pipeline themselves -
``pixie-media/`` exists for contributors who want to modify the image.

- **usbboot-pc.** Hybrid ISO that boots into a Debian live environment
  with the `pixie` wizard installed into `/opt/pixie/venv`, and an
  exFAT `PIXIE_IMGS` partition for pre-built images. live-boot's
  SquashFS + tmpfs overlay provides the ephemeral rootfs (no
  `overlayroot` package needed). End-to-end use case in
  [`docs/src/tutorials/pixie-usbboot-pc.md`](../docs/src/tutorials/pixie-usbboot-pc.md).
- **usbboot-rpi.** arm64 Pi-bootable raw image (FAT32 firmware + ext4
  live squashfs + auto-growing exFAT `PIXIE_IMGS`). Boots a CM5 /
  Pi5 / Pi4 from a USB stick into the same pixie TUI as usbboot-pc;
  the headline use case is reflashing a CM5 in a closed IO-case
  (eMMC) without the jumper-rpiboot-Etcher disassembly dance.
  End-to-end use case in
  [`docs/src/tutorials/pixie-usbboot-rpi.md`](../docs/src/tutorials/pixie-usbboot-rpi.md).

  The end-to-end PXE chain (server hands a per-MAC iPXE plan, client
  loads the live trio, flashes a target disk, signals done) is
  exercised by `make test-pxe` and runs in CI on every push.

## Running pixie-web

The supported way to set up a long-running pixie-web is the
container deploy (`deploy/compose.yml` / `deploy/quadlet/`); see
[`deploy/README.md`](../deploy/README.md) and
[walkthrough-server-docker.md](../docs/src/walkthrough-server-docker.md).
