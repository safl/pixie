"""``pixie.oras_pull``: resolve + download an oras blob to a file.

The build pipelines shell out to ``python -m pixie.oras_pull`` to pull
a nosi base image the same way a live Fetch does. These tests exercise
the pull glue (digest verification + atomic write + the CLI's exit
codes) with :func:`pixie.oras.resolve_ref` and the fetcher stream both
monkeypatched, so no registry / network is touched.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import pixie.oras_pull as oras_pull
from pixie.oras import OrasError, ResolvedBlob


def _fake_resolved(digest: str) -> ResolvedBlob:
    return ResolvedBlob(
        blob_url="https://ghcr.io/v2/safl/nosi/arch-headless/blobs/" + digest,
        headers={"Authorization": "Bearer tok"},
        digest=digest,
        size=None,
        title=None,
    )


def _patch_stream(monkeypatch, payload: bytes) -> None:
    """Patch the fetcher stream to drop ``payload`` into the dest dir and
    report its real sha256, mimicking a successful download."""

    def fake_stream(url, headers, dest_dir, progress=None):
        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest_dir / "blob.inflight"
        tmp.write_bytes(payload)
        return tmp, hashlib.sha256(payload).hexdigest(), len(payload)

    monkeypatch.setattr(oras_pull, "_stream_to_tmpfile", fake_stream)


def test_pull_writes_blob_and_returns_digest(monkeypatch, tmp_path: Path) -> None:
    payload = b"arch-headless disk image bytes"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(oras_pull, "resolve_ref", lambda ref: _fake_resolved(digest))
    _patch_stream(monkeypatch, payload)

    out = tmp_path / "nested" / "base.img.gz"
    got = oras_pull.pull("oras://ghcr.io/safl/nosi/arch-headless:latest", out)

    assert got == digest
    assert out.read_bytes() == payload


def test_pull_rejects_digest_mismatch(monkeypatch, tmp_path: Path) -> None:
    payload = b"the actual bytes"
    wrong_digest = "sha256:" + hashlib.sha256(b"something else").hexdigest()
    monkeypatch.setattr(oras_pull, "resolve_ref", lambda ref: _fake_resolved(wrong_digest))
    _patch_stream(monkeypatch, payload)

    out = tmp_path / "base.img.gz"
    with pytest.raises(ValueError, match="digest mismatch"):
        oras_pull.pull("oras://ghcr.io/safl/nosi/arch-headless:latest", out)
    assert not out.exists()


def test_main_reports_resolution_error(monkeypatch, tmp_path: Path, capsys) -> None:
    def boom(ref):
        raise OrasError("no such tag")

    monkeypatch.setattr(oras_pull, "resolve_ref", boom)
    rc = oras_pull.main(["oras://ghcr.io/safl/nosi/arch-headless:latest", str(tmp_path / "o")])
    assert rc == 1
    assert "no such tag" in capsys.readouterr().err


def test_main_success(monkeypatch, tmp_path: Path, capsys) -> None:
    payload = b"ok"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(oras_pull, "resolve_ref", lambda ref: _fake_resolved(digest))
    _patch_stream(monkeypatch, payload)

    out = tmp_path / "base.img.gz"
    rc = oras_pull.main(["oras://ghcr.io/safl/nosi/arch-headless:latest", str(out)])
    assert rc == 0
    assert out.read_bytes() == payload
    assert digest in capsys.readouterr().out
