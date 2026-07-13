"""Image catalog discovery and inspection.

Recognises the supported on-disk image formats (``.qcow2``, ``.img``,
``.img.zst``, ``.img.xz``, ``.img.gz``, ``.img.bz2``), lists them
under a configured image root, and extracts detail metadata for
individual images via the appropriate tool (``qemu-img info`` for
qcow2, ``zstd -l`` / ``xz -l`` / ``gzip -l`` for the corresponding
compressed raws; bzip2 has no listing tool so .img.bz2 has no
detail block).

Format-choice rationale: pixie-shipped images all use **gzip** for
universal flasher / OS / tooling support. The flash code accepts
**any** of ``.img``, ``.img.zst``, ``.img.xz``, ``.img.gz``,
``.img.bz2`` for operator-supplied images so format choice is
not forced on operators with their own pipelines.

- The **USB stick image** ships as ``.iso.gz``. Operators write
  it host-side via Etcher / Rufus / Raspberry Pi Imager, which
  decompress .gz natively (xz tripped Etcher's bundled
  decompressor regardless of how the file was shaped; gzip has
  no equivalent quirk). Stick prep is a one-shot, host-side cost.
  Universal flasher compat wins for media written once during
  setup; zstd's flash-time-decompression edge is irrelevant for a
  one-shot write -- the per-job reflash hot path applies to
  operator-supplied target images, not to pixie-shipped artifacts.
- Operators running per-job CI reflash on a fast disk can pick
  ``.img.zst`` for their own images and the flash code will
  stream-decompress at zstd's ~800-1500 MB/s. zstd's only
  downside is the version-cliff in some host-side flasher
  ecosystems, which doesn't apply to pixie's flash code -- it
  shells out to the system ``zstd`` binary, which is universal
  on Linux.
- Decompression speed ranking (rough): zstd > gzip > xz > bzip2.
  Pick based on workload: gzip for one-shot delivery, zstd for
  hot-path reflash.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Default image root. Operators override via the ``PIXIE_IMAGE_ROOT``
# environment variable. The USB live stick mounts the PIXIE_IMAGES
# partition here.
DEFAULT_IMAGE_ROOT = Path("/var/lib/pixie/images")

# Supported extensions, ordered most-specific first so multi-suffix
# variants (``.img.zst``, ``.img.xz``, ``.img.gz``, ``.img.bz2``)
# win over the bare ``.img``.
_EXTENSIONS: tuple[tuple[str, str], ...] = (
    (".img.zst", "img.zst"),
    (".img.xz", "img.xz"),
    (".img.gz", "img.gz"),
    (".img.bz2", "img.bz2"),
    (".qcow2", "qcow2"),
    (".img", "img"),
)

# Extensions explicitly NOT supported by the single-stream flash
# pipeline. Tarballs wrap the actual image inside per-file headers;
# decompressing the gzip/xz layer doesn't yield raw image bytes,
# it yields a tar stream. dd'ing that into a target disk would
# write tar headers into the MBR. Operators with these files must
# extract first (``tar -xzf foo.tar.gz`` etc.) and drop the
# resulting .img onto PIXIE_IMAGES.
_TARBALL_HINT_EXTS: tuple[str, ...] = (
    ".tar.gz",
    ".tar.xz",
    ".tar.bz2",
    ".tar.zst",
    ".tgz",
    ".txz",
    ".tbz2",
    ".tzst",
)


def is_tarball_extension(name: str) -> bool:
    """Return True if ``name`` looks like a tar archive that pixie
    cannot flash directly (caller should hint the operator to
    extract first)."""
    lower = name.lower()
    return any(lower.endswith(ext) for ext in _TARBALL_HINT_EXTS)


@dataclass(frozen=True)
class Image:
    """A discovered image file. Plain bytes-on-disk metadata only.

    ``sha256`` is the lower-case hex SHA-256 of the image bytes when
    a cached value is available (sidecar ``.sha256`` file or
    in-memory). ``None`` means "no sidecar present"; callers compute
    on demand if they need it.

    ``arch`` is an informational architecture hint (``x86_64`` /
    ``arm64`` / etc.) derived from the filename via
    :func:`detect_arch_from_name`. Never restricts flash eligibility
    -- pixie writes whatever bytes the operator points at; arch is a
    display-only column so the operator can see at a glance what
    platform an image targets. ``None`` when the filename carries
    no recognised arch token.
    """

    name: str
    path: Path
    format: str
    size_bytes: int
    sha256: str | None = None
    arch: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "format": self.format,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "arch": self.arch,
        }


def default_image_root() -> Path:
    """Resolve the configured image root.

    Precedence: ``PIXIE_IMAGE_ROOT`` env var, then ``DEFAULT_IMAGE_ROOT``.
    """
    env = os.environ.get("PIXIE_IMAGE_ROOT")
    return Path(env) if env else DEFAULT_IMAGE_ROOT


# Formats pixie can flash or serve as an NBD-backed root disk. Everything
# in ``_EXTENSIONS`` is a raw-disk container the flash pipeline knows
# how to stream through ``dd``. Not a superset: entries the catalog
# ships as sidecars for another consumer (e.g. ``tar.gz`` netboot
# bundles, which nbdmux unpacks at warm time to obtain vmlinuz +
# initrd) are deliberately absent so the operator picker + machine-
# binding gate exclude them. Kept in one place so a new bindable
# format is enabled by adding it to ``_EXTENSIONS`` alone.
BINDABLE_FORMATS: frozenset[str] = frozenset(fmt for _, fmt in _EXTENSIONS)


def detect_format(path: Path) -> str | None:
    """Return the image format identifier for ``path``, or ``None``."""
    name = path.name.lower()
    for ext, fmt in _EXTENSIONS:
        if name.endswith(ext):
            return fmt
    return None


# Filename arch tokens, mapped to a canonical short name. Order
# matters: scan the LONGEST tokens first so ``x86_64`` wins over a
# spurious match on ``x86`` in (say) ``x86_64-thing``. The canonical
# names match what ``uname -m`` reports on Linux for the same
# platform, so the displayed value lines up with what an operator
# sees logging into a flashed target.
_ARCH_TOKENS: tuple[tuple[str, str], ...] = (
    ("x86_64", "x86_64"),
    ("x86-64", "x86_64"),
    ("aarch64", "arm64"),
    ("amd64", "x86_64"),
    ("arm64", "arm64"),
    ("armhf", "arm"),
    ("armv7l", "arm"),
    ("armv6l", "armv6"),
    ("riscv64", "riscv64"),
    ("ppc64le", "ppc64le"),
    ("s390x", "s390x"),
    ("i686", "i386"),
    ("i386", "i386"),
)


def detect_arch_from_name(name: str) -> str | None:
    """Best-effort architecture hint from an image filename.

    Returns a canonical short arch name (matching ``uname -m`` on
    Linux: ``x86_64``, ``arm64``, ``i386``, ``arm``, ``riscv64``,
    etc.) or ``None`` when nothing is recognised.

    Pure substring match, case-insensitive, longest token first.
    Informational only -- callers do not filter or restrict based
    on it (pixie writes whatever bytes the operator points at).
    Common token forms map to one canonical:

    * ``amd64`` / ``x86_64`` / ``x86-64`` -> ``x86_64``
    * ``arm64`` / ``aarch64`` -> ``arm64``
    * ``armhf`` / ``armv7l`` -> ``arm``
    """
    lower = name.lower()
    for token, canonical in _ARCH_TOKENS:
        if token in lower:
            return canonical
    return None


def list_images(root: Path) -> list[Image]:
    """List supported images directly under ``root`` (non-recursive).

    Reads any cached SHA from the sidecar ``<file>.sha256`` if
    present (cheap; an operator may have computed it via
    ``sha256sum > <file>.sha256``). Does NOT compute SHA on the fly
    -- multi-GiB hashing on every listing would be punishing; this
    is for the ``pixie`` CLI's USB-stick scan path where O(1) listing
    matters.
    """
    if not root.exists() or not root.is_dir():
        return []

    out: list[Image] = []
    for p in sorted(root.iterdir()):
        # Symlinks could point outside ``root``; the bytes would
        # then be served via ``GET /images/<sha>`` even though
        # they live outside the operator-configured image root.
        # Reject symlinks defensively -- operators who really
        # want to share files across roots can copy or hardlink.
        if p.is_symlink():
            continue
        if not p.is_file():
            continue
        # Skip sidecar files; they're not images themselves.
        if p.name.endswith(".sha256"):
            continue
        fmt = detect_format(p)
        if fmt is None:
            continue
        # ``stat`` can race with concurrent unlink (operator drops
        # a file out of PIXIE_IMAGES between iterdir and stat).
        # Skip rather than crash the listing.
        try:
            size_bytes = p.stat().st_size
        except FileNotFoundError:
            continue
        out.append(
            Image(
                name=p.name,
                path=p,
                format=fmt,
                size_bytes=size_bytes,
                sha256=_read_sidecar_sha(p),
                arch=detect_arch_from_name(p.name),
            )
        )
    return out


def _sidecar_path(image_path: Path) -> Path:
    """Where the SHA-256 sidecar for ``image_path`` lives.

    Convention: ``foo.img.zst`` -> ``foo.img.zst.sha256``. Matches
    the sha256sum-style sidecar most release artifacts ship with
    so an operator can verify manually:

        sha256sum -c foo.img.zst.sha256
    """
    return image_path.with_name(image_path.name + ".sha256")


SHA256_HEX_LEN = 64
_SHA_HEX = frozenset("0123456789abcdef")


def is_sha256_hex(s: str) -> bool:
    """Return ``True`` iff ``s`` is a lower-case 64-char SHA-256
    hex digest. Single predicate shared by sidecar parsing,
    manifest validation, and the URL-key dispatch in pixie.
    """
    return len(s) == SHA256_HEX_LEN and all(c in _SHA_HEX for c in s)


def _read_sidecar_sha(image_path: Path) -> str | None:
    """Read a sidecar ``<file>.sha256`` if present + parseable.

    Tolerates two common shapes:

      * Just the hex digest on one line (``abc123...``).
      * ``sha256sum`` output: ``abc123...  filename`` (we take
        the first whitespace-separated token).

    Returns ``None`` (not an error) if the sidecar is missing,
    unreadable, or the digest doesn't look like a 64-char lower-
    case hex string. Callers treat None as "no sidecar" and decide
    whether to compute on demand.
    """
    sidecar = _sidecar_path(image_path)
    try:
        head = sidecar.read_text(encoding="utf-8").strip().split(maxsplit=1)
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None
    if not head:
        return None
    digest = head[0].strip().lower()
    if not is_sha256_hex(digest):
        return None
    return digest


@dataclass(frozen=True)
class ImageSource:
    """One way to obtain an image's bytes.

    Post-v0.66.0 sources are always catalog entries fetched via
    withcache; ``kind`` is always ``"manifest"`` and ``location``
    carries the upstream HTTP(S) or ``oras://`` URL. The dual-kind
    "local" variant went with the retired local dir-scan.
    """

    kind: str  # "manifest"
    location: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "location": self.location}


@dataclass(frozen=True)
class UnifiedImage:
    """Image record on the merged listing.

    Two identity fields, distinct on purpose:

    ``ref`` is the **provenance ID** -- ``sha256(canonicalise_src(src))``,
    a deterministic 64-hex digest of the canonical form of the source URL.
    Populated for every catalog entry. This is THE value machine
    bindings target -- a rolling oras tag's ref stays stable across
    re-pushes, so binding to a tag survives the next rebuild upstream.
    Always non-empty.

    ``sha256`` is the **observed content hash**. May be None for a
    rolling manifest entry that has never been pinned. Distinct from
    ``ref`` -- the same content can land under multiple refs (e.g.
    operator catalogs the same image under ``oras://a`` and
    ``http://b``), and the same ref can map to different content
    over time (rolling tag re-push).

    ``names`` collects every label the image goes by; ``sources``
    every fetch path. pixie's ``_list_unified_images`` builds
    these one-per-``WithcacheCatalog`` entry; the merge that
    produced multi-name entries went with ``merge_with_catalog``.
    """

    ref: str
    sha256: str | None
    names: tuple[str, ...]
    format: str | None
    size_bytes: int | None
    sources: tuple[ImageSource, ...]
    arch: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "sha256": self.sha256,
            "names": list(self.names),
            "format": self.format,
            "size_bytes": self.size_bytes,
            "sources": [s.to_dict() for s in self.sources],
            "arch": self.arch,
        }


def _run_detail_tool(cmd: list[str], *, timeout: float = 30.0) -> tuple[str | None, str | None]:
    """Run a metadata-listing tool, returning ``(detail, error)``.

    Exactly one element is non-None: ``detail`` is the tool's
    stripped stdout on success, otherwise ``error`` carries the
    stderr (or a timeout note). Bounds the call with ``timeout`` so
    a hung tool (corrupt file, slow network mount) can't wedge an
    inspect request -- the same defensive shell-out pattern
    :mod:`pixie.disks` and :mod:`pixie.web._sysconfig` use.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"{cmd[0]} timed out after {timeout:g}s"
    if proc.returncode == 0:
        return proc.stdout.strip(), None
    return None, proc.stderr.strip()


def _set_detail(info: dict[str, Any], detail: str | None, error: str | None) -> None:
    """Stash a text ``detail`` block or a ``detail_error`` onto the
    inspect result, depending on which :func:`_run_detail_tool`
    returned."""
    if detail is not None:
        info["detail"] = detail
    else:
        info["detail_error"] = error


def inspect_image(path: Path) -> dict[str, Any]:
    """Return detailed metadata for a single image file.

    Always includes ``path``, ``format``, and ``size_bytes``. Adds a
    format-specific ``detail`` block when the relevant tool succeeds:

    - ``qcow2`` -> the JSON output of ``qemu-img info --output=json``
    - ``img.zst`` -> the textual output of ``zstd -l``
    - ``img.xz`` -> the textual output of ``xz -l``
    - ``img.gz`` -> the textual output of ``gzip -l``
    - ``img.bz2`` -> nothing (bzip2 has no listing tool)

    Raises :class:`FileNotFoundError` if the path does not exist, or
    :class:`IsADirectoryError` if the path is a directory (operator
    almost certainly meant a file inside; surfacing a "format='',
    size_bytes=40" record for a directory was misleading).
    """
    if not path.exists():
        raise FileNotFoundError(path)
    if path.is_dir():
        raise IsADirectoryError(path)

    fmt = detect_format(path)
    info: dict[str, Any] = {
        "path": str(path),
        "format": fmt,
        "size_bytes": path.stat().st_size,
    }

    # Tarballs aren't flashable -- the inspection helper points
    # the operator at the right next step instead of returning a
    # blank ``format: ''`` record that looks like a pixie bug.
    if fmt is None and is_tarball_extension(path.name):
        info["detail_error"] = (
            "tarball; not directly flashable -- extract first "
            f"(e.g. ``tar -xf {path.name}``) and drop the resulting "
            "``.img`` / ``.qcow2`` onto PIXIE_IMAGES"
        )
        return info

    # Any other unrecognised extension: same shape as the tarball
    # branch, just a generic "this isn't a format pixie knows about"
    # message listing what IS supported. Without this, an inspect
    # against e.g. README.md returned a confusing blank record
    # with format=''.
    if fmt is None:
        supported = ", ".join(ext for ext, _ in _EXTENSIONS)
        info["detail_error"] = (
            f"unrecognised format for {path.name!r}; supported extensions: {supported}"
        )
        return info

    if fmt == "qcow2":
        detail, error = _run_detail_tool(["qemu-img", "info", "--output=json", str(path)])
        if detail is not None:
            # ``qemu-img info`` can exit 0 yet emit non-JSON (truncated
            # output, an image it half-understood). Treat a decode
            # failure as a detail error rather than crashing the
            # inspect request -- mirrors the guarded parse in
            # ``flash._image_virtual_size``.
            try:
                info["detail"] = json.loads(detail)
            except json.JSONDecodeError as exc:
                info["detail_error"] = f"qemu-img info returned unparseable JSON: {exc}"
        else:
            info["detail_error"] = error
    elif fmt == "img.zst":
        detail, error = _run_detail_tool(["zstd", "-l", str(path)])
        _set_detail(info, detail, error)
    elif fmt == "img.xz":
        detail, error = _run_detail_tool(["xz", "-l", str(path)])
        _set_detail(info, detail, error)
    elif fmt == "img.gz":
        detail, error = _run_detail_tool(["gzip", "-l", str(path)])
        _set_detail(info, detail, error)
    # img.bz2: no listing tool ships with bzip2; ``detail`` block
    # is intentionally omitted.

    return info
