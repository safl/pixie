"""
Stage the flash payload (live-env media + small target image)
=============================================================

For ``mode=flash`` in ``pxe_run_chain_test.py``. Does two things:

- Copies the ``pixie-netboot-pc-x86_64`` bake's vmlinuz + initrd +
  squashfs into ``_build/test-pxe/live-env/`` (renamed to the plain
  ``vmlinuz`` / ``initrd`` / ``live.squashfs`` shape pixie's PXE
  renderer emits URLs for). Same as :mod:`pxe_inventory_stage`.
- Writes a small (16 MiB) synthetic disk image at
  ``_build/test-pxe/flash-target.img`` whose first 4 KiB carry a
  distinctive marker string. The chain-test driver seeds this into
  pixie's catalog, binds the client to pixie-flash-once with the
  image sha + a target_disk_serial that matches QEMU's virtio-blk
  serial (``PIXIETEST``), and after the client's live env has
  flashed the image, greps the QEMU-side blank disk for the marker.
  Small on purpose: the whole HTTP fetch + dd path runs in under a
  minute on a GHA runner.

Requires: nothing beyond stdlib + a readable
``pixie-netboot-pc-x86_64-v*`` artifact set.

Retargetable: False
"""

from __future__ import annotations

import hashlib
import logging as log
import os
import shutil
from argparse import ArgumentParser
from pathlib import Path

_ARTIFACT_ROOT = Path(os.environ.get("PIXIE_NETBOOT_ARTIFACT_DIR") or "").expanduser()
_DEFAULT_ARTIFACT_ROOT = Path.home() / "system_imaging" / "disk"

# 16 MiB. Big enough that the wget + dd path in the pixie CLI runs
# through the same code as a real image, small enough that the whole
# chain test finishes in under a minute on a GHA runner.
_FLASH_IMG_SIZE = 16 * 1024 * 1024

# Marker the chain test greps for on the QEMU-side blank disk after
# flash. Contains no ``\0`` at the start so a `strings`-style scan
# picks it up if we ever need to diagnose from a saved qcow2.
_FLASH_MARKER = b"PIXIE-FLASH-TARGET-MARKER-v1\n"


def _find_artifact_root() -> Path | None:
    for root in (_ARTIFACT_ROOT, _DEFAULT_ARTIFACT_ROOT):
        if not root or not root.is_dir():
            continue
        if list(root.glob("pixie-netboot-pc-x86_64-v*.vmlinuz")):
            return root
    return None


def _pick_artifacts(root: Path) -> tuple[Path, Path, Path]:
    vmlinuz = sorted(root.glob("pixie-netboot-pc-x86_64-v*.vmlinuz"))
    initrd = sorted(root.glob("pixie-netboot-pc-x86_64-v*.initrd"))
    squashfs = sorted(root.glob("pixie-netboot-pc-x86_64-v*.squashfs"))
    if not vmlinuz or not initrd or not squashfs:
        raise FileNotFoundError(
            f"missing vmlinuz/initrd/squashfs under {root} (need "
            "pixie-netboot-pc-x86_64-v*.{vmlinuz,initrd,squashfs})"
        )
    return vmlinuz[-1], initrd[-1], squashfs[-1]


def _stage_live_env(root: Path, workspace: Path) -> None:
    vmlinuz, initrd, squashfs = _pick_artifacts(root)
    live_env = workspace / "live-env"
    live_env.mkdir(parents=True, exist_ok=True)
    shutil.copy2(vmlinuz, live_env / "vmlinuz")
    shutil.copy2(initrd, live_env / "initrd")
    shutil.copy2(squashfs, live_env / "live.squashfs")
    for f in (live_env / "vmlinuz", live_env / "initrd", live_env / "live.squashfs"):
        f.chmod(0o644)
    log.error(
        f"pxe_flash_stage: live-env staged at {live_env} "
        f"({sum(f.stat().st_size for f in live_env.iterdir())} bytes)"
    )


def _write_flash_target(workspace: Path) -> tuple[Path, str]:
    """Write the synthetic flash-target image + return its (path, sha256).
    The image is ``_FLASH_MARKER`` padded with zeros to ``_FLASH_IMG_SIZE``;
    that gives us a marker at offset 0 for the after-flash grep while
    keeping the sha stable across CI runs."""
    out = workspace / "flash-target.img"
    body = _FLASH_MARKER + b"\0" * (_FLASH_IMG_SIZE - len(_FLASH_MARKER))
    out.write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    log.error(
        f"pxe_flash_stage: flash-target image staged at {out} "
        f"({out.stat().st_size} bytes, sha={sha[:12]}...)"
    )
    return out, sha


def add_args(parser: ArgumentParser) -> None:
    del parser


def main(args, cijoe) -> int:
    del args, cijoe
    root = _find_artifact_root()
    if root is None:
        candidates = [c for c in (_ARTIFACT_ROOT, _DEFAULT_ARTIFACT_ROOT) if c]
        log.error(
            "no netboot-pc vmlinuz/initrd found under %s",
            " or ".join(map(str, candidates)),
        )
        return 1

    workspace = Path.cwd() / "_build" / "test-pxe"
    workspace.mkdir(parents=True, exist_ok=True)
    log.error(f"pxe_flash_stage: workspace={workspace}")

    _stage_live_env(root, workspace)
    _write_flash_target(workspace)
    return 0
