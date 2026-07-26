"""CLI: download a single ``oras://`` artifact blob to a file.

Build pipelines -- the ``pixie-live-env`` image bake under ``cijoe/`` --
need to pull a nosi base disk image the same way the operator UI's
Fetch does, i.e. through :mod:`pixie.oras`. Those pipelines run under
cijoe's own interpreter where ``import pixie`` is unavailable, so this
module exposes the pull as a ``python -m pixie.oras_pull`` entry point.
A build step then shells out to the repo's uv env::

    uv run python -m pixie.oras_pull <oras-ref> <out-path>

and reuses the exact resolve + streaming download the appliance uses,
with no ``oras`` CLI dependency.

Reuses :func:`pixie.oras.resolve_ref` (anonymous token -> manifest ->
layer pick) and :func:`pixie.catalog._fetcher._stream_to_tmpfile` (the
curl Range-resume download) so the downloaded bytes and their digest
match a live Fetch byte-for-byte. The downloaded blob's sha256 is
verified against the layer digest the registry resolved to; a mismatch
is a hard error rather than a silently corrupt base image.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

from pixie.catalog._fetcher import FetchError, _stream_to_tmpfile
from pixie.oras import OrasError, resolve_ref


def pull(ref: str, out: Path) -> str:
    """Resolve ``ref`` and download its blob to ``out`` atomically.

    Returns the ``sha256:<hex>`` digest the registry resolved the
    reference to (for a tag ref this is frozen at resolve time). Raises
    :class:`pixie.oras.OrasError` on resolution failure,
    :class:`pixie.catalog._fetcher.FetchError` on the download, and
    :class:`ValueError` if the downloaded bytes do not hash to the
    resolved digest.
    """
    resolved = resolve_ref(ref)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Stage into a scratch dir on the SAME filesystem as ``out`` so the
    # final rename is atomic.
    tmp_dir = out.parent / ".oras-pull-tmp"
    tmp_path, sha256, _size = _stream_to_tmpfile(resolved.blob_url, resolved.headers, tmp_dir)

    expected = resolved.digest.split(":", 1)[1] if ":" in resolved.digest else resolved.digest
    if sha256 != expected:
        tmp_path.unlink(missing_ok=True)
        raise ValueError(
            f"oras blob digest mismatch for {ref}: registry resolved "
            f"{resolved.digest} but downloaded bytes hash to sha256:{sha256}"
        )

    tmp_path.replace(out)
    # Best-effort scratch-dir cleanup; harmless if other files remain.
    with contextlib.suppress(OSError):
        tmp_dir.rmdir()
    return resolved.digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pixie.oras_pull",
        description="Download a single oras:// artifact blob to a file.",
    )
    parser.add_argument("ref", help="oras:// reference (tag or @sha256:<digest>)")
    parser.add_argument("out", type=Path, help="output file path")
    args = parser.parse_args(argv)

    try:
        digest = pull(args.ref, args.out)
    except (OrasError, FetchError, ValueError, OSError) as exc:
        print(f"pixie.oras_pull: {exc}", file=sys.stderr)
        return 1
    print(f"pulled {args.ref} -> {args.out} ({digest})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
