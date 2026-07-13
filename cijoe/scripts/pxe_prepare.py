"""
Prepare the workspace for the PXE chain test
============================================

Ahead-of-test steps that don't want to live in the test script:

1. Build the pixie container from this checkout, tagged
   ``pixie:pxetest`` so the run step picks it up without racing
   the podman registry.

The run step (:mod:`pxe_run_chain_test`) creates its own network
+ workspace + starts the container, so we deliberately do NOT do
that here. This step is pure "materials prep" so the test can run
independently across pod-man / dnsmasq / bridge availability
problems on the host.

Retargetable: False
"""

from __future__ import annotations

import logging as log
import subprocess
import sys
from argparse import ArgumentParser
from pathlib import Path

CONTAINER_TAG = "pixie:pxetest"


def add_args(parser: ArgumentParser) -> None:
    del parser


def main(args, cijoe) -> int:
    del args, cijoe
    repo_root = Path.cwd().parent
    containerfile = repo_root / "Containerfile"
    if not containerfile.is_file():
        log.error(f"missing Containerfile at {containerfile}")
        return 1

    log.info(f"Building container image {CONTAINER_TAG} from {repo_root}")
    r = subprocess.run(
        ["podman", "build", "-t", CONTAINER_TAG, "-f", str(containerfile), str(repo_root)],
        stdout=sys.stdout,
        stderr=sys.stderr,
        check=False,
    )
    if r.returncode != 0:
        log.error(f"podman build failed (rc={r.returncode})")
        return r.returncode

    log.info(f"Prepared image {CONTAINER_TAG}")
    return 0
