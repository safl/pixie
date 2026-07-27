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
  server,
- on-the-wire sha256 verification (correct digest flashes; a wrong
  declared digest raises FlashIntegrityError).

Marked ``integration``: needs passwordless sudo + losetup to set up the
loop target (the write itself runs unprivileged against the chmod'd
device node; flash.py's post-write partprobe is best-effort so it no-ops
without CAP_SYS_ADMIN). Skipped when those aren't available.
"""

from __future__ import annotations

import gzip
import hashlib
import http.server
import os
import shutil
import subprocess
import threading
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


@pytest.fixture
def loop_target(tmp_path: Path) -> Iterator[Path]:
    backing = tmp_path / "target.raw"
    with open(backing, "wb") as f:
        f.truncate(_TARGET_SIZE)
    loop = subprocess.run(
        ["sudo", "losetup", "-f", "--show", str(backing)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(["sudo", "chmod", "666", loop], check=True)
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


def _flash_local_and_verify(image: Path, target: Path, expected: bytes) -> None:
    plan = flash.make_plan(flash.probe_image(image), flash.probe_target(target))
    assert flash.validate_plan(plan) == [], f"plan rejected: {flash.validate_plan(plan)}"
    flash.execute_plan(plan)
    assert _read_prefix(target, len(expected)) == expected


# ---- local-file flash, every supported format -------------------------


@requires_loop
def test_flash_raw_img_local(raw_img: Path, raw_bytes: bytes, loop_target: Path) -> None:
    _flash_local_and_verify(raw_img, loop_target, raw_bytes)


@requires_loop
@pytest.mark.parametrize("fmt", ["img.gz", "img.zst", "img.xz", "img.bz2", "qcow2"])
def test_flash_compressed_local(
    fmt: str, raw_img: Path, raw_bytes: bytes, loop_target: Path, tmp_path: Path
) -> None:
    if shutil.which(_FMT_TOOL[fmt]) is None:
        pytest.skip(f"{_FMT_TOOL[fmt]} not installed")
    dst = tmp_path / f"src.{fmt}"
    _compress(raw_img, fmt, dst)
    _flash_local_and_verify(dst, loop_target, raw_bytes)


# ---- URL streaming pipeline + on-the-wire digest verify ---------------


@requires_loop
def test_flash_raw_url_verifies_digest(
    raw_img: Path, raw_bytes: bytes, loop_target: Path, http_base: str
) -> None:
    sha = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
    img = flash.probe_image_url(f"{http_base}/{raw_img.name}", expected_sha=sha)
    plan = flash.make_plan(img, flash.probe_target(loop_target))
    flash.execute_plan(plan)  # tee|sha256sum verifies against the declared sha
    assert _read_prefix(loop_target, len(raw_bytes)) == raw_bytes


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
    img = flash.probe_image_url(f"{http_base}/{dst.name}")
    plan = flash.make_plan(img, flash.probe_target(loop_target))
    flash.execute_plan(plan)
    assert _read_prefix(loop_target, len(raw_bytes)) == raw_bytes
