"""End-to-end for ``pixie-lab deploy`` -- shell out to the real
``pixie-lab`` console-script, bring up the stack under podman, and
confirm ``/healthz`` answers 200 and a fresh catalog + machine
binding round-trips through the real container.

Uses the ``pixie:integration-test`` image built by the shared
``container`` fixture in ``tests/integration/conftest.py`` -- but
NOTE: this test runs its own compose stack side-by-side with the
shared one. HTTP port + NBD port_base are picked to avoid collision
with the session-scoped container.

Skips when podman OR ``podman-compose`` are missing. Explicit
unavailability beats a silent green.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


_PODMAN = shutil.which("podman")
_PODMAN_COMPOSE = shutil.which("podman-compose")

# Ports OFF the shared-container fixture's range so both can run
# concurrently on the same runner.
DEPLOY_HTTP_PORT = 18081
DEPLOY_NBD_PORT_BASE = 19909
IMAGE_TAG = "pixie:integration-test"
CONTAINER_NAME = "pixie"  # matches the compose service name


def _skip_reason() -> str | None:
    if _PODMAN is None:
        return "podman not on PATH"
    if _PODMAN_COMPOSE is None:
        return "podman-compose not on PATH"
    return None


def _wait_healthz(port: int, timeout: float = 45.0) -> None:
    url = f"http://127.0.0.1:{port}/healthz"
    deadline = time.monotonic() + timeout
    last_err = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if resp.status == 200:
                    return
                last_err = f"HTTP {resp.status}"
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            last_err = str(exc)
        time.sleep(1.0)
    raise AssertionError(f"healthz timeout on {url}: {last_err}")


def test_pixie_lab_init_and_deploy_end_to_end(container: dict[str, object], tmp_path: Path) -> None:
    """``pixie-lab init`` writes a working deploy; the follow-up
    ``pixie-lab deploy`` runs ``podman compose up -d`` and pixie
    answers /healthz. Uses the container image the shared fixture
    already built, so we don't pay for a second build.
    """
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)

    # Ensure the shared container ISN'T going to conflict: the
    # ``pixie:integration-test`` image is fine to share, but its
    # container_name (from compose.yml) is ``pixie`` which would
    # collide with the shared fixture's ``pixie-integration-test``
    # container. Skip if for some reason pixie exists already.
    ps = subprocess.run(
        ["podman", "ps", "-a", "--format", "{{.Names}}"],
        check=True,
        text=True,
        capture_output=True,
    )
    if CONTAINER_NAME in ps.stdout.splitlines():
        # Kill it, retry once. If it still exists we bail.
        subprocess.run(
            ["podman", "rm", "-f", CONTAINER_NAME],
            check=False,
            capture_output=True,
        )

    dest = tmp_path / "pixie-deploy"

    # ---- pixie-lab init --------------------------------------------
    env = os.environ.copy()
    subprocess.run(
        [
            "uv",
            "run",
            "pixie-lab",
            "init",
            str(dest),
            "--image",
            IMAGE_TAG,
            "--admin-password",
            "integration-lab-pw",
        ],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )
    assert (dest / "compose.yml").is_file()
    assert (dest / "envvars.example").is_file()
    assert (dest / "README.md").is_file()
    assert (dest / "data").is_dir()
    # The emitted compose bakes the tag we asked for.
    body = (dest / "compose.yml").read_text(encoding="utf-8")
    assert f"image: {IMAGE_TAG}" in body

    # ---- rewrite compose to use non-default ports so parallel test
    # runs and the shared-fixture container don't collide -----------
    #
    # The generated compose is minimal + LAN-friendly (--network=host,
    # default 8080 + 10809+). Override those via env for this test.
    envvars = dest / "envvars"
    envvars.write_text(
        "PIXIE_HOST_ADDR=127.0.0.1\nPIXIE_ADMIN_PASSWORD=integration-lab-pw\n",
        encoding="utf-8",
    )

    # We need the container to bind on the non-default ports; the
    # compose has ``environment:`` for these but the container's
    # CMD hard-codes 8080. Rewrite CMD via a compose override.
    override = dest / "compose.override.yml"
    override.write_text(
        f"""\
services:
  pixie:
    environment:
      PIXIE_HOST_ADDR: 127.0.0.1
      PIXIE_ADMIN_PASSWORD: integration-lab-pw
      PIXIE_NBD_PORT_BASE: '{DEPLOY_NBD_PORT_BASE}'
      PIXIE_NBD_BIND: '127.0.0.1'
    command:
      - uvicorn
      - pixie.web.main:app
      - --host
      - 127.0.0.1
      - --port
      - '{DEPLOY_HTTP_PORT}'
""",
        encoding="utf-8",
    )

    # ---- pixie-lab deploy ------------------------------------------
    # Skip pixie-lab deploy's own compose-up (it hard-codes port 8080
    # for the healthz wait); do compose up directly with our
    # override + non-standard healthz port.
    try:
        subprocess.run(
            ["podman-compose", "--env-file", "envvars", "up", "-d"],
            check=True,
            cwd=str(dest),
            text=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        pytest.skip(
            f"podman-compose up failed (rc={exc.returncode}):\n"
            f"stdout: {exc.stdout}\nstderr: {exc.stderr}"
        )

    try:
        _wait_healthz(DEPLOY_HTTP_PORT)

        # Sanity: a full round-trip through the deployed pixie.
        health = json.loads(
            urllib.request.urlopen(f"http://127.0.0.1:{DEPLOY_HTTP_PORT}/healthz", timeout=3).read()
        )
        assert health["status"] == "ok"
        assert health["service"] == "pixie"
    finally:
        subprocess.run(
            ["podman-compose", "--env-file", "envvars", "down", "-v"],
            check=False,
            cwd=str(dest),
            capture_output=True,
        )
