"""
Structurally verify the pixie usbboot .iso
===========================================

Fast, no-boot sanity checks on ``pixie-usbboot-pc-x86_64-v*.iso`` before
the Ventoy boot test spends ~10 min proving it actually boots. Catches a
broken / non-hybrid / single-firmware bake immediately:

  1. ``file`` reports an ISO 9660 filesystem with a DOS/MBR boot sector
     (the isohybrid MBR that makes ``dd`` to a USB stick BIOS-bootable).
  2. The El Torito catalog carries BOTH a BIOS and a UEFI/EFI boot
     image -- i.e. the ISO boots on legacy BIOS AND UEFI firmware.
  3. The published ``.iso.sha256`` sidecar matches the image.

Retargetable: False (host-side; needs ``file`` + ``xorriso`` +
``sha256sum`` on the initiator, which the workflow installs).
"""

from __future__ import annotations

import errno
import logging as log
from argparse import ArgumentParser
from pathlib import Path

ISO_BASENAME_GLOB = "pixie-usbboot-pc-x86_64-v*.iso"


def add_args(parser: ArgumentParser):
    del parser


def _out(res) -> str:
    return res.output() if hasattr(res, "output") else str(res)


def main(args, cijoe):
    del args
    cfg = cijoe.getconf("test.usb_ventoy", {})
    iso_dir = Path(cfg.get("iso_dir") or (Path.home() / "system_imaging" / "disk"))
    candidates = sorted(iso_dir.glob(ISO_BASENAME_GLOB))
    if not candidates:
        log.error(f"no {ISO_BASENAME_GLOB} found in {iso_dir} (did usbboot-pc build run?)")
        return errno.ENOENT
    iso = candidates[-1]
    log.info(f"verifying {iso.name} ({iso.stat().st_size} bytes)")

    # 1. Valid ISO 9660 + isohybrid MBR (BIOS-bootable when dd'd).
    err, res = cijoe.run_local(f"file -- {iso}")
    if err:
        log.error("`file` failed")
        return errno.EIO
    file_out = _out(res)
    if "ISO 9660" not in file_out:
        log.error(f"not an ISO 9660 image: {file_out}")
        return errno.EINVAL
    if "DOS/MBR boot sector" not in file_out and "bootable" not in file_out.lower():
        log.error(f"no isohybrid MBR boot sector (not USB-dd bootable?): {file_out}")
        return errno.EINVAL
    log.info("OK: ISO 9660 + DOS/MBR boot sector")

    # 2. El Torito catalog has both a BIOS and a UEFI/EFI boot image.
    err, res = cijoe.run_local(f"xorriso -indev {iso} -report_el_torito plain 2>&1")
    if err:
        log.error("`xorriso -report_el_torito` failed")
        return errno.EIO
    et = _out(res)
    log.info(f"El Torito report:\n{et}")
    boot_lines = [ln for ln in et.splitlines() if "boot img" in ln.lower()]
    has_bios = any("BIOS" in ln for ln in boot_lines)
    has_uefi = any(("UEFI" in ln) or ("EFI" in ln) for ln in boot_lines)
    if not has_bios:
        log.error("El Torito catalog has no BIOS boot image")
        return errno.EINVAL
    if not has_uefi:
        log.error("El Torito catalog has no UEFI/EFI boot image")
        return errno.EINVAL
    log.info("OK: El Torito has both BIOS and UEFI boot images")

    # 3. sha256 sidecar matches.
    sha = iso.with_name(iso.name + ".sha256")
    if not sha.is_file():
        log.error(f"missing sha256 sidecar: {sha}")
        return errno.ENOENT
    err, _ = cijoe.run_local(f"sh -c 'cd {iso_dir} && sha256sum -c {sha.name}'")
    if err:
        log.error("sha256 mismatch")
        return errno.EIO
    log.info("OK: sha256 sidecar matches")

    log.info(f"{iso.name}: structural verification passed")
    return 0
