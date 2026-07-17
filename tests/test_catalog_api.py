"""JSON + form catalog routes: add, fetch (via synthetic bytes), list,
delete, and the content-addressed blob + artifact serves.
"""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_ADMIN_PASSWORD


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, state_dir: Path) -> Iterator[TestClient]:
    """Custom client fixture that points the app at a per-test state
    dir so each test gets a fresh state.db + blobs/ + artifacts/."""
    monkeypatch.setenv("PIXIE_ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    monkeypatch.setenv("PIXIE_DATA_DIR", str(state_dir))
    from pixie.web.main import create_app

    app = create_app()
    with TestClient(app) as c:
        c.post("/ui/login", data={"password": TEST_ADMIN_PASSWORD})
        yield c


def _tiny_bundle_bytes() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        manifest = json.dumps(
            {"variant": "tiny", "arch": "x86_64", "kernel_version": "0.0.0-test"}
        ).encode()
        for name, body in (
            ("vmlinuz", b"KERNEL"),
            ("initrd", b"INITRD"),
            ("manifest.json", manifest),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


# ---------- add + list --------------------------------------------------


def test_add_entry_appears_in_catalog(client: TestClient) -> None:
    r = client.post(
        "/catalog/entries",
        json={
            "name": "tiny",
            "src": "https://example.com/tiny.img.gz",
            "format": "img.gz",
            "arch": "x86_64",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()["entry"]
    assert body["name"] == "tiny"
    assert body["fetched"] is False
    assert body["bindable"] is True

    entries = client.get("/catalog").json()["entries"]
    names = [e["name"] for e in entries]
    assert names == ["tiny"]


def test_add_entry_conflict_returns_409(client: TestClient) -> None:
    body = {"name": "dup", "src": "https://x/x.img.gz", "format": "img.gz"}
    assert client.post("/catalog/entries", json=body).status_code == 201
    r = client.post("/catalog/entries", json=body)
    assert r.status_code == 409


def test_add_entry_requires_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No session cookie -> 401 on write routes."""
    monkeypatch.setenv("PIXIE_ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    monkeypatch.setenv("PIXIE_DATA_DIR", str(tmp_path))
    from pixie.web.main import create_app

    with TestClient(create_app()) as c:
        r = c.post(
            "/catalog/entries",
            json={"name": "x", "src": "https://x/x.img.gz", "format": "img.gz"},
        )
        assert r.status_code == 401


def test_delete_entry_removes_it(client: TestClient) -> None:
    client.post(
        "/catalog/entries",
        json={"name": "tiny", "src": "https://x/x.img.gz", "format": "img.gz"},
    )
    r = client.delete("/catalog/entries", params={"name": "tiny"})
    assert r.status_code == 204
    assert client.get("/catalog").json()["entries"] == []


def test_delete_missing_returns_404(client: TestClient) -> None:
    r = client.delete("/catalog/entries", params={"name": "nope"})
    assert r.status_code == 404


# ---------- fetch (via synthetic bytes injection) ----------------------


def test_disk_image_blob_end_to_end(client: TestClient, state_dir: Path) -> None:
    """The fetch pipeline is HTTP-driven; here we bypass the network by
    calling ``stream_bytes_to_blob`` directly, then verify ``GET /catalog``
    reflects the sha and ``GET /b/<sha>/<name>`` serves the bytes."""
    from pixie.catalog._fetcher import stream_bytes_to_blob
    from pixie.catalog._schema import CatalogEntry
    from pixie.catalog._store import CatalogStore

    payload = b"ROOTFS-CONTENT"
    store = CatalogStore(state_dir)
    entry = CatalogEntry(name="tiny", src="https://example.com/tiny.img.gz", format="img.gz")
    store.upsert(entry)
    result = stream_bytes_to_blob(payload, entry, store)

    catalog_entry = client.get("/catalog").json()["entries"]
    assert len(catalog_entry) == 1
    row = catalog_entry[0]
    assert row["content_sha256"] == result.content_sha256
    assert row["size_bytes"] == len(payload)
    assert row["fetched"] is True

    # Open serve: no cookies + content-matches
    r = client.get(f"/b/{result.content_sha256}/tiny.img.gz")
    assert r.status_code == 200
    assert r.content == payload

    # HEAD is served with Content-Length + no body so the ported
    # pixie CLI's ``flash._probe_image_url_http`` can size an image
    # before dispatching an auto-flash. A 405 here (regression to
    # ``@router.get`` only) aborts every pixie-hosted auto-flash
    # before ``dd`` fires; see #237.
    h = client.head(f"/b/{result.content_sha256}/tiny.img.gz")
    assert h.status_code == 200
    assert h.content == b""
    assert h.headers["content-length"] == str(len(payload))

    # HEAD on a missing blob still 404s (same read guard as GET).
    h404 = client.head("/b/" + "b" * 64 + "/nope.img.gz")
    assert h404.status_code == 404


def test_netboot_bundle_serves_artifacts(client: TestClient, state_dir: Path) -> None:
    """A fetched tar.gz bundle unpacks + serves vmlinuz + initrd +
    manifest.json at content-addressed URLs."""
    from pixie.catalog._fetcher import stream_bytes_to_blob
    from pixie.catalog._schema import CatalogEntry
    from pixie.catalog._store import CatalogStore

    payload = _tiny_bundle_bytes()
    store = CatalogStore(state_dir)
    entry = CatalogEntry(name="tiny-nb", src="https://x/tiny-netboot.tar.gz", format="tar.gz")
    store.upsert(entry)
    result = stream_bytes_to_blob(payload, entry, store)

    sha = result.content_sha256
    r_vm = client.get(f"/artifacts/{sha}/vmlinuz")
    r_ir = client.get(f"/artifacts/{sha}/initrd")
    r_mf = client.get(f"/artifacts/{sha}/manifest.json")
    assert r_vm.status_code == 200
    assert r_vm.content == b"KERNEL"
    assert r_ir.status_code == 200
    assert r_ir.content == b"INITRD"
    assert r_mf.status_code == 200
    assert r_mf.json()["variant"] == "tiny"

    # Artifacts accept HEAD too, mirrors the blob route: iPXE only
    # GETs but keeping the two routes symmetric protects against a
    # future probe-verb change (or an operator curl -I) 405ing.
    h_vm = client.head(f"/artifacts/{sha}/vmlinuz")
    assert h_vm.status_code == 200
    assert h_vm.content == b""
    assert h_vm.headers["content-length"] == str(len(b"KERNEL"))


def test_artifact_serve_rejects_bad_sha(client: TestClient) -> None:
    r = client.get("/artifacts/not-a-sha/vmlinuz")
    assert r.status_code == 404
    r = client.get("/artifacts/" + "a" * 64 + "/../secret")
    # Path traversal caught by the filename allowlist.
    assert r.status_code == 404


def test_blob_serve_rejects_bad_sha(client: TestClient) -> None:
    r = client.get("/b/short/blob.img.gz")
    assert r.status_code == 404


def test_artifact_serve_rejects_unknown_filename(client: TestClient) -> None:
    r = client.get("/artifacts/" + "a" * 64 + "/secret.env")
    assert r.status_code == 404


def test_serve_404_when_content_not_yet_on_disk(client: TestClient) -> None:
    """The URL sha is content-addressed; nothing has fetched, nothing
    serves. 404 without leaking anything about what shas the store
    might know about."""
    r = client.get("/b/" + "a" * 64 + "/foo.img.gz")
    assert r.status_code == 404
    r = client.get("/artifacts/" + "a" * 64 + "/vmlinuz")
    assert r.status_code == 404
