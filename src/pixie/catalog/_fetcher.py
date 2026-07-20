"""Fetch pipeline: download a catalog entry's src to disk, sha256 it,
and (for tar.gz netboot bundles) unpack vmlinuz + initrd +
manifest.json into a content-addressed artifacts directory.

Pixie's one fetch verb. Presence on disk IS readiness; there is no
warming stage, no ready/pending vocabulary, no misses. If a fetch
half-completes, its ``.inflight`` tmpfile survives the crash but the
catalog entry stays unfetched; a subsequent Fetch starts from zero.
(Resume-on-truncation, ORAS retry loop, etc are v0.3+ concerns.)

Runs synchronously inside the caller's thread. The routes layer wraps
each Fetch call in an :class:`asyncio.Task` so the HTTP handler
returns 202 immediately while the download proceeds; see
``pixie.catalog._routes``.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import logging
import lzma
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pixie import oras
from pixie._partition import PartitionNotFound, extract_partition
from pixie._util import CHUNK, now_iso
from pixie.catalog._schema import CatalogEntry
from pixie.catalog._store import CatalogStore

_log = logging.getLogger(__name__)

# Progress callback: the fetch pipeline invokes this at each phase
# transition + periodically during long streaming loops. The dict
# payload is JSON-serialisable (str / int / None) so a route can echo
# it straight back to the UI. Callers pass ``None`` when they don't
# care about progress; the fetcher handles that as a no-op.
ProgressReporter = Callable[[dict[str, Any]], None] | None


class FetchError(Exception):
    """Fetch pipeline failure. String is operator-shaped: it's what
    ``/catalog`` renders as the ``last_error`` on the entry row."""


@dataclass
class FetchResult:
    """Post-fetch summary. Handler returns to the caller so it can
    update UI + optionally trigger downstream side-effects."""

    name: str
    content_sha256: str
    size_bytes: int
    format: str
    # Set only for tar.gz netboot bundles; None for plain disk images.
    artifact_files: list[str] = field(default_factory=list)


# ------------------------------------------------------------------------
# HTTPS + ORAS URL resolution
# ------------------------------------------------------------------------


def _resolve_fetch_url(src: str) -> tuple[str, dict[str, str]]:
    """Turn a catalog entry's ``src`` into a plain HTTPS URL + auth
    headers the origin expects. ``oras://`` refs go through the ORAS
    adapter; ``https://`` refs are returned as-is."""
    if src.startswith("oras://"):
        resolved = oras.resolve_ref(src)
        headers = dict(resolved.headers or {})
        return resolved.blob_url, headers
    if src.startswith(("http://", "https://")):
        return src, {}
    raise FetchError(f"unsupported src scheme: {src!r}")


# ------------------------------------------------------------------------
# Streaming download with sha256
# ------------------------------------------------------------------------


def _stream_to_tmpfile(
    url: str,
    headers: dict[str, str],
    dest_dir: Path,
    progress: ProgressReporter = None,
) -> tuple[Path, str, int]:
    """Download bytes from ``url`` into ``dest_dir/<uuid>.inflight``
    via curl. Returns (path, sha256, size).

    Curl is the transport instead of urllib because ghcr / cloudflare
    middleboxes routinely close the socket after a per-transfer cap
    (three fetches this session all cut at ~1 GiB out of ~3 GiB
    payloads). ``--continue-at -`` + ``--retry-all-errors`` +
    ``--retry-max-time`` picks up where the previous byte landed
    without pixie hand-rolling the Range dance, and ``--speed-limit
    + --speed-time`` detects a stalled TCP that a bare socket
    timeout would miss. sha256 gets recomputed from the finished
    file (single read, fast on any SSD) rather than streamed at
    download time so the download+resume plumbing stays inside curl.

    Raises :class:`FetchError` on non-zero curl exit or empty body.
    Callers pass a ``dest_dir`` that lives on the same filesystem
    as the final blob path so the ``os.replace`` at commit time is
    atomic.

    ``progress`` is called at start (``phase='downloading'`` +
    ``total_bytes`` when a HEAD probe succeeded) then polled off the
    growing tmpfile size while curl runs, and once again on
    completion. The payload always carries ``phase`` +
    ``bytes_downloaded`` + ``total_bytes`` (``None`` when the server
    omits Content-Length).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="fetch-", suffix=".inflight", dir=str(dest_dir))
    tmp_path = Path(tmp_name)
    os.close(fd)

    total_bytes = _probe_content_length(url, headers)
    if progress is not None:
        progress(
            {
                "phase": "downloading",
                "bytes_downloaded": 0,
                "total_bytes": total_bytes,
            }
        )

    # Retry knobs are env-tunable so tests can force fast failure
    # (retry=0) instead of sitting through 10 * 5s = 50 s of curl's
    # retry backoff for a deterministically-broken mock server.
    retry_count = os.environ.get("PIXIE_FETCH_RETRY", "10")
    retry_delay = os.environ.get("PIXIE_FETCH_RETRY_DELAY", "5")
    retry_max_time = os.environ.get("PIXIE_FETCH_RETRY_MAX_TIME", "900")

    argv: list[str] = [
        "curl",
        "--silent",
        "--show-error",
        "--fail",  # non-zero exit on HTTP >= 400
        "--location",  # follow redirects
        "--continue-at",
        "-",  # resume from current tmpfile size
        "--retry",
        retry_count,
        "--retry-all-errors",
        "--retry-delay",
        retry_delay,
        "--retry-max-time",
        retry_max_time,
        "--speed-limit",
        "1024",  # bytes/s: treat < 1 KiB/s for --speed-time seconds as stall
        "--speed-time",
        "60",
        "--connect-timeout",
        "30",
        "--user-agent",
        "pixie-fetch/0.3-curl",
        "--output",
        str(tmp_path),
    ]
    for k, v in headers.items():
        argv.extend(["--header", f"{k}: {v}"])
    argv.append(url)

    try:
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        tmp_path.unlink(missing_ok=True)
        raise FetchError(f"curl binary not found on PATH: {exc}") from exc

    last_emit = _monotonic()
    try:
        while proc.poll() is None:
            time.sleep(0.5)
            if progress is not None:
                now = _monotonic()
                if now - last_emit >= 0.5:
                    try:
                        size = tmp_path.stat().st_size
                    except OSError:
                        size = 0
                    progress(
                        {
                            "phase": "downloading",
                            "bytes_downloaded": size,
                            "total_bytes": total_bytes,
                        }
                    )
                    last_emit = now
    finally:
        proc.wait()

    stderr_bytes = proc.stderr.read() if proc.stderr is not None else b""
    stderr_text = stderr_bytes.decode(errors="replace").strip()

    if proc.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        # Curl exit 18 = CURLE_PARTIAL_FILE: server closed the
        # connection before delivering all the bytes its
        # Content-Length promised. Rebrand as "truncated" so the
        # operator sees the same actionable wording the pre-curl
        # fetcher used, not just a bare exit code.
        if proc.returncode == 18:
            prefix = "download truncated for "
        else:
            prefix = f"curl exit {proc.returncode} for "
        raise FetchError(prefix + url + (f": {stderr_text}" if stderr_text else ""))

    try:
        written = tmp_path.stat().st_size
    except OSError as exc:
        raise FetchError(f"download tmpfile disappeared for {url}: {exc}") from exc

    if written == 0:
        tmp_path.unlink(missing_ok=True)
        raise FetchError(f"empty body from {url}")

    if total_bytes is not None and written != total_bytes:
        tmp_path.unlink(missing_ok=True)
        raise FetchError(
            f"download size mismatch for {url}: curl produced {written} bytes, "
            f"Content-Length was {total_bytes}"
        )

    sha = hashlib.sha256()
    with open(tmp_path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            sha.update(chunk)

    if progress is not None:
        progress(
            {
                "phase": "downloading",
                "bytes_downloaded": written,
                "total_bytes": total_bytes,
            }
        )

    return tmp_path, sha.hexdigest(), written


def _probe_content_length(url: str, headers: dict[str, str]) -> int | None:
    """HEAD ``url`` (following redirects) and return the final
    Content-Length as an int, or ``None`` when the server omits it
    or the probe fails. Best-effort: never raises, only informs the
    progress reporter's total-bytes field.
    """
    argv = ["curl", "--silent", "--show-error", "--fail", "--location", "--head"]
    for k, v in headers.items():
        argv.extend(["--header", f"{k}: {v}"])
    argv.append(url)
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    # HEAD-following-redirects prints multiple response blocks; the
    # final Content-Length header wins.
    last: int | None = None
    for line in result.stdout.splitlines():
        head, _, value = line.partition(":")
        if head.strip().lower() == "content-length":
            with contextlib.suppress(ValueError):
                last = int(value.strip())
    return last


def _monotonic() -> float:
    """Indirection so tests can freeze / step the clock without
    touching ``time`` globally. Kept trivial so mypy still sees it
    as ``() -> float``."""
    import time as _time

    return _time.monotonic()


# ------------------------------------------------------------------------
# Fetch verb: one call per catalog entry
# ------------------------------------------------------------------------


def fetch(
    entry: CatalogEntry,
    store: CatalogStore,
    progress: ProgressReporter = None,
) -> FetchResult:
    """Download + sha256 + (for tar.gz) unpack. Idempotent: if the
    entry is already fetched AND its blob still exists on disk, this
    returns the existing FetchResult immediately.

    For non-tar.gz formats: writes to ``blobs/<sha>/blob``.

    For ``tar.gz`` netboot bundles: streams the bytes to a tmpfile,
    computes sha256, then extracts vmlinuz + initrd + manifest.json
    into ``artifacts/<sha>/``. The tmpfile is discarded (bundles are
    only useful unpacked; the tar.gz itself is not served).

    ``progress`` is a live-status callback the routes layer wires to
    ``app.state.fetch_states[entry.name]``. Called at every phase
    transition (``downloading`` -> ``decompressing`` -> ``unpacking``
    -> done) so the operator UI can render a live status pill without
    polling the disk.

    Raises :class:`FetchError` on any failure. Catalog row is updated
    with content_sha + size + fetched_at only on success.
    """
    # Fast path: already fetched + blob still on disk.
    if entry.is_fetched():
        if entry.format == "tar.gz":
            manifest = store.artifact_path(entry.content_sha256, "manifest.json")
            if manifest.is_file():
                return FetchResult(
                    name=entry.name,
                    content_sha256=entry.content_sha256,
                    size_bytes=entry.size_bytes,
                    format=entry.format,
                    artifact_files=_list_artifact_files(store.artifact_dir(entry.content_sha256)),
                )
        else:
            blob = store.blob_path(entry.content_sha256)
            if blob.is_file():
                return FetchResult(
                    name=entry.name,
                    content_sha256=entry.content_sha256,
                    size_bytes=entry.size_bytes,
                    format=entry.format,
                )

    url, headers = _resolve_fetch_url(entry.src)
    _log.info("fetch %r: streaming from %s", entry.name, url)
    tmp_path, sha256, size = _stream_to_tmpfile(
        url, headers, store.state_dir / "tmp", progress=progress
    )

    blob_path = tmp_path  # placeholder; overwritten in the img branches
    try:
        if entry.format == "tar.gz":
            if progress is not None:
                progress({"phase": "unpacking"})
            artifact_dir = store.artifact_dir(sha256)
            _unpack_netboot_bundle(tmp_path, artifact_dir)
            artifacts = _list_artifact_files(artifact_dir)
            _log.info(
                "fetch %r: extracted %d artifacts to %s", entry.name, len(artifacts), artifact_dir
            )
        elif entry.format in _COMPRESSED_IMG_FORMATS:
            # ``img.gz`` / ``img.zst`` / ``img.xz`` publish the SOURCE
            # as compressed bytes but the appliance's boot chain (NBD
            # -> initrd -> mount) needs raw block-device bytes.
            # Decompress on the way in so the on-disk blob IS the
            # mountable disk image. Content-address on the RAW sha
            # (post-decompression) so /pxe/<mac> plans + NBD exports
            # stay stable across recompressions of the same source.
            if progress is not None:
                progress({"phase": "decompressing", "format": entry.format})
            decompressed_tmp, sha256, size = _decompress_to_tmpfile(
                tmp_path, entry.format, store.state_dir / "tmp"
            )
            tmp_path.unlink(missing_ok=True)
            tmp_path = decompressed_tmp
            _log.info(
                "fetch %r: decompressed %s -> %d raw bytes (sha=%s)",
                entry.name,
                entry.format,
                size,
                sha256[:12],
            )
            blob_dir = store.blob_path(sha256).parent
            blob_dir.mkdir(parents=True, exist_ok=True)
            blob_path = store.blob_path(sha256)
            os.replace(tmp_path, blob_path)
            tmp_path = blob_path
            artifacts = []
        else:
            blob_dir = store.blob_path(sha256).parent
            blob_dir.mkdir(parents=True, exist_ok=True)
            blob_path = store.blob_path(sha256)
            # Atomic: rename within same filesystem.
            os.replace(tmp_path, blob_path)
            tmp_path = blob_path  # so the finally-cleanup no-ops
            artifacts = []
    finally:
        # If tmp_path is still a scratch file (``.inflight`` from the
        # download stage, or ``.raw`` from a decompression that raised
        # before the rename-into-blobs) and NOT the final promoted
        # blob_path, remove it. A successful run reassigns tmp_path to
        # blob_path first, so this only fires on the error paths (or,
        # for tar.gz, always: the tar.gz bytes themselves are never
        # promoted, only the unpacked artifacts/ contents).
        if tmp_path.exists() and tmp_path.suffix in (".inflight", ".raw"):
            tmp_path.unlink(missing_ok=True)

    # Extract partition 1 (Linux root) to a sibling ``rootfs.raw``
    # for whole-disk img formats. Both the ephemeral nbdboot path
    # (nbdkit --filter=cow file file=rootfs.raw) and the persist path
    # (qemu-img create -b rootfs.raw) point at this file, so the
    # target-side initrd sees /dev/nbd0 as ext4 at offset 0 in both
    # modes and never has to think about partition tables. Whole-disk
    # blob stays alongside for the flash modes. Silent no-op on
    # tar.gz (netboot bundles) and on unpartitioned raw filesystem
    # images -- the ephemeral serve path falls back to the blob when
    # rootfs.raw is absent.
    if entry.format in _WHOLE_DISK_IMG_FORMATS:
        _extract_rootfs_if_partitioned(entry, blob_path, progress)

    store.mark_fetched(entry.name, content_sha256=sha256, size_bytes=size)
    return FetchResult(
        name=entry.name,
        content_sha256=sha256,
        size_bytes=size,
        format=entry.format,
        artifact_files=artifacts,
    )


_COMPRESSED_IMG_FORMATS = frozenset({"img.gz", "img.zst", "img.xz"})
_WHOLE_DISK_IMG_FORMATS = frozenset({"img", "img.gz", "img.zst", "img.xz"})


def _extract_rootfs_if_partitioned(
    entry: CatalogEntry,
    blob_path: Path,
    progress: Callable[[dict[str, Any]], None] | None,
) -> None:
    """Try to extract partition 1 into ``blobs/<sha>/rootfs.raw``.

    Silent on PartitionNotFound: the fetch still succeeds and the
    ephemeral / persist serve paths fall back to using the blob
    directly (the pre-extract-at-fetch behaviour). Loud on other IO
    errors so the operator sees a truncated-extract in the log
    rather than a silent regression at boot time."""
    rootfs = blob_path.parent / "rootfs.raw"
    if rootfs.is_file():
        return  # idempotent; a prior fetch already produced it
    if progress is not None:
        progress({"phase": "extracting-rootfs"})
    try:
        info = extract_partition(blob_path, rootfs, partition_number=1)
    except PartitionNotFound as exc:
        _log.info(
            "fetch %r: skipping rootfs extract (%s); ephemeral / persist will serve the whole blob",
            entry.name,
            exc,
        )
        return
    _log.info(
        "fetch %r: extracted partition 1 (%d bytes at offset %d) -> %s",
        entry.name,
        info.size_bytes,
        info.start_bytes,
        rootfs,
    )


def _decompress_to_tmpfile(src: Path, fmt: str, tmp_dir: Path) -> tuple[Path, str, int]:
    """Stream-decompress ``src`` (a downloaded compressed disk image)
    into a fresh tmpfile under ``tmp_dir``. Returns the tmpfile path
    plus the sha256 + size of the DECOMPRESSED bytes.

    Uses stdlib decoders for ``.gz`` and ``.xz``; shells out to the
    ``zstd`` binary for ``.zst`` (the Zstandard stdlib module lands in
    Python 3.14+ but pixie still supports 3.11). Raises
    :class:`FetchError` with an operator-actionable message when the
    input is corrupt or the decoder is missing.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(tempfile.mkstemp(prefix="decompress-", suffix=".raw", dir=tmp_dir)[1])
    hasher = hashlib.sha256()
    size = 0
    try:
        if fmt == "img.gz":
            with gzip.open(src, "rb") as src_fh, open(tmp_path, "wb") as dst_fh:
                while chunk := src_fh.read(CHUNK):
                    dst_fh.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
        elif fmt == "img.xz":
            with lzma.open(src, "rb") as src_fh, open(tmp_path, "wb") as dst_fh:
                while chunk := src_fh.read(CHUNK):
                    dst_fh.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
        elif fmt == "img.zst":
            if shutil.which("zstd") is None:
                raise FetchError(
                    "img.zst decompression needs the 'zstd' binary on PATH; install zstd"
                )
            with open(tmp_path, "wb") as dst_fh:
                proc = subprocess.Popen(
                    ["zstd", "-d", "--stdout", "--quiet", str(src)],
                    stdout=subprocess.PIPE,
                )
                assert proc.stdout is not None
                while chunk := proc.stdout.read(CHUNK):
                    dst_fh.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
                rc = proc.wait()
                if rc != 0:
                    raise FetchError(f"zstd -d exited {rc} on {src}")
        else:
            raise FetchError(f"unsupported compressed img format: {fmt!r}")
    except (OSError, EOFError, lzma.LZMAError, gzip.BadGzipFile) as exc:
        tmp_path.unlink(missing_ok=True)
        raise FetchError(f"decompress {fmt} failed: {exc}") from exc
    return tmp_path, hasher.hexdigest(), size


# ------------------------------------------------------------------------
# Netboot bundle unpack
# ------------------------------------------------------------------------


_REQUIRED_ARTIFACTS = frozenset({"vmlinuz", "initrd", "manifest.json"})


def _unpack_netboot_bundle(tar_gz_path: Path, artifact_dir: Path) -> None:
    """Extract ``tar_gz_path`` into ``artifact_dir`` atomically.

    Contract with nosi's ``cijoe/scripts/netboot_bundle_pack.py``: the
    tar.gz carries ``vmlinuz`` + ``initrd`` + ``manifest.json`` at the
    archive root. Anything else is ignored (tar filters block
    absolute paths + parent traversal for safety).

    Atomicity: extract into a sibling staging dir, then rename onto
    the final path. Callers get either a complete artifact directory
    or none at all; no partial state that a subsequent serve could
    trip on.
    """
    if artifact_dir.exists() and (artifact_dir / "manifest.json").exists():
        # Idempotent: same content sha, already unpacked.
        return

    artifact_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="unpack-", dir=str(artifact_dir.parent)))
    try:
        with tarfile.open(str(tar_gz_path), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                name = os.path.basename(member.name)
                if not name or name.startswith(".") or name != member.name:
                    # Reject nested paths + hidden files. nosi bakes
                    # the three known filenames at the archive root.
                    continue
                src = tar.extractfile(member)
                if src is None:
                    continue
                dest = staging / name
                with dest.open("wb") as out:
                    shutil.copyfileobj(src, out, length=CHUNK)

        missing = _REQUIRED_ARTIFACTS - {p.name for p in staging.iterdir()}
        if missing:
            raise FetchError(
                f"netboot bundle {tar_gz_path.name} missing required artifacts: {sorted(missing)}"
            )

        # Validate manifest.json is well-formed (its presence flips
        # readiness in the nbdboot flow; a garbled file would show up
        # as a mysterious boot failure otherwise).
        try:
            manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FetchError(f"netboot bundle manifest.json is not valid JSON: {exc}") from exc
        if not isinstance(manifest, dict):
            raise FetchError("netboot bundle manifest.json is not a JSON object")

        # Commit: rename staging onto the final path.
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)
        os.rename(staging, artifact_dir)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _list_artifact_files(artifact_dir: Path) -> list[str]:
    if not artifact_dir.is_dir():
        return []
    return sorted(p.name for p in artifact_dir.iterdir() if p.is_file())


# ------------------------------------------------------------------------
# Helper: catalog entry from a nosi-style TOML row
# ------------------------------------------------------------------------


def entry_from_dict(row: dict[str, object]) -> CatalogEntry:
    """Turn a nosi-style catalog dict row into a ``CatalogEntry``. Used
    by the /catalog/entries POST route + operator UI form."""
    now = now_iso()
    return CatalogEntry(
        name=str(row.get("name") or "").strip(),
        src=str(row.get("src") or "").strip(),
        format=str(row.get("format") or "").strip(),
        arch=str(row.get("arch") or ""),
        description=str(row.get("description") or ""),
        netboot_src=str(row.get("netboot_src") or ""),
        added_at=now,
    )


# ------------------------------------------------------------------------
# Development shim: BytesIO -> file so tests can inject bytes
# ------------------------------------------------------------------------


def stream_bytes_to_blob(
    payload: bytes,
    entry: CatalogEntry,
    store: CatalogStore,
) -> FetchResult:
    """Test/support helper: pretend the fetch succeeded with ``payload``.
    Skips the HTTP round-trip; useful for unit tests that don't want
    to bring up an http.server. Handles both the disk-image and
    tar.gz paths symmetrically with :func:`fetch`.
    """
    sha256 = hashlib.sha256(payload).hexdigest()
    size = len(payload)
    tmp = store.state_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp / f"synthetic-{sha256[:12]}.inflight"
    tmp_path.write_bytes(payload)

    try:
        if entry.format == "tar.gz":
            artifact_dir = store.artifact_dir(sha256)
            _unpack_netboot_bundle(tmp_path, artifact_dir)
            artifacts = _list_artifact_files(artifact_dir)
        else:
            blob_dir = store.blob_path(sha256).parent
            blob_dir.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_path, store.blob_path(sha256))
            tmp_path = store.blob_path(sha256)
            artifacts = []
    finally:
        if entry.format == "tar.gz" and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    store.mark_fetched(entry.name, content_sha256=sha256, size_bytes=size)
    return FetchResult(
        name=entry.name,
        content_sha256=sha256,
        size_bytes=size,
        format=entry.format,
        artifact_files=artifacts,
    )


# tests; keep imports for future consumers so the module stays stable.
_ = io.BytesIO
