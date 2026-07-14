"""
Stage the ramboot payload (netboot bundle + disk image) for the chain test
=========================================================================

Reads the ``pixie-netboot-pc-x86_64`` bake outputs (published by
``cijoe/tasks/netboot-pc.yaml`` or downloaded from an earlier CI
job's ``pixie-netboot-pc-x86_64`` artifact) and materialises the two
things ``pxe_run_chain_test.py`` needs when it runs in ramboot
mode:

- ``_build/test-pxe/bundle.tar.gz``: a real netboot bundle with
  ``vmlinuz``, ``initrd``, and a stub ``manifest.json``. Pixie's
  fetch pipeline expects this exact shape.
- ``_build/test-pxe/disk.img``: a 64 MiB blob for pixie's NBD
  supervisor to serve. The ramboot chain we're testing stops at
  the initrd's ramboot script (well before mount / pivot), so the
  bytes don't need to be a valid filesystem.

Skips (rc=0) when no netboot artifacts are staged -- lets the same
task file run cleanly on a workstation without a prior bake.

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
import textwrap
from argparse import ArgumentParser
from pathlib import Path

_ARTIFACT_ROOT = Path(os.environ.get("PIXIE_NETBOOT_ARTIFACT_DIR") or "").expanduser()
_DEFAULT_ARTIFACT_ROOT = Path.home() / "system_imaging" / "disk"


def _find_artifact_root() -> Path | None:
    """Prefer the operator-set ``PIXIE_NETBOOT_ARTIFACT_DIR`` env var,
    fall back to bty's / pixie's default publish dir. Returns None if
    neither carries a matching artifact set."""
    for root in (_ARTIFACT_ROOT, _DEFAULT_ARTIFACT_ROOT):
        if not root or not root.is_dir():
            continue
        # Any file matching ``pixie-netboot-pc-x86_64-v*.vmlinuz``
        # counts as staged; assume the sibling initrd is beside it.
        if list(root.glob("pixie-netboot-pc-x86_64-v*.vmlinuz")):
            return root
    return None


def _pick_artifacts(root: Path) -> tuple[Path, Path]:
    vmlinuz_matches = sorted(root.glob("pixie-netboot-pc-x86_64-v*.vmlinuz"))
    initrd_matches = sorted(root.glob("pixie-netboot-pc-x86_64-v*.initrd"))
    if not vmlinuz_matches or not initrd_matches:
        raise FileNotFoundError(
            f"missing vmlinuz/initrd under {root} (need "
            "pixie-netboot-pc-x86_64-v*.{vmlinuz,initrd})"
        )
    # Latest version wins on lexicographic sort of the vX.Y.Z field.
    return vmlinuz_matches[-1], initrd_matches[-1]


def _build_bundle(vmlinuz: Path, initrd: Path, out: Path) -> None:
    """Package vmlinuz + initrd + a minimal manifest.json into a tar.gz.
    ``manifest.json`` is a JSON object (pixie's fetcher rejects
    non-objects) with just enough for pixie's operator UI to render a
    row; the ramboot chain itself doesn't consume the fields."""
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


def _make_disk_image(out: Path, size_mib: int = 128) -> None:
    """Build a real bootable disk image so the ramboot chain can
    complete pivot_root and land in userspace.

    The image is: MBR + one Linux partition covering the whole
    disk, ext4, with a busybox-driven ``/init`` that mounts /proc
    + /sys + /dev, echoes ``pixie-init: hello from rootfs`` to
    /dev/kmsg (surfaces on the QEMU serial console), then sleeps
    forever. That's the smallest thing that:

    - Gives the ramboot script's ``partx --add`` scan a partition
      to pick as root
    - Has a valid /init for initramfs-tools' /init to
      pivot_root + exec into
    - Emits a stable marker on serial so the chain test can
      assert pivot_root succeeded

    Anything more (network, TUI, inventory POST) is deferred to
    a follow-up PR that lands a proper Debian-derived rootfs.

    Needs sudo + losetup + sfdisk + mkfs.ext4 + busybox-static +
    mount. Aborts on any missing dependency."""
    for binary in ("sudo", "losetup", "sfdisk", "mkfs.ext4", "mount", "umount"):
        if shutil.which(binary) is None:
            raise FileNotFoundError(f"{binary} not on PATH; install e2fsprogs + util-linux")
    busybox = _find_busybox_static()
    if busybox is None:
        raise FileNotFoundError(
            "busybox-static not found; install `busybox-static` (apt) or ship a static binary"
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as fh:
        fh.truncate(size_mib * 1024 * 1024)

    # MBR partition table with one Linux partition spanning the whole
    # disk. sfdisk reads directives from stdin; ',,L' = default start,
    # rest of disk, Linux type.
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
    try:
        subprocess.run(
            ["sudo", "-n", "mkfs.ext4", "-q", "-L", "pixie-root", part_dev],
            check=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            mnt = Path(tmp)
            subprocess.run(["sudo", "-n", "mount", part_dev, str(mnt)], check=True)
            try:
                _populate_rootfs(mnt, busybox)
            finally:
                subprocess.run(["sudo", "-n", "umount", str(mnt)], check=True)
    finally:
        subprocess.run(["sudo", "-n", "losetup", "-d", loop], check=False)

    log.error(f"pxe_ramboot_stage: rootfs disk ready at {out} ({out.stat().st_size} bytes)")


def _find_busybox_static() -> Path | None:
    """busybox-static (apt package) lands as ``/bin/busybox`` in
    Debian/Ubuntu. Some distros ship it under ``/usr/bin/busybox``
    or ``/usr/lib/busybox-static/bin/busybox``; check the common
    locations. Runtime dependency is a statically-linked binary
    (dynamically-linked would need libc + friends inside the
    rootfs, which defeats the point of a minimal init)."""
    for p in (
        Path("/usr/bin/busybox"),
        Path("/bin/busybox"),
        Path("/usr/lib/busybox-static/bin/busybox"),
    ):
        if p.is_file():
            # Static-linkage check via ``file``: dynamically-linked
            # busybox would be missing its interpreter inside the
            # rootfs and /init would exec-fail on pivot.
            try:
                out = subprocess.check_output(["file", "-b", str(p)], text=True)
            except (subprocess.SubprocessError, FileNotFoundError):
                out = ""
            if "statically linked" in out:
                return p
    return None


def _populate_rootfs(mnt: Path, busybox: Path) -> None:
    """Lay down the minimum Linux rootfs userspace needs to pivot
    into: /init (busybox script), /bin/busybox, /proc + /sys +
    /dev + /root + /tmp mount points."""
    # Directories the initramfs-tools /init assumes exist post-pivot
    # (proc/sys/dev get mounted from within our /init below; the mount
    # points themselves must pre-exist).
    for d in ("bin", "sbin", "proc", "sys", "dev", "run", "tmp", "root"):
        subprocess.run(["sudo", "-n", "mkdir", "-p", str(mnt / d)], check=True)

    # busybox is the whole userspace here; drop the static binary and
    # symlink /init + a handful of sbin helpers onto it.
    subprocess.run(
        ["sudo", "-n", "cp", str(busybox), str(mnt / "bin" / "busybox")],
        check=True,
    )
    subprocess.run(["sudo", "-n", "chmod", "0755", str(mnt / "bin" / "busybox")], check=True)

    init_body = textwrap.dedent(
        """\
        #!/bin/busybox sh
        # pixie test-pxe-ramboot init: proves pivot_root worked, then
        # posts a minimal inventory blob back to pixie so the ramboot
        # chain test can assert BOTH the pivot AND the end-to-end
        # inventory pipeline. Everything here uses busybox applets
        # only -- no python, no libc-dynamic-linked utilities.
        /bin/busybox --install -s /bin
        /bin/mount -t proc  proc /proc
        /bin/mount -t sysfs sysfs /sys
        /bin/mount -t devtmpfs devtmpfs /dev || true

        say() {
            /bin/echo "pixie-init: $*" > /dev/kmsg 2>/dev/null || true
            /bin/echo "pixie-init: $*" > /dev/console 2>/dev/null || true
        }

        say "hello from rootfs (test-pxe-ramboot)"

        # Parse pixie.server= + pixie.mac= off the kernel cmdline
        # (baked into the ramboot iPXE plan by pixie's renderer).
        CMDLINE=$(/bin/cat /proc/cmdline)
        parse_kv() {
            /bin/echo "$CMDLINE" | /bin/tr ' ' '\\n' \\
                | /bin/grep "^$1=" \\
                | /bin/sed "s/^$1=//"
        }
        SERVER=$(parse_kv pixie.server)
        MAC=$(parse_kv pixie.mac)
        say "cmdline server=${SERVER:-<unset>} mac=${MAC:-<unset>}"

        if [ -n "$SERVER" ] && [ -n "$MAC" ]; then
            BODY='{"disks":[{"path":"/dev/nbd0","size":"64M","type":"disk","vendor":"pixie-test","model":"ramboot-e2e"}],"lshw":{"class":"system","product":"pixie-test-e2e"}}'
            say "posting inventory to $SERVER/pxe/$MAC/inventory"
            # busybox wget POST: --post-data + Content-Type header;
            # -O - streams the (empty) 204 response to stdout so a
            # transport error is not silent.
            if /bin/busybox wget \\
                --header="Content-Type: application/json" \\
                --post-data="$BODY" \\
                -O - \\
                "$SERVER/pxe/$MAC/inventory" >/dev/null 2>&1; then
                say "inventory posted ok"
            else
                say "inventory post failed"
            fi
        else
            say "no pixie.server / pixie.mac on cmdline; skipping inventory post"
        fi

        exec /bin/busybox sh -c 'while :; do /bin/busybox sleep 3600; done'
        """
    )
    init_tmp = mnt.parent / "init.tmp"
    init_tmp.write_text(init_body, encoding="utf-8")
    subprocess.run(
        ["sudo", "-n", "install", "-m", "0755", str(init_tmp), str(mnt / "init")],
        check=True,
    )
    # /sbin/init is what initramfs-tools' pivot_root path execs by
    # default; symlink to /init so either lookup works.
    subprocess.run(
        ["sudo", "-n", "ln", "-s", "/init", str(mnt / "sbin" / "init")],
        check=True,
    )
    init_tmp.unlink()


def add_args(parser: ArgumentParser) -> None:
    del parser


def main(args, cijoe) -> int:
    del args, cijoe
    root = _find_artifact_root()
    if root is None:
        # Dump what we can see so an operator (or CI) can tell whether
        # the artifact download step ran and where files ended up.
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

    vmlinuz, initrd = _pick_artifacts(root)
    # log.error() instead of log.info() so cijoe's --monitor renderer
    # surfaces the progress lines on CI (info-level output is buffered
    # into the per-step report.html and not streamed to stdout).
    log.error(f"pxe_ramboot_stage: cwd={Path.cwd()}")
    log.error(f"pxe_ramboot_stage: workspace={workspace}")
    log.error(f"pxe_ramboot_stage: building {bundle_out} from {vmlinuz.name} + {initrd.name}")
    _build_bundle(vmlinuz, initrd, bundle_out)
    log.error(f"pxe_ramboot_stage: bundle staged ({bundle_out.stat().st_size} bytes)")

    log.error(f"pxe_ramboot_stage: creating {disk_out} (64 MiB sparse)")
    _make_disk_image(disk_out)
    log.error(f"pxe_ramboot_stage: disk staged ({disk_out.stat().st_size} bytes)")
    return 0
