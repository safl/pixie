"""PXE + boot flow routes.

* ``GET /pxe-bootstrap.ipxe``: iPXE bootstrap that chain-loads the
  per-MAC plan. Fetched over HTTP (or served from TFTP after an
  external TFTP daemon has picked it up; pixie's in-process TFTP
  lands in a later PR).
* ``GET /pxe/{mac}``: the per-machine plan. Discovery-side write:
  every hit upserts the row (creating on first contact) + refreshes
  ``last_seen_at``. Then renders the plan through
  :class:`pixie.pxe._renderer.PlanRenderer`.
"""

from __future__ import annotations

import logging

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
