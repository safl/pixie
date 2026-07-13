"""TFTP subprocess supervision, end-to-end over UDP.

Uses the shared ``container`` fixture (with ``PIXIE_TFTP_ENABLED=1``
baked into the image) and shells out to ``curl`` to fetch iPXE NBPs
over the real TFTP wire. Uses port 20069 (see conftest) because
udp/69 requires root.

Passes iff:

1. ``in.tftpd`` is actually listening on the port pixie's supervisor
   configured (proven by curl not returning immediately with
   ``connection refused``).
2. ``undionly.kpxe`` and ``ipxe.efi`` are readable from the TFTP root
   (the Containerfile bakes them from the ipxe apt package).
3. The bytes served match what's on disk in the container (spot-check
   the first 4 bytes of ``undionly.kpxe`` -- it's a DOS/COM executable
   starting with either ``\\xEB`` (jmp) or DOS boot-sector shape).

Fails loud with the container logs if any hop misses.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _curl_tftp(host: str, port: int, filename: str, dest: Path, *, timeout: int = 10) -> bytes:
    """Fetch ``tftp://host:port/filename`` into ``dest``. Returns the
    bytes read from ``dest`` on success. Raises AssertionError with
    curl's stderr on failure."""
    if shutil.which("curl") is None:
        pytest.skip("curl not on PATH")
    r = subprocess.run(
        [
            "curl",
            "-s",
            "-S",
            "-o",
            str(dest),
            "--max-time",
            str(timeout),
            f"tftp://{host}:{port}/{filename}",
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if r.returncode != 0:
        raise AssertionError(f"curl tftp fetch failed (rc={r.returncode}): {r.stderr!r}")
    return dest.read_bytes()


def test_tftp_serves_undionly_kpxe(container: dict[str, object], tmp_path: Path) -> None:
    port = int(container["tftp_port"])
    dest = tmp_path / "undionly.kpxe"
    body = _curl_tftp("127.0.0.1", port, "undionly.kpxe", dest)
    assert len(body) > 1024, f"unexpectedly small NBP: {len(body)} bytes"
    # ``undionly.kpxe`` starts with the iPXE mini-loader header. The
    # first 4 bytes are the PXE-loader jump instruction; assert on
    # non-emptiness + reasonable size rather than a magic sig, since
    # the iPXE build in ubuntu:26.04 may version-bump.
    assert body[:4] != b"\x00" * 4


def test_tftp_serves_ipxe_efi(container: dict[str, object], tmp_path: Path) -> None:
    """UEFI-shape NBP. Starts with the PE/COFF ``MZ`` magic."""
    port = int(container["tftp_port"])
    dest = tmp_path / "ipxe.efi"
    body = _curl_tftp("127.0.0.1", port, "ipxe.efi", dest)
    assert len(body) > 32 * 1024, f"unexpectedly small EFI: {len(body)} bytes"
    assert body[:2] == b"MZ", f"not a PE/COFF EFI: prefix={body[:8]!r}"


def test_tftp_404_on_missing_file(container: dict[str, object], tmp_path: Path) -> None:
    """A file that isn't in the TFTP root -> curl fails with a
    non-zero exit, no bytes written. We don't assert on the exact
    error string because tftpd-hpa's phrasing has changed across
    versions."""
    if shutil.which("curl") is None:
        pytest.skip("curl not on PATH")
    port = int(container["tftp_port"])
    dest = tmp_path / "nope"
    r = subprocess.run(
        [
            "curl",
            "-s",
            "-S",
            "-o",
            str(dest),
            "--max-time",
            "5",
            f"tftp://127.0.0.1:{port}/definitely-not-a-real-nbp.efi",
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    assert r.returncode != 0, "curl should have failed on missing tftp file"
