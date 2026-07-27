"""
Build the pixie USB live env (iso-hybrid) via archiso / mkarchiso
=================================================================

Produces the ``pixie-usbboot-pc-x86_64`` hybrid ISO -- an Arch Linux
live image (the ``pixie-media/archiso`` profile) that boots both from
CD media and from a USB stick (BIOS + UEFI), and mounts + boots via BMC
virtual media (Redfish / PiKVM / JetKVM). Replaces the retired Debian
live-build pipeline (``usb_iso_build.py`` + ``pixie-media/live-build``):
the Arch kernel + full linux-firmware carry the NIC drivers in-tree, so
the r8125 DKMS stack is gone.

Workflow:

1. Copy ``pixie-media/archiso`` (the mkarchiso profile) into a fresh
   ``cijoe/_build/usbboot-pc/profile``.
2. Build + vendor the pixie CLI tree (``uv build --wheel`` -> ``pip
   install --target``, pixie-lab + rich + tomlkit) and stage it into the
   profile's ``airootfs/opt/pixie/lib`` so the live env's stock python3
   runs it (the same vendoring model the nbdboot live-env image uses).
3. Stamp the pixie-lab version into every ``__PIXIE_VERSION__``
   placeholder in the copied profile (profiledef, bootloader menus,
   /etc/motd, /etc/issue, /etc/profile.d).
4. Run ``mkarchiso`` inside a privileged, host-networked container
   (podman or docker) -- mkarchiso needs root + ``mount --bind /dev``
   for pacstrap, which a rootless/unprivileged container can't provide.
5. Publish the resulting ISO to ``publish.dir`` as
   ``pixie-usbboot-pc-x86_64-v{version}.iso``.
6. Append a writable ``PIXIE_IMGS`` exFAT partition to the trailing edge
   (sfdisk + losetup + mkfs.exfat) so the single dd-able file carries
   both the boot path and the operator's image-catalog area. mkarchiso's
   isohybrid output is MBR (type 0 ISO9660 + type ef EFI, already
   non-overlapping), so unlike the old live-build path this only appends
   a third partition -- no EFI relocation.
7. Verify the structure (reusing ``media_common._verify_iso``) and
   write a sha256 manifest.

The cwd at run time is ``cijoe/`` (the Makefile cd's there), so the
pixie-media tree lives at ``Path.cwd().parent / "pixie-media"`` and the
build scratch dir is ``Path.cwd() / "_build" / "usbboot-pc"``.

Requires: a container runtime (podman or docker) able to run
``--privileged --network=host``, passwordless sudo (for the exFAT append
via losetup/mkfs.exfat), ``uv`` on PATH, and ``exfatprogs`` +
``gptfdisk``/``util-linux`` on the host for the append + verify.

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

# Reuse the vendored-CLI assembler from the nbdboot live-env builder.
from live_env_image import _build_vendored_cli

# Shared media-build helpers: the version reader + the post-append
# structural verifier (build-system-agnostic -- the archiso isohybrid
# lands the same MBR layout the verifier asserts).
from media_common import _read_pixie_version, _verify_iso

PUBLISH_BASENAME_FMT = "pixie-usbboot-pc-x86_64-v{version}.iso"

# The stock archlinux container image; mkarchiso + its build deps are
# pulled on top via ``pacman -S archiso`` in the in-container script.
ARCH_IMAGE = "docker.io/archlinux"

# Just the PIXIE_IMGS partition stub; the bake doesn't populate it.
# Operators drop image files (.qcow2 / .img.gz / .img / .iso / .iso.gz)
# onto the partition; the live env's image-root scan picks them up. The
# partition auto-grows to fill the disk on first boot via
# pixie-usb-grow.service, so 32 MiB is the bake-time minimum, not the
# runtime size (exfatprogs mkfs.exfat refuses very small volumes).
TRAILING_EXFAT_SIZE = "32M"


def add_args(parser: ArgumentParser):
    del parser  # no flags; signature kept for cijoe consistency


def _container_runtime(cijoe) -> str | None:
    """Pick a container runtime able to run ``--privileged``: prefer
    podman (the lab / WSL2 dev host), fall back to docker (GHA)."""
    for rt in ("podman", "docker"):
        err, _ = cijoe.run_local(f"sh -c 'command -v {rt} >/dev/null 2>&1'")
        if not err:
            return rt
    return None


def main(args, cijoe):
    del args
    cijoe_dir = Path.cwd()
    repo_root = cijoe_dir.parent
    pixie_media = repo_root / "pixie-media"

    variant = cijoe.getconf("pixie", {}).get("variant", "")
    if variant != "usbboot-pc":
        log.info(f"Skipping archiso_build (variant={variant!r}; only 'usbboot-pc' runs mkarchiso)")
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

    profile_src = pixie_media / "archiso"
    if not profile_src.exists():
        log.error(f"archiso profile tree missing: {profile_src}")
        return errno.ENOENT

    runtime = _container_runtime(cijoe)
    if runtime is None:
        log.error("no container runtime found (need podman or docker for mkarchiso)")
        return errno.ENOENT
    log.info(f"using container runtime: {runtime}")

    build_dir = cijoe_dir / "_build" / "usbboot-pc"
    if build_dir.exists():
        # mkarchiso writes root-owned trees (work/ + out/); rm needs sudo.
        err, _ = cijoe.run_local(f"sudo rm -rf {build_dir}")
        if err:
            log.error(f"failed to remove stale build dir {build_dir}")
            return err
    build_dir.mkdir(parents=True)

    # 1. Copy the profile tree into the working dir.
    profile = build_dir / "profile"
    shutil.copytree(profile_src, profile, symlinks=True)

    pixie_version = _read_pixie_version(cijoe_dir)
    iso_basename = PUBLISH_BASENAME_FMT.format(version=pixie_version)

    # 2. Vendored CLI tree -> profile airootfs/opt/pixie/lib.
    log.info("assembling the vendored pixie CLI tree")
    vendor = _build_vendored_cli(cijoe, repo_root, build_dir)
    if vendor is None:
        return errno.EIO
    cli_dst = profile / "airootfs" / "opt" / "pixie" / "lib"
    err, _ = cijoe.run_local(f"sh -c 'rm -rf {cli_dst} && mkdir -p {cli_dst}'")
    if err:
        log.error(f"failed resetting {cli_dst}")
        return err
    err, _ = cijoe.run_local(f"sh -c 'cp -a {vendor}/. {cli_dst}/'")
    if err:
        log.error("failed staging the vendored CLI into the profile airootfs")
        return err
    # Drop the committed .gitkeep so it doesn't ship in the live env.
    cijoe.run_local(f"rm -f {cli_dst}/.gitkeep")

    # 3. Stamp __PIXIE_VERSION__ across the copied profile.
    log.info(f"stamping pixie version {pixie_version} into the profile")
    err, _ = cijoe.run_local(
        f"sh -c 'grep -rlF __PIXIE_VERSION__ {profile} | "
        f"xargs --no-run-if-empty sed -i s/__PIXIE_VERSION__/{pixie_version}/g'"
    )
    if err:
        log.error("__PIXIE_VERSION__ substitution failed")
        return err

    # 4. Run mkarchiso in a privileged container.
    out_dir = build_dir / "out"
    in_container = profile / "mkarchiso-in-container.sh"
    if not in_container.exists():
        log.error(f"in-container build script missing: {in_container}")
        return errno.ENOENT
    log.info(f"running mkarchiso in a privileged {runtime} container ({ARCH_IMAGE})")
    err, _ = cijoe.run_local(
        f"sudo {runtime} run --rm --privileged --network=host "
        f"-v {build_dir}:/build {ARCH_IMAGE} "
        f"bash /build/profile/mkarchiso-in-container.sh /build/profile /build/out"
    )
    if err:
        log.error("mkarchiso failed; see the container output above")
        return err

    # 5. Locate + publish the ISO (root-owned; mkarchiso ran under sudo).
    isos = sorted(out_dir.glob("*.iso"))
    if not isos:
        log.error(f"no ISO produced under {out_dir}")
        cijoe.run_local(f"sudo ls -la {out_dir}")
        return errno.ENOENT
    iso = isos[0]
    uid, gid = os.geteuid(), os.getegid()
    dst = publish_dir / iso_basename
    err, _ = cijoe.run_local(f"sudo cp {iso} {dst}")
    if err:
        log.error(f"failed to publish {iso} -> {dst}")
        return err
    cijoe.run_local(f"sudo chown {uid}:{gid} {dst}")
    log.info(f"published {dst}")

    # 6. Append the trailing PIXIE_IMGS exFAT partition.
    err = _append_pixie_imgs(cijoe, dst)
    if err:
        return err

    # 7. Structural verification (3 MBR partitions: type 0 / ef / 07).
    err = _verify_iso(cijoe, dst)
    if err:
        return err

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


def _append_pixie_imgs(cijoe, iso_path: Path) -> int:
    """Append a trailing exFAT partition labelled PIXIE_IMGS to the
    mkarchiso isohybrid image.

    mkarchiso's iso-hybrid output is an MBR (``label: dos``) disk image
    with two non-overlapping partitions: p1 ISO9660 (type 0, bootable,
    the isohybrid BIOS payload) and p2 EFI (type ef, the UEFI El Torito
    FAT image). Unlike the old Debian live-build output, the EFI entry
    does NOT overlap the ISO9660 range, so there is nothing to relocate:
    we just add p3 filling the space we truncate on.

    Steps:

    1. ``truncate -s +<N>`` extends the file with sparse zeros.
    2. Read the existing MBR via ``sfdisk --json``; keep p1 + p2 exactly
       as-is (the isohybrid boot code in MBR bytes 0..445 is untouched --
       sfdisk only rewrites the 446..510 partition-table area).
    3. Rewrite the table with a third entry (type 07, PIXIE_IMGS)
       starting 8-sector-aligned after the highest existing partition.
    4. ``losetup -fP`` + ``mkfs.exfat -L PIXIE_IMGS`` on p3, left empty
       (operator-managed image storage; auto-grows on first boot).
    """
    log.info(f"Appending +{TRAILING_EXFAT_SIZE} PIXIE_IMGS exFAT to {iso_path}")

    err, _ = cijoe.run_local(f"truncate -s +{TRAILING_EXFAT_SIZE} {iso_path}")
    if err:
        log.error(f"truncate +{TRAILING_EXFAT_SIZE} failed on {iso_path}")
        return err

    err, state = cijoe.run_local(f"sudo sfdisk --json {iso_path}")
    if err:
        log.error("sfdisk --json failed")
        return err
    try:
        table = json.loads(state.output())
        ptable = table["partitiontable"]
        partitions = ptable["partitions"]
    except (json.JSONDecodeError, KeyError) as exc:
        log.error(f"could not parse sfdisk --json: {exc}")
        return errno.EIO

    if ptable.get("label") != "dos":
        log.error(
            f"expected an MBR (dos) label on the mkarchiso isohybrid, got {ptable.get('label')!r}"
        )
        return errno.EIO
    if len(partitions) != 2:
        log.error(f"expected 2 partitions (ISO9660 + EFI) pre-append, found {len(partitions)}")
        return errno.EIO

    # Preserve p1 + p2 verbatim; place p3 after the highest end, aligned.
    highest_end = max(p["start"] + p["size"] for p in partitions)
    p3_start = ((highest_end + 7) // 8) * 8

    err, state = cijoe.run_local(f"stat -c %s {iso_path}")
    if err:
        log.error("stat failed on the iso file")
        return err
    file_sectors = int(state.output().strip()) // 512
    p3_size = file_sectors - p3_start
    if p3_size <= 0:
        log.error(f"no room for PIXIE_IMGS: file_sectors={file_sectors}, p3_start={p3_start}")
        return errno.EIO

    lines = ["label: dos", "unit: sectors", ""]
    for p in partitions:
        entry = f"start={p['start']}, size={p['size']}, type={p['type']}"
        if p.get("bootable"):
            entry += ", bootable"
        lines.append(entry)
    lines.append(f"start={p3_start}, size={p3_size}, type=07")
    sfdisk_script = iso_path.parent / "_mbr.sfdisk"
    sfdisk_script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info(f"PIXIE_IMGS at sectors {p3_start}..{p3_start + p3_size - 1}")

    err, _ = cijoe.run_local(f"sh -c 'sudo sfdisk {iso_path} < {sfdisk_script}'")
    sfdisk_script.unlink(missing_ok=True)
    if err:
        log.error("sfdisk partition-table rewrite failed")
        return err

    err, state = cijoe.run_local(f"sudo losetup -fP --show {iso_path}")
    if err:
        log.error(f"losetup -fP {iso_path} failed")
        return err
    loop = state.output().strip().splitlines()[-1].strip()
    if not loop.startswith("/dev/loop"):
        log.error(f"unexpected losetup output: {loop!r}")
        return errno.EIO

    cijoe.run_local("sudo udevadm settle")
    err, _ = cijoe.run_local(f"sudo mkfs.exfat -L PIXIE_IMGS {loop}p3")
    if err:
        cijoe.run_local(f"sudo losetup -d {loop}")
        log.error(f"mkfs.exfat {loop}p3 failed")
        return err
    err, _ = cijoe.run_local(f"sudo losetup -d {loop}")
    if err:
        log.error(f"losetup -d {loop} failed")
        return err

    log.info(f"Appended PIXIE_IMGS exFAT partition (p3) to {iso_path}")
    return 0
