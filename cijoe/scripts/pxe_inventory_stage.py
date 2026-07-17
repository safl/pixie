"""
Stage the netboot-pc bake into a live-env dir the pixie container mounts
=======================================================================

For ``mode=inventory`` in ``pxe_run_chain_test.py``: pixie serves its
own live env at ``<state_dir>/live-env/{vmlinuz,initrd,squashfs}``, so
a ``boot_mode=pixie-inventory`` bind produces a chain that fetches
those three files over HTTP from pixie itself. This script picks the
latest ``pixie-netboot-pc-x86_64`` bake and copies its three
artifacts into ``_build/test-pxe/live-env/``, renamed to the plain
``vmlinuz`` / ``initrd`` / ``squashfs`` shape pixie's PXE renderer
emits URLs for.

Deliberately narrower than :mod:`pxe_ramboot_stage`: no tar.gz bundle,
no ext4 disk image, no unsquashfs. The pixie-inventory chain does
NOT go through catalog + NBD; the live-env kernel is served straight
out of the operator-visible ``live-env/`` directory. That directory
gets bind-mounted into the pixie container by the chain test.

Requires: nothing beyond stdlib + a readable
``pixie-netboot-pc-x86_64-v*`` artifact set.

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
    """Prefer the operator-set ``PIXIE_NETBOOT_ARTIFACT_DIR`` env var,
    fall back to pixie's default publish dir. Returns ``None`` if
    neither carries a matching artifact set."""
    for root in (_ARTIFACT_ROOT, _DEFAULT_ARTIFACT_ROOT):
        if not root or not root.is_dir():
            continue
        if list(root.glob("pixie-netboot-pc-x86_64-v*.vmlinuz")):
            return root
    return None


def _pick_artifacts(root: Path) -> tuple[Path, Path, Path]:
    """Locate the (vmlinuz, initrd, squashfs) trio for the latest
    netboot-pc bake staged under ``root``. Raises if any of the three
    is missing -- pixie's PXE renderer's ``_live_env_ready()`` check
    requires all three."""
    vmlinuz = sorted(root.glob("pixie-netboot-pc-x86_64-v*.vmlinuz"))
    initrd = sorted(root.glob("pixie-netboot-pc-x86_64-v*.initrd"))
    squashfs = sorted(root.glob("pixie-netboot-pc-x86_64-v*.squashfs"))
    if not vmlinuz or not initrd or not squashfs:
        raise FileNotFoundError(
            f"missing vmlinuz/initrd/squashfs under {root} (need "
            "pixie-netboot-pc-x86_64-v*.{vmlinuz,initrd,squashfs})"
        )
    return vmlinuz[-1], initrd[-1], squashfs[-1]


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
    live_env = workspace / "live-env"
    live_env.mkdir(parents=True, exist_ok=True)

    vmlinuz, initrd, squashfs = _pick_artifacts(root)
    log.error(f"pxe_inventory_stage: cwd={Path.cwd()}")
    log.error(f"pxe_inventory_stage: workspace={workspace}")
    log.error(f"pxe_inventory_stage: copying {vmlinuz.name} -> {live_env / 'vmlinuz'}")
    shutil.copy2(vmlinuz, live_env / "vmlinuz")
    log.error(f"pxe_inventory_stage: copying {initrd.name} -> {live_env / 'initrd'}")
    shutil.copy2(initrd, live_env / "initrd")
    log.error(f"pxe_inventory_stage: copying {squashfs.name} -> {live_env / 'squashfs'}")
    shutil.copy2(squashfs, live_env / "live.squashfs")

    # World-readable so the pixie container's non-root uvicorn can
    # serve them via StaticFiles from the bind-mount.
    for f in (live_env / "vmlinuz", live_env / "initrd", live_env / "live.squashfs"):
        f.chmod(0o644)
    log.error(
        f"pxe_inventory_stage: staged live-env at {live_env} "
        f"({sum(f.stat().st_size for f in live_env.iterdir())} bytes)"
    )
    return 0
