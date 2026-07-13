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
from pixie.exports._routes import router as exports_router
from pixie.exports._store import ExportsStore
from pixie.exports._supervisor import DEFAULT_PORT_BASE, NbdServer
from pixie.web._auth import (
    SESSION_AUTHED_KEY,
    SESSION_COOKIE,
    check_password,
    require_auth,
)

_HERE = Path(__file__).resolve().parent
_TEMPLATES_DIR = _HERE / "_templates"
_STATIC_DIR = _HERE / "_static"

DEFAULT_STATE_DIR = Path("/var/lib/pixie")
STATE_DIR_ENV = "PIXIE_DATA_DIR"
FETCH_POOL_SIZE_ENV = "PIXIE_FETCH_POOL_SIZE"
DEFAULT_FETCH_POOL_SIZE = 4

# NBD supervisor knobs. In production ``--network=host`` covers the
# bind + port range; in dev / tests operators tweak.
NBD_PORT_BASE_ENV = "PIXIE_NBD_PORT_BASE"
NBD_BIND_ENV = "PIXIE_NBD_BIND"
DEFAULT_NBD_BIND = "0.0.0.0"
NBDKIT_BIN_ENV = "PIXIE_NBDKIT_BIN"
DEFAULT_NBDKIT_BIN = "nbdkit"


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


def _require_ui_auth(request: Request) -> None:
    if not request.session.get(SESSION_AUTHED_KEY):
        raise NotAuthenticated


def create_app() -> FastAPI:
    """Build the FastAPI app. Factory shape so tests can construct a
    fresh app per fixture without global state."""

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        # startup: state already attached below; nothing async to do.
        try:
            yield
        finally:
            # Stop nbdkit children on graceful shutdown so a Ctrl-C
            # + restart doesn't leave orphan processes clinging to
            # NBD ports.
            with contextlib_suppress(Exception):
                app.state.nbd_server.stop()

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
    app.state.nbd_server = NbdServer(
        port_base=_resolve_nbd_port_base(),
        bind=_resolve_nbd_bind(),
        nbdkit_bin=_resolve_nbdkit_bin(),
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
    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

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
            {"version": pixie.__version__, "error": None},
        )

    @app.post("/ui/login", response_class=HTMLResponse)
    def ui_login(request: Request, password: str = Form(...)) -> Any:
        if not check_password(password):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"version": pixie.__version__, "error": "Invalid password."},
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
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "version": pixie.__version__,
                "entries": request.app.state.catalog_store.list_entries(),
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
            # 303 back to /ui/ silently on conflict; UI shows the row
            # already exists.
            return RedirectResponse(url="/ui/", status_code=status.HTTP_303_SEE_OTHER)
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
        return RedirectResponse(url="/ui/", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/ui/catalog/fetch")
    def ui_catalog_fetch(
        request: Request,
        name: str = Form(...),
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        store = request.app.state.catalog_store
        entry = store.get_entry(name)
        if entry is None:
            return RedirectResponse(url="/ui/", status_code=status.HTTP_303_SEE_OTHER)

        states = request.app.state.fetch_states
        if states.get(name, {}).get("state") == "fetching":
            return RedirectResponse(url="/ui/", status_code=status.HTTP_303_SEE_OTHER)
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
        return RedirectResponse(url="/ui/", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/ui/catalog/delete")
    def ui_catalog_delete(
        request: Request,
        name: str = Form(...),
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        store = request.app.state.catalog_store
        store.delete(name)
        request.app.state.fetch_states.pop(name, None)
        return RedirectResponse(url="/ui/", status_code=status.HTTP_303_SEE_OTHER)

    # ---------- feature routers --------------------------------------
    #
    # Catalog + blob + artifacts routes live at the same URL shape they
    # had in the trio (``/catalog``, ``/b/``, ``/artifacts/``) so
    # operator muscle memory + iPXE templates keep working.
    app.include_router(catalog_router)
    app.include_router(exports_router)

    return app


# Module-level app so ``uvicorn pixie.web.main:app`` works without a
# factory flag.
app = create_app()
