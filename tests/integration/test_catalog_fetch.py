"""End-to-end: pixie's HTTP fetch pipeline downloads from a real URL,
hashes on the way down, and unpacks tar.gz bundles into the content-
addressed artifacts tree.

Existing ``test_pxe.py::test_nbdboot_plan_end_to_end`` places blobs +
artifacts on disk directly and patches the DB to skip the fetch step;
this suite exercises the fetch pipeline itself against real HTTP.

Flow:

1. Spin a stdlib ``http.server`` on 127.0.0.1:<port> serving:
   - ``bundle.tar.gz`` with real vmlinuz + initrd + manifest.json
   - ``disk.img`` with a synthetic disk-image blob
2. Add both entries to pixie's catalog via ``POST /catalog/entries``.
3. Trigger fetches via ``POST /catalog/entries/<name>/fetch``.
4. Poll ``GET /catalog`` until both entries have ``content_sha256``.
5. Assert:
   - ``blobs/<sha>/blob`` exists with the correct bytes for both.
   - ``artifacts/<sha>/{vmlinuz,initrd,manifest.json}`` exist for the
     bundle and their bytes match what went into the tar.gz.
   - The bundle's ``content_sha256`` in the catalog matches the
     externally-computed sha256 of the served tar.gz.
6. Bind a machine to the disk entry's sha with boot_mode=nbdboot;
   ``GET /pxe/<mac>`` returns a plan that references
   ``/artifacts/<bundle-sha>/vmlinuz`` + ``/artifacts/<bundle-sha>/
   initrd`` (the plan renderer picked the SAME bundle sha the fetch
   pipeline produced).

Zero fakes. Container is real, HTTP source is real (stdlib server),
tar.gz is real (built with the stdlib ``tarfile`` module), plan is
served by the real renderer.
"""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import socketserver
import tarfile
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest

from tests.integration.conftest import _get, _post_json

pytestmark = pytest.mark.integration


_BUNDLE_VMLINUZ = b"KERNEL-BYTES-real-http-fetch-e2e"
_BUNDLE_INITRD = b"INITRD-BYTES-real-http-fetch-e2e"
_BUNDLE_MANIFEST = json.dumps(
    {"variant": "e2e-tiny", "arch": "x86_64", "kernel_version": "0.0.0-test"}
).encode("utf-8")
# ~64 KiB so the read path stresses ``tee | sha256sum`` a bit rather
# than fitting in a single pipe read.
_DISK_BYTES = (b"pixie-disk-image-real-http-fetch-e2e\n" * 2000)[:65536]


def _build_bundle() -> bytes:
    """Real tar.gz with the three files pixie's fetcher expects."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body in (
            ("vmlinuz", _BUNDLE_VMLINUZ),
            ("initrd", _BUNDLE_INITRD),
            ("manifest.json", _BUNDLE_MANIFEST),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(body)
            info.mtime = 0  # deterministic sha across runs
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


class _StaticFiles(http.server.BaseHTTPRequestHandler):
    """Serves an in-memory {path: bytes} table. No filesystem access;
    the whole thing lives in the test process so a container reaching
    it via 127.0.0.1 gets exactly what we expect."""

    files: ClassVar[dict[str, bytes]] = {}

    def do_GET(self) -> None:
        body = self.files.get(self.path)
        if body is None:
            self.send_error(404, f"no such path: {self.path}")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # Silence stderr noise; pytest's capture already carries what
        # we need. Signature matches BaseHTTPRequestHandler's.
        del format, args


class _ReusableServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture
def http_server(bundle_bytes: bytes) -> Iterator[str]:
    """Ephemeral in-process HTTP server on 127.0.0.1:<random-port> that
    serves the netboot bundle + disk-image bytes. Yields the base URL."""
    _StaticFiles.files = {
        "/bundle.tar.gz": bundle_bytes,
        "/disk.img": _DISK_BYTES,
    }
    server = _ReusableServer(("127.0.0.1", 0), _StaticFiles)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def bundle_bytes() -> bytes:
    return _build_bundle()


def _wait_fetched(base: str, name: str, timeout: float = 30.0) -> dict:
    """Poll ``GET /catalog`` until ``name``'s content_sha256 appears
    (or fail on ``fetch_error``). Returns the entry dict."""
    deadline = time.monotonic() + timeout
    last_state = "?"
    while time.monotonic() < deadline:
        entries = json.loads(_get(base, "/catalog").read()).get("entries", [])
        for entry in entries:
            if entry.get("name") != name:
                continue
            state = entry.get("fetch_state") or "?"
            last_state = state
            if state == "error":
                err = entry.get("fetch_error", "unknown")
                raise AssertionError(f"fetch failed for {name!r}: {err}")
            if entry.get("content_sha256"):
                return entry
        time.sleep(0.5)
    raise AssertionError(
        f"fetch never populated content_sha256 for {name!r} within {timeout}s "
        f"(last state: {last_state})"
    )


def test_fetch_bundle_downloads_hashes_and_unpacks(
    api: dict[str, object], http_server: str, bundle_bytes: bytes
) -> None:
    base = str(api["base_url"])
    cookie = str(api["cookie"])
    state_dir = Path(str(api["state_dir"]))

    bundle_url = f"{http_server}/bundle.tar.gz"
    r = _post_json(
        base,
        "/catalog/entries",
        {"name": "netboot", "src": bundle_url, "format": "tar.gz"},
        cookie=cookie,
    )
    assert getattr(r, "status", getattr(r, "code", 0)) == 201, "add entry failed"

    r = _post_json(base, "/catalog/entries/netboot/fetch", {}, cookie=cookie)
    assert getattr(r, "status", getattr(r, "code", 0)) == 202, "fetch trigger failed"

    entry = _wait_fetched(base, "netboot")

    expected_sha = hashlib.sha256(bundle_bytes).hexdigest()
    assert entry["content_sha256"] == expected_sha, (
        f"catalog reported sha {entry['content_sha256']} != externally computed {expected_sha}"
    )
    # For tar.gz format pixie discards the source blob after unpack
    # (bundles are only useful unpacked; the tarball itself is not
    # served) -- see catalog/_fetcher.py:192. Assert no leftover blob.
    blob = state_dir / "blobs" / expected_sha / "blob"
    assert not blob.exists(), "tar.gz format should not persist the source blob"

    # Artifacts unpacked correctly.
    art = state_dir / "artifacts" / expected_sha
    assert (art / "vmlinuz").read_bytes() == _BUNDLE_VMLINUZ
    assert (art / "initrd").read_bytes() == _BUNDLE_INITRD
    assert (art / "manifest.json").read_bytes() == _BUNDLE_MANIFEST


def test_fetch_disk_image_hashes_but_does_not_unpack(
    api: dict[str, object], http_server: str
) -> None:
    """A ``format=img`` entry produces only a blob on disk; no artifacts
    dir (unpacking is tar.gz-only)."""
    base = str(api["base_url"])
    cookie = str(api["cookie"])
    state_dir = Path(str(api["state_dir"]))

    disk_url = f"{http_server}/disk.img"
    _post_json(
        base,
        "/catalog/entries",
        {"name": "test-disk", "src": disk_url, "format": "img"},
        cookie=cookie,
    )
    _post_json(base, "/catalog/entries/test-disk/fetch", {}, cookie=cookie)

    entry = _wait_fetched(base, "test-disk")
    expected_sha = hashlib.sha256(_DISK_BYTES).hexdigest()
    assert entry["content_sha256"] == expected_sha

    blob = state_dir / "blobs" / expected_sha / "blob"
    assert blob.is_file()
    assert blob.read_bytes() == _DISK_BYTES

    # No artifacts dir for a raw disk image.
    art = state_dir / "artifacts" / expected_sha
    assert not art.exists(), "raw disk-image fetch should not create an artifacts dir"


def test_fetched_bundle_powers_nbdboot_plan(
    api: dict[str, object], http_server: str, bundle_bytes: bytes
) -> None:
    """The plan renderer consumes the REAL fetched artifacts (not
    hand-placed bytes as in test_pxe.py) and produces a nbdboot plan
    that references the bundle's fetch-time-computed sha."""
    base = str(api["base_url"])
    cookie = str(api["cookie"])

    bundle_url = f"{http_server}/bundle.tar.gz"
    disk_url = f"{http_server}/disk.img"
    mac = "aa:bb:cc:dd:ee:33"

    # Netboot bundle first (target of the disk's netboot_src ref).
    _post_json(
        base,
        "/catalog/entries",
        {"name": "netboot-b", "src": bundle_url, "format": "tar.gz"},
        cookie=cookie,
    )
    _post_json(base, "/catalog/entries/netboot-b/fetch", {}, cookie=cookie)
    bundle_entry = _wait_fetched(base, "netboot-b")

    _post_json(
        base,
        "/catalog/entries",
        {
            "name": "disk-b",
            "src": disk_url,
            "format": "img",
            "netboot_src": bundle_url,
        },
        cookie=cookie,
    )
    _post_json(base, "/catalog/entries/disk-b/fetch", {}, cookie=cookie)
    disk_entry = _wait_fetched(base, "disk-b")

    # Bind the machine to the disk-image entry with nbdboot mode.
    body = {"boot_mode": "nbdboot", "image_content_sha256": disk_entry["content_sha256"]}
    req = urllib.request.Request(
        f"{base}/machines/{mac}",
        data=json.dumps(body).encode("utf-8"),
        method="PUT",
        headers={"Content-Type": "application/json", "Cookie": cookie},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        assert resp.status == 200, "bind machine failed"

    # ``GET /pxe/<mac>`` returns a plan built from the REAL fetched
    # artifacts. It must reference the bundle sha the fetch pipeline
    # produced.
    plan = _get(base, f"/pxe/{mac}").read().decode("utf-8")
    assert plan.startswith("#!ipxe"), f"non-ipxe plan: {plan[:200]}"
    assert f"/artifacts/{bundle_entry['content_sha256']}/vmlinuz" in plan
    assert f"/artifacts/{bundle_entry['content_sha256']}/initrd" in plan
    assert "pixie.nbd=tcp://" in plan
