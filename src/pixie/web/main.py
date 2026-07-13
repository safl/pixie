"""FastAPI application factory.

Skeleton at 0.1.0: exposes ``/healthz`` (open, JSON) + session-cookie
login/logout under ``/ui/`` + a placeholder dashboard. Feature routers
land in later PRs; this file becomes the mount point.

Run locally with:

    uv run uvicorn pixie.web.main:app --reload

Serialised session secret: a random URL-safe 32-byte token per process,
persisted across restarts is a v0.2+ concern (a settings-store row or a
``session-secret`` file in the state dir). At 0.1.0 sessions expire on
every restart, which is fine while nothing operator-visible is stored.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import pixie
from pixie.web._auth import (
    SESSION_AUTHED_KEY,
    SESSION_COOKIE,
    check_password,
    require_auth,
)

_HERE = Path(__file__).resolve().parent
_TEMPLATES_DIR = _HERE / "_templates"
_STATIC_DIR = _HERE / "_static"


class NotAuthenticated(Exception):
    """Raised by the UI dependency when a browser request lacks the
    session cookie. The exception handler redirects to /ui/login."""


def _require_ui_auth(request: Request) -> None:
    """UI variant of ``require_auth``: raises :class:`NotAuthenticated`
    so the handler can redirect the browser instead of returning JSON."""
    if not request.session.get(SESSION_AUTHED_KEY):
        raise NotAuthenticated


def create_app() -> FastAPI:
    """Build the FastAPI app. Factory shape so tests can construct a
    fresh app per fixture without global state."""
    app = FastAPI(
        title="pixie",
        version=pixie.__version__,
        description="Bare-metal netboot appliance.",
    )

    # SessionMiddleware signs the ``pixie-token`` cookie. Sliding TTL:
    # 7 days from last touch. ``https_only=False`` because the pixie
    # deploy is LAN-only by design; operators front with TLS if they
    # want it, we don't force a scheme.
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
    async def _redirect_to_login(request: Request, _exc: NotAuthenticated) -> RedirectResponse:
        return RedirectResponse(url="/ui/login", status_code=status.HTTP_303_SEE_OTHER)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Mirror the shape the operator UI expects downstream; keeps the
        # JSON body compact vs. FastAPI's default.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": exc.errors()},
        )

    # ---------- open routes ------------------------------------------

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        """Liveness probe. Container healthcheck reads this."""
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
            {"version": pixie.__version__},
        )

    # ---------- ping under session-auth ------------------------------
    #
    # A protected endpoint that returns JSON. Exists so tests + monitoring
    # can prove session-cookie auth works end-to-end without depending on
    # feature routes that don't exist yet.

    @app.get("/api/ping")
    def api_ping(
        _auth: None = Depends(require_auth),
    ) -> dict[str, Any]:
        return {"pong": True, "version": pixie.__version__}

    return app


# Module-level app so ``uvicorn pixie.web.main:app`` works without a
# factory flag.
app = create_app()
