"""End-to-end: a machine bound to a fetched catalog entry produces a
nbdboot iPXE plan whose kernel + initrd URLs point at the real
extracted netboot bundle, and whose NBD URL points at a real running
nbdkit that speaks NBD.

We prove the full stack works by:

1. Placing a synthetic disk-image blob under
   ``<state>/blobs/<disk-sha>/blob``.
2. Placing an unpacked netboot bundle under
   ``<state>/artifacts/<bundle-sha>/{vmlinuz,initrd,manifest.json}``.
3. Adding two catalog entries via the JSON API (disk +
   bundle), cross-referenced by ``netboot_src`` URL.
4. Marking the entries "fetched" (patching the sha256 + size fields
   directly on the DB is the cheapest way to skip the real HTTP
   download; the on-disk bytes ARE real).
5. Binding a machine to the disk-image entry with
   ``boot_mode=nbdboot``.
6. ``GET /pxe/<mac>`` and asserting the rendered iPXE:
   - starts with ``#!ipxe``,
   - references ``/artifacts/<bundle-sha>/vmlinuz`` +
     ``/artifacts/<bundle-sha>/initrd`` (content-addressed URLs),
   - carries ``pixie.nbd=tcp://<host>:<port>`` where ``<port>`` is
     the port the auto-created NBD export is listening on,
   - handshakes NBD on that port (raw socket read of NBDMAGIC).

Zero fakes. Real container, real nbdkit, real HTTP + NBD.
"""

from __future__ import annotations

import json
import re
import socket
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.integration.conftest import _get, _post_json

pytestmark = pytest.mark.integration


_NBD_MAGIC = b"NBDMAGIC"


def _place_blob(state_dir: Path, sha: str, body: bytes) -> None:
    p = state_dir / "blobs" / sha / "blob"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    p.chmod(0o644)
    for parent in (p.parent, p.parent.parent):
        parent.chmod(0o755)


def _place_artifact(state_dir: Path, sha: str, files: dict[str, bytes]) -> None:
    d = state_dir / "artifacts" / sha
    d.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (d / name).write_bytes(body)
        (d / name).chmod(0o644)
    for parent in (d, d.parent):
        parent.chmod(0o755)


def _mark_fetched(state_dir: Path, name: str, sha: str, size: int) -> None:
    """Patch state.db directly to advertise ``name`` as fetched at
    ``sha`` + ``size``. Faster than a real HTTP fetch, and the point
    of THIS test is the plan renderer + NBD wiring, not the fetch
    pipeline (covered elsewhere)."""
    conn = sqlite3.connect(str(state_dir / "state.db"))
    try:
        conn.execute(
            "UPDATE catalog_entries SET content_sha256 = ?, size_bytes = ?, "
            "fetched_at = '2026-07-13T00:00:00Z' WHERE name = ?",
            (sha, size, name),
        )
        conn.commit()
    finally:
        conn.close()


def _nbd_handshake_ok(host: str, port: int, timeout: float = 3.0) -> bool:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        raw = b""
        while len(raw) < 8:
            chunk = sock.recv(8 - len(raw))
            if not chunk:
                return False
            raw += chunk
        return raw == _NBD_MAGIC


def test_nbdboot_plan_end_to_end(api: dict[str, object]) -> None:
    base = str(api["base_url"])
    cookie = str(api["cookie"])
    state_dir = Path(str(api["state_dir"]))

    disk_sha = "1" * 64
    bundle_sha = "2" * 64

    # ---- 1 + 2: place synthetic bytes on disk under the mount ----
    disk_bytes = b"pixie-disk-image-integration-blob\n" * 300  # ~10KiB
    _place_blob(state_dir, disk_sha, disk_bytes)

    manifest = json.dumps(
        {"variant": "tiny", "arch": "x86_64", "kernel_version": "0.0.0-test"}
    ).encode("utf-8")
    _place_artifact(
        state_dir,
        bundle_sha,
        {
            "vmlinuz": b"KERNEL-BYTES",
            "initrd": b"INITRD-BYTES",
            "manifest.json": manifest,
        },
    )

    # ---- 3: add both catalog entries via the JSON API ------------
    disk_src = "oras://ghcr.io/example/tiny:test"
    bundle_src = "oras://ghcr.io/example/tiny-netboot:test"

    r = _post_json(
        base,
        "/catalog/entries",
        {
            "name": "tiny",
            "src": disk_src,
            "format": "img.gz",
            "arch": "x86_64",
            "netboot_src": bundle_src,
        },
        cookie=cookie,
    )
    assert r.status == 201, r.read().decode()

    r = _post_json(
        base,
        "/catalog/entries",
        {
            "name": "tiny-netboot",
            "src": bundle_src,
            "format": "tar.gz",
            "arch": "x86_64",
        },
        cookie=cookie,
    )
    assert r.status == 201, r.read().decode()

    # ---- 4: mark both entries "fetched" by patching state.db ----
    _mark_fetched(state_dir, "tiny", disk_sha, len(disk_bytes))
    _mark_fetched(state_dir, "tiny-netboot", bundle_sha, len(manifest))

    # Sanity-check that /catalog reflects the sha we wrote.
    entries = json.loads(_get(base, "/catalog").read())["entries"]
    by_name = {e["name"]: e for e in entries}
    assert by_name["tiny"]["content_sha256"] == disk_sha
    assert by_name["tiny-netboot"]["content_sha256"] == bundle_sha

    # ---- 5: bind a machine to the disk-image entry --------------
    mac = "aa:bb:cc:dd:ee:99"
    r = _put_json(
        base,
        f"/machines/{mac}",
        {"boot_mode": "nbdboot", "image_content_sha256": disk_sha},
        cookie=cookie,
    )
    assert r.status == 200, r.read().decode()

    # ---- 6: fetch the plan and assert the wire contract ---------
    plan = _get(base, f"/pxe/{mac}").read().decode("utf-8")
    assert plan.startswith("#!ipxe")
    assert f"/artifacts/{bundle_sha}/vmlinuz" in plan
    assert f"/artifacts/{bundle_sha}/initrd" in plan

    # The port lives in the iPXE ``set nbd-port <n>`` line that the
    # kernel cmdline references as ``${nbd-port}``. The ${...} form
    # is what iPXE actually sees on the target -- variables aren't
    # expanded server-side. Parse the ``set`` line for the real port.
    m = re.search(r"^set nbd-port (\d+)", plan, re.M)
    assert m, f"no ``set nbd-port <n>`` in plan: {plan!r}"
    nbd_port = int(m.group(1))
    # And the kernel cmdline references it symbolically (proves the
    # template wired the variable through, not that it hard-coded
    # anything).
    assert "pixie.nbd=tcp://${nbd-host}:${nbd-port}" in plan
    # The template emits BOTH ``pixie.*`` and ``bty.*`` prefixes for
    # every NBD/image/overlay/server/mac token so pixie can chain
    # either a fresh-cut netboot bundle (reads ``pixie.*``) or a
    # nosi bundle baked before the rename (reads ``bty.*``). If a
    # future edit updates one side only, this asserts the twin.
    assert "bty.nbd=tcp://${nbd-host}:${nbd-port}" in plan
    assert plan.count("nbd=tcp://${nbd-host}:${nbd-port}") == 2
    assert plan.count("image=${nbd-name}") == 2
    assert plan.count("mac=aa:bb:cc:dd:ee:99") == 2

    # A REAL NBD server on the other end (no shim). Prove it by
    # reading the first 8 bytes of the handshake.
    assert _nbd_handshake_ok("127.0.0.1", nbd_port), (
        f"port {nbd_port} did not speak NBD after nbdboot bind"
    )

    # The auto-spawned export lands on ``GET /exports`` too, keyed on
    # the same content sha we bound the machine to.
    exports = json.loads(_get(base, "/exports").read())["exports"]
    matching = [e for e in exports if e["content_sha256"] == disk_sha]
    assert matching, f"no export for disk sha in /exports: {exports}"
    assert matching[0]["status"] == "running"
    assert matching[0]["nbd_port"] == nbd_port


def test_nbdboot_without_bundle_unpacked_returns_unavailable(api: dict[str, object]) -> None:
    """When the netboot bundle is declared but its artifacts are NOT
    on disk (i.e. the tar.gz was never unpacked, perhaps because the
    fetch aborted), the renderer refuses to render a mismatched
    fallback. The plan is a hard ``exit`` with the reason in the
    comment."""
    base = str(api["base_url"])
    cookie = str(api["cookie"])
    state_dir = Path(str(api["state_dir"]))

    disk_sha = "3" * 64
    bundle_sha = "4" * 64  # advertised but NOT unpacked on disk

    _place_blob(state_dir, disk_sha, b"disk-bytes")

    disk_src = "oras://ghcr.io/example/two:test"
    bundle_src = "oras://ghcr.io/example/two-netboot:test"

    _post_json(
        base,
        "/catalog/entries",
        {
            "name": "two",
            "src": disk_src,
            "format": "img.gz",
            "netboot_src": bundle_src,
        },
        cookie=cookie,
    )
    _post_json(
        base,
        "/catalog/entries",
        {"name": "two-nb", "src": bundle_src, "format": "tar.gz"},
        cookie=cookie,
    )
    _mark_fetched(state_dir, "two", disk_sha, 10)
    _mark_fetched(state_dir, "two-nb", bundle_sha, 10)

    mac = "aa:bb:cc:dd:ee:aa"
    _put_json(
        base,
        f"/machines/{mac}",
        {"boot_mode": "nbdboot", "image_content_sha256": disk_sha},
        cookie=cookie,
    )

    plan = _get(base, f"/pxe/{mac}").read().decode("utf-8")
    assert plan.startswith("#!ipxe")
    assert "not unpacked" in plan or "manifest.json missing" in plan
    assert "exit" in plan
    assert "/artifacts/" not in plan  # no artifact URL in an unavailable plan


# --------------------------- helper --------------------------------------
#
# The integration conftest ships _post_json + _get + _delete but no
# PUT (nothing in PR 3a needed one). Add a local PUT helper here rather
# than widening the shared conftest for a single caller.


def _put_json(base: str, path: str, body: dict[str, object], *, cookie: str = ""):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"{base}{path}", data=data, method="PUT")
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        return urllib.request.urlopen(req, timeout=10.0)
    except urllib.error.HTTPError as exc:
        return exc
