"""Integration tests for ``pixie.flash``: real writes to a loop device.

``flash.py`` is the destructive core -- it dd's images to block devices
and rewrites UEFI boot order -- and until now was only reachable through
the containerised boot chain, which pytest-cov can't measure (the code
runs in the container). These tests drive the real write path IN-PROCESS
against a loop-device target, so they both exercise the destructive code
for real AND land in the coverage report:

- local-file flash for every supported format (raw + gz/zst/xz/bz2/qcow2)
  with a byte-for-byte read-back,
- the URL streaming pipeline (curl | decompress | dd) from a local HTTP
  server, with a progress callback so the dd/download progress pumps run,
- on-the-wire sha256 verification (correct digest flashes; a wrong
  declared digest raises FlashIntegrityError),
- operator cancel mid-stream (FlashCancelled) and the target-vanished
  race (FlashRaceError).

The UEFI boot-entry registration path is exercised deterministically in
``tests/test_flash_unit.py`` (a fake ``efibootmgr`` + canned ``lsblk``),
because loop-device PARTTYPE reporting needs a udev the CI container and
some dev hosts lack.

Marked ``integration``: needs passwordless sudo + losetup to set up the
loop target (the write itself runs unprivileged against the chmod'd
device node; flash.py's post-write partprobe is best-effort so it no-ops
without CAP_SYS_ADMIN). Skipped when those aren't available. The real
end-to-end flash-over-netboot / flash-over-usbboot paths are validated
separately by the ``test-pxe-flash*`` and ``verify-usbboot`` CI chains.
"""

from __future__ import annotations

import gzip
import hashlib
import http.server
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from pixie import flash

pytestmark = pytest.mark.integration

_TARGET_SIZE = 64 * 1024 * 1024  # loop-device target
_IMG_SIZE = 8 * 1024 * 1024  # raw image written to it (comfortably < target)

_FMT_TOOL = {
    "img.gz": "gzip",
    "img.zst": "zstd",
    "img.xz": "xz",
    "img.bz2": "bzip2",
    "qcow2": "qemu-img",
}


def _sudo_n_ok() -> bool:
    return subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0


requires_loop = pytest.mark.skipif(
    not _sudo_n_ok() or shutil.which("losetup") is None,
    reason="needs passwordless sudo + losetup for a loop-device target",
)


@pytest.fixture
def raw_bytes() -> bytes:
    return os.urandom(_IMG_SIZE)


@pytest.fixture
def raw_img(tmp_path: Path, raw_bytes: bytes) -> Path:
    p = tmp_path / "src.img"
    p.write_bytes(raw_bytes)
    return p


def _losetup(backing: Path) -> str:
    loop = subprocess.run(
        ["sudo", "losetup", "-f", "--show", str(backing)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(["sudo", "chmod", "666", loop], check=True)
    return loop


@pytest.fixture
def loop_target(tmp_path: Path) -> Iterator[Path]:
    backing = tmp_path / "target.raw"
    with open(backing, "wb") as f:
        f.truncate(_TARGET_SIZE)
    loop = _losetup(backing)
    try:
        yield Path(loop)
    finally:
        subprocess.run(["sudo", "losetup", "-d", loop], check=False)


@pytest.fixture
def http_base(tmp_path: Path) -> Iterator[str]:
    """Serve ``tmp_path`` (where the image fixtures write) over HTTP so the
    flash URL/streaming path has a real endpoint."""

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a: object, **k: object) -> None:
            super().__init__(*a, directory=str(tmp_path), **k)  # type: ignore[arg-type]

        def log_message(self, format: str, *args: object) -> None:  # quiet
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


def _compress(raw: Path, fmt: str, dst: Path) -> None:
    if fmt == "img.gz":
        with open(raw, "rb") as fi, gzip.open(dst, "wb") as fo:
            shutil.copyfileobj(fi, fo)
    elif fmt == "img.zst":
        subprocess.run(["zstd", "-q", "-f", "-o", str(dst), str(raw)], check=True)
    elif fmt in ("img.xz", "img.bz2"):
        tool = "xz" if fmt == "img.xz" else "bzip2"
        with open(dst, "wb") as out:
            subprocess.run([tool, "-q", "-c", str(raw)], check=True, stdout=out)
    elif fmt == "qcow2":
        subprocess.run(["qemu-img", "convert", "-O", "qcow2", str(raw), str(dst)], check=True)
    else:  # pragma: no cover - guard
        raise ValueError(fmt)


def _read_prefix(dev: Path, n: int) -> bytes:
    with open(dev, "rb") as f:
        return f.read(n)


class _Recorder:
    """Collects ``FlashProgress.event`` names so tests can assert the
    lifecycle fired AND so passing a callback exercises flash.py's dd /
    download progress pumps + subprocess-log pumps (a big chunk of the
    module that only runs when a progress callback is set)."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def __call__(self, p: flash.FlashProgress) -> None:
        self.events.append(p.event)


def _assert_lifecycle(rec: _Recorder) -> None:
    assert rec.events[0] == "started"
    assert "writing" in rec.events
    assert rec.events[-1] == "partprobed"  # ... "synced", "partprobed"


def _flash_and_verify(
    image_or_plan: Path | flash.FlashPlan, target: Path, expected: bytes
) -> _Recorder:
    if isinstance(image_or_plan, flash.FlashPlan):
        plan = image_or_plan
    else:
        plan = flash.make_plan(flash.probe_image(image_or_plan), flash.probe_target(target))
        assert flash.validate_plan(plan) == [], f"plan rejected: {flash.validate_plan(plan)}"
    rec = _Recorder()
    flash.execute_plan(plan, progress=rec)
    assert _read_prefix(target, len(expected)) == expected
    _assert_lifecycle(rec)
    return rec


# ---- local-file flash, every supported format -------------------------


@requires_loop
def test_flash_raw_img_local(raw_img: Path, raw_bytes: bytes, loop_target: Path) -> None:
    _flash_and_verify(raw_img, loop_target, raw_bytes)


@requires_loop
@pytest.mark.parametrize("fmt", ["img.gz", "img.zst", "img.xz", "img.bz2", "qcow2"])
def test_flash_compressed_local(
    fmt: str, raw_img: Path, raw_bytes: bytes, loop_target: Path, tmp_path: Path
) -> None:
    if shutil.which(_FMT_TOOL[fmt]) is None:
        pytest.skip(f"{_FMT_TOOL[fmt]} not installed")
    dst = tmp_path / f"src.{fmt}"
    _compress(raw_img, fmt, dst)
    _flash_and_verify(dst, loop_target, raw_bytes)


# ---- URL streaming pipeline + on-the-wire digest verify ---------------


@requires_loop
def test_flash_raw_url_verifies_digest(
    raw_img: Path, raw_bytes: bytes, loop_target: Path, http_base: str
) -> None:
    sha = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
    img = flash.probe_image_url(f"{http_base}/{raw_img.name}", expected_sha=sha)
    plan = flash.make_plan(img, flash.probe_target(loop_target))
    # tee|sha256sum verifies against the declared sha as it streams.
    _flash_and_verify(plan, loop_target, raw_bytes)


@requires_loop
def test_flash_url_wrong_digest_raises(raw_img: Path, loop_target: Path, http_base: str) -> None:
    img = flash.probe_image_url(f"{http_base}/{raw_img.name}", expected_sha="sha256:" + "0" * 64)
    plan = flash.make_plan(img, flash.probe_target(loop_target))
    with pytest.raises(flash.FlashIntegrityError):
        flash.execute_plan(plan)


@requires_loop
@pytest.mark.parametrize("fmt", ["img.gz", "img.zst", "img.xz", "img.bz2", "qcow2"])
def test_flash_compressed_url(
    fmt: str,
    raw_img: Path,
    raw_bytes: bytes,
    loop_target: Path,
    http_base: str,
    tmp_path: Path,
) -> None:
    """Each format over the URL path: the streaming decompressor pipeline
    (curl | zstd/xz/bzip2/gzip -d | dd) for the ``img.*`` variants, and
    the download-then-convert path for qcow2 (random-access, can't
    stream)."""
    if shutil.which(_FMT_TOOL[fmt]) is None:
        pytest.skip(f"{_FMT_TOOL[fmt]} not installed")
    dst = tmp_path / f"src.{fmt}"
    _compress(raw_img, fmt, dst)
    # Declare the digest of the fetched (compressed) blob so the streaming
    # tee|sha256sum integrity path runs for the img.* variants and the
    # download-then-hash path runs for qcow2.
    blob_sha = "sha256:" + hashlib.sha256(dst.read_bytes()).hexdigest()
    img = flash.probe_image_url(f"{http_base}/{dst.name}", expected_sha=blob_sha)
    # Compressed URL sources can't report a virtual size from HEAD, so
    # make_plan records a note that the size-fits check was skipped.
    plan = flash.make_plan(img, flash.probe_target(loop_target))
    assert any("size-fits-target check skipped" in n for n in plan.notes)
    _flash_and_verify(plan, loop_target, raw_bytes)


# ---- cancel + target-vanished race ------------------------------------


class _SlowHandler(http.server.BaseHTTPRequestHandler):
    """Dribbles the body out in small chunks so a URL flash is reliably
    still in flight when the cancel callback fires."""

    payload = b""

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        view = memoryview(self.payload)
        for i in range(0, len(view), 64 * 1024):
            try:
                self.wfile.write(view[i : i + 64 * 1024])
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            time.sleep(0.01)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def slow_http_base(raw_bytes: bytes) -> Iterator[str]:
    handler = type("_BoundSlowHandler", (_SlowHandler,), {"payload": raw_bytes})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


@requires_loop
def test_flash_url_cancel_raises_cancelled(loop_target: Path, slow_http_base: str) -> None:
    img = flash.probe_image_url(f"{slow_http_base}/src.img")
    plan = flash.make_plan(img, flash.probe_target(loop_target))
    with pytest.raises(flash.FlashCancelled):
        flash.execute_plan(plan, cancel=lambda: True)


@requires_loop
def test_flash_race_target_no_longer_block_raises(
    raw_img: Path, loop_target: Path, tmp_path: Path
) -> None:
    """A valid plan whose target stops being a block device between the
    dry-run and the flash must re-probe and raise FlashRaceError rather
    than writing blind. Simulated by pointing the plan at a symlink and
    repointing it at a regular file (losetup -d leaves the /dev node, so
    detaching wouldn't trip the block-device check)."""
    link = tmp_path / "target-link"
    link.symlink_to(loop_target)
    plan = flash.make_plan(flash.probe_image(raw_img), flash.probe_target(link))
    assert flash.validate_plan(plan) == []

    regular = tmp_path / "not-a-device"
    regular.write_bytes(b"")
    link.unlink()
    link.symlink_to(regular)

    with pytest.raises(flash.FlashRaceError):
        flash.execute_plan(plan)


@requires_loop
def test_flash_url_with_noncancelling_watchdog(
    raw_img: Path, raw_bytes: bytes, loop_target: Path, http_base: str
) -> None:
    """A cancel callback that never fires still spins up the watchdog; it
    exits on the pipeline's natural completion and the flash succeeds."""
    img = flash.probe_image_url(f"{http_base}/{raw_img.name}")
    plan = flash.make_plan(img, flash.probe_target(loop_target))
    flash.execute_plan(plan, cancel=lambda: False)
    assert _read_prefix(loop_target, len(raw_bytes)) == raw_bytes


@requires_loop
def test_flash_url_curl_failure_raises(raw_img: Path, loop_target: Path, http_base: str) -> None:
    """A fetch that 404s (curl -f) surfaces as FlashError, not a silent
    zero-byte flash. The source is removed after the HEAD probe so the
    GET fails."""
    img = flash.probe_image_url(f"{http_base}/{raw_img.name}")
    plan = flash.make_plan(img, flash.probe_target(loop_target))
    raw_img.unlink()
    with pytest.raises(flash.FlashError):
        flash.execute_plan(plan)


@requires_loop
def test_flash_url_corrupt_gz_decompress_failure_raises(
    loop_target: Path, http_base: str, tmp_path: Path
) -> None:
    bad = tmp_path / "corrupt.img.gz"
    bad.write_bytes(b"this is not valid gzip data" * 100)
    img = flash.probe_image_url(f"{http_base}/{bad.name}")
    plan = flash.make_plan(img, flash.probe_target(loop_target))
    with pytest.raises(flash.FlashError):
        flash.execute_plan(plan)


@requires_loop
def test_flash_local_corrupt_gz_decompress_failure_raises(
    loop_target: Path, tmp_path: Path
) -> None:
    bad = tmp_path / "corrupt.img.gz"
    bad.write_bytes(b"this is not valid gzip data" * 100)
    plan = flash.make_plan(flash.probe_image(bad), flash.probe_target(loop_target))
    with pytest.raises(flash.FlashError):
        flash.execute_plan(plan)


@requires_loop
def test_flash_dd_out_of_space_raises(raw_img: Path, tmp_path: Path) -> None:
    """A target smaller than the image, via a format whose decompressed
    size can't be known ahead of time (bz2 has no size header), so
    validate can't pre-reject it: dd hits ENOSPC and the flash surfaces
    FlashError rather than silently leaving a truncated disk."""
    if shutil.which("bzip2") is None:
        pytest.skip("bzip2 not installed")
    big = tmp_path / "big.img.bz2"
    _compress(raw_img, "img.bz2", big)  # decompresses to _IMG_SIZE (8 MiB)
    backing = tmp_path / "small.raw"
    with open(backing, "wb") as f:
        f.truncate(4 * 1024 * 1024)  # 4 MiB < 8 MiB
    loop = _losetup(backing)
    try:
        plan = flash.make_plan(flash.probe_image(big), flash.probe_target(Path(loop)))
        assert flash.validate_plan(plan) == []  # size check skipped (unknown virtual size)
        with pytest.raises(flash.FlashError):
            flash.execute_plan(plan)
    finally:
        subprocess.run(["sudo", "losetup", "-d", loop], check=False)


@pytest.fixture
def tiny_loop(tmp_path: Path) -> Iterator[Path]:
    backing = tmp_path / "tiny.raw"
    with open(backing, "wb") as f:
        f.truncate(4 * 1024 * 1024)  # 4 MiB, smaller than the 8 MiB test image
    loop = _losetup(backing)
    try:
        yield Path(loop)
    finally:
        subprocess.run(["sudo", "losetup", "-d", loop], check=False)


@requires_loop
def test_flash_local_raw_dd_out_of_space_raises(raw_img: Path, tiny_loop: Path) -> None:
    # Build the plan directly (validate would reject a raw image larger
    # than the target) so the dd write itself hits ENOSPC.
    plan = flash.make_plan(flash.probe_image(raw_img), flash.probe_target(tiny_loop))
    with pytest.raises(flash.FlashError):
        flash.execute_plan(plan)


@requires_loop
def test_flash_url_compressed_dd_out_of_space_raises(
    raw_img: Path, tiny_loop: Path, http_base: str, tmp_path: Path
) -> None:
    big = tmp_path / "big.img.bz2"
    _compress(raw_img, "img.bz2", big)
    img = flash.probe_image_url(f"{http_base}/{big.name}")
    plan = flash.make_plan(img, flash.probe_target(tiny_loop))
    with pytest.raises(flash.FlashError):
        flash.execute_plan(plan)


@requires_loop
def test_flash_url_compressed_curl_failure_raises(
    raw_img: Path, loop_target: Path, http_base: str, tmp_path: Path
) -> None:
    gz = tmp_path / "vanishing.img.gz"
    _compress(raw_img, "img.gz", gz)
    img = flash.probe_image_url(f"{http_base}/{gz.name}")
    plan = flash.make_plan(img, flash.probe_target(loop_target))
    gz.unlink()  # GET now 404s (curl -f)
    with pytest.raises(flash.FlashError):
        flash.execute_plan(plan)


@requires_loop
def test_flash_url_qcow2_curl_failure_raises(
    raw_img: Path, loop_target: Path, http_base: str, tmp_path: Path
) -> None:
    if shutil.which("qemu-img") is None:
        pytest.skip("qemu-img not installed")
    qc = tmp_path / "vanishing.qcow2"
    _compress(raw_img, "qcow2", qc)
    img = flash.probe_image_url(f"{http_base}/{qc.name}")
    plan = flash.make_plan(img, flash.probe_target(loop_target))
    qc.unlink()
    with pytest.raises(flash.FlashError):
        flash.execute_plan(plan)


@requires_loop
def test_execute_local_unknown_format_raises(raw_img: Path, loop_target: Path) -> None:
    # Defensive dispatch guard: a plan carrying an unrecognised format
    # (validate_plan would normally have rejected it) fails cleanly rather
    # than writing garbage.
    img = flash.ImageInfo(path=raw_img, format="bogus", size_bytes=10, virtual_size_bytes=10)
    plan = flash.make_plan(img, flash.probe_target(loop_target))
    with pytest.raises(flash.FlashError, match="cannot flash"):
        flash.execute_plan(plan)


@requires_loop
def test_execute_url_unknown_format_raises(loop_target: Path) -> None:
    img = flash.ImageInfo(
        path=None,
        url="http://127.0.0.1:1/x",
        format="bogus",
        size_bytes=10,
        virtual_size_bytes=None,
    )
    plan = flash.make_plan(img, flash.probe_target(loop_target))
    with pytest.raises(flash.FlashError, match="cannot flash"):
        flash.execute_plan(plan)
