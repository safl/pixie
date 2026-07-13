"""Integration-test fixtures: build + run pixie in a real podman
container.

CI + dev share the same shape: build image ``pixie:dev`` from the
repo's Containerfile, start it with ``--network=host`` (so the NBD
port range is directly reachable from the test process) and a
bind-mount for ``/var/lib/pixie`` (so tests can pre-place blobs on
the host filesystem and observe state.db + artifacts/ directly).

Skips + a note on the reason are the mode when podman is not on
PATH or the image build refuses -- explicit unavailability beats
silent-green fake tests. If the container fails to become healthy
within ``_HEALTHZ_TIMEOUT`` we tear down + fail loudly with the
tail of ``podman logs``.

Session-scoped so the whole integration file shares one container.
Each test cleans up its own catalog + exports via the HTTP API to
keep isolation without paying a container-startup cost per test.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

# Image tag used for the integration run. Kept out of the caller's
# way so a co-located ``pixie:dev`` image from bare ``podman build``
# survives an integration run + doesn't stomp on a downstream image
# the operator may have on the same machine.
IMAGE_TAG = "pixie:integration-test"
CONTAINER_NAME = "pixie-integration-test"
HOST_HTTP_PORT = 18080  # avoid 8080 clash if the operator's bty-web is up
HOST_NBD_PORT_BASE = 19809

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
_HEALTHZ_TIMEOUT = 45.0  # container image build + import + uvicorn ready
_PODMAN = shutil.which("podman")


def _skip_reason() -> str | None:
    if _PODMAN is None:
        return "podman not on PATH"
    if os.environ.get("PIXIE_SKIP_INTEGRATION"):
        return f"PIXIE_SKIP_INTEGRATION={os.environ['PIXIE_SKIP_INTEGRATION']}"
    return None


def _run(
    cmd: list[str], *, check: bool = True, capture: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        capture_output=capture,
    )


def _container_logs(name: str) -> str:
    """Best-effort log fetch for failure diagnostics."""
    if _PODMAN is None:
        return "(podman not on PATH)"
    r = subprocess.run(
        [_PODMAN, "logs", "--tail", "80", name],
        check=False,
        text=True,
        capture_output=True,
    )
    return f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"


def _wait_healthz(port: int, deadline: float) -> None:
    """Poll ``/healthz`` on the given port until 200 or the deadline
    passes. Raises AssertionError with the container logs on timeout."""
    url = f"http://127.0.0.1:{port}/healthz"
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
    logs = _container_logs(CONTAINER_NAME)
    raise AssertionError(f"healthz timeout on {url}: {last_err}\n--- container logs ---\n{logs}")


@pytest.fixture(scope="session")
def container(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, object]]:
    """Build the image if missing, then run one container for the whole
    test session. Yields a dict with the base URL + state dir + admin
    password so downstream tests can drive the API + inspect on-disk
    state directly."""
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)
    assert _PODMAN is not None  # narrow for mypy after the skip

    # Bind-mount the state dir so tests can lay down synthetic blobs
    # (skips the fetch pipeline for exports tests) and read state.db.
    state_dir = tmp_path_factory.mktemp("pixie-state")
    state_dir.chmod(0o777)
    admin_pw = "integration-admin"

    print(f"\n[integration] building image {IMAGE_TAG} from {_REPO_ROOT}...", file=sys.stderr)
    build = _run(
        [_PODMAN, "build", "-t", IMAGE_TAG, "-f", "Containerfile", str(_REPO_ROOT)],
        check=False,
    )
    if build.returncode != 0:
        pytest.skip(
            f"container build failed (rc={build.returncode})\n"
            f"--- stdout ---\n{build.stdout}\n--- stderr ---\n{build.stderr}"
        )

    # Leftover from a previous crashed run.
    subprocess.run(
        [_PODMAN, "rm", "-f", CONTAINER_NAME],
        check=False,
        capture_output=True,
    )

    # ``--network=host`` mirrors production; tests reach the container
    # over 127.0.0.1 without any port-publish gymnastics. Bind /var/lib
    # /pixie so ``state.db`` and ``blobs/`` sit under the pytest tmp.
    print(f"[integration] starting {CONTAINER_NAME}...", file=sys.stderr)
    run = _run(
        [
            _PODMAN,
            "run",
            "-d",
            "--rm",
            "--name",
            CONTAINER_NAME,
            "--network=host",
            "-e",
            f"PIXIE_ADMIN_PASSWORD={admin_pw}",
            "-e",
            f"PIXIE_NBD_PORT_BASE={HOST_NBD_PORT_BASE}",
            "-e",
            "PIXIE_NBD_BIND=127.0.0.1",
            # A local writable dir for state; the container's default
            # /var/lib/pixie is intended to be a volume mount.
            "-v",
            f"{state_dir}:/var/lib/pixie:Z",
            # Override the container's uvicorn port -- the Containerfile
            # ships 8080 which conflicts with a local bty-web install.
            IMAGE_TAG,
            "uvicorn",
            "pixie.web.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(HOST_HTTP_PORT),
        ],
        check=False,
    )
    if run.returncode != 0:
        pytest.skip(f"container run failed (rc={run.returncode})\n--- stderr ---\n{run.stderr}")

    try:
        _wait_healthz(HOST_HTTP_PORT, time.monotonic() + _HEALTHZ_TIMEOUT)
        yield {
            "base_url": f"http://127.0.0.1:{HOST_HTTP_PORT}",
            "state_dir": state_dir,
            "admin_password": admin_pw,
            "nbd_port_base": HOST_NBD_PORT_BASE,
        }
    finally:
        subprocess.run(
            [_PODMAN, "stop", "--time", "5", CONTAINER_NAME],
            check=False,
            capture_output=True,
        )
        # rm -f in case stop was ignored; --rm at start should handle
        # the cleanup either way.
        subprocess.run(
            [_PODMAN, "rm", "-f", CONTAINER_NAME],
            check=False,
            capture_output=True,
        )


def _login_cookie(base_url: str, password: str) -> str:
    """Capture the ``pixie-token`` cookie off the 303 that POST
    /ui/login returns (urllib follows redirects + drops Set-Cookie by
    default; intercept the 303)."""

    class _CaptureRedirect(urllib.request.HTTPRedirectHandler):
        def http_error_303(  # type: ignore[override]
            self,
            req: urllib.request.Request,
            fp: object,
            code: int,
            msg: str,
            headers: object,
        ) -> None:
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)  # type: ignore[arg-type]

    opener = urllib.request.build_opener(_CaptureRedirect)
    req = urllib.request.Request(
        f"{base_url}/ui/login",
        data=f"password={password}".encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        opener.open(req, timeout=5.0)
    except urllib.error.HTTPError as exc:
        for raw in exc.headers.get_all("Set-Cookie") or []:
            head = raw.split(";", 1)[0].strip()
            if head.startswith("pixie-token="):
                return head
    raise AssertionError("no pixie-token in login response")


@pytest.fixture
def api(container: dict[str, object]) -> dict[str, object]:
    """Per-test API context: base_url + authed session cookie + helpers
    to reset per-test state (delete all exports + catalog entries at
    setup so leftover state from a prior test doesn't leak)."""
    base_url = str(container["base_url"])
    password = str(container["admin_password"])
    cookie = _login_cookie(base_url, password)

    # Reset state: kill every export, delete every machine + catalog
    # entry so tests can't leak state to each other.
    exports = json.loads(_get(base_url, "/exports").read()).get("exports", [])
    for e in exports:
        _delete(base_url, f"/exports/{e['name']}", cookie=cookie)
    machines = json.loads(_get(base_url, "/machines").read()).get("machines", [])
    for m in machines:
        _delete(base_url, f"/machines/{m['mac']}", cookie=cookie)
    entries = json.loads(_get(base_url, "/catalog").read()).get("entries", [])
    for e in entries:
        _delete(base_url, f"/catalog/entries?name={e['name']}", cookie=cookie)

    return {
        "base_url": base_url,
        "cookie": cookie,
        "state_dir": container["state_dir"],
        "nbd_port_base": container["nbd_port_base"],
    }


# --------------------- tiny HTTP helpers (no httpx dep needed) ------------


def _get(base: str, path: str, *, cookie: str = "") -> http.client.HTTPResponse:
    req = urllib.request.Request(f"{base}{path}", method="GET")
    if cookie:
        req.add_header("Cookie", cookie)
    return urllib.request.urlopen(req, timeout=10.0)


def _post_json(
    base: str, path: str, body: dict[str, object], *, cookie: str = ""
) -> http.client.HTTPResponse:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"{base}{path}", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        return urllib.request.urlopen(req, timeout=15.0)
    except urllib.error.HTTPError as exc:
        return exc  # type: ignore[return-value]


def _delete(base: str, path: str, *, cookie: str = "") -> http.client.HTTPResponse:
    req = urllib.request.Request(f"{base}{path}", method="DELETE")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        return urllib.request.urlopen(req, timeout=10.0)
    except urllib.error.HTTPError as exc:
        return exc  # type: ignore[return-value]
