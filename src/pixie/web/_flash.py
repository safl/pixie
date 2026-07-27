"""One-shot flash messages over the signed session cookie.

The post/redirect/get pattern the UI uses means a mutating POST can't
render its own result -- it 303s to a GET. Set a flash in the POST
handler right before the redirect; the next HTML render pops it into the
``flash`` / ``flash_kind`` template vars the base layout already renders
(``layout.html``). Wired as a Jinja2Templates ``context_processor`` so
every page picks it up with no per-route plumbing.

``kind`` is a Bootstrap alert suffix: ``success`` / ``danger`` /
``warning`` / ``info``.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request

_FLASH_KEY = "_flash_msg"
_FLASH_KIND_KEY = "_flash_kind"


def set_flash(request: Request, message: str, kind: str = "success") -> None:
    """Stash a one-shot banner shown on the next rendered page."""
    request.session[_FLASH_KEY] = message
    request.session[_FLASH_KIND_KEY] = kind


def flash_context(request: Request) -> dict[str, Any]:
    """Jinja2Templates context processor: pop any pending flash so it
    shows exactly once. Returns an empty dict (no ``flash`` var) when
    there is nothing to show, so the layout's ``{% if flash %}`` stays
    false on ordinary renders."""
    message = request.session.pop(_FLASH_KEY, None)
    kind = request.session.pop(_FLASH_KIND_KEY, None)
    if not message:
        return {}
    return {"flash": message, "flash_kind": kind or "info"}
