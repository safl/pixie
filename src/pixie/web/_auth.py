"""Session-cookie auth for pixie.

Single-tenant: one admin password, checked at ``POST /ui/login``. A
successful login flips ``request.session["pixie_authed"] = True``; the
session is a server-signed cookie managed by Starlette's
:class:`SessionMiddleware`, so no DB session table is needed.

The password is sourced from ``$PIXIE_ADMIN_PASSWORD`` if set +
non-empty, otherwise falls back to :data:`DEFAULT_ADMIN_PASSWORD`
(``"pixie-lab"``). Auth is ALWAYS on; an unset env var just means the
operator gets the well-known default until they override it. The
startup banner (added in a later PR) will warn when the default is in
use so an exposed pixie does not silently ship with
``pixie-lab / pixie-lab``.

Failure modes return 401; ``/ui/*`` routes catch the exception in a
middleware and redirect to ``/ui/login``.
"""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request, status

# Well-known fallback password. Kept here (no ``_config`` module in the
# skeleton yet) so callers don't need a two-step import; the config-file
# path lands with the first PR that grows a ``pixie.toml`` reader.
DEFAULT_ADMIN_PASSWORD = "pixie-lab"

# Session-cookie name. Set explicitly so integration tests and operator
# scripts can grep for a stable token in Set-Cookie.
SESSION_COOKIE = "pixie-token"

# Session key the auth dep checks. Set on successful /ui/login.
SESSION_AUTHED_KEY = "pixie_authed"

# Admin password env var. Overrides the well-known default.
ADMIN_PASSWORD_ENV = "PIXIE_ADMIN_PASSWORD"


def admin_password() -> str:
    """The active admin password.

    Env var overrides the default. Never returns ``None``; auth is
    always on.
    """
    env = (os.environ.get(ADMIN_PASSWORD_ENV) or "").strip()
    return env or DEFAULT_ADMIN_PASSWORD


def using_default_password() -> bool:
    """True iff the active password is the well-known fallback."""
    return admin_password() == DEFAULT_ADMIN_PASSWORD


def check_password(password: str) -> bool:
    """Constant-time compare against the active admin password."""
    return hmac.compare_digest(password, admin_password())


def require_auth(request: Request) -> None:
    """Mutating routes depend on this. 401 unless ``POST /ui/login`` has
    flipped the session flag for this client."""
    if not request.session.get(SESSION_AUTHED_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="login required",
        )
