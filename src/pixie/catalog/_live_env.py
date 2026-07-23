"""Fetch + stage the pixie live-env (the netboot-pc bake).

The live env -- ``vmlinuz`` + ``initrd`` + ``live.squashfs`` that the
``pixie-flash-*`` / ``pixie-inventory`` / ``pixie-tui`` boot modes chain
into -- was the one artifact pixie could not retrieve itself: it had to
be baked locally (``make build VARIANT=netboot-pc``) or hand-copied into
``PIXIE_LIVE_ENV_DIR``. This module pulls it from a single tarball
``src`` (the same ``https://`` / ``oras://`` schemes the catalog fetch
speaks) and stages the three files atomically, reusing the catalog
fetch pipeline's curl download + resume plumbing.

Contract with CI's ``publish-release`` job: the tarball carries exactly
``vmlinuz`` + ``initrd`` + ``live.squashfs`` at the archive root. Any
other member is ignored; a missing one is a hard error (better a loud
"tarball is wrong" than a live env that half-stages and boots into a
mysterious squashfs-fetch hang).
"""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pixie._util import CHUNK
from pixie.catalog._fetcher import (
    FetchError,
    ProgressReporter,
    _resolve_fetch_url,
    _stream_to_tmpfile,
)

# The three files a target's iPXE chain pulls from
# ``/boot/pixie-live-env/``. Names match what ``pixie-live-env.j2``
# references (note ``live.squashfs``, not the bake's raw ``*.squashfs``
# -- the CI tar step renames it).
LIVE_ENV_FILES: frozenset[str] = frozenset({"vmlinuz", "initrd", "live.squashfs"})


@dataclass
class LiveEnvResult:
    """Outcome of a successful stage: the src it came from, the sha256
    of the downloaded tarball, its byte size, and the staged file
    sizes."""

    src: str
    sha256: str
    size: int
    files: dict[str, int] = field(default_factory=dict)


def _unpack_live_env_tar(tar_path: Path, live_env_dir: Path) -> dict[str, int]:
    """Extract the live-env trio from ``tar_path`` into ``live_env_dir``
    atomically. Returns ``{name: size_bytes}``.

    Hardening mirrors :func:`pixie.catalog._fetcher._unpack_netboot_bundle`:
    only basenames in :data:`LIVE_ENV_FILES` at the archive root are
    accepted; nested paths, parent traversal, and dotfiles are dropped.
    Files land in a staging subdir first, then each is ``os.replace``'d
    onto its final name so a target fetching mid-stage sees the old
    file or the new one, never a half-written squashfs.
    """
    live_env_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(live_env_dir)))
    try:
        # ``r:*`` transparently handles .tar / .tar.gz / .tar.xz so the
        # CI side can pick whatever compression without a pixie change.
        with tarfile.open(str(tar_path), mode="r:*") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                name = os.path.basename(member.name)
                if not name or name.startswith(".") or name != member.name:
                    continue
                if name not in LIVE_ENV_FILES:
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                with (staging / name).open("wb") as out:
                    shutil.copyfileobj(extracted, out, length=CHUNK)

        missing = LIVE_ENV_FILES - {p.name for p in staging.iterdir()}
        if missing:
            raise FetchError(
                f"live-env tarball {tar_path.name} missing required file(s): {sorted(missing)}"
            )

        sizes: dict[str, int] = {}
        for name in sorted(LIVE_ENV_FILES):
            final = live_env_dir / name
            os.replace(staging / name, final)
            sizes[name] = final.stat().st_size
        return sizes
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def stage_live_env(
    src: str,
    live_env_dir: Path,
    *,
    progress: ProgressReporter = None,
) -> LiveEnvResult:
    """Download the live-env tarball at ``src`` and stage its three
    files into ``live_env_dir``.

    Reuses the catalog fetch's curl transport (resume + retry + stall
    detection). The tarball lands in ``live_env_dir`` itself so the
    unpack + ``os.replace`` stay on one filesystem. Raises
    :class:`FetchError` on an empty/bad src, an unsupported scheme, a
    download failure, or a tarball missing a required file.
    """
    if not (src or "").strip():
        raise FetchError("no live-env src configured (set PIXIE_LIVE_ENV_SRC)")
    url, headers = _resolve_fetch_url(src.strip())
    live_env_dir.mkdir(parents=True, exist_ok=True)
    tmp_tar, sha256, size = _stream_to_tmpfile(url, headers, live_env_dir, progress)
    try:
        if progress is not None:
            progress({"phase": "unpacking"})
        sizes = _unpack_live_env_tar(tmp_tar, live_env_dir)
    finally:
        tmp_tar.unlink(missing_ok=True)
    return LiveEnvResult(src=src.strip(), sha256=sha256, size=size, files=sizes)
