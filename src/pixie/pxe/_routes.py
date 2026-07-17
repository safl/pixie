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

import contextlib
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from pixie.events._kinds import (
    MACHINE_INVENTORY_UPDATED,
    PXE_PLAN_RENDERED,
    PXE_PLAN_UNAVAILABLE,
    PXE_STATUS_RECEIVED,
)
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
            MACHINE_INVENTORY_UPDATED,
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
    # Every plan render emits one event so an operator can grep the
    # log for "every time this MAC PXE'd + what mode it got". The
    # unavailable variant is distinguished by the renderer emitting
    # ``exit`` instead of a real chain -- the plan body carries the
    # reason as a comment, which we extract for details.
    log = getattr(request.app.state, "events_log", None)
    if log is not None:
        is_unavailable = "\nexit\n" in body and "unavailable" in body.lower()
        if is_unavailable:
            log.emit(
                PXE_PLAN_UNAVAILABLE,
                subject_kind="machine",
                subject_id=canon,
                summary=f"{canon}: boot_mode={row.boot_mode} not renderable",
                details={"boot_mode": row.boot_mode},
            )
        else:
            log.emit(
                PXE_PLAN_RENDERED,
                subject_kind="machine",
                subject_id=canon,
                summary=f"{canon}: boot_mode={row.boot_mode}",
                details={"boot_mode": row.boot_mode},
            )
    # Always ``text/plain`` per bty's convention; iPXE parses on
    # bytes, not on Content-Type.
    return PlainTextResponse(body, media_type="text/plain")


@router.post("/pxe/{mac}/status", status_code=204)
async def pxe_status(request: Request, mac: str) -> PlainTextResponse:
    """Accept a status token from the target's initrd or live env.

    Bty's ramboot dracut hook + pixie's own tui both fire tokens like
    ``ramboot.up`` / ``ramboot.nbd_connect_failed`` / ``ramboot.die``
    so an operator watching /ui/events sees the boot flow land or
    fail. The body carries either ``status=<token>`` in a form or a
    JSON object -- normalise both to a string.
    """
    try:
        canon = normalise_mac(mac)
    except BadMac as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctype = (request.headers.get("content-type") or "").lower()
    status_token = ""
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            status_token = str(payload.get("status") or "").strip()
    else:
        form = await request.form()
        raw = form.get("status")
        if isinstance(raw, str):
            status_token = raw.strip()
    if not status_token:
        # Fall back to a raw body read for the wget-style
        # ``--post-data=status=X`` shape busybox uses in the ramboot
        # initrd (application/x-www-form-urlencoded handled above,
        # but old busybox drops the content-type header).
        raw_body = (await request.body()).decode("utf-8", errors="replace").strip()
        if raw_body.startswith("status="):
            status_token = raw_body.split("=", 1)[1]
    if not status_token:
        raise HTTPException(status_code=400, detail="missing status token")
    log = getattr(request.app.state, "events_log", None)
    if log is not None:
        log.emit(
            PXE_STATUS_RECEIVED,
            subject_kind="machine",
            subject_id=canon,
            summary=f"{canon}: {status_token}",
            details={"status": status_token},
        )
    # pixie-flash-once completes -> flip to ipxe-exit so the next PXE
    # boot loads the disk. pixie-flash-always keeps re-arming; the
    # operator explicitly picked "flash every boot". Any other status
    # (started, failed, etc.) or boot_mode is a pure event emit.
    if status_token == "done":
        machines = _get_machines(request)
        row = machines.get(canon)
        if row is not None and row.boot_mode == "pixie-flash-once":
            # Guard would trip if inventory is missing on the row
            # (shouldn't happen; getting here required a flash-once
            # bind which itself passed the guard). Swallow so the
            # /done POST still returns 204.
            with contextlib.suppress(ValueError):
                machines.upsert_binding(
                    canon,
                    boot_mode="ipxe-exit",
                    image_content_sha256=row.image_content_sha256,
                    labels=list(row.labels),
                    sanboot_drive=row.sanboot_drive,
                    target_disk_serial=row.target_disk_serial,
                )
    return PlainTextResponse("", status_code=204)


@router.get("/pxe/{mac}/plan")
def pxe_plan_json(request: Request, mac: str) -> dict[str, Any]:
    """JSON plan the LIVE-ENV pixie CLI GETs after boot.

    Distinct from ``GET /pxe/{mac}`` (which returns iPXE): once the
    live env has booted and the pixie CLI is up on tty1, it GETs
    THIS endpoint to figure out what to do:

      ``mode=exit``        -> nothing to do here, exit cleanly.
      ``mode=inventory``   -> POST /pxe/{mac}/inventory + reboot.
      ``mode=interactive`` -> drop the operator into the wizard.
      ``mode=flash``       -> auto-flash the bound image (requires
                              ``image_content_sha256`` + a target
                              disk on the machine row).

    The mapping mirrors bty's shape so the ported pixie CLI needs
    zero cmdline changes. For the minimal live-env slice landing
    here we only surface ``inventory`` + ``exit`` -- the flash path
    lands with a follow-up PR that adds the target-disk fields on
    the machine row + the ``POST /pxe/{mac}/done`` endpoint that
    flips ``pixie-flash-once`` observers to see the machine as
    flashed."""
    try:
        canon = normalise_mac(mac)
    except BadMac as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    machines = _get_machines(request)
    row = machines.get(canon)
    if row is None:
        # A GET /plan without a prior discovery hit is unusual (the
        # target would already have iPXE'd via /pxe/{mac}) but
        # possible during testing. Fall through to ``exit``: the CLI
        # will POST inventory anyway (that path does not touch the
        # machine row).
        return {"mode": "exit"}

    mode = row.boot_mode
    if mode == "pixie-inventory":
        return {"mode": "inventory"}
    if mode == "pixie-tui":
        return {"mode": "interactive"}
    if mode in ("pixie-flash-once", "pixie-flash-always"):
        # Resolve the bound catalog entry so the live env can fetch
        # the bytes + know the format. Bind-time validation (see
        # machines._store.upsert_binding) guarantees both fields are
        # non-empty for a flash mode; if either goes missing between
        # bind and here (schema drift, out-of-band DB edit), fall
        # back to the interactive wizard so the operator can pick.
        if not (row.image_content_sha256 and row.target_disk_serial):
            return {"mode": "interactive"}
        catalog = request.app.state.catalog_store
        entry = None
        for e in catalog.list_entries():
            if e.content_sha256 == row.image_content_sha256:
                entry = e
                break
        if entry is None:
            return {"mode": "interactive"}
        ctx = _render_context(request)
        # URL-quote the entry name for the image URL path segment:
        # nosi's operator-facing names contain spaces + parens
        # ("nosi debian-13-headless (x86_64, 2026.W29)") and pushing
        # them through the client's ``urllib.request.urlopen`` on
        # the live env drops the path from the URL entirely (parser
        # rejects raw whitespace in a URL). The blob route serves
        # by sha only, so the display name is decorative; encode it
        # for safe transport, leave the top-level ``name`` field
        # decoded for operator-readable logging on the client side.
        import urllib.parse as _urlparse

        image_url = (
            f"http://{ctx.host}:{ctx.port}"
            f"/b/{row.image_content_sha256}/{_urlparse.quote(entry.name, safe='')}"
        )
        # ``entry.format`` in the catalog is the ORIGINAL upstream
        # format (``img.gz`` / ``img.zst`` / ``img.xz`` / ``img``).
        # Pixie's fetcher decompresses ``img.gz`` / ``img.zst`` /
        # ``img.xz`` at fetch time and stores the DECOMPRESSED bytes
        # in the blob (see catalog._fetcher._COMPRESSED_IMG_FORMATS
        # + _decompress_to_tmpfile). The blob route serves those
        # decompressed bytes verbatim, so the flash pipeline in the
        # live env must NOT try to gunzip/unzstd/unxz the stream --
        # the bytes on the wire are already raw ``img``.
        #
        # Advertise ``format=img`` for any compressed variant whose
        # bytes are pre-decompressed on disk. Leaves plain ``img``
        # + ``tar.gz`` (bundle) untouched. Without this, a
        # ``format=img.gz`` payload from an entry we already
        # decompressed sends the CLI into gunzip-on-raw-bytes and
        # the flash never completes.
        _COMPRESSED_IMG_FORMATS = {"img.gz", "img.zst", "img.xz"}
        plan_format = "img" if entry.format in _COMPRESSED_IMG_FORMATS else entry.format
        plan: dict[str, Any] = {
            "mode": "flash",
            "image": image_url,
            "target_disk_serial": row.target_disk_serial,
            "name": entry.name,
            "disk_image_sha": row.image_content_sha256,
        }
        if plan_format:
            plan["format"] = plan_format
        return plan
    if mode == "ramboot":
        # ramboot targets normally boot the image's own kernel +
        # initrd -- no pixie CLI in the picture -- so this branch
        # only fires under the ramboot chain test, which pivots
        # through the pixie live env as a stand-in. Return
        # ``interactive`` (not ``exit``): ``exit`` triggers a
        # ``sys.exit(0)`` inside the CLI that races the
        # daemon-thread inventory post, so an ``exit`` here
        # occasionally kills the CLI before inventory reaches
        # pixie. ``interactive`` keeps the CLI up (wizard on
        # tty1) which is harmless in the test and never runs
        # on a real ramboot boot.
        return {"mode": "interactive"}
    # ipxe-exit / unknown -> nothing to do from the live env's side.
    return {"mode": "exit"}
