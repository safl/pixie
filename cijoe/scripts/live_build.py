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
3. Publish ``binary/live/{vmlinuz,initrd.img,filesystem.squashfs}``
   to the ``publish.dir`` from the cijoe config, renamed to
   ``pixie-netboot-pc-x86_64.{vmlinuz,initrd,squashfs}``.
4. Write a single sha256 manifest covering all three artifacts.

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

    # Locate the artifacts. live-build's netboot output paths vary
    # between releases (``binary/live/`` direct, tarballed under
    # ``binary/`` as ``live-image-amd64.tar.xz``, or split across
    # both); recursive globs find them wherever they ended up.
    # Filter ``vmlinuz*`` matches to skip the chroot/boot/ copy that
    # lb leaves behind for caching.
    def _outside_chroot(p: Path) -> bool:
        return "chroot" not in p.parts

    # Dump the build dir for diagnostics so the next time live-build's
    # output layout changes we can see the new shape in the logs.
    cijoe.run_local(f"sudo find {build_dir} -maxdepth 4 -type d 2>/dev/null | head -60")

    kernels = sorted(p for p in build_dir.rglob("vmlinuz*") if _outside_chroot(p))
    initrds = sorted(p for p in build_dir.rglob("initrd.img*") if _outside_chroot(p))
    squashfses = sorted(p for p in build_dir.rglob("filesystem.squashfs") if _outside_chroot(p))

    if not kernels:
        log.error(f"no kernel matching vmlinuz* under {build_dir} (excluding chroot)")
        cijoe.run_local(f"sudo find {build_dir} -name 'vmlinuz*' 2>/dev/null")
        return errno.ENOENT
    if not initrds:
        log.error(f"no initrd matching initrd.img* under {build_dir} (excluding chroot)")
        cijoe.run_local(f"sudo find {build_dir} -name 'initrd.img*' 2>/dev/null")
        return errno.ENOENT
    if not squashfses:
        log.error(f"no filesystem.squashfs under {build_dir}")
        cijoe.run_local(f"sudo find {build_dir} -name '*.squashfs' 2>/dev/null")
        return errno.ENOENT
    squashfs = squashfses[0]

    publish_map = (
        (kernels[0], publish_dir / publish_basenames[0]),
        (initrds[0], publish_dir / publish_basenames[1]),
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
