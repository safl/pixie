"""
Build the pixie network-boot live env via live-build
==================================================

Drives Debian's live-build to produce kernel + initrd + squashfs
artifacts that pixie hosts over HTTP for PXE clients to chain
into via iPXE. live-build runs debootstrap, mksquashfs, and
mkinitramfs directly on the build host, no QEMU. Same chroot config
tree as ``usb_iso_build``; only the binary-images output mode
differs.

Workflow:

1. Copy ``pixie-media/live-build/`` (the live-build config tree) into
   a fresh ``cijoe/_build/netboot/`` working dir.
2. Run ``sudo lb clean --all`` (idempotency) then ``sudo lb build``.
   live-build needs root for chroot operations; the build host (CI
   runner or local dev) must have passwordless sudo.
3. Regenerate the initrd with dracut inside the finished chroot
   (``_regenerate_netboot_initrd_with_dracut``). live-build builds a
   live-boot initrd, but the netboot flow boots a dracut ``root=live:``
   cmdline; ``--initramfs dracut-live`` can't be used because
   live-build's stock 5050-dracut hook is broken on trixie. So we build
   normally (keeping the signed kernel + squashfs) and swap in a dracut
   initrd afterwards.
4. Publish vmlinuz + squashfs from ``binary/`` and the dracut initrd
   from the chroot to the ``publish.dir`` from the cijoe config, renamed
   to ``pixie-netboot-pc-x86_64.{vmlinuz,initrd,squashfs}``.
5. Write a single sha256 manifest covering all three artifacts.

The cwd at run time is ``cijoe/`` (the Makefile cd's there before
invoking cijoe), so the pixie-media tree lives at
``Path.cwd().parent / "pixie-media"`` and the build scratch dir is
``Path.cwd() / "_build" / "netboot"``.

Skipped for any variant other than ``netboot-pc``.

Retargetable: False
"""

from __future__ import annotations

import errno
import logging as log
import os
import shutil
from argparse import ArgumentParser
from pathlib import Path

# Reuse the version reader from the USB iso build script. Same
# pyproject.toml lookup, same placeholder convention. Kept as a
# script-level import (rather than a duplicated helper) so a
# single source of truth governs how the bake derives the
# stamped version string.
from usb_iso_build import _read_pixie_version

PUBLISH_BASENAME_FMTS = (
    "pixie-netboot-pc-x86_64-v{version}.vmlinuz",
    "pixie-netboot-pc-x86_64-v{version}.initrd",
    "pixie-netboot-pc-x86_64-v{version}.squashfs",
)


def _resolve_chroot_kver(cijoe, chroot_dir: Path) -> str:
    """Resolve the kernel version whose modules live in the built chroot.

    Prefer the version that advertises a ``build`` symlink (kernel headers
    -> the kernel the r8125 DKMS module was compiled against, and the one
    live-build installed), mirroring how the r8125 hook selects KVER.
    Falls back to the highest-sorted version if none advertise headers.
    """
    err, state = cijoe.run_local(f"sudo ls -1 {chroot_dir}/lib/modules")
    names = [n for n in (state.output() or "").split() if n] if not err else []
    for name in names:
        e, _ = cijoe.run_local(f"sudo test -e {chroot_dir}/lib/modules/{name}/build")
        if not e:
            return name
    return sorted(names)[-1] if names else ""


def _regenerate_netboot_initrd_with_dracut(cijoe, build_dir: Path) -> tuple[int, Path | None]:
    """Rebuild the netboot initrd with dracut, inside the finished chroot.

    live-build builds the netboot-pc image with its DEFAULT initramfs
    (live-boot), NOT ``--initramfs dracut-live``: that flag drags in
    live-build's stock 5050-dracut hook, which is broken on Debian trixie
    (it force-swaps the signed kernel for ``-unsigned`` and purges
    ``initramfs-tools`` mid-transaction, leaving dracut unconfigured and
    failing the build). So the initrd live-build produced is a live-boot
    one -- wrong for the netboot flow, whose server template boots
    ``root=live:<url>`` (dracut dmsquash-live + livenet).

    We fix it deterministically here, after ``lb build`` succeeds: run
    dracut INSIDE the built chroot -- which has the signed kernel's
    modules (incl. the r8125 DKMS driver), the dracut +
    dmsquash-live/livenet/network module packages, and
    ``/etc/dracut.conf.d/10-pixie-live-env.conf`` -- to overwrite
    ``/boot/initrd.img-<kver>`` with a dracut initrd, then verify it
    actually carries dmsquash-live + livenet. Returns
    ``(0, <chroot initrd path>)`` on success, ``(errno, None)`` otherwise.
    """
    chroot_dir = build_dir / "chroot"
    kver = _resolve_chroot_kver(cijoe, chroot_dir)
    if not kver:
        log.error(f"could not resolve kernel version under {chroot_dir}/lib/modules")
        return errno.ENOENT, None
    log.info(f"Regenerating netboot initrd with dracut for kernel {kver}")

    # Bind pseudo-filesystems so any udev/modinfo probing inside dracut
    # has them (``--no-hostonly`` already skips host device detection, but
    # this keeps the run robust). ALWAYS unmounted in the finally below:
    # a stray ``/dev`` bind-mount left under the chroot would be
    # catastrophic for the next run's ``sudo rm -rf {build_dir}``.
    cijoe.run_local(f"sudo mount -t proc proc {chroot_dir}/proc")
    cijoe.run_local(f"sudo mount -t sysfs sys {chroot_dir}/sys")
    cijoe.run_local(f"sudo mount --bind /dev {chroot_dir}/dev")
    cijoe.run_local(f"sudo mount --bind /dev/pts {chroot_dir}/dev/pts")
    try:
        initrd_rel = f"/boot/initrd.img-{kver}"
        # Separate ``--add`` per module avoids nested-quoting fragility.
        # ``--add-drivers r8125`` forces the out-of-tree 2.5GbE DKMS
        # driver in; the rest of the broad in-tree set comes from
        # hostonly=no in the conf.d drop-in.
        dracut_cmd = (
            f"sudo chroot {chroot_dir} dracut --no-hostonly --force "
            f"--add dmsquash-live --add livenet --add network "
            f"--add-drivers r8125 {initrd_rel} {kver}"
        )
        err, _ = cijoe.run_local(dracut_cmd)
        if err:
            log.error("dracut initrd regeneration failed")
            return err, None
        # Prove the fetch chain is actually baked in -- we can't boot the
        # initrd here, but ``lsinitrd`` lists the dracut modules it holds.
        err, state = cijoe.run_local(f"sudo chroot {chroot_dir} lsinitrd {initrd_rel}")
        listing = state.output() if not err else ""
        missing = [m for m in ("dmsquash-live", "livenet") if m not in listing]
        if missing:
            log.error(f"regenerated initrd is missing dracut modules: {missing}")
            return errno.EINVAL, None
        log.info("dracut initrd verified: carries dmsquash-live + livenet")
        return 0, chroot_dir / "boot" / f"initrd.img-{kver}"
    finally:
        for mnt in ("dev/pts", "dev", "sys", "proc"):
            cijoe.run_local(f"sudo umount -l {chroot_dir}/{mnt}")


def add_args(parser: ArgumentParser):
    del parser  # no flags; signature kept for cijoe consistency


def main(args, cijoe):
    del args
    cijoe_dir = Path.cwd()
    pixie_media = cijoe_dir.parent / "pixie-media"

    variant = cijoe.getconf("pixie", {}).get("variant", "")
    if variant != "netboot-pc":
        log.info(f"Skipping live_build (variant={variant!r}; only 'netboot-pc' runs lb netboot)")
        return 0

    images = cijoe.getconf("system-imaging.images", {})
    image = images.get("pixie-netboot-pc-x86_64")
    if not image:
        log.error("missing system-imaging.images.pixie-netboot-pc-x86_64 in config")
        return errno.EINVAL

    publish_dir_str = image.get("publish", {}).get("dir")
    if not publish_dir_str:
        log.error("system-imaging.images.pixie-netboot-pc-x86_64.publish.dir is unset")
        return errno.EINVAL
    publish_dir = Path(publish_dir_str)
    publish_dir.mkdir(parents=True, exist_ok=True)

    build_dir = cijoe_dir / "_build" / "netboot"
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
    # in the copied tree before ``lb build`` runs. Mirrors the
    # equivalent block in ``cijoe/scripts/usb_iso_build.py``: same
    # placeholder convention, same set of files (auto/config,
    # /etc/issue, /etc/motd, /etc/profile.d/pixie-version.sh, plymouth
    # theme). Without this step the pixie-netboot live env boots with
    # the literal ``__PIXIE_VERSION__`` in /etc/issue / motd / shell
    # prompt -- operator sees the placeholder instead of the real
    # version on tty2 and can't match a booted target back to a
    # release.
    pixie_version = _read_pixie_version(cijoe_dir)
    publish_basenames = tuple(fmt.format(version=pixie_version) for fmt in PUBLISH_BASENAME_FMTS)
    sha256_basename = f"pixie-netboot-pc-x86_64-v{pixie_version}.sha256"
    log.info(f"Stamping pixie version {pixie_version} into live-build tree")
    err, _ = cijoe.run_local(
        f"sh -c 'grep -rlF __PIXIE_VERSION__ {build_dir} | "
        f"xargs --no-run-if-empty sed -i s/__PIXIE_VERSION__/{pixie_version}/g'"
    )
    if err:
        log.error("__PIXIE_VERSION__ substitution failed")
        return err

    log.info(f"Running lb build in {build_dir}")
    err, _ = cijoe.run_local(f"sh -c 'cd {build_dir} && sudo lb clean --all && sudo lb build'")
    if err:
        log.error("lb build failed; see live-build.log under the build dir")
        return err

    # Swap the live-boot initrd live-build produced for a dracut one
    # (dmsquash-live + livenet + network). See the helper's docstring for
    # WHY this is a post-build step rather than ``--initramfs dracut-live``.
    initrd_err, dracut_initrd = _regenerate_netboot_initrd_with_dracut(cijoe, build_dir)
    if initrd_err or dracut_initrd is None:
        log.error("failed to regenerate the netboot initrd with dracut")
        return initrd_err or errno.EINVAL

    # Locate the kernel + squashfs artifacts. live-build's netboot output
    # paths vary between releases (``binary/live/`` direct, tarballed under
    # ``binary/`` as ``live-image-amd64.tar.xz``, or split across both);
    # recursive globs find them wherever they ended up. Filter
    # ``vmlinuz*`` matches to skip the chroot/boot/ copy lb leaves behind
    # for caching. The INITRD does NOT come from these globs: the binary
    # one is the wrong (live-boot) initrd; we publish the dracut initrd
    # regenerated above straight from the chroot instead.
    def _outside_chroot(p: Path) -> bool:
        return "chroot" not in p.parts

    # Dump the build dir for diagnostics so the next time live-build's
    # output layout changes we can see the new shape in the logs.
    cijoe.run_local(f"sudo find {build_dir} -maxdepth 4 -type d 2>/dev/null | head -60")

    kernels = sorted(p for p in build_dir.rglob("vmlinuz*") if _outside_chroot(p))
    squashfses = sorted(p for p in build_dir.rglob("filesystem.squashfs") if _outside_chroot(p))

    if not kernels:
        log.error(f"no kernel matching vmlinuz* under {build_dir} (excluding chroot)")
        cijoe.run_local(f"sudo find {build_dir} -name 'vmlinuz*' 2>/dev/null")
        return errno.ENOENT
    if not squashfses:
        log.error(f"no filesystem.squashfs under {build_dir}")
        cijoe.run_local(f"sudo find {build_dir} -name '*.squashfs' 2>/dev/null")
        return errno.ENOENT
    squashfs = squashfses[0]

    publish_map = (
        (kernels[0], publish_dir / publish_basenames[0]),
        (dracut_initrd, publish_dir / publish_basenames[1]),
        (squashfs, publish_dir / publish_basenames[2]),
    )

    # The artifacts are owned by root (live-build wrote them under sudo);
    # use ``sudo cp`` then ``sudo chown`` to land them under the user's
    # publish dir with the user's uid/gid so subsequent steps don't need
    # privileges.
    uid, gid = os.geteuid(), os.getegid()
    for src, dst in publish_map:
        err, _ = cijoe.run_local(f"sudo cp {src} {dst}")
        if err:
            log.error(f"failed to publish {src} -> {dst}")
            return err
        cijoe.run_local(f"sudo chown {uid}:{gid} {dst}")
        log.info(f"published {dst}")

    sha256_path = publish_dir / sha256_basename
    err, _ = cijoe.run_local(
        f"sh -c 'cd {publish_dir} && sha256sum {' '.join(publish_basenames)} > {sha256_path}'"
    )
    if err:
        log.error("failed computing sha256 manifest")
        return err

    cijoe.run_local(f"cat {sha256_path}")
    cijoe.run_local(f"ls -la {publish_dir}/pixie-netboot-pc-x86_64.*")

    return 0
