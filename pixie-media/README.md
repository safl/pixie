# pixie-media

Source content for the pixie USB live image.

**USB live image** (`VARIANT=usbboot-pc`) - a bootable x86_64 ISO
carrying the pixie runtime plus a writable exFAT `PIXIE_IMGS` partition
for pre-built images, for ad-hoc flash-install and inventory. It boots
from CD media, a USB stick (BIOS + UEFI), and BMC virtual media
(Redfish / PiKVM / JetKVM). Built via **archiso** (`mkarchiso`) from the
`archiso/` profile and shipped uncompressed as
`pixie-usbboot-pc-x86_64.iso` (every flasher reads a plain `.iso`
directly; at ~1.3 GiB with full linux-firmware it stays under GitHub's
2 GiB per-release-asset limit).

The image is an Arch Linux live env: the Arch kernel + full
linux-firmware carry the NIC / GPU drivers in-tree, so it needs no DKMS
(this replaced the earlier Debian live-build ISO, which built an r8125
DKMS module to get 2.5G Realtek NICs up).

This directory holds the **content** baked into the image; the cijoe
**orchestration** (configs, tasks, scripts) that consumes it lives at
`cijoe/` at the repo root. Operators drive everything via the top-level
Makefile: `make build VARIANT=usbboot-pc`.

## Layout

- `archiso/` - the mkarchiso profile, forked from archiso's `releng`.
  - `profiledef.sh` - ISO metadata, `bootmodes` (bios.syslinux +
    uefi.systemd-boot), and `file_permissions` (exec bits for the pixie
    scripts).
  - `packages.x86_64` - the trimmed package set (flash / inventory
    tooling; no DKMS / headers / build tools).
  - `airootfs/` - the pixie userspace overlaid onto the live env's root:
    the operator CLI launcher (`usr/local/bin/pixie` + the vendored tree
    staged into `opt/pixie/lib` at build time), the boot service trio
    and support units (`pixie-on-tty1`, `pixie-images-discover`,
    `var-lib-pixie-images.mount`, `pixie-usb-grow`, the banners), the
    realtek offload udev rule, `motd` / `issue`, and `root:pixie` for
    ssh diagnostics.
  - `efiboot/` + `syslinux/` - the UEFI (systemd-boot) and BIOS
    (syslinux) bootloader configs, patched for a serial console
    (`console=ttyS0,115200`), immediate boot, and pixie branding.
  - `mkarchiso-in-container.sh` - runs `mkarchiso` inside the privileged
    build container (mirror pin + serialized downloads).
- `auxiliary/` - iPXE embed script + headers consumed by
  `cijoe/scripts/pixie_ipxe_build.py` (not part of the ISO build).

## Pipeline

From the repo root:

```
make build VARIANT=usbboot-pc
```

runs `cijoe tasks/usbboot-pc.yaml`, whose single `archiso_build` step:

1. Copies `archiso/` into a working dir and vendors the pixie-lab CLI
   (`uv build --wheel` -> `pip install --target`, plus rich + tomlkit)
   into its `airootfs/opt/pixie/lib` so the live env's stock python3
   runs it. Stamps the pixie version into the profile.
2. Runs `mkarchiso` inside a privileged, host-networked **archlinux
   container** (podman on the lab / dev host, docker on CI). mkarchiso
   needs root + `mount --bind /dev` for pacstrap, which a
   rootless/unprivileged container can't provide.
3. Appends a writable exFAT `PIXIE_IMGS` partition to the trailing edge
   of the isohybrid ISO (`sfdisk`, `losetup -fP`, `mkfs.exfat`; the
   32 MiB stub auto-grows to fill the stick on first boot via
   `pixie-usb-grow.service`), then verifies the structure and writes a
   sha256 manifest.

Output: `~/system_imaging/disk/pixie-usbboot-pc-x86_64-v<version>.iso`
(+ `.sha256`). Write it with `dd if=... of=/dev/sdX bs=4M`, open it in
Etcher / Rufus DD-mode, or drop it onto a Ventoy stick.

## Build prerequisites

- A container runtime able to run `--privileged --network=host`
  (`podman` on the lab / dev host, `docker` preinstalled on CI).
- `exfatprogs` on the host for the `PIXIE_IMGS` exFAT append
  (`mkfs.exfat`); `sfdisk` / `losetup` / `blkid` come from util-linux.
- `uv` on PATH to build + vendor the pixie-lab CLI.
- Passwordless `sudo` for the container run + the loop-device append.
- `cijoe` (install via `make media-deps`).

## Verify

`make test-usb-ventoy` runs the structural check (`usb_iso_verify`:
ISO 9660 + isohybrid MBR + El Torito BIOS **and** UEFI boot images +
sha256) and a QEMU/Ventoy boot that asserts `pixie-on-tty1` renders on
tty1 and the image-discovery mount lands. CI's `verify-usbboot` job runs
the same against the freshly built artifact.

## Status

The usbboot-pc ISO ships on every tagged release at
[the GitHub releases page](https://github.com/safl/pixie/releases).
Most operators never run this build pipeline themselves; `pixie-media/`
exists for contributors who want to modify the image.
