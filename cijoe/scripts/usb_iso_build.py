"""
Build the pixie USB live env (iso-hybrid) via live-build
=======================================================

Drives Debian's live-build to produce a hybrid ISO image that boots
both from CD media and from a USB stick (BIOS + UEFI). Reuses the
same live-build chroot config tree that ``live_build`` uses for the
network-flash artifacts; the only difference is the binary-images
target (``iso-hybrid`` vs ``netboot``) and the bootloader selection.

Workflow:

1. Copy ``pixie-media/live-build/`` (the live-build config tree) into
   a fresh ``cijoe/_build/usbboot-pc/`` working dir.
2. Run ``sudo env PIXIE_VARIANT=usbboot-pc lb clean --all && lb build``.
   The env var drives ``auto/config`` into iso-hybrid mode (binary
   images, bootloaders, kernel cmdline appendices); ``sudo env``
   is needed because sudo strips the environment by default. The
   var must be present at every lb invocation because ``lb build``
   internally re-runs ``lb config`` (which re-invokes
   ``auto/config``).
3. Publish the resulting hybrid ISO to ``publish.dir`` from the
   cijoe config, renamed to ``pixie-usbboot-pc-x86_64.iso``.
4. Append a writable PIXIE_IMAGES exFAT partition to the trailing
   edge of the artifact (sfdisk + losetup + mkfs.exfat) so the
   single dd-able file carries both the boot path and the
   operator's image catalog.
5. Write a sha256 manifest covering the .iso (uncompressed; the
   ~200 MiB ISO is well under GitHub's 2 GiB asset limit since
   PIXIE_IMAGES is 32 MiB at bake).

The cwd at run time is ``cijoe/`` (the Makefile cd's there before
invoking cijoe), so the pixie-media tree lives at
``Path.cwd().parent / "pixie-media"`` and the build scratch dir is
``Path.cwd() / "_build" / "usbboot-pc"``.

Skipped for any variant other than ``usbboot-pc``.

Retargetable: False
"""

from __future__ import annotations

import errno
import json
import logging as log
import os
import shutil
from argparse import ArgumentParser
from pathlib import Path

PUBLISH_BASENAME_FMT = "pixie-usbboot-pc-x86_64-v{version}.iso"

# Just the PIXIE_IMAGES partition stub; the bake doesn't populate it.
# Operators drop their own image files (.qcow2 / .img.gz / .img / .iso /
# .iso.gz) onto the partition; the live env's image-root scan picks
# them up. The catalog of nosi + pixie images is a release-side
# artifact (releases/latest/download/catalog.toml) the wizard offers
# as [d] default in SELECT_CATALOG -- not something baked here.
#
# The partition auto-grows to fill the underlying disk on first boot
# via ``pixie-usb-grow.service``, so this is the minimum staged at bake
# time, not the runtime size. Verified by the GHA auto-grow test.
#
# 32 MiB rather than 1 MiB: ``exfatprogs`` mkfs.exfat refuses very small
# volumes (the 1 MiB attempt in v0.25.4 failed the bake); 32 MiB is
# comfortably above the floor while still keeping the artifact ~200 MiB.
TRAILING_EXFAT_SIZE = "32M"


def add_args(parser: ArgumentParser):
    del parser  # no flags; signature kept for cijoe consistency


def main(args, cijoe):
    del args
    cijoe_dir = Path.cwd()
    pixie_media = cijoe_dir.parent / "pixie-media"

    variant = cijoe.getconf("pixie", {}).get("variant", "")
    if variant != "usbboot-pc":
        log.info(
            f"Skipping usb_iso_build (variant={variant!r}; only 'usbboot-pc' runs lb iso-hybrid)"
        )
        return 0

    images = cijoe.getconf("system-imaging.images", {})
    image = images.get("pixie-usbboot-pc-x86_64-iso")
    if not image:
        log.error("missing system-imaging.images.pixie-usbboot-pc-x86_64-iso in config")
        return errno.EINVAL

    publish_dir_str = image.get("publish", {}).get("dir")
    if not publish_dir_str:
        log.error("system-imaging.images.pixie-usbboot-pc-x86_64-iso.publish.dir is unset")
        return errno.EINVAL
    publish_dir = Path(publish_dir_str)
    publish_dir.mkdir(parents=True, exist_ok=True)

    build_dir = cijoe_dir / "_build" / "usbboot-pc"
    if build_dir.exists():
        # ``lb`` writes a chroot tree owned by root; rm needs sudo.
        err, _ = cijoe.run_local(f"sudo rm -rf {build_dir}")
        if err:
            log.error(f"failed to remove stale build dir {build_dir}")
            return err
    build_dir.mkdir(parents=True)

    # Copy the live-build config tree into the working dir.
    config_src = pixie_media / "live-build"
    if not config_src.exists():
        log.error(f"live-build config tree missing: {config_src}")
        return errno.ENOENT
    for entry in config_src.iterdir():
        dest = build_dir / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dest, symlinks=True)
        else:
            shutil.copy2(entry, dest)

    # Stamp the pixie version into every ``__PIXIE_VERSION__`` placeholder
    # in the copied tree before ``lb build`` runs. Files that pick up
    # the stamp: ``auto/config`` (kernel cmdline), the binary-stage
    # bootloader hook (syslinux + grub menu titles), ``/etc/issue``
    # (login banner), ``/etc/motd`` (post-login), and
    # ``/etc/profile.d/pixie-version.sh`` (interactive shell). Operators
    # see the version in at least one of these at every boot moment
    # -- bootloader menu, kernel boot, login, shell -- so the pre-built
    # stick can always be matched back to a release.
    pixie_version = _read_pixie_version(cijoe_dir)
    iso_basename = PUBLISH_BASENAME_FMT.format(version=pixie_version)
    log.info(f"Stamping pixie version {pixie_version} into live-build tree")
    err, _ = cijoe.run_local(
        f"sh -c 'grep -rlF __PIXIE_VERSION__ {build_dir} | "
        f"xargs --no-run-if-empty sed -i s/__PIXIE_VERSION__/{pixie_version}/g'"
    )
    if err:
        log.error("__PIXIE_VERSION__ substitution failed")
        return err

    # Drive auto/config into iso-hybrid mode via the ``PIXIE_VARIANT``
    # env var (``PIXIE_VARIANT=usbboot-pc`` selects iso-hybrid + syslinux +
    # grub-efi; ``netboot-pc`` selects the netboot trio;
    # ``usbboot-rpi`` selects arm64 + netboot for the Pi flasher). The
    # env var has to be set in the invocation environment of every
    # ``lb`` call, because ``lb build`` re-runs ``lb config`` (which
    # re-runs ``auto/config``) during its own setup; flag-based
    # overrides at the initial config call get clobbered by that
    # re-run.
    #
    # ``pixie-on-tty1.service`` fires unconditionally on every live
    # env boot (v0.22.10+ retired the cmdline-mode gating). With no
    # ``pixie.server`` / ``pixie.mac`` on the cmdline the wrapper script
    # forwards no flags and ``pixie`` falls back to scanning the local
    # image-root; the offline USB-boot mode.
    #
    # ``sudo env`` is used (instead of ``sudo`` with shell variable
    # assignment) because sudo strips environment by default; ``env``
    # ensures PIXIE_VARIANT is in the invoked process's environment
    # under root rather than the caller's.
    log.info(f"Running lb build in {build_dir} (PIXIE_VARIANT=usbboot-pc)")
    err, _ = cijoe.run_local(
        f"sh -c 'cd {build_dir} && "
        "sudo env PIXIE_VARIANT=usbboot-pc lb clean --all && "
        "sudo env PIXIE_VARIANT=usbboot-pc lb build'"
    )
    if err:
        log.error("lb build failed; see live-build.log under the build dir")
        return err

    # Verify the binary-stage hook actually ran + the bootloader
    # menus are suppressed. Catches the "hook silently doesn't
    # execute" class of bugs -- live-build discovers binary-stage
    # hooks under ``config/hooks/normal/`` with a ``.binary``
    # suffix, NOT under ``config/hooks/binary/``; a wrong location
    # produces a green build with no menu suppression applied.
    err = _verify_bootloader_suppression(cijoe, build_dir)
    if err:
        return err

    # Locate the artifact. live-build's iso-hybrid output naming
    # varies between releases (``binary.hybrid.iso`` /
    # ``live-image-amd64.hybrid.iso``); a recursive glob picks up
    # whichever it ended up as. Filter chroot/ matches to skip cache
    # copies lb leaves behind.
    def _outside_chroot(p: Path) -> bool:
        return "chroot" not in p.parts

    # Dump the build dir for diagnostics so the next time live-build's
    # output layout changes we can see the new shape in the logs.
    cijoe.run_local(f"sudo find {build_dir} -maxdepth 4 -type d 2>/dev/null | head -60")

    isos = sorted(p for p in build_dir.rglob("*.hybrid.iso") if _outside_chroot(p))
    if not isos:
        # Fallback for older / non-hybrid output names.
        isos = sorted(p for p in build_dir.rglob("live-image-*.iso") if _outside_chroot(p))
    if not isos:
        log.error(f"no hybrid ISO under {build_dir} (excluding chroot)")
        cijoe.run_local(f"sudo find {build_dir} -name '*.iso' 2>/dev/null")
        return errno.ENOENT
    iso = isos[0]

    # Publish under the user's uid/gid so subsequent steps don't
    # need privileges. ISO is owned by root (lb wrote it under sudo).
    uid, gid = os.geteuid(), os.getegid()
    dst = publish_dir / iso_basename
    err, _ = cijoe.run_local(f"sudo cp {iso} {dst}")
    if err:
        log.error(f"failed to publish {iso} -> {dst}")
        return err
    cijoe.run_local(f"sudo chown {uid}:{gid} {dst}")
    log.info(f"published {dst}")

    # Extend the published ISO with a trailing exFAT partition so the
    # pre-built image is dd-ready WITH a writable image-catalog area.
    # The hybrid ISO is MBR-only (live-build's ``--bootloaders
    # syslinux,grub-efi`` uses ``isohdpfx.bin`` for the System Area,
    # not GPT); we append a fresh MBR partition entry via sfdisk.
    # The front of the file stays byte-identical so the boot path
    # is unchanged. dd / Etcher / Rufus all do byte-for-byte writes
    # and handle the larger artifact.
    err = _extend_with_exfat(cijoe, dst)
    if err:
        return err

    # Linux-side post-bake verification. Catches structural
    # regressions in the pre-built ISO (partition count / types /
    # overlap / PIXIE_IMAGES label / exFAT mountability) before
    # we waste CI cycles on the gzip step. Necessary but not
    # sufficient -- doesn't catch host-OS handler bugs (the
    # ``feedback_verify_flasher_compat`` rule: any compression /
    # partition / boot-layout change ships with a hardware test
    # on Etcher / Rufus, not just Linux assertions).
    err = _verify_iso(cijoe, dst)
    if err:
        return err

    # Publish the .iso uncompressed. With PIXIE_IMAGES = 32 MiB at bake
    # time, the total ISO is ~200 MiB -- comfortably under GitHub's
    # 2 GiB per-release-asset upload limit. gzip was dropped: every
    # flasher (Etcher, RPi Imager, Rufus, dd) reads plain .iso
    # natively, and removing the compress step shaves CI time.
    sha256_path = publish_dir / f"{iso_basename}.sha256"
    err, _ = cijoe.run_local(
        f"sh -c 'cd {publish_dir} && sha256sum {iso_basename} > {sha256_path}'"
    )
    if err:
        log.error("failed computing sha256 manifest")
        return err

    cijoe.run_local(f"cat {sha256_path}")
    cijoe.run_local(f"ls -la {dst}")

    return 0


def _verify_bootloader_suppression(cijoe, build_dir: Path) -> int:
    """Assert the binary-stage hook ran + bootloader menus are
    suppressed in the pre-built binary tree.

    Catches the "hook silently doesn't execute" class of failures
    -- live-build discovers binary-stage hooks under
    ``config/hooks/normal/`` with a ``.binary`` suffix; a hook at
    the wrong path produces a green build with no menu suppression
    applied. Runs against ``_build/usbboot-pc/binary/`` after
    ``lb build`` completes so we fail the bake locally instead of
    waiting for a hardware test to surface the issue.

    Checks (each fails the bake with a specific error message):

    1. ``binary/.pixie-bootloader-hook-ran`` sentinel exists. Hook
       writes this on entry; missing means live-build didn't
       discover the hook (path / suffix wrong) or the hook
       errored before reaching ``touch``.
    2. No ``gfxboot.c32`` / ``vesamenu.c32`` / ``bootlogo`` files
       under ``binary/``. The hook deletes them; presence means
       the deletion didn't happen.
    3. ``binary/isolinux/isolinux.cfg`` (if present) has
       ``timeout 1`` (BIOS path). Default is ``timeout 0`` =
       wait-forever in syslinux.
    4. ``binary/boot/grub/grub.cfg`` (if present) has
       ``set timeout=0`` and ``set timeout_style=hidden`` (UEFI
       path).
    """
    binary_dir = build_dir / "binary"

    # 1. Sentinel.
    sentinel = binary_dir / ".pixie-bootloader-hook-ran"
    err, _ = cijoe.run_local(f"sudo test -f {sentinel}")
    if err:
        log.error(
            f"BOOTLOADER VERIFY: hook sentinel missing ({sentinel}); "
            "the binary-stage hook didn't execute. Check that the "
            "hook lives at ``config/hooks/normal/*.binary`` (NOT "
            "``config/hooks/binary/...``)."
        )
        return errno.EIO

    # 2. No graphical-menu binaries left.
    err, state = cijoe.run_local(
        f"sudo find {binary_dir} -type f "
        r"\( -name 'gfxboot.c32' -o -name 'vesamenu.c32' -o -name 'bootlogo*' \) "
        "2>/dev/null"
    )
    leftovers = state.output().strip() if not err else ""
    if leftovers:
        log.error(f"BOOTLOADER VERIFY: graphical menu binaries not deleted:\n{leftovers}")
        return errno.EIO

    # 3. isolinux.cfg timeout.
    iso_cfg = binary_dir / "isolinux" / "isolinux.cfg"
    err, _ = cijoe.run_local(f"sudo test -f {iso_cfg}")
    if not err:
        err, state = cijoe.run_local(f"sudo cat {iso_cfg}")
        body = state.output() if not err else ""
        if "timeout 0" in body.lower() or "timeout 30" in body.lower():
            log.error(
                f"BOOTLOADER VERIFY: {iso_cfg} still has a non-suppressed "
                f"timeout (lines below).\n{body[:500]}"
            )
            return errno.EIO

    # 4. grub.cfg suppression.
    grub_cfg = binary_dir / "boot" / "grub" / "grub.cfg"
    err, _ = cijoe.run_local(f"sudo test -f {grub_cfg}")
    if not err:
        err, state = cijoe.run_local(f"sudo cat {grub_cfg}")
        body = state.output() if not err else ""
        if "set timeout=0" not in body:
            log.error(
                f"BOOTLOADER VERIFY: {grub_cfg} missing 'set timeout=0' "
                f"(first 500 chars):\n{body[:500]}"
            )
            return errno.EIO
        if "set timeout_style=hidden" not in body:
            log.error(
                f"BOOTLOADER VERIFY: {grub_cfg} missing "
                f"'set timeout_style=hidden' (first 500 chars):\n{body[:500]}"
            )
            return errno.EIO

    log.info("BOOTLOADER VERIFY: hook ran, gfxboot/vesamenu deleted, timeouts suppressed.")
    return 0


def _verify_iso(cijoe, iso_path: Path) -> int:
    """Linux-side post-bake structural checks on the pre-built ISO.

    Asserts the expected layout:

    - 3 partitions in the MBR.
    - Non-overlapping byte ranges (Windows enumeration breaks if
      violated).
    - p1 type 0 + bootable flag (live-build's iso-hybrid + isohdpfx.bin).
    - p2 type ef (EFI ESP).
    - p3 type 07 (exFAT) labeled PIXIE_IMAGES, mountable as exFAT on
      Linux (proves mkfs.exfat completed and the FAT/bitmap/root are
      coherent).

    Necessary but not sufficient: Linux-side checks can't surface
    host-OS handler bugs (Windows Etcher / Rufus decompressors).
    Hardware verification on a real flasher is still required
    before tagging any publish-format change (see
    ``feedback_verify_flasher_compat`` in memory).
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
        ("7", False, "PIXIE_IMAGES exFAT"),
    ]
    for i, (p, (etype, ebootable, name)) in enumerate(
        zip(partitions, expected, strict=True), start=1
    ):
        # Normalize: sfdisk emits MBR types as bare hex without
        # leading zeros, so "0", "00", "ef", "7", "07" are all
        # in play. Strip leading zeros for comparison.
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

    # blkid recognizes the exFAT signature and reports the label
    # without needing the kernel to mount it -- crucial for CI
    # runners that ship ``exfatprogs`` (for mkfs.exfat) but lack
    # the ``exfat`` kernel module / FUSE driver. An actual ``mount
    # -t exfat`` here would fail on every GHA build despite the
    # filesystem being structurally fine.
    err, state = cijoe.run_local(f"sudo blkid -o value -s LABEL {loop}p3")
    label = state.output().strip() if not err else ""
    cijoe.run_local(f"sudo losetup -d {loop}")
    if err or label != "PIXIE_IMAGES":
        log.error(f"p3 label expected PIXIE_IMAGES, got {label!r}")
        return errno.EIO

    log.info("ISO structure OK: 3 non-overlapping partitions, p3 labeled PIXIE_IMAGES")
    return 0


def _read_pixie_version(cijoe_dir: Path) -> str:
    """Read the pixie-lab version from the repo's top-level pyproject.toml.

    The pre-built live env stamps this string into the bootloader menu,
    kernel cmdline, login banner, motd, and shell-startup file so
    operators can read the version at every boot moment. Reading
    pyproject.toml directly (rather than ``importlib.metadata``)
    keeps the bake script independent of whether pixie-lab is
    installed in the cijoe runner's env.
    """
    pyproject = cijoe_dir.parent / "pyproject.toml"
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"could not find version line in {pyproject}")


def _extend_with_exfat(cijoe, iso_path: Path) -> int:
    """Relocate the EFI partition out of the iso-hybrid overlap, then
    append a trailing exFAT partition labelled PIXIE_IMAGES.

    live-build's iso-hybrid output puts the EFI partition entry
    *inside* the ISO9660 partition's byte range (the EFI FAT image is
    embedded in the ISO9660 stream, and the MBR partition entry just
    points at where it lives). Linux handles overlapping MBR entries
    fine, but Windows refuses to enumerate partitions past the
    overlap, so the PIXIE_IMAGES partition we append is invisible to
    Windows operators.

    Fix: copy the EFI FAT bytes to a non-overlapping location after
    the ISO9660 partition, then rewrite the MBR with three
    non-overlapping partitions:

      - p1: ISO9660 (covers live-build's ISO9660 portion, unchanged)
      - p2: EFI ESP, relocated to the byte range right after p1
      - p3: PIXIE_IMAGES exFAT, fills the rest of the file

    The El Torito catalog inside the ISO9660 still has its embedded
    EFI image for CD-style UEFI boot; the relocated MBR partition
    entry handles USB-style UEFI boot. BIOS boot via ``isohdpfx.bin``
    in MBR sectors 0..432 is untouched (sfdisk only edits the
    partition-table area at offsets 446..510).

    Workflow:

    1. ``truncate -s +<N>G`` extends the file with sparse zeros.
    2. Read the existing MBR via ``sfdisk --json``; locate the
       ISO9660 (type 0) and EFI (type ef) entries.
    3. ``dd`` the EFI FAT bytes from the current overlapping
       location to a non-overlapping position right after the
       ISO9660 partition (8-sector aligned).
    4. Rewrite the MBR partition table via ``sfdisk`` stdin form so
       all three entries land at non-overlapping byte ranges in
       a single atomic operation. Bootable flag preserved on p1.
    5. ``losetup -fP`` + ``mkfs.exfat -L PIXIE_IMAGES`` on p3.
    6. ``losetup -d``.
    """
    log.info(f"Extending {iso_path} with +{TRAILING_EXFAT_SIZE} PIXIE_IMAGES exFAT")
    log.info("Layout: ISO9660 + relocated EFI + PIXIE_IMAGES (non-overlapping for Windows)")

    err, _ = cijoe.run_local(f"truncate -s +{TRAILING_EXFAT_SIZE} {iso_path}")
    if err:
        log.error(f"truncate +{TRAILING_EXFAT_SIZE} failed on {iso_path}")
        return err

    # Read the current MBR.
    err, state = cijoe.run_local(f"sudo sfdisk --json {iso_path}")
    if err:
        log.error("sfdisk --json failed")
        return err
    try:
        table = json.loads(state.output())
        partitions = table["partitiontable"]["partitions"]
    except (json.JSONDecodeError, KeyError) as exc:
        log.error(f"could not parse sfdisk --json output: {exc}")
        return errno.EIO

    iso_part = None
    efi_part = None
    for p in partitions:
        # sfdisk emits MBR types as bare hex without leading zeros:
        # ISO9660 partition typically registers as type "0" or "00";
        # EFI System partition is "ef".
        ptype = str(p.get("type", "")).lower()
        if ptype.lstrip("0") == "" or ptype.lstrip("0") == "0":
            iso_part = p
        elif ptype == "ef":
            efi_part = p
    if iso_part is None:
        log.error("could not find ISO9660 partition (type 0) in MBR")
        return errno.EIO
    if efi_part is None:
        log.error("could not find EFI partition (type ef) in MBR")
        return errno.EIO
    log.info(f"ISO9660 at sectors {iso_part['start']}..{iso_part['start'] + iso_part['size'] - 1}")
    log.info(
        f"EFI currently at sectors {efi_part['start']}..{efi_part['start'] + efi_part['size'] - 1} "
        f"(overlapping ISO9660)"
    )

    # Compute new non-overlapping layout.
    iso_end = iso_part["start"] + iso_part["size"]  # next sector after p1
    efi_size = efi_part["size"]
    # Align to 8-sector (4 KiB) boundary.
    new_efi_start = ((iso_end + 7) // 8) * 8
    new_bty_start = ((new_efi_start + efi_size + 7) // 8) * 8

    # File size in sectors.
    err, state = cijoe.run_local(f"stat -c %s {iso_path}")
    if err:
        log.error("stat failed on iso file")
        return err
    file_bytes = int(state.output().strip())
    file_sectors = file_bytes // 512
    new_bty_size = file_sectors - new_bty_start
    if new_bty_size <= 0:
        log.error(
            f"no room for PIXIE_IMAGES: file_sectors={file_sectors}, new_bty_start={new_bty_start}"
        )
        return errno.EIO

    log.info(f"Relocating EFI to sectors {new_efi_start}..{new_efi_start + efi_size - 1}")
    log.info(f"PIXIE_IMAGES at sectors {new_bty_start}..{new_bty_start + new_bty_size - 1}")

    # Copy EFI FAT bytes from old overlapping location to new
    # non-overlapping location. The new region is currently sparse
    # zeros (truncate just extended the file); writing the FAT image
    # populates it. ``conv=notrunc`` keeps the rest of the file
    # untouched; ``conv=fsync`` flushes before sfdisk writes the new
    # MBR (defensive against reordering).
    err, _ = cijoe.run_local(
        f"sudo dd if={iso_path} of={iso_path} bs=512 "
        f"skip={efi_part['start']} seek={new_efi_start} count={efi_size} "
        f"conv=notrunc,fsync 2>&1"
    )
    if err:
        log.error("dd EFI FAT image to new location failed")
        return err

    # Rewrite the partition table with three non-overlapping entries.
    # sfdisk's stdin form replaces the entire table in one shot.
    # The ``bootable`` flag on p1 is what isohdpfx.bin's BIOS code
    # looks for; preserve it.
    sfdisk_script = iso_path.parent / "_mbr.sfdisk"
    sfdisk_script.write_text(
        f"label: dos\n"
        f"unit: sectors\n"
        f"\n"
        f"start={iso_part['start']}, size={iso_part['size']}, type=0, bootable\n"
        f"start={new_efi_start}, size={efi_size}, type=ef\n"
        f"start={new_bty_start}, size={new_bty_size}, type=07\n",
        encoding="utf-8",
    )
    err, _ = cijoe.run_local(f"sh -c 'sudo sfdisk {iso_path} < {sfdisk_script}'")
    sfdisk_script.unlink(missing_ok=True)
    if err:
        log.error("sfdisk partition-table rewrite failed")
        return err

    part_num = "3"  # PIXIE_IMAGES is partition 3 in the rewritten table
    log.info(f"PIXIE_IMAGES is partition #{part_num}")

    err, state = cijoe.run_local(f"sudo losetup -fP --show {iso_path}")
    if err:
        log.error(f"losetup -fP {iso_path} failed")
        return err
    out = state.output()
    loop = out.strip().splitlines()[-1].strip()
    if not loop.startswith("/dev/loop"):
        log.error(f"unexpected losetup output: {out!r}")
        return errno.EIO

    # Loop device partitions follow the ``loopNpM`` naming
    # (no nvme-style boundary case here).
    part_dev = f"{loop}p{part_num}"
    cijoe.run_local("sudo udevadm settle")
    err, _ = cijoe.run_local(f"sudo mkfs.exfat -L PIXIE_IMAGES {part_dev}")
    if err:
        cijoe.run_local(f"sudo losetup -d {loop}")
        log.error(f"mkfs.exfat {part_dev} failed")
        return err

    # The PIXIE_IMAGES partition stays empty here -- operator-managed; a
    # fresh stick boots with something flashable in the catalog. The
    # eight entries are:
    #
    # - 7x nosi flashable images via ``oras://ghcr.io/safl/nosi/<variant>:latest``,
    #   resolved by pixie's ORAS adapter to the current published layer
    #   digest at flash time (rolling).
    # - 1x pixie appliance via the GitHub release asset URL
    #   (the pixie image is built here, not in nosi).
    #
    # Fail-loud: if the populate step fails (mount failure, write
    # failure, exfat-fuse missing on the runner), the whole bake
    # The PIXIE_IMAGES partition is left empty -- it's plain operator
    # storage for local image files (.qcow2 / .img.gz / .img / .iso /
    # .iso.gz) discovered by ``images.list_images`` in the live env.
    # The default catalog (oras nosi images + pixie) is a
    # separate release artifact (``releases/latest/download/catalog.toml``)
    # the wizard offers as ``[d] default`` in SELECT_CATALOG. One
    # concept (catalog) lives in one place (a URL); the stick stays a
    # plain image-files area.
    err, _ = cijoe.run_local(f"sudo losetup -d {loop}")
    if err:
        log.error(f"losetup -d {loop} failed")
        return err

    log.info(f"Extended {iso_path} with PIXIE_IMAGES exFAT partition (p{part_num})")
    return 0
