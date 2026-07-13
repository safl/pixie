"""HTTP routes for the exports surface.

Read routes (``GET /exports``, ``GET /export/{name}``) are OPEN so
in-network monitoring can poll without a session. Write routes
(``POST /exports``, ``DELETE /exports/{name}``) require the pixie
session cookie.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from pixie.catalog._store import CatalogStore
from pixie.exports._store import Export, ExportsStore
from pixie.exports._supervisor import NbdServer
from pixie.web._auth import require_auth

_log = logging.getLogger(__name__)

# Same allowlist as nbdmux 0.9.2; the export name lands in nbdkit's
# ``-e <name>`` argv and (indirectly) in operator-facing URLs.
_EXPORT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class RegisterBody(BaseModel):
    """Register an export against an already-fetched catalog entry.

    The ``content_sha256`` refers to the catalog entry's fetched
    content sha (which the catalog list surfaces once the entry has
    been Fetched). The name is a short operator-typable identifier
    used both as the nbdkit ``-e`` param and as the URL segment.
    """

    name: str = Field(..., min_length=1, max_length=64)
    content_sha256: str = Field(..., min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")


router = APIRouter()


def _get_exports(request: Request) -> ExportsStore:
    store: ExportsStore | None = getattr(request.app.state, "exports_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="exports store not initialised")
    return store


def _get_catalog(request: Request) -> CatalogStore:
    store: CatalogStore | None = getattr(request.app.state, "catalog_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="catalog store not initialised")
    return store


def _get_nbd(request: Request) -> NbdServer:
    nbd: NbdServer | None = getattr(request.app.state, "nbd_server", None)
    if nbd is None:
        raise HTTPException(status_code=503, detail="nbd supervisor not initialised")
    return nbd


def _refresh_row(export: Export, nbd: NbdServer, exports: ExportsStore) -> Export:
    """Merge live supervisor state into a stored row before returning
    it to the operator. Doesn't persist the port -- we do that from
    the spawn path -- but keeps ``GET /exports`` honest when nbdkit
    has died out of band."""
    port = nbd.port_for(export.name)
    if port is None and export.status == "running":
        # Supervisor lost track: row still says running but no proc.
        exports.update_runtime(export.name, nbd_port=0, status="error", error="nbdkit exited")
        export.nbd_port = 0
        export.status = "error"
        export.error = "nbdkit exited"
    elif port is not None and export.nbd_port != port:
        exports.update_runtime(export.name, nbd_port=port, status="running", error="")
        export.nbd_port = port
        export.status = "running"
        export.error = ""
    return export


@router.get("/exports")
def list_exports(request: Request) -> dict[str, list[dict[str, Any]]]:
    exports = _get_exports(request)
    nbd = _get_nbd(request)
    rows = [_refresh_row(e, nbd, exports).to_dict() for e in exports.list()]
    return {"exports": rows}


@router.get("/exports/{name}")
def get_export(request: Request, name: str) -> dict[str, Any]:
    exports = _get_exports(request)
    row = exports.get(name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no export named {name!r}")
    nbd = _get_nbd(request)
    return _refresh_row(row, nbd, exports).to_dict()


@router.post("/exports", status_code=201)
def register_export(
    request: Request,
    body: RegisterBody,
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    """Register + spawn nbdkit for the given ``content_sha256``.

    Fails with 400 if the catalog blob is missing (operator hit Register
    before Fetch), 409 if the name is already taken, 500 with
    ``last_error`` set on the row if nbdkit refused to start.
    """
    if not _EXPORT_NAME_RE.match(body.name):
        raise HTTPException(
            status_code=400,
            detail="name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}",
        )
    exports = _get_exports(request)
    catalog = _get_catalog(request)
    nbd = _get_nbd(request)

    if exports.get(body.name) is not None:
        raise HTTPException(status_code=409, detail=f"export {body.name!r} already exists")

    blob_path = catalog.blob_path(body.content_sha256)
    if not blob_path.is_file():
        raise HTTPException(
            status_code=400,
            detail=(
                f"no blob on disk for content_sha256={body.content_sha256[:12]!r}; "
                "Fetch the catalog entry first"
            ),
        )

    export = Export(name=body.name, content_sha256=body.content_sha256)
    exports.upsert(export)

    try:
        port = nbd.spawn(body.name, blob_path)
    except RuntimeError as exc:
        exports.update_runtime(body.name, nbd_port=0, status="error", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    exports.update_runtime(body.name, nbd_port=port, status="running", error="")
    log = getattr(request.app.state, "events_log", None)
    if log is not None:
        log.emit(
            "export.registered",
            subject_kind="export",
            subject_id=body.name,
            summary=f"{body.name} on port {port}",
            details={"content_sha256": body.content_sha256, "nbd_port": port},
        )
    row = exports.get(body.name)
    assert row is not None
    return row.to_dict()


@router.delete("/exports/{name}", status_code=204)
def delete_export(
    request: Request,
    name: str,
    _auth: None = Depends(require_auth),
) -> Response:
    exports = _get_exports(request)
    nbd = _get_nbd(request)
    if exports.get(name) is None:
        raise HTTPException(status_code=404, detail=f"no export named {name!r}")
    nbd.terminate(name)
    exports.delete(name)
    log = getattr(request.app.state, "events_log", None)
    if log is not None:
        log.emit("export.deleted", subject_kind="export", subject_id=name, summary=name)
    return Response(status_code=204)
