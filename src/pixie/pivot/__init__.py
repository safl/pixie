"""Pixie's initramfs pivot overlay.

Rationale
---------

The mode-specific pivot script (``/scripts/nbdboot`` -- the one that
parses ``pixie.nbd``/``pixie.image`` off the kernel cmdline, sets up
NBD + overlayfs, then pivot_roots) used to be BAKED INTO nosi's
netboot bundle via pixie-media's live-build hook chain (nosi
inherits pixie-media). That coupled two projects at the artifact
level:

* nosi could not publish a "generic" kernel + initrd anyone could
  netboot for their own purposes without carrying pixie's mode-
  specific pivot script.
* Renaming or updating the pivot script required rebuilding every
  nosi image.

Fix: pixie ships the pivot script here and serves it as a
supplementary initramfs overlay (newc-cpio, gzip-compressed) at
``GET /pivot/nbdboot.cpio.gz``. iPXE's ``nbdboot.j2`` template
loads two ``initrd`` directives -- nosi's own initrd first, then
this overlay -- and Linux concatenates them; the overlay's
``/scripts/nbdboot`` wins for the ``boot=nbdboot`` dispatch. Nosi's
netboot bundle becomes just "kernel + initrd + Debian
initramfs-tools" -- no pixie flavour.

This module owns:

- The script bytes (:data:`NBDBOOT_SCRIPT`, loaded from
  ``pivot/nbdboot`` in the source tree).
- The newc-cpio + gzip builder (:func:`build_pivot_cpio_gz`) that
  wraps the script into a ``/scripts/nbdboot`` entry the kernel
  can find at boot.

Downstream:

- ``pxe/_renderer.py`` no longer emits ``boot=ramboot``; the
  nbdboot template drops a second ``initrd`` line pointing at
  :func:`pixie.web.main`'s ``/pivot/nbdboot.cpio.gz`` route.
- ``pixie-media`` can eventually drop its own
  ``/scripts/ramboot`` (needs a coordinated nosi rebuild).
"""

from __future__ import annotations

import gzip
import io
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# The script path in the source tree. Loaded once at import time so
# the cpio builder can hash / re-emit without a filesystem walk.
_NBDBOOT_SRC = _HERE / "nbdboot"
NBDBOOT_SCRIPT: bytes = _NBDBOOT_SRC.read_bytes()


# newc-cpio format: 110-byte ASCII header + name + NUL-pad-to-4 +
# data + pad-to-4. See
# https://www.kernel.org/doc/Documentation/early-userspace/buffer-format.rst
_NEWC_MAGIC = b"070701"


def _newc_header(
    *,
    ino: int,
    mode: int,
    name: bytes,
    filesize: int,
    nlink: int = 1,
    mtime: int = 0,
) -> bytes:
    """Build one 110-byte newc header. ``name`` is the path bytes
    (no trailing NUL; namesize includes the NUL). ``mode`` combines
    file-type + permission bits (e.g. ``0o040755`` for a directory,
    ``0o100755`` for a regular executable). ``mtime=0`` gives byte-
    identical output across builds -- pixie's cpio contents only
    change when ``NBDBOOT_SCRIPT`` changes, so builders that hash
    the response body see a stable digest."""
    namesize = len(name) + 1  # NUL terminator counted
    fields = (
        _NEWC_MAGIC,
        b"%08x" % ino,
        b"%08x" % mode,
        b"%08x" % 0,  # uid = root
        b"%08x" % 0,  # gid = root
        b"%08x" % nlink,
        b"%08x" % mtime,
        b"%08x" % filesize,
        b"%08x" % 0,  # devmajor
        b"%08x" % 0,  # devminor
        b"%08x" % 0,  # rdevmajor
        b"%08x" % 0,  # rdevminor
        b"%08x" % namesize,
        b"%08x" % 0,  # check (unused for newc)
    )
    header = b"".join(fields)
    assert len(header) == 110, f"newc header must be 110 bytes, got {len(header)}"
    return header


def _pad4(buf: io.BytesIO) -> None:
    """Zero-pad the buffer to the next 4-byte boundary. newc requires
    every header + data blob start at a 4-byte-aligned offset."""
    over = buf.tell() & 3
    if over:
        buf.write(b"\0" * (4 - over))


def _emit_entry(
    buf: io.BytesIO,
    *,
    ino: int,
    mode: int,
    name: bytes,
    data: bytes,
) -> None:
    """Append one newc-cpio entry (header + name + pad + data + pad)
    to ``buf``. The trailer is emitted separately by the caller."""
    buf.write(_newc_header(ino=ino, mode=mode, name=name, filesize=len(data)))
    buf.write(name)
    buf.write(b"\0")  # namesize includes the NUL
    _pad4(buf)
    buf.write(data)
    _pad4(buf)


def build_pivot_cpio() -> bytes:
    """Build the pivot overlay as an uncompressed newc cpio. Contents:

    - ``scripts`` directory (mode 0755) -- Debian live-boot's ``/init``
      calls ``. /scripts/${BOOT}``, so a base initrd already has this
      directory; including it in the overlay is harmless (cpio
      overlay semantics: same-path entries overwrite / merge).
    - ``scripts/nbdboot`` regular file (mode 0755) -- the pivot
      script.
    - ``TRAILER!!!`` sentinel that terminates the archive.

    ``mtime=0`` on every entry so the output is byte-identical
    across runs given the same input script.
    """
    buf = io.BytesIO()
    ino = 1
    _emit_entry(buf, ino=ino, mode=0o040755, name=b"scripts", data=b"")
    ino += 1
    _emit_entry(
        buf,
        ino=ino,
        mode=0o100755,
        name=b"scripts/nbdboot",
        data=NBDBOOT_SCRIPT,
    )
    ino += 1
    # TRAILER!!!: a header with filesize=0 and the special name.
    buf.write(_newc_header(ino=0, mode=0, name=b"TRAILER!!!", filesize=0, nlink=1))
    buf.write(b"TRAILER!!!\0")
    _pad4(buf)
    return buf.getvalue()


def build_pivot_cpio_gz() -> bytes:
    """Same as :func:`build_pivot_cpio` but gzipped. iPXE + Linux both
    accept plain-cpio or gzipped-cpio as an initrd blob; gzip halves
    the served size for what would otherwise be a mostly-ASCII shell
    script, which matters when a target is on a bench-side console
    watching the transfer.

    ``mtime=0`` on the gzip header + no filename record so the
    compressed bytes are also byte-identical across builds."""
    raw = build_pivot_cpio()
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, compresslevel=9) as gz:
        gz.write(raw)
    return buf.getvalue()
