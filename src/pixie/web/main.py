"""FastAPI application factory.

At 0.2.0: skeleton auth + catalog router mounted. The app attaches a
:class:`pixie.catalog.CatalogStore` and a fetch thread pool onto
``app.state`` so the routes can find them via a request-scoped dep.

Run locally with:

    uv run uvicorn pixie.web.main:app --reload

State + fetch-pool configuration falls back to sane defaults, so a
plain ``uv run pytest`` never needs env-vars set. Overrides:

* ``PIXIE_DATA_DIR``   - state dir (default: /var/lib/pixie).
* ``PIXIE_FETCH_POOL_SIZE`` - concurrent fetches (default: 4).
"""

from __future__ import annotations

import os
import secrets
import tempfile
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from contextlib import suppress as contextlib_suppress
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import pixie
from pixie.catalog._routes import router as catalog_router
from pixie.catalog._store import CatalogStore
from pixie.events import EventsLog
from pixie.events._routes import router as events_router
from pixie.exports._routes import router as exports_router
from pixie.exports._store import ExportsStore
from pixie.exports._supervisor import DEFAULT_PORT_BASE, NbdServer
from pixie.machines._routes import router as machines_router
from pixie.machines._store import MachinesStore
from pixie.pxe._renderer import PlanRenderer
from pixie.pxe._routes import router as pxe_router
from pixie.tftp import DEFAULT_TFTP_ROOT, TftpServer
from pixie.web._auth import (
    SESSION_AUTHED_KEY,
    SESSION_COOKIE,
    check_password,
    require_auth,
    using_default_password,
)
from pixie.web._table_state import DEFAULT_PER_PAGE

_HERE = Path(__file__).resolve().parent
_TEMPLATES_DIR = _HERE / "_templates"
_STATIC_DIR = _HERE / "_static"

DEFAULT_STATE_DIR = Path("/var/lib/pixie")
STATE_DIR_ENV = "PIXIE_DATA_DIR"
LIVE_ENV_DIR_ENV = "PIXIE_LIVE_ENV_DIR"
FETCH_POOL_SIZE_ENV = "PIXIE_FETCH_POOL_SIZE"
DEFAULT_FETCH_POOL_SIZE = 4

# NBD supervisor knobs. In production ``--network=host`` covers the
# bind + port range; in dev / tests operators tweak.
NBD_PORT_BASE_ENV = "PIXIE_NBD_PORT_BASE"
NBD_BIND_ENV = "PIXIE_NBD_BIND"
DEFAULT_NBD_BIND = "0.0.0.0"
NBDKIT_BIN_ENV = "PIXIE_NBDKIT_BIN"
DEFAULT_NBDKIT_BIN = "nbdkit"

# TFTP subprocess supervision. OFF by default so unit-test / dev
# runs don't try to bind udp/69 (root-required); flipped on inside
# the container image by the compose file's default env.
TFTP_ENABLED_ENV = "PIXIE_TFTP_ENABLED"
TFTP_BIND_ENV = "PIXIE_TFTP_BIND"
TFTP_PORT_ENV = "PIXIE_TFTP_PORT"
TFTP_ROOT_ENV = "PIXIE_TFTP_ROOT"
TFTP_BIN_ENV = "PIXIE_TFTP_BIN"
DEFAULT_TFTP_BIND = "0.0.0.0"
DEFAULT_TFTP_PORT = 69
DEFAULT_TFTP_BIN = "in.tftpd"


def _resolve_nbd_port_base() -> int:
    raw = (os.environ.get(NBD_PORT_BASE_ENV) or "").strip()
    if not raw:
        return DEFAULT_PORT_BASE
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_PORT_BASE


def _resolve_nbd_bind() -> str:
    return (os.environ.get(NBD_BIND_ENV) or "").strip() or DEFAULT_NBD_BIND


def _resolve_nbdkit_bin() -> str:
    return (os.environ.get(NBDKIT_BIN_ENV) or "").strip() or DEFAULT_NBDKIT_BIN


def _tftp_enabled() -> bool:
    return (os.environ.get(TFTP_ENABLED_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def _resolve_tftp_bind() -> str:
    return (os.environ.get(TFTP_BIND_ENV) or "").strip() or DEFAULT_TFTP_BIND


def _resolve_tftp_port() -> int:
    raw = (os.environ.get(TFTP_PORT_ENV) or "").strip()
    if not raw:
        return DEFAULT_TFTP_PORT
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_TFTP_PORT


def _resolve_tftp_root() -> Path:
    override = (os.environ.get(TFTP_ROOT_ENV) or "").strip()
    return Path(override) if override else DEFAULT_TFTP_ROOT


def _resolve_tftp_bin() -> str:
    return (os.environ.get(TFTP_BIN_ENV) or "").strip() or DEFAULT_TFTP_BIN


def _resolve_live_env_dir() -> Path | None:
    """Directory holding the pixie netboot-pc bake artifacts
    (vmlinuz + initrd + squashfs). Default: ``<state_dir>/live-env``.
    Explicit override via ``PIXIE_LIVE_ENV_DIR``. Returns None when
    the resolved path does not exist, so the renderer's
    ``_live_env_ready()`` cleanly says "no" without the operator
    having to set the env var to a special sentinel."""
    override = (os.environ.get(LIVE_ENV_DIR_ENV) or "").strip()
    if override:
        return Path(override)
    # Default sits under the state dir so an operator dropping
    # artifacts into ``/opt/pixie/data/live-env/`` on a compose
    # deploy is the whole install step.
    default = _resolve_state_dir() / "live-env"
    return default


def _resolve_state_dir() -> Path:
    """Where pixie writes state.db + blobs/ + artifacts/.

    Falls back to a per-invocation tempdir when the default
    ``/var/lib/pixie`` is not writable (test environments), so tests
    don't have to set env-vars to construct the app.
    """
    override = os.environ.get(STATE_DIR_ENV, "").strip()
    if override:
        return Path(override)
    if os.access(str(DEFAULT_STATE_DIR.parent), os.W_OK):
        DEFAULT_STATE_DIR.mkdir(parents=True, exist_ok=True)
        return DEFAULT_STATE_DIR
    return Path(tempfile.mkdtemp(prefix="pixie-state-"))


def _resolve_fetch_pool_size() -> int:
    raw = (os.environ.get(FETCH_POOL_SIZE_ENV) or "").strip()
    if not raw:
        return DEFAULT_FETCH_POOL_SIZE
    try:
        n = int(raw)
    except ValueError:
        return DEFAULT_FETCH_POOL_SIZE
    return max(1, n)


class NotAuthenticated(Exception):
    """Raised by the UI dependency when a browser request lacks the
    session cookie. The exception handler redirects to /ui/login."""


def _respawn_exports_at_startup(app: FastAPI) -> None:
    """Walk ``exports_store.list()`` and spawn nbdkit for each row
    whose catalog blob still exists on disk. Called once from the
    lifespan startup path so an operator who bounces the container
    (rebuild + recreate) does not come back to a wall of
    ``status=error nbdkit exited`` rows in the /ui/exports table.

    Runs BEFORE ``yield`` so the first ``GET /pxe/<mac>`` served
    after startup sees the exports as ``running`` again. Errors on
    an individual export (missing blob, nbdkit refuses to spawn)
    update that row's ``status`` + ``error`` but do NOT abort
    startup; the other exports still respawn.
    """
    import logging as _logging

    log = _logging.getLogger(__name__)
    catalog = app.state.catalog_store
    exports_store = app.state.exports_store
    nbd = app.state.nbd_server
    for export in exports_store.list():
        blob_path = catalog.blob_path(export.content_sha256)
        if not blob_path.exists():
            exports_store.update_runtime(
                export.name,
                nbd_port=0,
                status="error",
                error=f"blob missing at {blob_path}",
            )
            log.warning(
                "export %s: blob missing at %s; leaving row in error state",
                export.name,
                blob_path,
            )
            continue
        try:
            port = nbd.spawn(export.name, blob_path)
        except RuntimeError as exc:
            exports_store.update_runtime(
                export.name,
                nbd_port=0,
                status="error",
                error=f"respawn failed: {exc}",
            )
            log.warning("export %s: respawn failed: %s", export.name, exc)
            continue
        exports_store.update_runtime(export.name, nbd_port=port, status="running", error="")
        log.info("export %s: respawned on port %d", export.name, port)


def _require_ui_auth(request: Request) -> None:
    if not request.session.get(SESSION_AUTHED_KEY):
        raise NotAuthenticated


def create_app() -> FastAPI:
    """Build the FastAPI app. Factory shape so tests can construct a
    fresh app per fixture without global state."""

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        # startup: state already attached below; kick off TFTP if
        # enabled (bind udp/69 requires root; disabled in unit / dev).
        if getattr(app.state, "tftp_server", None) is not None:
            try:
                app.state.tftp_server.start()
            except RuntimeError as exc:
                import logging as _logging

                _logging.getLogger(__name__).warning("tftp start failed: %s", exc)
        # Re-spawn nbdkit for every stored export whose catalog blob
        # still exists. A container recreate takes down nbdkit
        # subprocesses (they are children of the previous uvicorn),
        # but the export rows in state.db persist. Without this
        # startup pass the rows come back as ``status=error``
        # ``nbdkit exited`` forever until an operator deletes + POSTs
        # them again. Idempotent per name; harmless on cold boot
        # (empty export list -> no-op).
        _respawn_exports_at_startup(app)
        try:
            yield
        finally:
            # Stop children on graceful shutdown so a Ctrl-C +
            # restart doesn't leave orphan processes clinging to
            # NBD ports or udp/69.
            with contextlib_suppress(Exception):
                app.state.nbd_server.stop()
            if getattr(app.state, "tftp_server", None) is not None:
                with contextlib_suppress(Exception):
                    app.state.tftp_server.stop()

    app = FastAPI(
        title="pixie",
        version=pixie.__version__,
        description="Bare-metal netboot appliance.",
        lifespan=_lifespan,
    )

    # State on app.state. Route handlers read via
    # ``request.app.state.<name>``.
    state_dir = _resolve_state_dir()
    app.state.catalog_store = CatalogStore(state_dir)
    app.state.exports_store = ExportsStore(app.state.catalog_store.db_path)
    app.state.machines_store = MachinesStore(app.state.catalog_store.db_path)
    app.state.events_log = EventsLog(app.state.catalog_store.db_path)
    app.state.nbd_server = NbdServer(
        port_base=_resolve_nbd_port_base(),
        bind=_resolve_nbd_bind(),
        nbdkit_bin=_resolve_nbdkit_bin(),
    )
    app.state.live_env_dir = _resolve_live_env_dir()
    app.state.pxe_renderer = PlanRenderer(
        catalog=app.state.catalog_store,
        exports=app.state.exports_store,
        nbd=app.state.nbd_server,
        live_env_dir=app.state.live_env_dir,
    )
    app.state.tftp_server = (
        TftpServer(
            bind=_resolve_tftp_bind(),
            port=_resolve_tftp_port(),
            root=_resolve_tftp_root(),
            bin=_resolve_tftp_bin(),
        )
        if _tftp_enabled()
        else None
    )
    app.state.fetch_pool = ThreadPoolExecutor(
        max_workers=_resolve_fetch_pool_size(),
        thread_name_prefix="pixie-fetch",
    )
    app.state.fetch_states = {}

    # SessionMiddleware signs the ``pixie-token`` cookie. Sliding TTL:
    # 7 days from last touch. ``https_only=False`` because pixie is
    # LAN-only by design; operators front with TLS if they want it.
    app.add_middleware(
        SessionMiddleware,
        secret_key=secrets.token_urlsafe(32),
        session_cookie=SESSION_COOKIE,
        max_age=60 * 60 * 24 * 7,
        same_site="strict",
        https_only=False,
    )

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    # Register the table-helpers query-string builder as a Jinja
    # global so the pagination + search macros can compose links
    # without every template having to reach into ``request.url``.
    from pixie.web._table_state import build_query_string as _qs_helper

    templates.env.globals["_qs"] = _qs_helper
    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    # Serve the netboot-pc bake artifacts (vmlinuz + initrd +
    # squashfs) under ``/boot/pixie-live-env/`` so the live-env iPXE
    # chain can fetch them. Only mounted when the directory exists;
    # an operator dropping the three files in without a pixie
    # restart is picked up by the renderer's ``_live_env_ready()``
    # check per-render.
    if app.state.live_env_dir and app.state.live_env_dir.is_dir():
        app.mount(
            "/boot/pixie-live-env",
            StaticFiles(directory=str(app.state.live_env_dir)),
            name="live-env",
        )

    # ---------- exception handlers -----------------------------------

    @app.exception_handler(NotAuthenticated)
    async def _redirect_to_login(_request: Request, _exc: NotAuthenticated) -> RedirectResponse:
        return RedirectResponse(url="/ui/login", status_code=status.HTTP_303_SEE_OTHER)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": exc.errors()},
        )

    # ---------- open routes ------------------------------------------

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "service": "pixie", "version": pixie.__version__}

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/ui/", status_code=status.HTTP_303_SEE_OTHER)

    # ---------- ui: login / logout / dashboard -----------------------

    @app.get("/ui/login", response_class=HTMLResponse)
    def ui_login_form(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "version": pixie.__version__,
                "error": None,
                "authed": False,
                "page": "login",
                "using_default_password": using_default_password(),
            },
        )

    @app.post("/ui/login", response_class=HTMLResponse)
    def ui_login(request: Request, password: str = Form(...)) -> Any:
        if not check_password(password):
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "version": pixie.__version__,
                    "error": "Invalid password.",
                    "authed": False,
                    "page": "login",
                    "using_default_password": using_default_password(),
                },
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        request.session[SESSION_AUTHED_KEY] = True
        return RedirectResponse(url="/ui/", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/ui/logout")
    def ui_logout(request: Request) -> RedirectResponse:
        request.session.clear()
        return RedirectResponse(url="/ui/login", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/ui/", response_class=HTMLResponse)
    def ui_dashboard(
        request: Request,
        _auth: None = Depends(_require_ui_auth),
    ) -> HTMLResponse:
        # Landing page: summary counts + latest events. The catalog
        # moved to its own /ui/catalog route so the brand pill's
        # "Home" click lands on a page that reads as a status
        # overview rather than a management surface.
        catalog = request.app.state.catalog_store
        exports_store = request.app.state.exports_store
        machines_store = request.app.state.machines_store
        events_log = request.app.state.events_log
        nbd = request.app.state.nbd_server
        from pixie.exports._routes import _refresh_row

        entries = catalog.list_entries()
        exports = [_refresh_row(e, nbd, exports_store) for e in exports_store.list()]
        machines = machines_store.list()
        events = events_log.list(limit=10)
        # Split the catalog into disk-image entries (bindable = can be
        # served over NBD for ramboot + flashed later) and netboot
        # bundles (tar.gz of vmlinuz+initrd used as the kernel/initrd
        # side of a ramboot). ``fetched`` counts the ones whose bytes
        # are actually on disk. The operator wanted the dashboard to
        # read as ``<fetched> / <total>`` per category instead of a
        # bare single number.
        images = [e for e in entries if getattr(e, "bindable", False)]
        bundles = [e for e in entries if not getattr(e, "bindable", False)]
        stats = {
            "machines_total": len(machines),
            "machines_bound": sum(1 for m in machines if m.image_content_sha256),
            "machines_with_inventory": sum(1 for m in machines if m.inventory),
            "catalog_total": len(entries),
            "catalog_fetched": sum(1 for e in entries if getattr(e, "content_sha256", "")),
            "catalog_images_total": len(images),
            "catalog_images_fetched": sum(1 for e in images if e.content_sha256),
            "catalog_bundles_total": len(bundles),
            "catalog_bundles_fetched": sum(1 for e in bundles if e.content_sha256),
            "exports_total": len(exports),
            "exports_running": sum(1 for e in exports if e.status == "running"),
            "exports_error": sum(1 for e in exports if e.status == "error"),
        }
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "version": pixie.__version__,
                "stats": stats,
                "events": events,
                "authed": True,
                "page": "dashboard",
            },
        )

    @app.get("/ui/catalog", response_class=HTMLResponse)
    def ui_catalog(
        request: Request,
        q: str = "",
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        _auth: None = Depends(_require_ui_auth),
    ) -> HTMLResponse:
        """Catalog + exports in one view. Each disk-image entry
        carries its NBD-serving state (port + status + nbdkit error);
        netboot bundles just show their fetch state -- they are served
        as HTTP artifacts from ``/artifacts/<sha>/{vmlinuz,initrd}``
        rather than over NBD, so no port is meaningful for them."""
        from pixie.exports._routes import _refresh_row
        from pixie.web._table_state import filter_rows, parse_pagination

        catalog = request.app.state.catalog_store
        exports_store = request.app.state.exports_store
        nbd_server = request.app.state.nbd_server
        exports_by_sha: dict[str, Any] = {}
        for row in exports_store.list():
            refreshed = _refresh_row(row, nbd_server, exports_store)
            exports_by_sha[refreshed.content_sha256] = refreshed
        all_entries = catalog.list_entries()
        # Freeform text filter across the fields an operator most
        # often greps by: display name, source URL, sibling netboot
        # URL, arch, format tag, description.
        filtered = filter_rows(
            all_entries,
            q,
            fields=("name", "src", "netboot_src", "arch", "format", "description"),
        )
        page_state = parse_pagination(dict(request.query_params), total=len(filtered))
        page_entries = filtered[page_state.offset : page_state.offset + page_state.per_page]
        return templates.TemplateResponse(
            request,
            "catalog.html",
            {
                "version": pixie.__version__,
                "entries": page_entries,
                "fetch_states": request.app.state.fetch_states,
                "exports_by_sha": exports_by_sha,
                "q": q,
                "page_state": page_state,
                "total_entries": len(all_entries),
                "authed": True,
                "page": "catalog",
            },
        )

    @app.get("/ui/catalog/{name}", response_model=None)
    def ui_catalog_detail(
        request: Request,
        name: str,
        _auth: None = Depends(_require_ui_auth),
    ) -> HTMLResponse | RedirectResponse:
        """Per-entry detail: description + sibling relations. A
        disk-image entry with a ``netboot_src`` URL surfaces its
        netboot-bundle sibling (if the operator has imported it); a
        netboot-bundle entry surfaces every disk-image entry pointing
        at its ``src`` via ``netboot_src``. Falls through to
        /ui/catalog on an unknown name."""
        from pixie.exports._routes import _refresh_row

        catalog = request.app.state.catalog_store
        entry = catalog.get_entry(name)
        if entry is None:
            return RedirectResponse(url="/ui/catalog", status_code=status.HTTP_303_SEE_OTHER)
        all_entries = catalog.list_entries()
        # Forward relation: this entry's netboot_src -> sibling bundle.
        forward = None
        if entry.netboot_src:
            forward = next((e for e in all_entries if e.src == entry.netboot_src), None)
        # Reverse relation: any disk image whose netboot_src == entry.src.
        backward = [e for e in all_entries if e.netboot_src == entry.src] if entry.src else []
        # NBD export state for the entry itself (disk images only).
        exp = None
        if entry.bindable and entry.content_sha256:
            for row in request.app.state.exports_store.list():
                if row.content_sha256 == entry.content_sha256:
                    exp = _refresh_row(
                        row,
                        request.app.state.nbd_server,
                        request.app.state.exports_store,
                    )
                    break
        return templates.TemplateResponse(
            request,
            "catalog_detail.html",
            {
                "version": pixie.__version__,
                "entry": entry,
                "fetch_state": (request.app.state.fetch_states.get(entry.name) or {}),
                "netboot_sibling": forward,
                "disk_image_users": backward,
                "export": exp,
                "warn_delete": bool(request.query_params.get("warn_delete")),
                "warn_delete_blob": bool(request.query_params.get("warn_delete_blob")),
                "orphans_bundle_name": (
                    forward.name
                    if (forward is not None and entry.bindable and forward.fetched)
                    else None
                ),
                "breaks_ramboot_for": [e.name for e in backward] if not entry.bindable else [],
                "blob_using_machines": [
                    m.mac
                    for m in request.app.state.machines_store.list()
                    if entry.content_sha256 and m.image_content_sha256 == entry.content_sha256
                ],
                "blob_running_exports": [
                    e.name
                    for e in request.app.state.exports_store.list()
                    if entry.content_sha256
                    and e.content_sha256 == entry.content_sha256
                    and e.status == "running"
                ],
                "authed": True,
                "page": "catalog",
            },
        )

    @app.get("/ui/exports")
    def ui_exports_redirect() -> RedirectResponse:
        """Exports merged into the Catalog view. Keep the URL alive
        as a permanent redirect so any operator bookmarks and any
        older docs still land on the right place."""
        return RedirectResponse(url="/ui/catalog", status_code=status.HTTP_308_PERMANENT_REDIRECT)

    @app.post("/ui/exports/delete")
    def ui_exports_delete(
        request: Request,
        name: str = Form(...),
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        request.app.state.nbd_server.terminate(name)
        request.app.state.exports_store.delete(name)
        return RedirectResponse(url="/ui/catalog", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/ui/machines", response_class=HTMLResponse)
    def ui_machines(
        request: Request,
        q: str = "",
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        _auth: None = Depends(_require_ui_auth),
    ) -> HTMLResponse:
        from pixie.web._table_state import filter_rows, parse_pagination

        all_machines = request.app.state.machines_store.list()
        filtered = filter_rows(
            all_machines,
            q,
            fields=("mac", "boot_mode", "image_content_sha256", "last_seen_ip"),
        )
        page_state = parse_pagination(dict(request.query_params), total=len(filtered))
        page_machines = filtered[page_state.offset : page_state.offset + page_state.per_page]
        return templates.TemplateResponse(
            request,
            "machines.html",
            {
                "version": pixie.__version__,
                "machines": page_machines,
                "q": q,
                "page_state": page_state,
                "total_machines": len(all_machines),
                "authed": True,
                "page": "machines",
            },
        )

    @app.get("/ui/machines/{mac}", response_model=None)
    def ui_machine_detail(
        request: Request,
        mac: str,
        _auth: None = Depends(_require_ui_auth),
    ) -> HTMLResponse | RedirectResponse:
        """Per-machine detail: telemetry + boot-mode binding form +
        the inventory blob pixie stored on the row (from
        ``POST /pxe/<mac>/inventory``, driven by the live env's
        pixie CLI). Falls through to /ui/machines on a bad MAC or a
        row that doesn't exist yet."""
        from pixie.machines._store import BOOT_MODES, BadMac

        try:
            machine = request.app.state.machines_store.get(mac)
        except BadMac:
            return RedirectResponse(url="/ui/machines", status_code=status.HTTP_303_SEE_OTHER)
        if machine is None:
            return RedirectResponse(url="/ui/machines", status_code=status.HTTP_303_SEE_OTHER)
        events = request.app.state.events_log.list(
            subject_kind="machine",
            subject_id=machine.mac,
            limit=25,
        )
        # Bindable entries: fetched disk images (bindable=True on the
        # catalog schema means it has a content_sha256 an operator can
        # point a machine at). Netboot-bundle rows are excluded --
        # they are pointed at by the sibling disk-image row's
        # ``netboot_src``, not bound directly.
        bindable_entries = [
            e
            for e in request.app.state.catalog_store.list_entries()
            if getattr(e, "bindable", False)
        ]
        return templates.TemplateResponse(
            request,
            "machine_detail.html",
            {
                "version": pixie.__version__,
                "machine": machine,
                "events": events,
                "bindable_entries": bindable_entries,
                "boot_modes": sorted(BOOT_MODES),
                "authed": True,
                "page": "machines",
            },
        )

    @app.post("/ui/machines/bind")
    def ui_machines_bind(
        request: Request,
        mac: str = Form(...),
        boot_mode: str = Form(...),
        image_content_sha256: str = Form(""),
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        import contextlib as _contextlib

        from pixie.machines._store import BadMac

        # UI-side: silently redirect back on invalid input; a full
        # field-error flash chain lands in a follow-up.
        with _contextlib.suppress(BadMac, ValueError):
            request.app.state.machines_store.upsert_binding(
                mac,
                boot_mode=boot_mode,
                image_content_sha256=image_content_sha256.strip().lower(),
            )
        return RedirectResponse(url="/ui/machines", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/ui/machines/delete")
    def ui_machines_delete(
        request: Request,
        mac: str = Form(...),
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        request.app.state.machines_store.delete(mac)
        return RedirectResponse(url="/ui/machines", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/ui/events", response_class=HTMLResponse)
    def ui_events(
        request: Request,
        q: str = "",
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        _auth: None = Depends(_require_ui_auth),
    ) -> HTMLResponse:
        from pixie.web._table_state import filter_rows, parse_pagination

        # Cap the pull at 2000 so a very long events log doesn't
        # blow up in-memory filtering. Post-pagination we still
        # only render one page.
        all_events = request.app.state.events_log.list(limit=2000)
        filtered = filter_rows(
            all_events,
            q,
            fields=("kind", "subject_kind", "subject_id", "summary", "ts"),
        )
        page_state = parse_pagination(dict(request.query_params), total=len(filtered))
        page_events = filtered[page_state.offset : page_state.offset + page_state.per_page]
        return templates.TemplateResponse(
            request,
            "events.html",
            {
                "version": pixie.__version__,
                "events": page_events,
                "q": q,
                "page_state": page_state,
                "total_events": len(all_events),
                "authed": True,
                "page": "events",
            },
        )

    # ---------- ping under session-auth ------------------------------

    @app.get("/api/ping")
    def api_ping(
        _auth: None = Depends(require_auth),
    ) -> dict[str, Any]:
        return {"pong": True, "version": pixie.__version__}

    # ---------- ui: catalog admin forms ------------------------------
    #
    # These forms redirect back to /ui/ so an operator's browser stays
    # on the dashboard after each mutation. Behaviour mirrors the JSON
    # /catalog routes but with form-encoded input + 303 redirect.

    from pixie._util import now_iso as _now_iso
    from pixie.catalog._fetcher import FetchError
    from pixie.catalog._fetcher import fetch as _fetch
    from pixie.catalog._schema import CatalogEntry as _Entry
    from pixie.catalog._store import CatalogStore as _Store  # noqa: F401

    @app.post("/ui/catalog/add")
    def ui_catalog_add(
        request: Request,
        name: str = Form(...),
        src: str = Form(...),
        format: str = Form(...),
        arch: str = Form(""),
        description: str = Form(""),
        netboot_src: str = Form(""),
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        store = request.app.state.catalog_store
        if store.get_entry(name):
            # 303 back to /ui/catalog silently on conflict; UI shows
            # the row already exists.
            return RedirectResponse(url="/ui/catalog", status_code=status.HTTP_303_SEE_OTHER)
        store.upsert(
            _Entry(
                name=name.strip(),
                src=src.strip(),
                format=format.strip(),
                arch=arch.strip(),
                description=description.strip(),
                netboot_src=netboot_src.strip(),
                added_at=_now_iso(),
            )
        )
        return RedirectResponse(url="/ui/catalog", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/ui/catalog/fetch")
    def ui_catalog_fetch(
        request: Request,
        name: str = Form(...),
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        store = request.app.state.catalog_store
        entry = store.get_entry(name)
        if entry is None:
            return RedirectResponse(url="/ui/catalog", status_code=status.HTTP_303_SEE_OTHER)

        states = request.app.state.fetch_states
        if states.get(name, {}).get("state") == "fetching":
            return RedirectResponse(url="/ui/catalog", status_code=status.HTTP_303_SEE_OTHER)
        states[name] = {"state": "fetching", "started_at": _now_iso(), "error": None}

        def _run() -> None:
            try:
                _fetch(entry, store)
                states[name] = {"state": "done", "started_at": states[name].get("started_at")}
            except FetchError as exc:
                states[name] = {
                    "state": "error",
                    "started_at": states[name].get("started_at"),
                    "error": str(exc),
                }
            except Exception as exc:  # pragma: no cover -- defensive
                states[name] = {
                    "state": "error",
                    "started_at": states[name].get("started_at"),
                    "error": f"internal: {exc}",
                }

        request.app.state.fetch_pool.submit(_run)
        return RedirectResponse(url="/ui/catalog", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/ui/catalog/delete")
    def ui_catalog_delete(
        request: Request,
        name: str = Form(...),
        force: str = Form(""),
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        """Delete a catalog entry. Relation-aware: when the entry has
        a sibling that would be left dangling (a disk image that
        names an already-fetched netboot bundle, or a netboot bundle
        that is named by any disk image's ``netboot_src``), bounce
        the operator back to the entry's detail page with a
        ``warn_delete`` marker so the second click is intentional.
        A hidden ``force=1`` from that confirmation form skips the
        bounce and deletes.

        Skipping this check on the JSON API is deliberate: automation
        callers set force=1 or use the raw ``DELETE /catalog/entries``
        endpoint that never had a warning path."""
        store = request.app.state.catalog_store
        entry = store.get_entry(name)
        if entry is None:
            return RedirectResponse(url="/ui/catalog", status_code=status.HTTP_303_SEE_OTHER)
        if not force:
            # Compute the relations for this entry so we know whether
            # deletion would break someone.
            all_entries = store.list_entries()
            breaks_ramboot_for: list[str] = []
            orphans_bundle: str | None = None
            if entry.bindable:
                # Deleting a disk image orphans its sibling bundle
                # (harmless -- the bundle is still fetched -- but the
                # operator should know).
                if entry.netboot_src:
                    sibling = next(
                        (e for e in all_entries if e.src == entry.netboot_src),
                        None,
                    )
                    if sibling is not None and sibling.fetched:
                        orphans_bundle = sibling.name
            else:
                # Deleting a bundle breaks ramboot for every disk
                # image whose netboot_src pointed at it.
                if entry.src:
                    breaks_ramboot_for = [e.name for e in all_entries if e.netboot_src == entry.src]
            if breaks_ramboot_for or orphans_bundle:
                # Bounce with a marker so /ui/catalog/<name> can
                # render the warning inline.
                return RedirectResponse(
                    url=f"/ui/catalog/{name}?warn_delete=1",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
        store.delete(name)
        request.app.state.fetch_states.pop(name, None)
        return RedirectResponse(url="/ui/catalog", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/ui/catalog/delete-blob")
    def ui_catalog_delete_blob(
        request: Request,
        name: str = Form(...),
        force: str = Form(""),
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        """Delete the on-disk BYTES for an entry (blob file + artifact
        dir if any) while keeping the catalog row. The row's
        content_sha256 + size + fetched_at are cleared so Fetch runs
        the full pipeline again next time.

        Relation-aware: if any machine has ``image_content_sha256 ==
        entry.content_sha256`` (i.e. is bound to ramboot for this
        entry) OR a running NBD export serves the blob, bounce to
        the entry's detail page with ``warn_delete_blob=1`` so the
        operator can either point the machine at a different image
        or explicitly confirm the delete via a hidden ``force=1``
        input on that warning banner."""
        import shutil

        store = request.app.state.catalog_store
        entry = store.get_entry(name)
        if entry is None or not entry.content_sha256:
            return RedirectResponse(url="/ui/catalog", status_code=status.HTTP_303_SEE_OTHER)
        sha = entry.content_sha256
        if not force:
            using_machines = [
                m.mac
                for m in request.app.state.machines_store.list()
                if m.image_content_sha256 == sha
            ]
            running_exports = [
                e.name
                for e in request.app.state.exports_store.list()
                if e.content_sha256 == sha and e.status == "running"
            ]
            if using_machines or running_exports:
                return RedirectResponse(
                    url=f"/ui/catalog/{name}?warn_delete_blob=1",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
        # Force path (or unused blob): stop any nbdkit process serving
        # the blob first so the file handle drops, then remove bytes.
        for exp in request.app.state.exports_store.list():
            if exp.content_sha256 == sha:
                request.app.state.nbd_server.terminate(exp.name)
                request.app.state.exports_store.delete(exp.name)
        blob = store.blob_path(sha)
        with contextlib_suppress(FileNotFoundError, OSError):
            blob.unlink()
            # Best-effort remove the enclosing ``<sha>/`` dir when
            # empty. Content-addressed storage means other entries
            # could share the same sha; the rmdir call only succeeds
            # when we're the last reference.
            with contextlib_suppress(OSError):
                blob.parent.rmdir()
        artifact_dir = store.artifact_dir(sha)
        with contextlib_suppress(FileNotFoundError, OSError):
            shutil.rmtree(artifact_dir)
        store.mark_unfetched(name)
        request.app.state.fetch_states.pop(name, None)
        log = getattr(request.app.state, "events_log", None)
        if log is not None:
            log.emit(
                "catalog.blob.deleted",
                subject_kind="entry",
                subject_id=name,
                summary=f"blob deleted for {name} (sha {sha[:12]})",
                details={"sha": sha, "forced": bool(force)},
            )
        return RedirectResponse(url=f"/ui/catalog/{name}", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/ui/catalog/import")
    def ui_catalog_import(
        request: Request,
        url: str = Form(...),
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        """Fetch a catalog TOML from the given URL and upsert every
        entry it declares. Matches the shape bty publishes at
        ``GET /catalog.toml``: ``version = 1`` + ``[[images]]`` array
        with ``name``/``src``/``format`` required, ``arch`` +
        ``netboot_src`` + ``description`` optional. Existing rows are
        overwritten by name; unfetched rows stay unfetched (import
        stages entries only, doesn't fetch bytes)."""
        import httpx

        from pixie.catalog._schema import parse_catalog_toml

        target_url = (url or "").strip()
        if not target_url:
            return RedirectResponse(url="/ui/catalog", status_code=status.HTTP_303_SEE_OTHER)
        try:
            r = httpx.get(target_url, timeout=15.0, follow_redirects=True)
            r.raise_for_status()
            entries = parse_catalog_toml(r.content)
        except (httpx.HTTPError, ValueError, Exception) as exc:
            log = getattr(request.app.state, "events_log", None)
            if log is not None:
                log.emit(
                    "catalog.import.failed",
                    subject_kind="catalog",
                    subject_id=target_url,
                    summary=f"import from {target_url} failed",
                    details={"error": str(exc)[:200]},
                )
            return RedirectResponse(url="/ui/catalog", status_code=status.HTTP_303_SEE_OTHER)
        store = request.app.state.catalog_store
        added = 0
        for entry in entries:
            if not store.get_entry(entry.name):
                added += 1
            store.upsert(entry)
        log = getattr(request.app.state, "events_log", None)
        if log is not None:
            log.emit(
                "catalog.import.ok",
                subject_kind="catalog",
                subject_id=target_url,
                summary=f"imported {len(entries)} entries from {target_url} ({added} new)",
                details={"url": target_url, "count": len(entries), "new": added},
            )
        return RedirectResponse(url="/ui/catalog", status_code=status.HTTP_303_SEE_OTHER)

    # ---------- feature routers --------------------------------------
    #
    # Catalog + blob + artifacts routes live at the same URL shape they
    # had in the trio (``/catalog``, ``/b/``, ``/artifacts/``) so
    # operator muscle memory + iPXE templates keep working.
    app.include_router(catalog_router)
    app.include_router(exports_router)
    app.include_router(machines_router)
    app.include_router(pxe_router)
    app.include_router(events_router)

    return app


# Module-level app so ``uvicorn pixie.web.main:app`` works without a
# factory flag.
app = create_app()
