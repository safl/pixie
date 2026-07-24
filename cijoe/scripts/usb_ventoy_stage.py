"""
Stage a Ventoy disk for the pixie Ventoy-boot test
===================================================

Mirrors what an operator does in practice:

  1. Download Ventoy.
  2. Install Ventoy onto a USB stick (here: a 4 GiB sparse file
     loop-attached so ``Ventoy2Disk.sh`` sees a block device).
  3. Drop ``pixie-usbboot-pc-x86_64-v*.iso`` onto the Ventoy data
     partition so the Ventoy menu offers it.
  4. Drop a ``pixie-images/`` subdir with one sentinel image + a
     ``catalog.toml`` -- the operator's image catalog the live env
     should discover via ``pixie-images-discover.service``.
  5. Write a ``ventoy/ventoy.json`` with BOTH ``VTOY_MENU_TIMEOUT`` and
     ``VTOY_SECONDARY_TIMEOUT`` = 1 so the two Ventoy menus auto-boot
     the only ISO without keyboard input (bty proved both are needed).

The catalog carries a single ``oras://`` entry (rolling nosi tag);
it exercises pixie's catalog parser as the realistic source operators
drop next to their images.

Retargetable: False (host-side staging on the cijoe initiator;
sudo needed for losetup + Ventoy2Disk.sh).
"""

from __future__ import annotations

import errno
import logging as log
import urllib.request
from argparse import ArgumentParser
from pathlib import Path

ISO_BASENAME_GLOB = "pixie-usbboot-pc-x86_64-v*.iso"
DISK_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB

# ``pixie-images-discover`` bind-mounts the first vfat/exfat partition
# whose ``pixie-images/`` subdir holds a usable image (``*.img*`` /
# ``*.qcow2``). A 1 MiB sparse ``.img.gz`` is enough to trip that; it is
# never flashed.
IMAGES_SUBDIR = "pixie-images"
SENTINEL_IMAGE_NAME = "demo-pixie-ventoy-test.img.gz"
SENTINEL_IMAGE_BYTES = 1 * 1024 * 1024

# Single-entry catalog an operator would realistically drop alongside
# their images: a remote ``oras://`` ref pixie's catalog parser reads.
CATALOG_TOML = """\
version = 1

[[images]]
name = "ventoy-test-fedora-44-headless"
src = "oras://ghcr.io/safl/nosi/fedora-44-headless:latest"
format = "img.gz"
arch = "x86_64"
description = "Sentinel remote entry for the Ventoy-boot test"
"""


def add_args(parser: ArgumentParser):
    del parser


def main(args, cijoe):
    del args
    cfg = cijoe.getconf("test.usb_ventoy", {})
    iso_dir = Path(cfg.get("iso_dir") or (Path.home() / "system_imaging" / "disk"))
    candidates = sorted(iso_dir.glob(ISO_BASENAME_GLOB))
    if not candidates:
        log.error(f"no {ISO_BASENAME_GLOB} found in {iso_dir} (did usbboot-pc build run?)")
        return errno.ENOENT
    src_iso = candidates[-1]
    log.info(f"using {src_iso.name} ({src_iso.stat().st_size} bytes)")

    ventoy_version = cfg.get("ventoy_version", "1.1.05")
    ventoy_url = (
        f"https://github.com/ventoy/Ventoy/releases/download/"
        f"v{ventoy_version}/ventoy-{ventoy_version}-linux.tar.gz"
    )

    guest_path_raw = cijoe.getconf("qemu.guests.usb-ventoy.path")
    if not guest_path_raw:
        log.error("missing qemu.guests.usb-ventoy.path in cijoe config")
        return errno.EINVAL
    guest_path = Path(guest_path_raw)
    guest_path.mkdir(parents=True, exist_ok=True)

    disk = guest_path / "disk.img"
    ventoy_tarball = guest_path / f"ventoy-{ventoy_version}-linux.tar.gz"
    ventoy_dir = guest_path / f"ventoy-{ventoy_version}"

    # 1. Download Ventoy (cached across runs).
    if not ventoy_tarball.is_file():
        log.info(f"downloading {ventoy_url}")
        urllib.request.urlretrieve(ventoy_url, ventoy_tarball)
    if not ventoy_dir.is_dir():
        log.info(f"extracting {ventoy_tarball.name}")
        err, _ = cijoe.run_local(f"tar -xzf {ventoy_tarball} -C {guest_path}")
        if err:
            log.error("tar -xzf failed")
            return errno.EIO

    # 2. Create the sparse 4 GiB disk (Ventoy will format it).
    log.info(f"creating sparse disk: {disk} ({DISK_BYTES} bytes)")
    with disk.open("wb") as fh:
        fh.truncate(DISK_BYTES)

    # 3. losetup + Ventoy2Disk.sh -I (force install).
    log.info("losetup -fP --show <disk>")
    err, out = cijoe.run_local(f"sudo losetup -fP --show {disk}")
    if err:
        log.error("losetup failed")
        return errno.EIO
    loop_dev = out.output().strip().splitlines()[-1].strip() if hasattr(out, "output") else ""
    if not loop_dev.startswith("/dev/loop"):
        log.error(f"unexpected losetup output: {out!r}")
        return errno.EIO
    log.info(f"loop device: {loop_dev}")

    mount_dir = guest_path / "ventoy-mount"
    mount_dir.mkdir(exist_ok=True)
    sentinel_tmp = guest_path / "demo.tmp"
    catalog_tmp = guest_path / "catalog.toml.tmp"
    ventoy_json_tmp = guest_path / "ventoy.json.tmp"

    try:
        # ``yes | Ventoy2Disk.sh -I <dev>``: force install over any
        # existing partition table; ``yes`` answers the confirmation
        # prompts (the disk is about to be erased).
        log.info(f"Ventoy2Disk.sh -I {loop_dev}")
        err, _ = cijoe.run_local(f"sudo sh -c 'yes | {ventoy_dir}/Ventoy2Disk.sh -I {loop_dev}'")
        if err:
            log.error("Ventoy2Disk.sh failed")
            return errno.EIO

        # Ventoy creates two partitions: p1 is the exFAT data partition
        # (ISOs + operator files), p2 is the small VTOYEFI partition.
        cijoe.run_local("sudo udevadm settle --timeout=10")
        cijoe.run_local(f"sudo partprobe {loop_dev} || true")
        cijoe.run_local("sudo udevadm settle --timeout=10")
        cijoe.run_local(f"sudo lsblk -bno NAME,SIZE,TYPE,LABEL,FSTYPE {loop_dev}")

        ventoy_data = f"{loop_dev}p1"
        # Kernel exfat first (fast), fall back to mount.exfat-fuse.
        # Ubuntu's ``mount -t exfat`` does NOT auto-fall-back to FUSE
        # when the kernel module is missing; GHA's ubuntu-latest ships
        # a stripped kernel without exfat, so the workflow installs
        # ``exfat-fuse`` for the ``/sbin/mount.exfat-fuse`` userspace
        # path.
        log.info(f"mounting {ventoy_data}")
        err, _ = cijoe.run_local(f"sudo mount -t exfat {ventoy_data} {mount_dir}")
        if err:
            log.info("kernel exfat unavailable; falling back to mount.exfat-fuse")
            err, _ = cijoe.run_local(f"sudo mount.exfat-fuse {ventoy_data} {mount_dir}")
            if err:
                log.error(
                    f"both kernel and FUSE exfat mounts failed on {ventoy_data} "
                    f"(install ``exfat-fuse`` or ``linux-modules-extra-$(uname -r)``)"
                )
                return errno.EIO

        try:
            # 4. Drop the .iso at the root of the Ventoy data partition.
            log.info(f"copying {src_iso.name} -> Ventoy data partition")
            err, _ = cijoe.run_local(f"sudo cp {src_iso} {mount_dir}/")
            if err:
                log.error("cp .iso failed")
                return errno.EIO

            # 5. pixie-images/ subdir with sentinel image + catalog.toml.
            cijoe.run_local(f"sudo mkdir -p {mount_dir}/{IMAGES_SUBDIR}")

            log.info(f"staging sentinel image ({SENTINEL_IMAGE_BYTES} bytes)")
            with sentinel_tmp.open("wb") as fh:
                fh.truncate(SENTINEL_IMAGE_BYTES)
            err, _ = cijoe.run_local(
                f"sudo cp {sentinel_tmp} {mount_dir}/{IMAGES_SUBDIR}/{SENTINEL_IMAGE_NAME}"
            )
            if err:
                log.error("cp sentinel image failed")
                return errno.EIO

            log.info("staging catalog.toml")
            catalog_tmp.write_text(CATALOG_TOML, encoding="utf-8")
            err, _ = cijoe.run_local(
                f"sudo cp {catalog_tmp} {mount_dir}/{IMAGES_SUBDIR}/catalog.toml"
            )
            if err:
                log.error("cp catalog.toml failed")
                return errno.EIO

            # 6. ventoy/ventoy.json: fully-headless auto-boot. Ventoy
            # shows TWO menus that BOTH need a timeout (bty verified via
            # a stuck-guest screendump): the primary ISO list
            # (VTOY_MENU_TIMEOUT) and the secondary boot-mode menu
            # (VTOY_SECONDARY_TIMEOUT). The primary timeout does not
            # cascade; without the secondary knob the guest sits at the
            # boot-mode menu forever and no kernel ever boots.
            cijoe.run_local(f"sudo mkdir -p {mount_dir}/ventoy")
            ventoy_json_tmp.write_text(
                "{\n"
                '  "control": [\n'
                '    {"VTOY_MENU_TIMEOUT": "1"},\n'
                '    {"VTOY_SECONDARY_TIMEOUT": "1"}\n'
                "  ]\n"
                "}\n",
                encoding="utf-8",
            )
            err, _ = cijoe.run_local(f"sudo cp {ventoy_json_tmp} {mount_dir}/ventoy/ventoy.json")
            if err:
                log.error("cp ventoy.json failed")
                return errno.EIO

            # Record what landed for the cijoe report.
            cijoe.run_local(f"sudo ls -la {mount_dir}/")
            cijoe.run_local(f"sudo ls -la {mount_dir}/{IMAGES_SUBDIR}/")
            cijoe.run_local(f"sudo cat {mount_dir}/{IMAGES_SUBDIR}/catalog.toml")
            cijoe.run_local(f"sudo cat {mount_dir}/ventoy/ventoy.json")
            cijoe.run_local(f"sudo sync {mount_dir}")
        finally:
            cijoe.run_local(f"sudo umount {mount_dir}")
    finally:
        cijoe.run_local(f"sudo losetup -d {loop_dev}")
        sentinel_tmp.unlink(missing_ok=True)
        catalog_tmp.unlink(missing_ok=True)
        ventoy_json_tmp.unlink(missing_ok=True)
        if mount_dir.is_dir() and not any(mount_dir.iterdir()):
            mount_dir.rmdir()

    log.info(f"Ventoy disk ready at {disk}")
    return 0
