"""
Stage the ramboot payload (netboot bundle + disk image) for the chain test
=========================================================================

Reads the ``pixie-netboot-pc-x86_64`` bake outputs (published by
``cijoe/tasks/netboot-pc.yaml`` or downloaded from an earlier CI
job's ``pixie-netboot-pc-x86_64`` artifact) and materialises the
two things ``pxe_run_chain_test.py`` needs when it runs in ramboot
mode:

- ``_build/test-pxe/bundle.tar.gz``: a real netboot bundle with
  ``vmlinuz``, ``initrd``, and a stub ``manifest.json``. Pixie's
  fetch pipeline expects this exact shape.
- ``_build/test-pxe/disk.img``: a bootable ext4 disk image whose
  root is the netboot-pc squashfs unpacked verbatim -- i.e. a
  real pixie live env. When ramboot mounts this over NBD and
  pivots, systemd starts, ``pixie-on-tty1.service`` fires, and
  the actual ported ``pixie`` CLI runs with ``--server`` +
  ``--mac`` from the kernel cmdline. That CLI's own
  ``_auto_post_inventory`` background thread POSTs an inventory
  blob back to pixie -- no test-side wget shim, no fake init;
  it's the same code path a real hardware boot exercises.

Requires: sudo + losetup + sfdisk + mkfs.ext4 + mount + rsync +
squashfs-tools (unsquashfs). The runner's ubuntu-latest image
ships most of these; ``rsync`` + ``squashfs-tools`` are apt
installs added by the CI job.

Retargetable: False
"""

from __future__ import annotations

import io
import json
import logging as log
import os
import shutil
import subprocess
import tarfile
import tempfile
from argparse import ArgumentParser
from pathlib import Path

_ARTIFACT_ROOT = Path(os.environ.get("PIXIE_NETBOOT_ARTIFACT_DIR") or "").expanduser()
_DEFAULT_ARTIFACT_ROOT = Path.home() / "system_imaging" / "disk"


def _find_artifact_root() -> Path | None:
    """Prefer the operator-set ``PIXIE_NETBOOT_ARTIFACT_DIR`` env var,
    fall back to pixie's default publish dir. Returns None if
    neither carries a matching artifact set."""
    for root in (_ARTIFACT_ROOT, _DEFAULT_ARTIFACT_ROOT):
        if not root or not root.is_dir():
            continue
        if list(root.glob("pixie-netboot-pc-x86_64-v*.vmlinuz")):
            return root
    return None


def _pick_artifacts(root: Path) -> tuple[Path, Path, Path]:
    """Locate the (vmlinuz, initrd, squashfs) trio for the latest
    netboot-pc bake staged under ``root``. Raises if any of the
    three is missing -- the ramboot test needs all three."""
    vmlinuz_matches = sorted(root.glob("pixie-netboot-pc-x86_64-v*.vmlinuz"))
    initrd_matches = sorted(root.glob("pixie-netboot-pc-x86_64-v*.initrd"))
    squashfs_matches = sorted(root.glob("pixie-netboot-pc-x86_64-v*.squashfs"))
    if not vmlinuz_matches or not initrd_matches or not squashfs_matches:
        raise FileNotFoundError(
            f"missing vmlinuz/initrd/squashfs under {root} (need "
            "pixie-netboot-pc-x86_64-v*.{vmlinuz,initrd,squashfs})"
        )
    return vmlinuz_matches[-1], initrd_matches[-1], squashfs_matches[-1]


def _build_bundle(vmlinuz: Path, initrd: Path, out: Path) -> None:
    """Package vmlinuz + initrd + a minimal manifest.json into a tar.gz.
    ``manifest.json`` is a JSON object (pixie's fetcher rejects
    non-objects) with just enough for pixie's operator UI to render
    a row; the ramboot chain itself doesn't consume the fields."""
    manifest = json.dumps(
        {
            "variant": "netboot-pc",
            "arch": "x86_64",
            "kernel_version": "pixie-test-bundle",
            "generated_by": "cijoe/scripts/pxe_ramboot_stage.py",
        }
    ).encode("utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, mode="w:gz") as tar:
        for name, path in (("vmlinuz", vmlinuz), ("initrd", initrd)):
            info = tar.gettarinfo(str(path), arcname=name)
            info.mtime = 0
            with open(path, "rb") as fh:
                tar.addfile(info, fh)
        m_info = tarfile.TarInfo(name="manifest.json")
        m_info.size = len(manifest)
        m_info.mtime = 0
        tar.addfile(m_info, io.BytesIO(manifest))


# Disk image is sized to comfortably hold the unpacked squashfs
# (which is ~1.5-2 GiB uncompressed on our netboot-pc bake) plus
# some headroom for the ext4 filesystem overhead. Bumping past 4
# GiB starts running into common /tmp size limits on GHA runners.
_DISK_SIZE_MIB = 3072


def _make_disk_image(out: Path, squashfs: Path) -> None:
    """Convert the netboot-pc squashfs into a bootable ext4 disk
    image (MBR + one Linux partition + ext4 rootfs whose contents
    are the squashfs unpacked verbatim).

    Ramboot then mounts this over NBD, overlays a tmpfs, and
    pivot_roots into the pixie live env's real systemd userspace.
    ``pixie-on-tty1.service`` fires; the actual ported pixie CLI
    runs with ``--server`` + ``--mac`` from cmdline; the CLI's own
    ``_auto_post_inventory`` POSTs inventory back to pixie."""
    for binary in (
        "sudo",
        "losetup",
        "sfdisk",
        "mkfs.ext4",
        "mount",
        "umount",
        "rsync",
        "unsquashfs",
    ):
        if shutil.which(binary) is None:
            raise FileNotFoundError(
                f"{binary} not on PATH; install e2fsprogs + util-linux + rsync + squashfs-tools"
            )

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as fh:
        fh.truncate(_DISK_SIZE_MIB * 1024 * 1024)

    log.error(f"pxe_ramboot_stage: writing MBR partition table to {out}")
    subprocess.run(
        ["sfdisk", "--quiet", str(out)],
        input=",,L\n",
        text=True,
        check=True,
    )

    loop = subprocess.check_output(
        ["sudo", "-n", "losetup", "-fP", "--show", str(out)],
        text=True,
    ).strip()
    log.error(f"pxe_ramboot_stage: attached loop device {loop}")
    part_dev = f"{loop}p1"
    # Manual tmpdir management: ``unsquashfs`` runs under sudo and
    # writes root-owned files, so Python's ``TemporaryDirectory``
    # cleanup fails with PermissionError. Clean up with sudo at
    # the end instead.
    tmp = Path(tempfile.mkdtemp(prefix="pixie-ramboot-"))
    try:
        subprocess.run(
            ["sudo", "-n", "mkfs.ext4", "-q", "-L", "pixie-root", part_dev],
            check=True,
        )
        mnt = tmp / "mnt"
        unsq = tmp / "squashfs"
        mnt.mkdir()
        log.error(f"pxe_ramboot_stage: unpacking {squashfs.name} -> {unsq}")
        subprocess.run(
            ["sudo", "-n", "unsquashfs", "-d", str(unsq), "-no-progress", str(squashfs)],
            check=True,
        )
        log.error(f"pxe_ramboot_stage: mounting {part_dev} -> {mnt}")
        subprocess.run(["sudo", "-n", "mount", part_dev, str(mnt)], check=True)
        try:
            _rsync_into_rootfs(unsq, mnt)
        finally:
            subprocess.run(["sudo", "-n", "umount", str(mnt)], check=True)
    finally:
        subprocess.run(["sudo", "-n", "losetup", "-d", loop], check=False)
        subprocess.run(["sudo", "-n", "rm", "-rf", str(tmp)], check=False)

    log.error(
        f"pxe_ramboot_stage: rootfs disk ready at {out} "
        f"({out.stat().st_size} bytes on-disk; disk size {_DISK_SIZE_MIB} MiB)"
    )


def _rsync_into_rootfs(src: Path, dst: Path) -> None:
    """Copy the unpacked squashfs tree onto the mounted ext4
    filesystem. Preserve permissions, owners, xattrs so the live
    env's setuid + capabilities survive."""
    log.error(f"pxe_ramboot_stage: rsync {src}/ -> {dst}/")
    # ``-a`` (archive: r + l + p + t + g + o + D) + ``-A`` (ACLs) +
    # ``-X`` (xattrs) + ``--numeric-ids`` (don't remap uids/gids
    # from name mismatches between the CI runner and the chroot).
    subprocess.run(
        [
            "sudo",
            "-n",
            "rsync",
            "-aAX",
            "--numeric-ids",
            f"{src}/",
            f"{dst}/",
        ],
        check=True,
    )
    log.error("pxe_ramboot_stage: rsync complete")


def add_args(parser: ArgumentParser) -> None:
    del parser


def main(args, cijoe) -> int:
    del args, cijoe
    root = _find_artifact_root()
    if root is None:
        candidates = [c for c in (_ARTIFACT_ROOT, _DEFAULT_ARTIFACT_ROOT) if c]
        log.error("no netboot-pc vmlinuz/initrd found under %s", " or ".join(map(str, candidates)))
        for c in candidates:
            if c.is_dir():
                entries = sorted(p.name for p in c.iterdir())
                log.error(f"listing {c} ({len(entries)} entries):")
                for name in entries[:40]:
                    log.error(f"  {name}")
            else:
                log.error(f"{c}: does not exist or is not a directory")
        return 1

    workspace = Path.cwd() / "_build" / "test-pxe"
    workspace.mkdir(parents=True, exist_ok=True)
    bundle_out = workspace / "bundle.tar.gz"
    disk_out = workspace / "disk.img"

    vmlinuz, initrd, squashfs = _pick_artifacts(root)
    log.error(f"pxe_ramboot_stage: cwd={Path.cwd()}")
    log.error(f"pxe_ramboot_stage: workspace={workspace}")
    log.error(f"pxe_ramboot_stage: building {bundle_out} from {vmlinuz.name} + {initrd.name}")
    _build_bundle(vmlinuz, initrd, bundle_out)
    log.error(f"pxe_ramboot_stage: bundle staged ({bundle_out.stat().st_size} bytes)")

    log.error(f"pxe_ramboot_stage: converting {squashfs.name} -> {disk_out} (real pixie live env)")
    _make_disk_image(disk_out, squashfs)
    log.error(f"pxe_ramboot_stage: disk staged ({disk_out.stat().st_size} bytes)")
    return 0
