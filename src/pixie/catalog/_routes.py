"""HTTP routes for the catalog + blob + artifacts surface.

Mounted from :mod:`pixie.web.main` at repo-root paths so operator
muscle memory (``/catalog``, ``/b/``, ``/artifacts/``) survives the
merge from bty + withcache + nbdmux into one process.

Read routes (``GET /catalog``, ``GET /b/<sha>/<name>``,
``GET /artifacts/<sha>/{file}``) are OPEN by design: the PXE-boot
targets that hit ``/artifacts/`` and ``/b/`` cannot hold a session
cookie, and the LAN-only trust model matches nbdmux's original
posture. Write routes (``POST`` / ``DELETE``) require a valid pixie
session.
"""

from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from pixie._util import now_iso
from pixie.catalog._fetcher import FetchError, entry_from_dict, fetch
from pixie.catalog._store import CatalogStore
from pixie.web._auth import require_auth

# Named field-safety regex for the content sha URL segment. iPXE fires
# at ``/artifacts/<sha>/vmlinuz``; a bad sha is a client bug + we 404
# rather than let a caller poke around the artifacts tree.
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# Filenames the netboot bundle carries + we serve. iPXE templates
# reference these; keep the allowlist tight so ``/artifacts/<sha>/../..``
# style traversal never even reaches the store.
_ARTIFACT_FILES = frozenset({"vmlinuz", "initrd", "manifest.json"})


class AddEntryBody(BaseModel):
    """Operator-facing body for ``POST /catalog/entries``. Deliberately
    tight: only fields the operator writes at add time; content_sha /
    size / fetched_at are populated by the fetch pipeline."""

    name: str = Field(..., min_length=1)
    src: str = Field(..., min_length=1)
    format: str = Field(..., min_length=1)
    arch: str = ""
    description: str = ""
    netboot_src: str = ""


def _get_store(request: Request) -> CatalogStore:
    """Route-scoped dep: catalog store lives on app.state, attached at
    startup by ``create_app``. Not passed via Depends so the router
    can stay a thin wrapper."""
    store: CatalogStore | None = getattr(request.app.state, "catalog_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="catalog store not initialised",
        )
    return store


def _get_fetch_pool(request: Request) -> ThreadPoolExecutor:
    """The fetch pipeline is stdlib blocking IO (``urllib`` + tarfile).
    Route handlers submit ``fetch(...)`` to a shared thread pool so
    concurrent downloads don't block the event loop."""
    pool: ThreadPoolExecutor | None = getattr(request.app.state, "fetch_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="fetch pool not initialised",
        )
    return pool


def _fetch_states(request: Request) -> dict[str, dict[str, Any]]:
    """Per-name fetch tracker used by ``GET /catalog`` to advertise
    in-flight / error state to the operator UI. Values shape:
    ``{"state": "fetching" | "error", "started_at": iso,
    "error": str | None}``.
    """
    states: dict[str, dict[str, Any]] | None = getattr(request.app.state, "fetch_states", None)
    if states is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="fetch state map not initialised",
        )
    return states


def _decode_sha(seg: str) -> str:
    if not _SHA_RE.match(seg):
        raise HTTPException(status_code=404, detail="not found")
    return seg


router = APIRouter()


# ----------------------- catalog CRUD --------------------------------


@router.get("/catalog")
def list_catalog(request: Request) -> dict[str, Any]:
    """Every catalog entry the operator has staged, downloaded or not.
    Presence in this list does not imply the bytes are on disk (see
    ``fetched``). Ships transient fetch state so the UI can render a
    "downloading" pill without a second poll."""
    store = _get_store(request)
    states = _fetch_states(request)
    entries: list[dict[str, Any]] = []
    for e in store.list_entries():
        row = e.to_dict()
        st = states.get(e.name)
        if st:
            row["fetch_state"] = st.get("state")
            if st.get("started_at"):
                row["fetch_started_at"] = st["started_at"]
            if st.get("error"):
                row["fetch_error"] = st["error"]
        entries.append(row)
    return {"entries": entries}


@router.post("/catalog/entries", status_code=201)
def add_entry(
    request: Request,
    body: AddEntryBody,
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    """Stage a new catalog entry. Does NOT fetch bytes; the operator
    hits Fetch as a separate step (or a UI Fetch button POSTs both
    add + fetch in a single click)."""
    store = _get_store(request)
    if store.get_entry(body.name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"entry {body.name!r} already exists",
        )
    entry = entry_from_dict(body.model_dump())
    store.upsert(entry)
    return {"entry": entry.to_dict()}


@router.delete("/catalog/entries", status_code=204)
def delete_entry(
    request: Request,
    name: str,
    _auth: None = Depends(require_auth),
) -> Response:
    """Delete a catalog entry by name (``?name=<name>`` query param).
    Blob + artifact bytes are NOT removed here even if the entry was
    the last reference; a separate GC route walks the store."""
    store = _get_store(request)
    if not store.delete(name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no entry with name={name!r}",
        )
    _fetch_states(request).pop(name, None)
    return Response(status_code=204)


@router.post("/catalog/entries/{name}/fetch", status_code=202)
async def start_fetch(
    request: Request,
    name: str,
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    """Kick off a fetch for the named entry. Returns 202 immediately;
    the actual download runs in the fetch pool. ``GET /catalog``
    reflects fetching / error state via the ``fetch_state`` field."""
    store = _get_store(request)
    entry = store.get_entry(name)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no entry with name={name!r}",
        )

    states = _fetch_states(request)
    if states.get(name, {}).get("state") == "fetching":
        # In-flight already; return the current state instead of
        # spawning a second task.
        return {"state": "fetching", "started_at": states[name].get("started_at")}

    states[name] = {"state": "fetching", "started_at": now_iso(), "error": None}
    pool = _get_fetch_pool(request)
    loop = asyncio.get_event_loop()

    def _run() -> None:
        try:
            fetch(entry, store)
            states[name] = {"state": "done", "started_at": states[name].get("started_at")}
        except FetchError as exc:
            states[name] = {
                "state": "error",
                "started_at": states[name].get("started_at"),
                "error": str(exc),
            }
        except Exception as exc:
            states[name] = {
                "state": "error",
                "started_at": states[name].get("started_at"),
                "error": f"internal: {exc}",
            }

    loop.run_in_executor(pool, _run)
    return {"state": "fetching", "started_at": states[name].get("started_at")}


# ----------------------- blob + artifact serve -----------------------


@router.get("/b/{sha}/{name:path}")
def serve_blob(request: Request, sha: str, name: str) -> FileResponse:
    """Content-addressed blob serve: ``/b/<content_sha256>/<display-name>``.

    ``name`` is display-only (so operators + logs see a recognisable
    filename); the sha is what routes the request. Any entry with a
    matching ``content_sha256`` -- multiple entries CAN share the
    same content -- serves the same bytes at the same URL. Renaming
    a catalog entry does not change its blob URL.

    Open route: iPXE targets don't carry sessions.
    """
    store = _get_store(request)
    sha = _decode_sha(sha)
    blob = store.blob_path(sha)
    if not blob.is_file():
        raise HTTPException(status_code=404, detail="blob not found")
    # Sanitise the ``name`` for Content-Disposition; iPXE doesn't
    # care, but operator curl -O should land on a reasonable filename.
    display = name.rsplit("/", 1)[-1] or f"pixie-{sha[:12]}.bin"
    return FileResponse(str(blob), filename=display)


@router.get("/artifacts/{sha}/{filename}")
def serve_artifact(request: Request, sha: str, filename: str) -> FileResponse:
    """Content-addressed netboot artifact serve. iPXE's ipxe_ramboot
    plan points at ``/artifacts/<content_sha256>/{vmlinuz,initrd}``.

    Open route + narrow allowlist for the ``filename`` segment; any
    other file name 404s so a caller can never coax a lookup outside
    ``artifact_dir/<sha>/``.
    """
    store = _get_store(request)
    sha = _decode_sha(sha)
    if filename not in _ARTIFACT_FILES:
        raise HTTPException(status_code=404, detail="not found")
    target = store.artifact_path(sha, filename)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(str(target))
