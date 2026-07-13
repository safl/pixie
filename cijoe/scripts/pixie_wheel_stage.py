"""
Stage the pixie-lab wheel for variants that bake pixie into their image
====================================================================

Builds a wheel from the parent repo via ``uv build`` and copies it
into a per-variant staging directory under ``pixie-media/``:

- ``netboot-pc`` / ``usbboot-pc`` / ``usbboot-rpi`` ->
  ``pixie-media/live-build/config/includes.chroot/opt/pixie/`` (consumed
  by the live-build hook ``0500-pixie-install.hook.chroot``, which
  ``pip install``s it into the chroot's ``/opt/pixie/venv``). All
  three variants share the same chroot tree; only the bake's
  binary-image shape and target architecture differ.

The cwd at run time is ``cijoe/`` (the Makefile cd's there before
invoking cijoe), so the repo root is ``Path.cwd().parent`` and the
pixie-media tree lives at ``repo_root / "pixie-media"``.

Variants not in the table are skipped with rc=0.

Retargetable: False
"""

from __future__ import annotations

import errno
import logging as log
import shutil
from argparse import ArgumentParser
from pathlib import Path

# Variant -> destination directory relative to ``pixie-media/``.
# Variants not listed here are skipped with rc=0. The live-build
# variants share the same chroot tree, so they share the same
# target dir.
#
# ``ramboot-init`` does not actually use the staged wheel at boot
# time (its initrd pivots to the catalog image's root before any
# of pixie's userspace runs), but the shared chroot's
# ``0500-pixie-install`` hook expects to find the wheel under
# ``/opt/pixie/`` regardless of variant. Staging it here keeps the
# chroot symmetric and lb build green.
_LIVE_CHROOT = Path("live-build") / "config" / "includes.chroot" / "opt" / "pixie"
TARGET_DIRS: dict[str, Path] = {
    "netboot-pc": _LIVE_CHROOT,
    "usbboot-pc": _LIVE_CHROOT,
    "usbboot-rpi": _LIVE_CHROOT,
    "ramboot-init": _LIVE_CHROOT,
}


def add_args(parser: ArgumentParser):
    del parser  # no flags; signature kept for cijoe consistency


def main(args, cijoe):
    del args
    cijoe_dir = Path.cwd()
    repo_root = cijoe_dir.parent
    pixie_media = repo_root / "pixie-media"

    variant = cijoe.getconf("pixie", {}).get("variant", "usbboot-pc")
    target_rel = TARGET_DIRS.get(variant)
    if target_rel is None:
        log.info(f"Skipping wheel stage (variant={variant!r}; nothing to bake)")
        return 0
    target_dir = pixie_media / target_rel
    target_dir.mkdir(parents=True, exist_ok=True)

    out_dir = cijoe_dir / "_build" / "wheel"
    if out_dir.exists():
        # Drop any wheel from a prior build so we don't accidentally stage
        # a stale version when the source tree's version bumps.
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    err, _ = cijoe.run_local(f"sh -c 'cd {repo_root} && uv build --wheel --out-dir {out_dir}'")
    if err:
        log.error(f"Failed to build pixie-lab wheel from {repo_root}")
        return err

    wheels = sorted(out_dir.glob("pixie_lab-*-py3-none-any.whl"))
    if not wheels:
        log.error(f"No wheel matching pixie_lab-*-py3-none-any.whl produced in {out_dir}")
        return errno.ENOENT
    if len(wheels) > 1:
        log.error(f"Expected exactly one wheel; found {len(wheels)}: {wheels}")
        return errno.E2BIG

    # Drop any previously-staged wheel(s) under the target dir - we want
    # exactly one for the consuming step's glob to be unambiguous.
    for stale in target_dir.glob("pixie_lab-*.whl"):
        if stale.name != wheels[0].name:
            log.info(f"Removing stale staged wheel {stale}")
            stale.unlink()

    target_path = target_dir / wheels[0].name
    shutil.copy2(wheels[0], target_path)
    log.info(f"Staged {wheels[0].name} -> {target_path}")

    return 0
