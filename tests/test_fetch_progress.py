"""Progress reporter contract for the fetch pipeline.

The web layer wires a callable to ``fetch()`` that echoes each phase
into ``app.state.fetch_states[<name>]``. The UI's live-status pill
polls ``/ui/catalog/fetch-states.json`` and rewrites the row's status
cell in place. These tests pin the callback contract so a downstream
change to the fetcher's phase vocabulary breaks the pill contract
loudly instead of silently ceasing to update.
"""

from __future__ import annotations

import http.server
import socketserver
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import authed as _authed


class _CountingHandler(http.server.BaseHTTPRequestHandler):
    """A one-shot HTTP server that returns a deterministic payload +
    Content-Length so the download loop's progress emit sees a
    non-None ``total_bytes``. Logging is silenced so pytest -q stays
    clean."""

    payload = b"pixie-fetch-progress-test-body" * 32

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        self.wfile.write(self.payload)

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


class _TruncatingHandler(http.server.BaseHTTPRequestHandler):
    """Advertises a Content-Length larger than the bytes it actually
    sends, then closes the connection: this is what a mid-transfer
    network hiccup or registry hiccup looks like on the wire. The
    curl-based fetcher's ``--continue-at -`` retry loop eventually
    gives up + exits non-zero, and the fetcher's size-vs-HEAD guard
    would also catch a short-but-CL-clean stream."""

    protocol_version = "HTTP/1.0"
    payload = b"only-part-of-the-body"
    declared_length = len(payload) + 1000

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", str(self.declared_length))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        self.wfile.write(self.payload)

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", str(self.declared_length))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def _spawn_server(
    handler: type[http.server.BaseHTTPRequestHandler] = _CountingHandler,
) -> tuple[socketserver.TCPServer, str, threading.Thread]:
    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}/blob", thread


def test_fetch_progress_reports_download_phase(tmp_path: Path) -> None:
    """``fetch()`` emits at least a ``downloading`` payload with
    ``bytes_downloaded`` and (because the mock server sets
    Content-Length) a matching ``total_bytes``."""
    from pixie.catalog._fetcher import fetch
    from pixie.catalog._schema import CatalogEntry
    from pixie.catalog._store import CatalogStore

    server, url, _thread = _spawn_server()
    try:
        store = CatalogStore(tmp_path)
        entry = CatalogEntry(name="tiny", src=url, format="img")
        store.upsert(entry)
        seen: list[dict[str, object]] = []
        fetch(entry, store, progress=seen.append)
    finally:
        server.shutdown()
        server.server_close()

    downloading = [p for p in seen if p.get("phase") == "downloading"]
    assert downloading, "expected at least one 'downloading' progress emit"
    final = downloading[-1]
    assert final.get("bytes_downloaded") == len(_CountingHandler.payload)
    assert final.get("total_bytes") == len(_CountingHandler.payload)


def test_fetch_raises_on_truncated_download(tmp_path: Path) -> None:
    """A connection that closes before delivering the bytes its own
    Content-Length promised must fail fast, at the download stage,
    with an operator-actionable message -- not silently produce a
    short file that only fails later during decompression with a
    confusing gzip error."""
    from pixie.catalog._fetcher import FetchError, fetch
    from pixie.catalog._schema import CatalogEntry
    from pixie.catalog._store import CatalogStore

    server, url, _thread = _spawn_server(_TruncatingHandler)
    try:
        store = CatalogStore(tmp_path)
        entry = CatalogEntry(name="short", src=url, format="img")
        store.upsert(entry)
        try:
            fetch(entry, store)
            raised = None
        except FetchError as exc:
            raised = exc
    finally:
        server.shutdown()
        server.server_close()

    assert raised is not None, "expected FetchError for a truncated download"
    assert "truncated" in str(raised)
    # No leftover .inflight scratch file for the truncated download.
    assert not list((tmp_path / "tmp").glob("*.inflight"))


def test_ui_fetch_states_json_reflects_report(client: TestClient) -> None:
    """The /ui/catalog/fetch-states.json endpoint mirrors
    ``app.state.fetch_states`` verbatim, so the UI poller sees the
    same phase dict the report callback wrote."""
    c = _authed(client)
    c.app.state.fetch_states["poll-me"] = {  # type: ignore[attr-defined]
        "state": "fetching",
        "phase": "downloading",
        "bytes_downloaded": 12345,
        "total_bytes": 99999,
    }
    r = c.get("/ui/fetch-states.json")
    assert r.status_code == 200
    body = r.json()
    assert body["poll-me"]["phase"] == "downloading"
    assert body["poll-me"]["bytes_downloaded"] == 12345
    assert body["poll-me"]["total_bytes"] == 99999


def test_ui_fetch_states_json_requires_auth(client: TestClient) -> None:
    """No session cookie -> redirect to /ui/login. Prevents an
    unauth'd caller from enumerating catalog names via the state
    dict."""
    r = client.get("/ui/fetch-states.json", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"
