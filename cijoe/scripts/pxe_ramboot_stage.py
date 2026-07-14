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
import tarfile
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


def _make_disk_image(out: Path, size_mib: int = 64) -> None:
    """Write a sparse zero-filled file. Pixie's NBD supervisor serves
    it as-is; the ramboot chain never mounts it (we stop asserting at
    the initrd's ramboot script)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as fh:
        fh.truncate(size_mib * 1024 * 1024)


def add_args(parser: ArgumentParser) -> None:
    del parser


def main(args, cijoe) -> int:
    del args, cijoe
    root = _find_artifact_root()
    if root is None:
        log.warning(
            "no netboot-pc artifacts found under %s or PIXIE_NETBOOT_ARTIFACT_DIR; "
            "skipping ramboot staging (test will fail catalog fetch)",
            _DEFAULT_ARTIFACT_ROOT,
        )
        return 0

    workspace = Path.cwd() / "_build" / "test-pxe"
    workspace.mkdir(parents=True, exist_ok=True)
    bundle_out = workspace / "bundle.tar.gz"
    disk_out = workspace / "disk.img"

    vmlinuz, initrd = _pick_artifacts(root)
    log.info(f"Building bundle.tar.gz from {vmlinuz.name} + {initrd.name}")
    _build_bundle(vmlinuz, initrd, bundle_out)
    log.info(f"Bundle -> {bundle_out} ({bundle_out.stat().st_size} bytes)")

    log.info(f"Creating disk.img at {disk_out} (64 MiB sparse)")
    _make_disk_image(disk_out)
    log.info(f"Disk -> {disk_out}")

    log.info("Ramboot payload staged")
    return 0
