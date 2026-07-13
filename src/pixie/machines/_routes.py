"""HTTP routes for machine CRUD.

Read routes (``GET /machines``, ``GET /machines/{mac}``) are OPEN
because the machine list is what an on-call operator glances at.
Write routes require the pixie session cookie.

Discovery (creating a row on first PXE contact) lives under the
``/pxe/`` router in :mod:`pixie.pxe._routes`; this module owns only
the operator-visible administration.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from pixie.machines._store import BOOT_MODES, BadMac, MachinesStore, normalise_mac
from pixie.web._auth import require_auth

router = APIRouter()


class BindBody(BaseModel):
    """Operator binding: pick a boot mode + optional image ref by
    content sha. Empty ``image_content_sha256`` clears the binding."""

    boot_mode: str = Field(..., description=f"one of {sorted(BOOT_MODES)}")
    image_content_sha256: str = Field(
        default="",
        description="64-char lowercase hex sha of a fetched catalog entry, or empty to clear",
    )


def _get_machines(request: Request) -> MachinesStore:
    store: MachinesStore | None = getattr(request.app.state, "machines_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="machines store not initialised")
    return store


@router.get("/machines")
def list_machines(request: Request) -> dict[str, list[dict[str, Any]]]:
    return {"machines": [m.to_dict() for m in _get_machines(request).list()]}


@router.get("/machines/{mac}")
def get_machine(request: Request, mac: str) -> dict[str, Any]:
    try:
        canon = normalise_mac(mac)
    except BadMac as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = _get_machines(request).get(canon)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no machine {canon}")
    return row.to_dict()


@router.put("/machines/{mac}")
def upsert_machine(
    request: Request,
    mac: str,
    body: BindBody,
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    try:
        canon = normalise_mac(mac)
    except BadMac as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        row = _get_machines(request).upsert_binding(
            canon,
            boot_mode=body.boot_mode,
            image_content_sha256=body.image_content_sha256.strip().lower(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    log = getattr(request.app.state, "events_log", None)
    if log is not None:
        details: dict[str, Any] = {"boot_mode": row.boot_mode}
        if row.image_content_sha256:
            details["image_content_sha256"] = row.image_content_sha256
        log.emit(
            "machine.bound",
            subject_kind="machine",
            subject_id=row.mac,
            summary=f"{row.mac} -> {row.boot_mode}",
            details=details,
        )
    return row.to_dict()


@router.delete("/machines/{mac}", status_code=204)
def delete_machine(
    request: Request,
    mac: str,
    _auth: None = Depends(require_auth),
) -> Response:
    try:
        canon = normalise_mac(mac)
    except BadMac as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not _get_machines(request).delete(canon):
        raise HTTPException(status_code=404, detail=f"no machine {canon}")
    log = getattr(request.app.state, "events_log", None)
    if log is not None:
        log.emit("machine.deleted", subject_kind="machine", subject_id=canon, summary=canon)
    return Response(status_code=204)
