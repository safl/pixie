"""
Stage the live-env media for the flash chain tests
==================================================

For ``mode=flash`` + ``mode=tui`` in ``pxe_run_chain_test.py``.
Copies the ``pixie-netboot-pc-x86_64`` bake's vmlinuz + initrd +
squashfs into ``_build/test-pxe/live-env/`` (renamed to the plain
``vmlinuz`` / ``initrd`` / ``live.squashfs`` shape pixie's PXE
renderer emits URLs for). Same as :mod:`pxe_inventory_stage`.

The flash test used to also write a small synthetic 16 MiB image
here (``_build/test-pxe/flash-target.img``) served over a local
HTTP shim. That was replaced with an ``oras://`` pull of a real
nosi image (see ``_seed_flash_and_bind`` in the chain test) so
the pipeline exercises real oras resolution + real img.gz
decompression + a full-size dd instead of a marker-at-offset-0
smoke test. The tar.gz-bundle path (``mode=ramboot``) still uses
the workspace HTTP server + a locally-assembled payload; the
choice there is deliberate (nosi's netboot bundles don't quite
match ramboot's expected bundle layout yet).

Retargetable: False
"""

from __future__ import annotations

import logging as log
import os
import shutil
from argparse import ArgumentParser
from pathlib import Path

_ARTIFACT_ROOT = Path(os.environ.get("PIXIE_NETBOOT_ARTIFACT_DIR") or "").expanduser()
_DEFAULT_ARTIFACT_ROOT = Path.home() / "system_imaging" / "disk"


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
    return 0
