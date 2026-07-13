"""PXE + boot flow routes.

* ``GET /pxe-bootstrap.ipxe``: iPXE bootstrap that chain-loads the
  per-MAC plan. Fetched over HTTP.
* ``GET /pxe/{mac}``: per-machine iPXE plan. Discovery-side write:
  every hit upserts the row (creating on first contact) + refreshes
  ``last_seen_at``.
* ``POST /pxe/{mac}/inventory``: accepts a JSON body (``{"lshw":
  ..., "disks": [...]}``) from a live-env target that has just
  collected its hardware inventory. Stores the blob on the machine
  row; ``/ui/machines`` renders it.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from pixie.machines._store import BadMac, MachinesStore, normalise_mac
from pixie.pxe._renderer import PlanRenderer, RenderContext

_log = logging.getLogger(__name__)

router = APIRouter()


def _client_ip(request: Request) -> str:
    """Best-effort client IP for ``last_seen_ip``. Trusts
    ``X-Forwarded-For`` only when the caller is inside pixie's own
    process (uvicorn), which is fine for LAN + is what bty did too.
    """
    return (request.client.host if request.client else "") or ""


def _get_machines(request: Request) -> MachinesStore:
    store: MachinesStore | None = getattr(request.app.state, "machines_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="machines store not initialised")
    return store


def _get_renderer(request: Request) -> PlanRenderer:
    r: PlanRenderer | None = getattr(request.app.state, "pxe_renderer", None)
    if r is None:
        raise HTTPException(status_code=503, detail="pxe renderer not initialised")
    return r


def _render_context(request: Request) -> RenderContext:
    """Derive the base URL + NBD-facing host from the incoming request.

    LAN-only trust model: whatever host the target used to reach us
    is the host we tell it to keep using. Operators who front pixie
    behind a proxy set ``PIXIE_PUBLIC_HOST`` to override.
    """
    import os

    override = (os.environ.get("PIXIE_PUBLIC_HOST") or "").strip()
    # ``request.url.hostname`` is what iPXE resolved -- either the LAN
    # IP or a hostname the operator configured DHCP to hand out. Falls
    # back to 127.0.0.1 for uvicorn edge cases.
    host = override or (request.url.hostname or "127.0.0.1")
    port = request.url.port or 8080
    nbd_host = (os.environ.get("PIXIE_NBD_PUBLIC_HOST") or "").strip() or host
    return RenderContext(host=host, port=port, nbd_host=nbd_host)


@router.get("/pxe-bootstrap.ipxe", response_class=PlainTextResponse)
def pxe_bootstrap(request: Request) -> PlainTextResponse:
    """iPXE fetches this from the TFTP or HTTP chain to reach the
    per-MAC plan. Deliberately independent of any machine row so a
    new-to-pixie target never 404s on its first hop."""
    ctx = _render_context(request)
    body = _get_renderer(request).render_bootstrap(ctx)
    return PlainTextResponse(body, media_type="text/plain")


@router.post("/pxe/{mac}/inventory", status_code=204)
async def pxe_inventory(request: Request, mac: str) -> PlainTextResponse:
    """Accept an inventory JSON body from a live-env target.

    The body shape is what bty-tui (and now pixie-tui) POST after
    ``lshw -json`` + ``lsblk``: ``{"lshw": <object|list>, "disks":
    [...]}``. We store the whole thing verbatim; the /ui/machines
    page renders selected fields, the JSON API returns it as-is."""
    try:
        canon = normalise_mac(mac)
    except BadMac as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"body must be JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    machines = _get_machines(request)
    machines.set_inventory(canon, payload)

    log = getattr(request.app.state, "events_log", None)
    if log is not None:
        details: dict[str, Any] = {}
        disks = payload.get("disks")
        if isinstance(disks, list):
            details["disks_count"] = len(disks)
        details["has_lshw"] = payload.get("lshw") is not None
        log.emit(
            "machine.inventory.updated",
            subject_kind="machine",
            subject_id=canon,
            summary=f"{canon} posted inventory",
            details=details,
        )
    return PlainTextResponse("", status_code=204)


@router.get("/machines/{mac}/inventory")
def get_inventory(request: Request, mac: str) -> dict[str, Any]:
    """Return the stored inventory for a machine, or 404 if none has
    been posted yet."""
    try:
        canon = normalise_mac(mac)
    except BadMac as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = _get_machines(request).get(canon)
    if row is None or not row.inventory:
        raise HTTPException(status_code=404, detail=f"no inventory for {canon}")
    return {
        "mac": row.mac,
        "inventory_at": row.inventory_at,
        "inventory": row.inventory,
    }


@router.get("/pxe/{mac}", response_class=PlainTextResponse)
def pxe_plan(request: Request, mac: str) -> PlainTextResponse:
    """Discovery + plan render. Every hit upserts the machine (creates
    on first contact) and refreshes ``last_seen_at``; then the plan
    is rendered per the current boot_mode."""
    try:
        canon = normalise_mac(mac)
    except BadMac as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    machines = _get_machines(request)
    row = machines.touch_seen(canon, ip=_client_ip(request))

    ctx = _render_context(request)
    body = _get_renderer(request).render(row, ctx)
    # Always ``text/plain`` per bty's convention; iPXE parses on
    # bytes, not on Content-Type.
    return PlainTextResponse(body, media_type="text/plain")
