"""
Build a slim pixie-flavoured iPXE binary
======================================

Clones iPXE upstream, copies in pixie's embed script + general.h trim
overrides, and builds ``bin-x86_64-efi/ipxe.efi`` via the build core
(:func:`build_ipxe_efi`).

The standalone CLI (``python3 cijoe/scripts/pixie_ipxe_build.py --out DIR``
/ ``make ipxe``) copies the binary into DIR. CI uses this to stage the
custom iPXE into the pixie and pixie-tftp image build contexts, so the
container deploy gets the one-bootfile chain guarantee.

Why custom build (vs. shipping Debian's stock ``/usr/lib/ipxe/
ipxe.efi`` as a symlink):

* Stock iPXE re-DHCPs after loading and tries to ``chain`` the
  DHCP filename (``ipxe.efi``) -- which is itself. Infinite loop.
* pixie's network architecture (v0.18+) deliberately does NOT
  configure DHCP user-class matching on the operator's router;
  the router-config cheatsheet stays a one-liner.
* Embedding ``chain http://${next-server}:8080/pxe-bootstrap.ipxe``
  inside the binary breaks the loop by pre-empting iPXE's
  DHCP-filename autoboot. Operator doesn't have to touch DHCP
  beyond pointing PXE clients at this appliance.

Build inputs (under ``pixie-media/auxiliary/``):

* ``ipxe-embed.ipxe`` -- the embedded boot script.
* ``ipxe-local-general.h`` -- trims iPXE's feature set so the
  binary stays close to Debian's stock 996 KB (the test
  firmware on UNDI 3.0.22 accepted that size; bigger builds
  failed to load).

Retargetable: False
"""

from __future__ import annotations

import logging as log
import shutil
import subprocess
import sys
from argparse import ArgumentParser
from pathlib import Path

# Upstream iPXE source.
IPXE_REPO = "https://github.com/ipxe/ipxe.git"
# iPXE git ref to build. Currently tracks ``master`` -- iPXE moves
# slowly enough that the tip mostly stays buildable + loadable on
# the firmware under test. NOTE: this is NOT pinned, so a bad
# upstream day can break the bake; pinning to a known-good commit
# hash here would make the build reproducible, but only after
# verifying that hash's binary still loads on the test firmware
# (UNDI 3.0.22 was size-sensitive, see ipxe-local-general.h).
IPXE_REV = "master"


class IpxeBuildError(RuntimeError):
    """The iPXE clone / compile failed; message is operator-actionable."""


def build_ipxe_efi(aux: Path, build_root: Path) -> Path:
    """Clone iPXE, stage pixie's embed script + trim, build and return the
    path to ``bin-x86_64-efi/ipxe.efi``. Raises :class:`IpxeBuildError` on
    any failure. Used by the standalone CLI (container / CI artifact)."""
    embed_src = aux / "ipxe-embed.ipxe"
    general_src = aux / "ipxe-local-general.h"
    for required in (embed_src, general_src):
        if not required.is_file():
            raise IpxeBuildError(f"missing build input: {required}")

    build_root.mkdir(parents=True, exist_ok=True)
    src_tree = build_root / "ipxe"

    # Clone or refresh the iPXE checkout. ``git fetch`` keeps
    # subsequent builds incremental.
    if (src_tree / ".git").is_dir():
        log.info(f"Reusing existing iPXE checkout at {src_tree}")
        rc = subprocess.call(["git", "-C", str(src_tree), "fetch", "--depth=1", "origin", IPXE_REV])
        if rc != 0:
            raise IpxeBuildError(f"git fetch failed (rc={rc})")
        rc = subprocess.call(["git", "-C", str(src_tree), "reset", "--hard", "FETCH_HEAD"])
        if rc != 0:
            raise IpxeBuildError(f"git reset failed (rc={rc})")
    else:
        log.info(f"Cloning iPXE -> {src_tree}")
        rc = subprocess.call(
            ["git", "clone", "--depth=1", "--branch", IPXE_REV, IPXE_REPO, str(src_tree)]
        )
        if rc != 0:
            raise IpxeBuildError(f"git clone failed (rc={rc})")

    # Stage pixie's build inputs into iPXE's source tree.
    src_dir = src_tree / "src"
    local_config = src_dir / "config" / "local"
    local_config.mkdir(parents=True, exist_ok=True)
    shutil.copy2(general_src, local_config / "general.h")
    shutil.copy2(embed_src, src_dir / "pixie-embed.ipxe")

    # Build the x86_64 EFI binary with the embedded script.
    log.info(f"Building ipxe.efi (EMBED={embed_src.name})")
    rc = subprocess.call(
        [
            "make",
            "-j",
            "4",
            "-C",
            str(src_dir),
            "bin-x86_64-efi/ipxe.efi",
            "EMBED=pixie-embed.ipxe",
            "NO_WERROR=1",
        ],
    )
    if rc != 0:
        raise IpxeBuildError(f"iPXE build failed (rc={rc})")

    built = src_dir / "bin-x86_64-efi" / "ipxe.efi"
    if not built.is_file():
        raise IpxeBuildError(f"expected build output not found: {built}")
    log.info(f"Built {built} ({built.stat().st_size} bytes)")
    return built


def _standalone(argv: list[str] | None = None) -> int:
    """``python3 cijoe/scripts/pixie_ipxe_build.py --out DIR`` -- build the
    custom ipxe.efi and copy it into DIR. Used by ``make ipxe`` / CI to
    stage the binary into the pixie + pixie-tftp image build contexts,
    independent of cijoe."""
    parser = ArgumentParser(description="Build pixie's custom embedded-chain iPXE (x86_64-efi).")
    parser.add_argument("--out", required=True, help="directory to write ipxe.efi into")
    parser.add_argument(
        "--aux", default=None, help="dir with ipxe-embed.ipxe + ipxe-local-general.h"
    )
    parser.add_argument("--build-root", default=None, help="scratch build dir")
    ns = parser.parse_args(argv)

    log.basicConfig(level=log.INFO, format="%(message)s")
    repo_root = Path(__file__).resolve().parents[2]
    aux = Path(ns.aux) if ns.aux else repo_root / "pixie-media" / "auxiliary"
    build_root = Path(ns.build_root) if ns.build_root else repo_root / "cijoe" / "_build" / "ipxe"
    out = Path(ns.out)
    out.mkdir(parents=True, exist_ok=True)

    try:
        built = build_ipxe_efi(aux, build_root)
    except IpxeBuildError as exc:
        log.error(str(exc))
        return 1
    dst = out / "ipxe.efi"
    shutil.copy2(built, dst)
    log.info(f"Staged {dst} ({dst.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(_standalone())
