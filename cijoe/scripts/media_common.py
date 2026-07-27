"""
Shared helpers for the pixie media builds
==========================================

Small, build-system-agnostic helpers used by more than one media build
script (``archiso_build.py`` for the usbboot .iso, ``live_env_image.py``
for the nbdboot live-env image):

- ``_read_pixie_version`` -- the pyproject version, stamped into the
  built artifacts so an operator can match a stick / image back to a
  release.
- ``_verify_iso`` -- post-append structural check on the usbboot .iso's
  MBR (3 non-overlapping partitions: ISO9660 + EFI + PIXIE_IMGS exFAT).

Extracted from the retired ``usb_iso_build.py`` (the Debian live-build
pipeline) so the shared logic outlives it.

Retargetable: n/a (helper module, not a cijoe step)
"""

from __future__ import annotations

import errno
import json
import logging as log
from pathlib import Path


def _read_pixie_version(cijoe_dir: Path) -> str:
    """Read the pixie-lab version from the repo's top-level pyproject.toml.

    The pre-built media stamps this string into the bootloader menu,
    kernel cmdline, login banner, motd, and shell-startup file so
    operators can read the version at every boot moment. Reading
    pyproject.toml directly (rather than ``importlib.metadata``) keeps
    the bake script independent of whether pixie-lab is installed in the
    cijoe runner's env.
    """
    pyproject = cijoe_dir.parent / "pyproject.toml"
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"could not find version line in {pyproject}")


def _verify_iso(cijoe, iso_path: Path) -> int:
    """Linux-side post-append structural checks on the usbboot .iso.

    Asserts the expected layout after the PIXIE_IMGS append:

    - 3 partitions in the MBR.
    - Non-overlapping byte ranges (Windows enumeration breaks if
      violated).
    - p1 type 0 + bootable flag (the isohybrid ISO9660 payload).
    - p2 type ef (EFI ESP).
    - p3 type 07 (exFAT) labeled PIXIE_IMGS, its label readable via
      ``blkid`` (proves mkfs.exfat completed and the FAT/bitmap/root are
      coherent) without needing the exfat kernel module -- crucial on CI
      runners that ship ``exfatprogs`` but not the driver.

    Necessary but not sufficient: Linux-side checks can't surface
    host-OS handler bugs (Windows Etcher / Rufus decompressors); a
    hardware / BMC-virtual-media boot is still the real proof.
    """
    log.info(f"Verifying pre-built ISO structure: {iso_path}")

    err, state = cijoe.run_local(f"sudo sfdisk --json {iso_path}")
    if err:
        log.error("sfdisk --json failed during verification")
        return err
    try:
        table = json.loads(state.output())
        partitions = table["partitiontable"]["partitions"]
    except (json.JSONDecodeError, KeyError) as exc:
        log.error(f"could not parse sfdisk --json: {exc}")
        return errno.EIO

    if len(partitions) != 3:
        log.error(f"expected 3 partitions, found {len(partitions)}")
        return errno.EIO

    expected = [
        ("0", True, "ISO9660"),
        ("ef", False, "EFI ESP"),
        ("7", False, "PIXIE_IMGS exFAT"),
    ]
    for i, (p, (etype, ebootable, name)) in enumerate(
        zip(partitions, expected, strict=True), start=1
    ):
        # Normalize: sfdisk emits MBR types as bare hex without leading
        # zeros, so "0", "00", "ef", "7", "07" are all in play.
        ptype = str(p.get("type", "")).lower().lstrip("0") or "0"
        if ptype != etype:
            log.error(f"p{i} ({name}): expected type {etype}, got {p.get('type')!r}")
            return errno.EIO
        actual_bootable = bool(p.get("bootable", False))
        if actual_bootable != ebootable:
            log.error(f"p{i} ({name}): expected bootable={ebootable}, got {actual_bootable}")
            return errno.EIO

    for i in range(len(partitions)):
        for j in range(i + 1, len(partitions)):
            pa, pb = partitions[i], partitions[j]
            a_start, a_end = pa["start"], pa["start"] + pa["size"]
            b_start, b_end = pb["start"], pb["start"] + pb["size"]
            if a_start < b_end and b_start < a_end:
                log.error(f"p{i + 1} [{a_start}..{a_end}) overlaps p{j + 1} [{b_start}..{b_end})")
                return errno.EIO

    err, state = cijoe.run_local(f"sudo losetup -fP --show {iso_path}")
    if err:
        log.error("losetup -fP failed during verification")
        return err
    loop = state.output().strip().splitlines()[-1].strip()
    cijoe.run_local("sudo udevadm settle")

    err, state = cijoe.run_local(f"sudo blkid -o value -s LABEL {loop}p3")
    label = state.output().strip() if not err else ""
    cijoe.run_local(f"sudo losetup -d {loop}")
    if err or label != "PIXIE_IMGS":
        log.error(f"p3 label expected PIXIE_IMGS, got {label!r}")
        return errno.EIO

    log.info("ISO structure OK: 3 non-overlapping partitions, p3 labeled PIXIE_IMGS")
    return 0
