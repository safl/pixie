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

from pixie.events._kinds import MACHINE_BINDING_CHANGED, MACHINE_BOUND, MACHINE_DELETED
from pixie.machines._store import (
    BOOT_MODES,
    DEFAULT_BOOT_MODE,
    BadMac,
    MachinesStore,
    normalise_mac,
    parse_labels,
)
from pixie.web._auth import require_auth

router = APIRouter()


class BindBody(BaseModel):
    """Operator binding: pick a boot mode + optional image ref by
    content sha. Empty ``image_content_sha256`` clears the binding.

    Extended fields (``labels`` / ``target_disk_serial``) let the
    operator tag the machine + tune the flash chain without a second
    round-trip. Fields default to no-op values so a pre-extension
    client can PUT with only ``boot_mode`` and get the same behaviour
    it did before.
    """

    boot_mode: str = Field(..., description=f"one of {sorted(BOOT_MODES)}")
    image_content_sha256: str = Field(
        default="",
        description="64-char lowercase hex sha of a fetched catalog entry, or empty to clear",
    )
    labels: list[str] = Field(
        default_factory=list,
        description=(
            "Free-form tags: alphanumeric-leading, a-z / 0-9 / space / ._-, "
            "max 64 chars each, max 16 tags."
        ),
    )
    target_disk_serial: str = Field(
        default="",
        description="Serial of the target disk for pixie-flash-*. Matched against the inventory.",
    )
    extra_cmdline: str = Field(
        default="",
        description=(
            "Kernel-cmdline tokens appended per-machine to the "
            "pixie-live-env + nbdboot chains. Blank means fall back to "
            "the global PIXIE_LIVE_ENV_EXTRA_CMDLINE. Single line."
        ),
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
    # Snapshot the pre-bind state so we can distinguish a fresh
    # bind (no row existed, or existed but had no binding) from a
    # binding CHANGE on an already-bound row. Two event kinds so an
    # operator can filter for "first bind ever for this MAC" vs
    # "someone swapped the image behind this MAC".
    previous = _get_machines(request).get(canon)
    try:
        # ``labels`` in the JSON body is already a list; run it through
        # ``parse_labels`` (via a comma-join) so the same validator
        # rejects bogus tokens on both the JSON + form paths. Labels
        # ride the bind body for JSON-API convenience but the bind
        # form no longer offers them -- labels are edited on their
        # own row on the machine detail page.
        labels = parse_labels(", ".join(str(x) for x in (body.labels or [])))
        # ``labels`` on ``BindBody`` defaults to an empty list. Passing
        # that empty list to ``upsert_binding`` would clobber any
        # existing labels on a bind that only wanted to touch the
        # boot mode. Preserve the previous labels when the caller did
        # not supply any new ones -- honours a partial-update PUT
        # without needing PATCH.
        labels_arg = labels if labels else (list(previous.labels) if previous else [])
        row = _get_machines(request).upsert_binding(
            canon,
            boot_mode=body.boot_mode,
            image_content_sha256=body.image_content_sha256.strip().lower(),
            labels=labels_arg,
            target_disk_serial=body.target_disk_serial,
            extra_cmdline=body.extra_cmdline,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    log = getattr(request.app.state, "events_log", None)
    if log is not None:
        details: dict[str, Any] = {"boot_mode": row.boot_mode}
        if row.image_content_sha256:
            details["image_content_sha256"] = row.image_content_sha256
        # ``machine.bound`` on a fresh MAC or on a row that was
        # discovered-only (previous.boot_mode default with no bound
        # image); ``machine.binding.changed`` when the mode or image
        # actually shifted between the previous state and the new one.
        was_bound = previous is not None and (
            bool(previous.image_content_sha256) or previous.boot_mode != DEFAULT_BOOT_MODE
        )
        changed = previous is not None and (
            previous.boot_mode != row.boot_mode
            or previous.image_content_sha256 != row.image_content_sha256
        )
        if previous is not None and was_bound and changed:
            details["previous_boot_mode"] = previous.boot_mode
            if previous.image_content_sha256:
                details["previous_image_content_sha256"] = previous.image_content_sha256
            log.emit(
                MACHINE_BINDING_CHANGED,
                subject_kind="machine",
                subject_id=row.mac,
                summary=f"{row.mac}: {previous.boot_mode} -> {row.boot_mode}",
                details=details,
            )
        else:
            log.emit(
                MACHINE_BOUND,
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
        log.emit(MACHINE_DELETED, subject_kind="machine", subject_id=canon, summary=canon)
    return Response(status_code=204)
