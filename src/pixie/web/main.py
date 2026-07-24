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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import pixie
from pixie.catalog import DEFAULT_CATALOG_URL
from pixie.catalog._routes import router as catalog_router
from pixie.catalog._store import CatalogStore
from pixie.events import EventsLog
from pixie.events._kinds import (
    AUTH_LOGIN_FAILED,
    AUTH_LOGIN_SUCCEEDED,
    CATALOG_BLOB_DELETED,
    CATALOG_ENTRY_ADDED,
    CATALOG_ENTRY_DELETED,
    CATALOG_ENTRY_UPDATED,
    CATALOG_FETCH_DONE,
    CATALOG_FETCH_FAILED,
    CATALOG_FETCH_STARTED,
    CATALOG_FETCH_UNCHANGED,
    CATALOG_IMPORT_FAILED,
    CATALOG_IMPORT_OK,
    EVENTS_CLEARED,
    EXPORT_NBDKIT_SPAWNED,
    LIVE_ENV_FETCH_DONE,
    LIVE_ENV_FETCH_FAILED,
    TFTP_STARTED,
    TFTP_STOPPED,
)
from pixie.events._log import Event
from pixie.events._routes import router as events_router
from pixie.exports._routes import router as exports_router
from pixie.exports._store import ExportsStore, OverlaysStore
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
from pixie.web._settings_store import (
    DEFAULT_LIVE_ENV_SRC,
    KEY_DATETIME_FORMAT,
    KEY_DISPLAY_TZ,
    KEY_LIVE_ENV_EXTRA_CMDLINE,
    KEY_LIVE_ENV_SRC,
    SettingsStore,
    SettingValueError,
    format_ts,
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
        events = getattr(app.state, "events_log", None)
        if events is not None:
            events.emit(
                EXPORT_NBDKIT_SPAWNED,
                subject_kind="export",
                subject_id=export.name,
                summary=f"nbdkit respawned on port {port} (startup)",
                details={"nbd_port": port, "reason": "startup-respawn"},
            )


def _respawn_overlays_at_startup(app: FastAPI) -> None:
    """Walk the ``overlays`` table and spawn qemu-nbd for each row
    whose qcow2 file still exists on disk. Same intent as
    :func:`_respawn_exports_at_startup` but for the persistent
    per-machine overlay path. Missing qcow2 files land the row in
    ``status=error`` -- the next plan render lazy-creates a fresh
    one, which is the operator-visible reset semantics.
    """
    import logging as _logging
    from pathlib import Path as _Path

    from pixie.pxe._renderer import _overlay_export_name

    log = _logging.getLogger(__name__)
    overlays_store = app.state.overlays_store
    nbd = app.state.nbd_server
    for overlay in overlays_store.list_all():
        qcow2_path = _Path(overlay.qcow2_path)
        if not qcow2_path.exists():
            overlays_store.update_runtime(
                overlay.alias,
                nbd_port=0,
                status="error",
                error=f"qcow2 missing at {qcow2_path}",
            )
            log.warning("overlay %s: qcow2 missing at %s", overlay.alias, qcow2_path)
            continue
        # No ``--offset`` bookkeeping in the extract-at-fetch design:
        # each qcow2 was created with ``backing_file`` pointing at
        # whichever file preferred_serve_path yields, so qemu-nbd
        # serves the right partition at offset 0 already.
        try:
            port = nbd.spawn_qcow2(
                _overlay_export_name(overlay),
                qcow2_path,
            )
        except RuntimeError as exc:
            overlays_store.update_runtime(
                overlay.alias,
                nbd_port=0,
                status="error",
                error=f"respawn failed: {exc}",
            )
            log.warning("overlay %s: qemu-nbd respawn failed: %s", overlay.alias, exc)
            continue
        overlays_store.update_runtime(
            overlay.alias,
            nbd_port=port,
            status="running",
            error="",
        )
        log.info("overlay %s: respawned on port %d", overlay.alias, port)


def _require_ui_auth(request: Request) -> None:
    if not request.session.get(SESSION_AUTHED_KEY):
        raise NotAuthenticated


#: How many "recent events" tiles the per-portion cards show above
#: the /ui/events log link. Tuned once here rather than sprinkled
#: through the /ui/catalog + /ui/machines handlers as a magic 10.
_RECENT_EVENTS_LIMIT = 10


def _deployment_envvar_docs() -> list[dict[str, str]]:
    """Documentation rows for the Settings > Deployment card. Kept in
    ONE place so the docs and the actual env-var constants (elsewhere
    in this module + in ``_auth``) can't drift silently. Rendered
    verbatim in ``settings.html``; each row = one variable."""
    from pixie.web._auth import ADMIN_PASSWORD_ENV

    return [
        {
            "name": ADMIN_PASSWORD_ENV,
            "default": "",
            "purpose": (
                "Password for the /ui/login form. MUST be set; pixie"
                " refuses UI writes when unset. The compose bundle"
                " ships with a random default operators are expected"
                " to replace."
            ),
        },
        {
            "name": "PIXIE_PUBLIC_HOST",
            "default": "127.0.0.1",
            "purpose": (
                "LAN hostname or IP pixie tells iPXE targets to fetch"
                " artifacts from. Set to the pixie host's LAN address"
                " on the deploy so PXE targets can reach it. The"
                " compose bundle sources this from PIXIE_HOST_ADDR"
                " in envvars."
            ),
        },
        {
            "name": "PIXIE_NBD_PUBLIC_HOST",
            "default": "PIXIE_PUBLIC_HOST",
            "purpose": (
                "Separate NBD-server hostname if pixie is fronted by a"
                " reverse proxy for HTTP but NBD is direct. Usually"
                " equal to PIXIE_PUBLIC_HOST."
            ),
        },
        {
            "name": STATE_DIR_ENV,
            "default": str(DEFAULT_STATE_DIR),
            "purpose": (
                "State root. Holds state.db, blobs (fetched image"
                " bytes), artifacts (extracted netboot bundles), and"
                " live-env by default."
            ),
        },
        {
            "name": LIVE_ENV_DIR_ENV,
            "default": "$PIXIE_DATA_DIR/live-env",
            "purpose": (
                "Where the pixie live-env vmlinuz + initrd +"
                " live.squashfs are staged. The pixie-* boot modes"
                " degrade to an unavailable plan when this dir is"
                " missing or incomplete."
            ),
        },
        {
            "name": "PIXIE_LIVE_ENV_EXTRA_CMDLINE",
            "default": "",
            "purpose": (
                "Extra kernel-cmdline tokens appended to every"
                " pixie-live-env chain. Global default; a machine"
                " with its own extra_cmdline binding overrides. See"
                " the Live env page for the live-editable override."
            ),
        },
        {
            "name": "PIXIE_LIVE_ENV_SRC",
            "default": (
                "https://github.com/safl/pixie/releases/latest/download/"
                "pixie-live-env-x86_64.tar.gz"
            ),
            "purpose": (
                "Tarball the dashboard 'Fetch live-env' action pulls the"
                " netboot-pc bake from (https:// or oras://). Defaults to"
                " the latest pixie GitHub release; point at a mirror for"
                " air-gapped deploys. Live-editable override on the"
                " Live env page."
            ),
        },
        {
            "name": "PIXIE_SEED_CATALOG",
            "default": "1",
            "purpose": (
                "Seed a fresh (empty) catalog from the bundled curated"
                " catalog (the netboot-capable nosi subset) on first"
                " start. Set to 0 to start with an empty catalog. Runs"
                " once; never clobbers an operator-populated catalog."
            ),
        },
        {
            "name": FETCH_POOL_SIZE_ENV,
            "default": str(DEFAULT_FETCH_POOL_SIZE),
            "purpose": (
                "How many catalog fetches run in parallel. Bump for"
                " a fast pipe with lots of catalog entries; lower on"
                " a shared network so a fetch storm doesn't starve"
                " concurrent flashes."
            ),
        },
        {
            "name": NBD_BIND_ENV,
            "default": DEFAULT_NBD_BIND,
            "purpose": (
                "Address nbdkit binds each per-image export to. Under"
                " --network=host on the compose deploy, 0.0.0.0"
                " reaches the LAN."
            ),
        },
        {
            "name": NBD_PORT_BASE_ENV,
            "default": "10809",
            "purpose": (
                "First TCP port nbdkit reserves for exports; each"
                " subsequent export lands on the next free port."
            ),
        },
        {
            "name": NBDKIT_BIN_ENV,
            "default": DEFAULT_NBDKIT_BIN,
            "purpose": "Path to the nbdkit binary. Defaults to `nbdkit` on $PATH.",
        },
        {
            "name": TFTP_ENABLED_ENV,
            "default": "false",
            "purpose": (
                "Set to 1 / true / yes / on to bring pixie's in-process"
                " TFTP responder up on startup. Requires udp/69, hence"
                " root; the compose file's --network=host + running as"
                " root inside the container handles this."
            ),
        },
        {
            "name": TFTP_BIND_ENV,
            "default": DEFAULT_TFTP_BIND,
            "purpose": "Address pixie's TFTP responder listens on.",
        },
        {
            "name": TFTP_PORT_ENV,
            "default": str(DEFAULT_TFTP_PORT),
            "purpose": "UDP port for pixie's TFTP responder. Standard PXE assumes 69.",
        },
        {
            "name": TFTP_ROOT_ENV,
            "default": "(package-bundled iPXE binaries)",
            "purpose": (
                "Directory pixie serves over TFTP. Defaults to the"
                " iPXE binaries pixie ships (undionly.kpxe, ipxe.efi)."
            ),
        },
        {
            "name": TFTP_BIN_ENV,
            "default": DEFAULT_TFTP_BIN,
            "purpose": "Path to the in.tftpd binary. Only used when TFTP is enabled.",
        },
    ]


def _deployment_state() -> dict[str, Any]:
    """Snapshot of the resolved deployment knobs for rendering in the
    Settings > Deployment card's DHCP + listen sections. Reads the
    resolvers above (each hides the env-var fallback chain) so the
    card shows the actual values pixie is using right now."""
    host = (os.environ.get("PIXIE_PUBLIC_HOST") or "").strip() or "127.0.0.1"
    return {
        "host": host,
        "port": 8080,  # pixie's HTTP listener is hard-coded on the compose bundle
        "tftp_enabled": _tftp_enabled(),
        "tftp_bind": _resolve_tftp_bind(),
        "tftp_port": _resolve_tftp_port(),
        "tftp_root": str(_resolve_tftp_root()),
        "nbd_bind": _resolve_nbd_bind(),
        "nbd_port_base": _resolve_nbd_port_base(),
    }


def _fetch_would_be_noop(entry: Any, store: Any) -> bool:
    """Would :func:`pixie.catalog._fetcher.fetch` take its fast path
    for this entry? True iff the entry is flagged fetched AND the
    corresponding on-disk artifact (blob for a disk image, unpacked
    manifest.json for a tar.gz netboot bundle) is still present.

    Used by the ``/ui/catalog/fetch`` handler to distinguish an
    Update click that would produce no visible change from one that
    would actually re-download bytes. The operator gets a
    catalog.fetch.unchanged event + a short-lived "already at latest"
    pill instead of a millisecond flicker on the fetching badge."""
    if not getattr(entry, "content_sha256", ""):
        return False
    if entry.format == "tar.gz":
        manifest = store.artifact_path(entry.content_sha256, "manifest.json")
        return bool(manifest.is_file())
    blob = store.blob_path(entry.content_sha256)
    return bool(blob.is_file())


def _recent_events_for(events_log: EventsLog, subject_kind: str) -> list[Event]:
    """Tail of the events log filtered to one subject kind. Callers
    are the per-portion recent-events cards on the catalog and
    machines list pages (the per-machine detail page uses a filtered
    read with ``subject_id`` + a larger limit and does not go
    through this helper)."""
    return events_log.list(subject_kind=subject_kind, limit=_RECENT_EVENTS_LIMIT)


def _reset_overlay_row(state: Any, alias: str) -> bool:
    """Tear down one persistent overlay by ``alias``: terminate its
    qemu-nbd, unlink the qcow2, drop the ``overlays`` row, and emit
    ``overlay.reset``.

    Returns True iff a row existed. Idempotent: a missing row or a
    missing file are both no-ops. Shared by the per-machine Reset button
    and the Overlays page (single Reset + bulk Prune) so every teardown
    path behaves identically. Kills qemu-nbd first so the qcow2 isn't
    held open when we unlink it. The next nbdboot that references the
    alias lazy-recreates a fresh qcow2 from the base image."""
    from pathlib import Path as _Path

    from pixie.events._kinds import OVERLAY_RESET
    from pixie.pxe._renderer import _overlay_export_name

    overlays = state.overlays_store
    row = overlays.get(alias)
    if row is None:
        return False
    state.nbd_server.terminate(_overlay_export_name(row))
    with contextlib_suppress(FileNotFoundError, OSError):
        _Path(row.qcow2_path).unlink()
    overlays.delete(alias)
    events = getattr(state, "events_log", None)
    if events is not None:
        events.emit(
            OVERLAY_RESET,
            subject_kind="overlay",
            subject_id=alias,
            summary=f"overlay {alias!r} reset (image {row.image_sha[:12]})",
            details={"alias": alias, "image_sha": row.image_sha, "attached_mac": row.attached_mac},
        )
    return True


def _seed_catalog_at_startup(app: FastAPI) -> None:
    """Seed a fresh (empty) catalog from the bundled curated catalog on
    first start, so a plain deploy comes up with the netboot-capable set
    offline. Gated on ``PIXIE_SEED_CATALOG`` (default on) and a one-shot
    ``catalog.seeded`` settings marker so it runs exactly once and never
    clobbers an operator-populated catalog: a non-empty catalog is left
    untouched (the marker is still set so the check is skipped next
    time). A bad bundled file is logged, not fatal -- the app must come
    up regardless."""
    import logging

    from pixie.catalog import (
        CATALOG_SEEDED_KEY,
        SEED_CATALOG_ENV,
        bundled_catalog_bytes,
    )
    from pixie.catalog._schema import parse_catalog_toml

    if (os.environ.get(SEED_CATALOG_ENV, "1") or "").strip() == "0":
        return
    settings = app.state.settings_store
    if settings.get(CATALOG_SEEDED_KEY):
        return
    store = app.state.catalog_store
    if store.list_entries():
        # Existing catalog (upgrade path): mark seeded, don't pollute.
        settings.set_value(CATALOG_SEEDED_KEY, "1")
        return
    try:
        entries = parse_catalog_toml(bundled_catalog_bytes())
    except (ValueError, OSError) as exc:
        logging.getLogger(__name__).warning("catalog seed: bundled catalog unreadable: %s", exc)
        return
    for entry in entries:
        store.upsert(entry)
    settings.set_value(CATALOG_SEEDED_KEY, "1")
    events = getattr(app.state, "events_log", None)
    if events is not None:
        events.emit(
            CATALOG_IMPORT_OK,
            subject_kind="catalog",
            subject_id="(bundled)",
            summary=f"seeded {len(entries)} curated catalog entries on first start",
            details={"count": len(entries), "source": "bundled"},
        )


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
                events = getattr(app.state, "events_log", None)
                if events is not None:
                    events.emit(TFTP_STARTED, summary="tftp subprocess up")
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
        # Same intent for persistent named overlays: any qcow2 whose
        # file still exists gets a qemu-nbd resurrected so the next
        # ``GET /pxe/<mac>`` for a machine bound to a non-blank
        # ``overlay_alias`` finds a running export at a known port.
        # Idempotent.
        _respawn_overlays_at_startup(app)
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
                    events = getattr(app.state, "events_log", None)
                    if events is not None:
                        events.emit(TFTP_STOPPED, summary="tftp subprocess down")

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
    app.state.overlays_store = OverlaysStore(app.state.catalog_store.db_path)
    app.state.machines_store = MachinesStore(app.state.catalog_store.db_path)
    app.state.events_log = EventsLog(app.state.catalog_store.db_path)
    app.state.settings_store = SettingsStore(app.state.catalog_store.db_path)
    # Seed the curated catalog on a fresh deploy (env-gated, one-shot).
    _seed_catalog_at_startup(app)
    app.state.nbd_server = NbdServer(
        port_base=_resolve_nbd_port_base(),
        bind=_resolve_nbd_bind(),
        nbdkit_bin=_resolve_nbdkit_bin(),
    )
    app.state.live_env_dir = _resolve_live_env_dir()
    # Root for per-machine qcow2 overlay files. Sub-directory under
    # the state dir so a single ``PIXIE_DATA_DIR`` override still
    # relocates everything (catalog blobs + overlays + state.db)
    # together.
    app.state.overlays_dir = state_dir / "overlays"
    app.state.overlays_dir.mkdir(parents=True, exist_ok=True)
    app.state.pxe_renderer = PlanRenderer(
        catalog=app.state.catalog_store,
        exports=app.state.exports_store,
        overlays=app.state.overlays_store,
        nbd=app.state.nbd_server,
        overlays_dir=app.state.overlays_dir,
        live_env_dir=app.state.live_env_dir,
        events=app.state.events_log,
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
    # Single-slot progress dict for the operator "Fetch live-env"
    # action (there is one live env per pixie, not one per resource).
    # Shape mirrors a fetch_states entry: state -> fetching / done /
    # error, plus phase + byte counters while the download runs.
    app.state.live_env_fetch_state = {}

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

    # ``fmt_ts`` folds a raw ISO-8601 timestamp (as pixie writes them
    # to state.db) through the operator's current Settings picks:
    # timezone + strftime pattern. A closure over ``app.state.settings_store``
    # keeps the filter dependency-free at the call site
    # (``{{ e.ts | fmt_ts }}``) while still picking up a live Settings
    # change on the next render, since ``resolve_*`` reads the DB on
    # every call.
    def _fmt_ts_filter(raw: str) -> str:
        return format_ts(raw or "", app.state.settings_store)

    templates.env.filters["fmt_ts"] = _fmt_ts_filter

    from pixie.web._inventory import humanize_bytes, humanize_hz

    templates.env.filters["humanize_bytes"] = humanize_bytes
    templates.env.filters["humanize_hz"] = humanize_hz
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

    @app.api_route("/pivot/nbdboot.cpio.gz", methods=["GET", "HEAD"])
    def pivot_nbdboot() -> Response:
        """Serve the nbdboot pivot overlay as a gzipped newc-cpio.

        Loaded by ``ipxe/nbdboot.j2`` as a supplementary ``initrd``
        directive after nosi's own initrd. Linux concatenates cpio
        streams during initramfs unpack; same-path entries from a
        later overlay win, so ``/scripts/nbdboot`` here shadows any
        legacy ``/scripts/ramboot`` in nosi's baked initrd -- and a
        future nosi release can ship a generic initrd (no
        pixie-flavour pivot script) without breaking anything on
        pixie's side.

        Route is OPEN by design (the target holds no session cookie
        at boot). Built once per app startup from
        :mod:`pixie.pivot`; the bytes are byte-identical across
        builds so a downstream cacher can treat the URL as
        immutable per pixie release."""
        from pixie.pivot import build_pivot_cpio_gz

        # Rebuilt lazily but memoised on ``app.state`` so the second
        # + subsequent hits don't pay the (~ms) cpio-assembly cost.
        blob = getattr(app.state, "pivot_nbdboot_cpio_gz", None)
        if blob is None:
            blob = build_pivot_cpio_gz()
            app.state.pivot_nbdboot_cpio_gz = blob
        return Response(content=blob, media_type="application/gzip")

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
        events = getattr(request.app.state, "events_log", None)
        if not check_password(password):
            if events is not None:
                events.emit(
                    AUTH_LOGIN_FAILED,
                    summary="wrong password",
                )
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
        if events is not None:
            events.emit(AUTH_LOGIN_SUCCEEDED, summary="admin logged in")
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
        events = request.app.state.events_log.list(limit=10)
        stats = _dashboard_stats(request)
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
        sort: str = "",
        dir: str = "",
        _auth: None = Depends(_require_ui_auth),
    ) -> HTMLResponse:
        """Catalog + exports in one view. Each disk-image entry
        carries its NBD-serving state (port + status + nbdkit error);
        netboot bundles just show their fetch state -- they are served
        as HTTP artifacts from ``/artifacts/<sha>/{vmlinuz,initrd}``
        rather than over NBD, so no port is meaningful for them."""
        from pixie.web._table_state import (
            filter_rows,
            parse_pagination,
            parse_sort,
            sort_rows,
        )

        catalog = request.app.state.catalog_store
        events_log = request.app.state.events_log
        all_entries = catalog.list_entries()
        filtered = filter_rows(
            all_entries,
            q,
            fields=("name", "src", "netboot_src", "arch", "format", "description"),
        )
        sort_state = parse_sort(
            dict(request.query_params),
            allowed={
                "name": "name",
                "format": "format",
                "arch": "arch",
                "added_at": "added_at",
                "size_bytes": "size_bytes",
            },
            default_column="name",
        )
        filtered = sort_rows(filtered, sort_state)
        page_state = parse_pagination(dict(request.query_params), total=len(filtered))
        page_entries = filtered[page_state.offset : page_state.offset + page_state.per_page]
        preserved = {
            k: v
            for k, v in {
                "q": q,
                "sort": sort_state.column if sort_state.column != "name" else "",
                "dir": sort_state.direction if sort_state.direction != "asc" else "",
                "per_page": str(page_state.per_page)
                if page_state.per_page != DEFAULT_PER_PAGE
                else "",
            }.items()
            if v
        }
        # Most recent catalog-scoped events (add / delete / fetch
        # started / done / failed). Renders under the entry table
        # with a link to /ui/events for the full log. Same shape
        # lands on the machines list page so a per-portion events
        # surface is where the operator expects it to be.
        catalog_events = _recent_events_for(events_log, "entry")
        return templates.TemplateResponse(
            request,
            "catalog.html",
            {
                "version": pixie.__version__,
                "entries": page_entries,
                "fetch_states": request.app.state.fetch_states,
                "q": q,
                "sort": sort_state,
                "page_state": page_state,
                "preserved": preserved,
                "catalog_events": catalog_events,
                "default_catalog_url": DEFAULT_CATALOG_URL,
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
                        request.app.state.events_log,
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
                "warn_update": bool(request.query_params.get("warn_update")),
                "orphans_bundle_name": (
                    forward.name
                    if (forward is not None and entry.bindable and forward.fetched)
                    else None
                ),
                "breaks_nbdboot_for": [e.name for e in backward] if not entry.bindable else [],
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
        sort: str = "",
        dir: str = "",
        _auth: None = Depends(_require_ui_auth),
    ) -> HTMLResponse:
        from pixie.web._table_state import (
            filter_rows,
            parse_pagination,
            parse_sort,
            sort_rows,
        )

        all_machines = request.app.state.machines_store.list()
        filtered = filter_rows(
            all_machines,
            q,
            fields=(
                "mac",
                "boot_mode",
                "image_content_sha256",
                "last_seen_ip",
                "labels",
                "target_disk_serial",
            ),
        )
        sort_state = parse_sort(
            dict(request.query_params),
            allowed={
                "mac": "mac",
                "boot_mode": "boot_mode",
                "last_seen_at": "last_seen_at",
                "last_seen_ip": "last_seen_ip",
                "discovered_at": "discovered_at",
            },
            default_column="last_seen_at",
            default_direction="desc",
        )
        filtered = sort_rows(filtered, sort_state)
        page_state = parse_pagination(dict(request.query_params), total=len(filtered))
        page_machines = filtered[page_state.offset : page_state.offset + page_state.per_page]
        preserved = {
            k: v
            for k, v in {
                "q": q,
                "sort": sort_state.column if sort_state.column != "last_seen_at" else "",
                "dir": sort_state.direction if sort_state.direction != "desc" else "",
                "per_page": str(page_state.per_page)
                if page_state.per_page != DEFAULT_PER_PAGE
                else "",
            }.items()
            if v
        }
        # Most recent machine-scoped events (bound / discovered /
        # inventory / status). Rendered under the machine table with
        # a link to /ui/events for the full log; the per-machine
        # detail page already does the same but filtered on that one
        # MAC.
        machine_events = _recent_events_for(request.app.state.events_log, "machine")
        return templates.TemplateResponse(
            request,
            "machines.html",
            {
                "version": pixie.__version__,
                "machines": page_machines,
                "q": q,
                "sort": sort_state,
                "page_state": page_state,
                "preserved": preserved,
                "machine_events": machine_events,
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
        from pixie.machines._store import BOOT_MODE_META, BadMac
        from pixie.web._bind_preview import bind_preview_text
        from pixie.web._inventory import normalise_inventory

        try:
            machine = request.app.state.machines_store.get(mac)
        except BadMac:
            return RedirectResponse(url="/ui/machines", status_code=status.HTTP_303_SEE_OTHER)
        if machine is None:
            return RedirectResponse(url="/ui/machines", status_code=status.HTTP_303_SEE_OTHER)
        inv = normalise_inventory(machine.inventory)
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
        # Overlay aliases the operator may attach for THIS machine's
        # currently-bound base image: every alias over that image that is
        # free (attached_mac == "") or already held by this machine. An
        # alias held by a DIFFERENT machine is single-writer-locked and
        # deliberately not offered. Empty until the machine's been bound
        # to a real image + at least one overlay over it exists.
        overlay_aliases: list[Any] = []
        if machine.image_content_sha256:
            overlay_aliases = [
                o
                for o in request.app.state.overlays_store.list_for_image(
                    machine.image_content_sha256
                )
                if o.attached_mac in ("", machine.mac)
            ]
        # Plain-English narration of what the next PXE will do given
        # the current bind. Rendered into the preview panel so an
        # operator sees a real description on page load, not a bare
        # placeholder that only fills in after a form-side JS edit.
        # The client-side JS refreshes this same string on any bind
        # form change; the server-side render is the correct initial
        # state (matches the JS's MODE_PREVIEWS shape).
        picked_image = next(
            (e for e in bindable_entries if e.content_sha256 == machine.image_content_sha256),
            None,
        )
        initial_bind_preview = bind_preview_text(
            boot_mode=machine.boot_mode,
            image_name=picked_image.name if picked_image else "",
            disk_label=machine.target_disk_serial,
            overlay_alias=machine.overlay_alias,
        )
        return templates.TemplateResponse(
            request,
            "machine_detail.html",
            {
                "version": pixie.__version__,
                "machine": machine,
                "inv": inv,
                "events": events,
                "bindable_entries": bindable_entries,
                "boot_mode_meta": BOOT_MODE_META,
                "overlay_aliases": overlay_aliases,
                "initial_bind_preview": initial_bind_preview,
                "global_live_env_extra_cmdline": (
                    request.app.state.settings_store.resolve_live_env_extra_cmdline()
                ),
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
        target_disk_serial: str = Form(""),
        extra_cmdline: str = Form(""),
        overlay_alias: str = Form(""),
        overlay_alias_new: str = Form(""),
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        """Persist a boot-mode binding. Labels are edited on their
        own row (see ``/ui/machines/{mac}/labels/edit``) so a bind
        does not risk clobbering the operator's tagging.

        UI-side: silently redirect back on invalid input; a full
        field-error flash chain lands in a follow-up. Labels on
        the row survive the bind untouched via ``upsert_binding``
        pulling them off the current row.

        ``overlay_alias`` carries the picker's chosen value; a magic
        ``__new`` sentinel says "take ``overlay_alias_new`` as a new
        alias name". Attaching an existing alias IMPLIES its base image
        (the machine's ``image_content_sha256`` is overridden to the
        overlay's). A new alias is created over the base image the
        operator selected in the image dropdown. Single-writer: an alias
        already held by a DIFFERENT machine is refused (the bind is
        dropped); the operator must detach it there first. Blank / a
        ``__new`` without a name / a new alias with no base image all
        fold back to ephemeral."""
        import contextlib as _contextlib

        from pixie.machines._store import BadMac, normalise_mac
        from pixie.web._overlay_bind import overlay_state, resolve_overlay_bind

        choice = overlay_alias.strip()
        if choice == "__new":
            choice = overlay_alias_new.strip()
        with _contextlib.suppress(BadMac, ValueError):
            canon = normalise_mac(mac)
            store = request.app.state.machines_store
            overlays, overlays_dir = overlay_state(request.app.state)
            current = store.get(canon)
            existing_labels = list(current.labels) if current else []

            # Single-writer + alias-implies-image + lazy row-create all
            # live in the shared resolver so the JSON API behaves the
            # same. A held-elsewhere alias raises ValueError (suppressed
            # here -> silent redirect, no partial state).
            image_sha, resolved = resolve_overlay_bind(
                overlays=overlays,
                overlays_dir=overlays_dir,
                mac=canon,
                boot_mode=boot_mode,
                image_sha=image_content_sha256.strip().lower(),
                alias=choice,
            )
            store.upsert_binding(
                canon,
                boot_mode=boot_mode,
                image_content_sha256=image_sha,
                labels=existing_labels,
                target_disk_serial=target_disk_serial,
                extra_cmdline=extra_cmdline,
                overlay_alias=resolved,
            )
        return RedirectResponse(url="/ui/machines", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/ui/machines/{mac}/overlay/reset")
    def ui_machines_overlay_reset(
        request: Request,
        mac: str,
        alias: str = Form(...),
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        """Terminate the qemu-nbd for this overlay, unlink the qcow2,
        and drop the overlays row. Next plan render lazily recreates
        a fresh qcow2 from the base blob. Idempotent: missing row or
        missing file are both no-ops (row still gets deleted).

        Confirm-modal is on the client. Server-side just carries the
        deletion out. Any operator who lands here without confirming
        picked the button already."""
        _reset_overlay_row(request.app.state, alias)
        return RedirectResponse(url=f"/ui/machines/{mac}", status_code=status.HTTP_303_SEE_OTHER)

    # ---------- ui: overlays management -----------------------------
    #
    # Persistent per-machine qcow2 overlays get their own page. The
    # bind form only shows one machine's profiles, so fleet-wide disk
    # pressure + reclaimable junk have no home there. Rows join the
    # overlays table against the machine registry, catalog, NBD
    # supervisor + the qcow2 on disk (see web/_overlays.py). Reset
    # tears one down; Prune reclaims every file-missing / machine-
    # orphaned row in one shot (never an idle keep).

    def _overlay_views_now(request: Request) -> list[Any]:
        from pixie.web._overlays import build_overlay_views

        state = request.app.state
        return build_overlay_views(
            overlays=state.overlays_store,
            machines=state.machines_store,
            catalog=state.catalog_store,
            nbd=state.nbd_server,
        )

    @app.get("/ui/overlays", response_model=None)
    def ui_overlays(
        request: Request,
        _auth: None = Depends(_require_ui_auth),
    ) -> HTMLResponse:
        from pixie.web._overlays import overlay_totals

        views = _overlay_views_now(request)
        # Recent overlay-lifecycle events (created / reset / booted).
        # subject_kind is "machine" for all of them, so filter by the
        # ``overlay.`` kind prefix rather than subject_kind here.
        overlay_events = [
            e for e in request.app.state.events_log.list(limit=200) if e.kind.startswith("overlay.")
        ][:_RECENT_EVENTS_LIMIT]
        return templates.TemplateResponse(
            request,
            "overlays.html",
            {
                "version": pixie.__version__,
                "overlays": views,
                "totals": overlay_totals(views),
                "overlay_events": overlay_events,
                "authed": True,
                "page": "overlays",
            },
        )

    @app.get("/ui/overlays-live.json")
    def ui_overlays_live(
        request: Request,
        _auth: None = Depends(_require_ui_auth),
    ) -> JSONResponse:
        from pixie.web._overlays import overlay_totals

        store: SettingsStore = request.app.state.settings_store
        views = _overlay_views_now(request)
        rows: dict[str, dict[str, Any]] = {}
        for v in views:
            j = v.to_json()
            j["mtime_display"] = format_ts(v.mtime, store) if v.mtime else "-"
            rows[v.key] = j
        return JSONResponse({"rows": rows, "totals": overlay_totals(views).to_json()})

    @app.post("/ui/overlays/reset")
    def ui_overlays_reset(
        request: Request,
        alias: str = Form(...),
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        """Tear down a single overlay from the Overlays page. Same
        operation as the per-machine Reset button; redirects back to
        the overlays list instead of the machine detail."""
        _reset_overlay_row(request.app.state, alias)
        return RedirectResponse(url="/ui/overlays", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/ui/overlays/prune")
    def ui_overlays_prune(
        request: Request,
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        """Reclaim every overlay whose qcow2 is gone from disk or whose
        machine pixie no longer tracks. Deliberate ``idle`` keeps (a
        profile a live machine could rebind to) are left untouched."""
        pruned = 0
        for v in _overlay_views_now(request):
            if v.reclaimable and _reset_overlay_row(request.app.state, v.alias):
                pruned += 1
        events = getattr(request.app.state, "events_log", None)
        if events is not None and pruned:
            from pixie.events._kinds import OVERLAY_RESET

            events.emit(
                OVERLAY_RESET,
                subject_kind="overlay",
                subject_id="",
                summary=f"pruned {pruned} reclaimable overlay(s)",
                details={"pruned": pruned},
            )
        return RedirectResponse(url="/ui/overlays", status_code=status.HTTP_303_SEE_OTHER)

    # ---------- ui: images (materialised catalog content) -----------
    #
    # Catalog = sources (what you can fetch). An Image = the fetched
    # content, identity = the disk content sha; machines, exports, and
    # overlays all key off that sha, so this page is a group-by-sha
    # rollup: per image, its on-disk footprint + every live usage +
    # links into the per-usage admin surfaces.

    @app.get("/ui/images", response_class=HTMLResponse)
    def ui_images(
        request: Request,
        _auth: None = Depends(_require_ui_auth),
    ) -> HTMLResponse:
        from pixie.web._images import build_image_views

        state = request.app.state
        views = build_image_views(
            catalog=state.catalog_store,
            exports=state.exports_store,
            overlays=state.overlays_store,
            machines=state.machines_store,
            nbd=state.nbd_server,
        )
        return templates.TemplateResponse(
            request,
            "images.html",
            {
                "version": pixie.__version__,
                "images": views,
                "authed": True,
                "page": "images",
            },
        )

    def _image_view_for(request: Request, sha: str) -> Any:
        from pixie.web._images import build_image_views

        state = request.app.state
        views = build_image_views(
            catalog=state.catalog_store,
            exports=state.exports_store,
            overlays=state.overlays_store,
            machines=state.machines_store,
            nbd=state.nbd_server,
        )
        return next((v for v in views if v.sha == sha), None)

    @app.get("/ui/images/{sha}", response_model=None)
    def ui_image_detail(
        request: Request,
        sha: str,
        _auth: None = Depends(_require_ui_auth),
    ) -> HTMLResponse | RedirectResponse:
        image = _image_view_for(request, sha)
        if image is None:
            return RedirectResponse(url="/ui/images", status_code=status.HTTP_303_SEE_OTHER)
        store = request.app.state.catalog_store
        return templates.TemplateResponse(
            request,
            "image_detail.html",
            {
                "version": pixie.__version__,
                "image": image,
                "blob_path": str(store.blob_path(sha)),
                "authed": True,
                "page": "images",
            },
        )

    @app.post("/ui/images/{sha}/delete")
    def ui_image_delete(
        request: Request,
        sha: str,
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        """GC an image: delete its on-disk bytes (the whole ``blobs/<sha>/``
        dir -- raw disk AND the ``rootfs.raw`` the per-entry delete leaks)
        and clear the sha off every catalog entry that resolved to it.
        Refuses while anything still depends on it (machines / running
        export / overlays); the usage count IS the refcount. Orphan blobs
        (no catalog entry) delete straight through."""
        import shutil

        state = request.app.state
        image = _image_view_for(request, sha)
        if image is None or image.usage_count > 0:
            # Still in use (or already gone): bounce back, no-op.
            return RedirectResponse(url="/ui/images", status_code=status.HTTP_303_SEE_OTHER)

        store = state.catalog_store
        # Defensive: drop any (idle) export bound to the blob first.
        for exp in state.exports_store.list():
            if exp.content_sha256 == sha:
                state.nbd_server.terminate(exp.name)
                state.exports_store.delete(exp.name)
        # Remove the whole sha dir (blob + rootfs.raw), not just ``blob``.
        with contextlib_suppress(FileNotFoundError, OSError):
            shutil.rmtree(store.blob_path(sha).parent)
        # Clear the sha off every entry that pointed at it.
        cleared = 0
        for e in store.list_entries():
            if e.content_sha256 == sha:
                store.mark_unfetched(e.name)
                cleared += 1
        events = getattr(state, "events_log", None)
        if events is not None:
            events.emit(
                CATALOG_BLOB_DELETED,
                subject_kind="entry",
                subject_id=image.primary_name,
                summary=f"image GC: freed {image.footprint_bytes} bytes (sha {sha[:12]})",
                details={"sha": sha, "entries_cleared": cleared, "orphan": image.orphan},
            )
        return RedirectResponse(url="/ui/images", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/ui/machines/{mac}/labels/edit")
    def ui_machines_labels_edit(
        request: Request,
        mac: str,
        labels: str = Form(""),
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        """Edit the labels on one machine row. Independent of the
        bind form: labels are metadata (search, filter, grouping)
        and have no effect on the plan render, so a label edit does
        not need a boot_mode + image + target_disk_serial round-trip.

        Blank clears the labels. ``parse_labels`` enforces the same
        alphanumeric-leading shape the JSON PUT path does; a bad
        label surfaces as 400 (previously silently swallowed, which
        made "why did my label edit not take" a debug ratdance)."""
        from pixie.machines._store import BadMac, parse_labels

        try:
            parsed = parse_labels(labels)
            request.app.state.machines_store.set_labels(mac, parsed)
        except BadMac as exc:
            raise HTTPException(status_code=400, detail=f"invalid MAC: {exc}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url=f"/ui/machines/{mac}", status_code=status.HTTP_303_SEE_OTHER)

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
        kind: str = "",
        subject_kind: str = "",
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        sort: str = "",
        dir: str = "",
        _auth: None = Depends(_require_ui_auth),
    ) -> HTMLResponse:
        from pixie.events._kinds import KNOWN_EVENT_KINDS
        from pixie.web._table_state import (
            filter_rows,
            parse_pagination,
            parse_sort,
            sort_rows,
        )

        all_events = request.app.state.events_log.list(limit=2000)
        # Kind + subject_kind dropdowns are strict-equality filters
        # applied BEFORE the freeform ``q`` search, so a "delete all
        # entries" query narrowed to ``kind=catalog.entry.deleted`` doesn't
        # also drag in the ``catalog.import.ok`` rows whose summary
        # mentions "delete". Values are allowlisted against the event
        # kind registry + observed subject_kinds so an operator can't
        # accidentally hit the page with a bogus ``?kind=nope`` value
        # and see "0 of 0" without knowing why.
        kind_choices = sorted(KNOWN_EVENT_KINDS)
        subject_kind_choices = sorted({e.subject_kind for e in all_events if e.subject_kind})
        kind_selected = kind if kind in kind_choices else ""
        subject_kind_selected = subject_kind if subject_kind in subject_kind_choices else ""
        if kind_selected:
            all_events = [e for e in all_events if e.kind == kind_selected]
        if subject_kind_selected:
            all_events = [e for e in all_events if e.subject_kind == subject_kind_selected]
        filtered = filter_rows(
            all_events,
            q,
            fields=("kind", "subject_kind", "subject_id", "summary", "ts"),
        )
        sort_state = parse_sort(
            dict(request.query_params),
            allowed={
                "ts": "ts",
                "kind": "kind",
                "subject_id": "subject_id",
            },
            default_column="ts",
            default_direction="desc",
        )
        filtered = sort_rows(filtered, sort_state)
        page_state = parse_pagination(dict(request.query_params), total=len(filtered))
        page_events = filtered[page_state.offset : page_state.offset + page_state.per_page]
        preserved = {
            k: v
            for k, v in {
                "q": q,
                "kind": kind_selected,
                "subject_kind": subject_kind_selected,
                "sort": sort_state.column if sort_state.column != "ts" else "",
                "dir": sort_state.direction if sort_state.direction != "desc" else "",
                "per_page": str(page_state.per_page)
                if page_state.per_page != DEFAULT_PER_PAGE
                else "",
            }.items()
            if v
        }
        return templates.TemplateResponse(
            request,
            "events.html",
            {
                "version": pixie.__version__,
                "events": page_events,
                "q": q,
                "kind_choices": kind_choices,
                "kind_selected": kind_selected,
                "subject_kind_choices": subject_kind_choices,
                "subject_kind_selected": subject_kind_selected,
                "sort": sort_state,
                "page_state": page_state,
                "preserved": preserved,
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

    # ---------- dashboard + events live refresh ---------------------
    #
    # Two more polling endpoints in the same shape as fetch-states +
    # machines-live. The dashboard cards + recent-events table poll
    # these so an operator watching a target fetch complete or a
    # binding change sees the count tick without a page reload.

    def _dashboard_stats(request: Request) -> dict[str, Any]:
        """Same shape as ``ui_dashboard`` computes; factored so the
        JSON endpoint + the HTML render share the calculation."""
        from pixie.exports._routes import _refresh_row

        catalog = request.app.state.catalog_store
        exports_store = request.app.state.exports_store
        machines_store = request.app.state.machines_store
        events_log = request.app.state.events_log
        nbd = request.app.state.nbd_server
        entries = catalog.list_entries()
        exports = [_refresh_row(e, nbd, exports_store, events_log) for e in exports_store.list()]
        machines = machines_store.list()
        images = [e for e in entries if getattr(e, "bindable", False)]
        bundles = [e for e in entries if not getattr(e, "bindable", False)]
        # New model: Catalog = sources; Images = the materialised
        # entities (identity = disk sha) with a footprint + usage
        # refcount; Overlays = per-machine writable. Roll those up so
        # the dashboard speaks the same vocabulary as the nav.
        from pixie.web._images import build_image_views
        from pixie.web._inventory import humanize_bytes
        from pixie.web._overlays import build_overlay_views, overlay_totals

        overlays_store = request.app.state.overlays_store
        image_views = build_image_views(
            catalog=catalog,
            exports=exports_store,
            overlays=overlays_store,
            machines=machines_store,
            nbd=nbd,
        )
        images_bytes = sum(v.footprint_bytes for v in image_views)
        images_reclaimable = sum(1 for v in image_views if not v.in_use)
        ov_tot = overlay_totals(
            build_overlay_views(
                overlays=overlays_store, machines=machines_store, catalog=catalog, nbd=nbd
            )
        )
        # Live-env media state. ``pixie-tui`` / ``pixie-inventory`` /
        # ``pixie-flash-*`` all need vmlinuz + initrd + live.squashfs
        # staged under ``PIXIE_LIVE_ENV_DIR`` (defaults to
        # ``<state>/live-env``). Surface the staged / missing signal
        # + per-file byte counts so the operator can tell at a glance
        # whether the pixie boot modes are live or would fall back to
        # the "unavailable" plan.
        live_env_dir = request.app.state.live_env_dir
        live_env_ready = False
        live_env_files: dict[str, int | None] = {
            "vmlinuz": None,
            "initrd": None,
            "live.squashfs": None,
        }
        if live_env_dir is not None:
            for name in live_env_files:
                p = live_env_dir / name
                try:
                    live_env_files[name] = p.stat().st_size if p.is_file() else None
                except OSError:
                    live_env_files[name] = None
            live_env_ready = all(v is not None for v in live_env_files.values())
        # Events summary: total row count + unacknowledged error
        # count + newest ts. ``events_ack_ts`` is a soft cursor an
        # operator advances by hitting Acknowledge on the dashboard;
        # events with ts > that value count towards ``error_count``,
        # everything older is treated as read.
        settings_store = request.app.state.settings_store
        events_ack_ts = settings_store.get("events_ack_ts") or ""
        events_stats = events_log.stats(ack_ts=events_ack_ts)
        return {
            "machines_total": len(machines),
            "machines_bound": sum(1 for m in machines if m.image_content_sha256),
            "machines_with_inventory": sum(1 for m in machines if m.inventory),
            "catalog_total": len(entries),
            "catalog_fetched": sum(1 for e in entries if getattr(e, "content_sha256", "")),
            "catalog_unfetched": sum(1 for e in entries if not getattr(e, "content_sha256", "")),
            "catalog_images_total": len(images),
            "catalog_images_fetched": sum(1 for e in images if e.content_sha256),
            "catalog_bundles_total": len(bundles),
            "catalog_bundles_fetched": sum(1 for e in bundles if e.content_sha256),
            "images_count": len(image_views),
            "images_bytes": images_bytes,
            "images_bytes_display": humanize_bytes(images_bytes),
            "images_reclaimable": images_reclaimable,
            "overlays_count": ov_tot.count,
            "overlays_bytes": ov_tot.used_bytes,
            "overlays_bytes_display": humanize_bytes(ov_tot.used_bytes),
            "overlays_running": ov_tot.running,
            "exports_total": len(exports),
            "exports_running": sum(1 for e in exports if e.status == "running"),
            "exports_error": sum(1 for e in exports if e.status == "error"),
            "events_total": events_stats["total"],
            "events_error_count": events_stats["error_count"],
            "events_last_ts": events_stats["last_ts"],
            "events_ack_ts": events_ack_ts,
            "live_env_ready": live_env_ready,
            "live_env_dir": str(live_env_dir) if live_env_dir is not None else "",
            "live_env_files": live_env_files,
            "live_env_src": request.app.state.settings_store.resolve_live_env_src(),
            "live_env_fetch_state": dict(request.app.state.live_env_fetch_state),
        }

    @app.get("/ui/dashboard-live.json")
    def ui_dashboard_live(
        request: Request,
        _auth: None = Depends(_require_ui_auth),
    ) -> JSONResponse:
        return JSONResponse(_dashboard_stats(request))

    @app.get("/ui/events-live.json")
    def ui_events_live(
        request: Request,
        since_ts: str = "",
        limit: int = 25,
        _auth: None = Depends(_require_ui_auth),
    ) -> JSONResponse:
        """Return the N most recent events. Optional ``since_ts``
        (a raw ISO string the caller got from a previous poll) trims
        to rows strictly newer than that stamp so the JS can insert
        just the new rows into the log. The clamp on ``limit``
        protects the endpoint against a runaway ``?limit=99999``."""
        limit = max(1, min(limit, 200))
        settings_store: SettingsStore = request.app.state.settings_store
        events = request.app.state.events_log.list(limit=limit)
        out: list[dict[str, Any]] = []
        for e in events:
            if since_ts and e.ts <= since_ts:
                continue
            out.append(
                {
                    "ts": e.ts,
                    "ts_display": format_ts(e.ts, settings_store),
                    "kind": e.kind,
                    "subject_kind": e.subject_kind,
                    "subject_id": e.subject_id,
                    "summary": e.summary or "",
                }
            )
        return JSONResponse({"events": out})

    @app.post("/ui/events/ack")
    def ui_events_ack(
        request: Request,
        next: str = Form("/ui/"),
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        """Advance the events-ack cursor to now so the dashboard's
        error count zeros out. Kept as a bulk-ack (no per-event
        checkbox) because the intent is "operator has seen the
        current state, silence the counter until something new
        fails" -- a per-row ack would need a per-row column and a
        write for each acknowledgement. Row-level filtering /
        searching still happens on ``/ui/events`` via the existing
        kind + subject_kind pickers.

        ``next`` lets the caller pick the landing page: the dashboard
        button omits it (default /ui/); the events page passes
        /ui/events so the operator stays put. Constrained to a /ui/
        path so the field can't be turned into an open redirect.
        """
        from pixie._util import now_iso as _ack_now

        request.app.state.settings_store.set_value("events_ack_ts", _ack_now())
        target = next if next.startswith("/ui/") else "/ui/"
        return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/ui/events/clear")
    def ui_events_clear(
        request: Request,
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        """Wipe the whole event log, then drop one ``events.cleared``
        marker so the freshly-empty log still records the reset. Also
        advances the ack cursor: with the history gone there's nothing
        left to be unacknowledged, so the dashboard error count zeros.
        Destructive; the events page gates the button behind a confirm
        dialog."""
        from pixie._util import now_iso as _ack_now

        removed = request.app.state.events_log.clear()
        request.app.state.events_log.emit(
            EVENTS_CLEARED,
            summary=f"event log cleared ({removed} rows)",
            details={"removed": removed},
        )
        request.app.state.settings_store.set_value("events_ack_ts", _ack_now())
        return RedirectResponse(url="/ui/events", status_code=status.HTTP_303_SEE_OTHER)

    # ---------- live fetch progress ---------------------------------
    #
    # Small JSON echo of ``app.state.fetch_states``. The catalog page
    # polls this while any row is in flight so the operator sees
    # ``downloading 42 / 512 MiB`` -> ``decompressing`` -> ``unpacking``
    # -> ``done`` without a full page reload. Auth-required because
    # the payload names catalog entries; not sensitive by itself but
    # part of the admin-only surface.

    @app.get("/ui/fetch-states.json")
    def ui_catalog_fetch_states(
        request: Request,
        _auth: None = Depends(_require_ui_auth),
    ) -> JSONResponse:
        return JSONResponse(dict(request.app.state.fetch_states))

    # ---------- machines live refresh --------------------------------
    #
    # Compact JSON of the operator-visible per-machine fields the
    # list + detail templates render live. The machines page + detail
    # page poll this so a target booting into nbdboot updates
    # ``last_seen_at`` + ``last_seen_ip`` + inventory-disks count
    # without a page reload. Keyed by MAC so the JS updates the
    # matching row in place. Auth-required because the payload names
    # machines by MAC.

    @app.get("/ui/machines-live.json")
    def ui_machines_live(
        request: Request,
        _auth: None = Depends(_require_ui_auth),
    ) -> JSONResponse:
        store: SettingsStore = request.app.state.settings_store
        out: dict[str, dict[str, Any]] = {}
        for m in request.app.state.machines_store.list():
            disks = (m.inventory or {}).get("disks") or []
            # Pre-formatted timestamps let the JS drop cells into the
            # DOM verbatim + stay consistent with the server-rendered
            # fmt_ts filter (same timezone + strftime picks from
            # Settings). Raw ISO stays alongside in case the browser
            # ever wants to compute "time since" on the client.
            out[m.mac] = {
                "boot_mode": m.boot_mode,
                "image_content_sha256": m.image_content_sha256,
                "labels": list(m.labels),
                "last_seen_at": m.last_seen_at,
                "last_seen_at_display": format_ts(m.last_seen_at, store),
                "last_seen_ip": m.last_seen_ip,
                "inventory_at": m.inventory_at or "",
                "inventory_at_display": format_ts(m.inventory_at or "", store),
                "disks_count": len(disks) if isinstance(disks, list) else 0,
                "has_lshw": bool((m.inventory or {}).get("lshw")),
                "target_disk_serial": m.target_disk_serial,
            }
        return JSONResponse(out)

    # ---------- ui: catalog admin forms ------------------------------
    #
    # These forms redirect back to /ui/ so an operator's browser stays
    # on the dashboard after each mutation. Behaviour mirrors the JSON
    # /catalog routes but with form-encoded input + 303 redirect.

    from pixie._util import now_iso as _now_iso
    from pixie.catalog._fetcher import FetchError
    from pixie.catalog._fetcher import fetch as _fetch
    from pixie.catalog._schema import CatalogEntry as _Entry

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
        entry = _Entry(
            name=name.strip(),
            src=src.strip(),
            format=format.strip(),
            arch=arch.strip(),
            description=description.strip(),
            netboot_src=netboot_src.strip(),
            added_at=_now_iso(),
        )
        store.upsert(entry)
        events = getattr(request.app.state, "events_log", None)
        if events is not None:
            events.emit(
                CATALOG_ENTRY_ADDED,
                subject_kind="entry",
                subject_id=entry.name,
                summary=f"{entry.name} ({entry.format})",
                details={"src": entry.src},
            )
        return RedirectResponse(url="/ui/catalog", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/ui/catalog/fetch")
    def ui_catalog_fetch(
        request: Request,
        name: str = Form(...),
        force: str = Form(""),
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        store = request.app.state.catalog_store
        entry = store.get_entry(name)
        if entry is None:
            return RedirectResponse(url="/ui/catalog", status_code=status.HTTP_303_SEE_OTHER)

        # Update-fetch guard. A fetch on an already-fetched entry
        # ("Update" in the UI) re-runs the pipeline; if the sha
        # shifts (moved oras:// tag, upstream re-tag) any machine
        # currently bound to the OLD sha silently rots. Bounce to
        # the detail page with warn_update=1 unless the operator
        # explicitly opts in via force=1 (from the banner).
        # A pristine fetch (no content_sha256 yet) skips the guard --
        # the entry is not in use so there is nothing to warn about.
        if entry.content_sha256 and not force:
            using_machines = [
                m.mac
                for m in request.app.state.machines_store.list()
                if m.image_content_sha256 == entry.content_sha256
            ]
            running_exports = [
                e.name
                for e in request.app.state.exports_store.list()
                if e.content_sha256 == entry.content_sha256 and e.status == "running"
            ]
            if using_machines or running_exports:
                return RedirectResponse(
                    url=f"/ui/catalog/{name}?warn_update=1",
                    status_code=status.HTTP_303_SEE_OTHER,
                )

        states = request.app.state.fetch_states
        if states.get(name, {}).get("state") == "fetching":
            return RedirectResponse(url="/ui/catalog", status_code=status.HTTP_303_SEE_OTHER)
        is_update = bool(entry.content_sha256)
        events = getattr(request.app.state, "events_log", None)

        # Unchanged fast path: if this is an Update click on an
        # entry that IS already fetched AND its blob (or unpacked
        # artifact set) is still on disk, the fetcher would take
        # its own fast path and return immediately -- the operator
        # sees no visible change, only a millisecond flicker on
        # the state pill. Detect that here and record a short-lived
        # "unchanged" acknowledgement instead so the row shows
        # "already at latest" for ~8 s and the events log carries
        # the operator's click as a catalog.fetch.unchanged event.
        if is_update and _fetch_would_be_noop(entry, store):
            states[name] = {
                "state": "unchanged",
                "at_iso": _now_iso(),
                "content_sha256": entry.content_sha256,
            }
            if events is not None:
                events.emit(
                    CATALOG_FETCH_UNCHANGED,
                    subject_kind="entry",
                    subject_id=name,
                    summary=f"{name}: already at latest (sha {entry.content_sha256[:12]})",
                    details={"content_sha256": entry.content_sha256},
                )

            # Auto-clear the acknowledgement after a short window so
            # a page revisit an hour later doesn't still show it. Uses
            # the fetch pool as the timer host (a real fetch would use
            # a pool slot too, so budgeting matches).
            def _clear_unchanged() -> None:
                import time as _time

                _time.sleep(8.0)
                cur = states.get(name)
                if cur is not None and cur.get("state") == "unchanged":
                    states.pop(name, None)

            request.app.state.fetch_pool.submit(_clear_unchanged)
            return RedirectResponse(url="/ui/catalog", status_code=status.HTTP_303_SEE_OTHER)

        states[name] = {"state": "fetching", "started_at": _now_iso(), "error": None}
        if events is not None:
            events.emit(
                CATALOG_FETCH_STARTED,
                subject_kind="entry",
                subject_id=name,
                summary=f"{name} <- {entry.src}",
                details={"src": entry.src, "update": is_update},
            )

        def _report(payload: dict[str, Any]) -> None:
            # Merge each phase transition into the row's live state so
            # the UI polling endpoint (/ui/catalog/fetch-states.json)
            # sees ``phase`` + ``bytes_downloaded`` / ``total_bytes``
            # (during downloading) or ``format`` (during
            # decompressing). We keep ``state=='fetching'`` throughout
            # so existing "is this row in flight?" checks (the button-
            # disable in catalog.html, the fresh-fetch guard above)
            # still fire while the phase spins through its stages.
            row = states.get(name) or {}
            merged: dict[str, Any] = {
                "state": "fetching",
                "started_at": row.get("started_at"),
                "error": None,
            }
            merged.update(payload)
            states[name] = merged

        def _run() -> None:
            try:
                result = _fetch(entry, store, progress=_report)
                states[name] = {"state": "done", "started_at": states[name].get("started_at")}
                if events is not None:
                    events.emit(
                        CATALOG_FETCH_DONE,
                        subject_kind="entry",
                        subject_id=name,
                        summary=(
                            f"{name}: {result.size_bytes} bytes, sha {result.content_sha256[:12]}"
                        ),
                        details={
                            "content_sha256": result.content_sha256,
                            "size_bytes": result.size_bytes,
                        },
                    )
                    if is_update:
                        events.emit(
                            CATALOG_ENTRY_UPDATED,
                            subject_kind="entry",
                            subject_id=name,
                            summary=f"{name}: bytes refreshed",
                            details={"content_sha256": result.content_sha256},
                        )
            except FetchError as exc:
                states[name] = {
                    "state": "error",
                    "started_at": states[name].get("started_at"),
                    "error": str(exc),
                }
                if events is not None:
                    events.emit(
                        CATALOG_FETCH_FAILED,
                        subject_kind="entry",
                        subject_id=name,
                        summary=str(exc),
                        details={"error": str(exc)[:200]},
                    )
            except Exception as exc:  # pragma: no cover -- defensive
                states[name] = {
                    "state": "error",
                    "started_at": states[name].get("started_at"),
                    "error": f"internal: {exc}",
                }
                if events is not None:
                    events.emit(
                        CATALOG_FETCH_FAILED,
                        subject_kind="entry",
                        subject_id=name,
                        summary=f"internal: {exc}",
                        details={"error": f"internal: {exc}"[:200]},
                    )

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
            breaks_nbdboot_for: list[str] = []
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
                # Deleting a bundle breaks nbdboot for every disk
                # image whose netboot_src pointed at it.
                if entry.src:
                    breaks_nbdboot_for = [e.name for e in all_entries if e.netboot_src == entry.src]
            if breaks_nbdboot_for or orphans_bundle:
                # Bounce with a marker so /ui/catalog/<name> can
                # render the warning inline.
                return RedirectResponse(
                    url=f"/ui/catalog/{name}?warn_delete=1",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
        store.delete(name)
        request.app.state.fetch_states.pop(name, None)
        events = getattr(request.app.state, "events_log", None)
        if events is not None:
            events.emit(
                CATALOG_ENTRY_DELETED,
                subject_kind="entry",
                subject_id=name,
                summary=name,
                details={"forced": bool(force)},
            )
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
        entry.content_sha256`` (i.e. is bound to nbdboot for this
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
                CATALOG_BLOB_DELETED,
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
        except (httpx.HTTPError, ValueError) as exc:
            log = getattr(request.app.state, "events_log", None)
            if log is not None:
                log.emit(
                    CATALOG_IMPORT_FAILED,
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
                CATALOG_IMPORT_OK,
                subject_kind="catalog",
                subject_id=target_url,
                summary=f"imported {len(entries)} entries from {target_url} ({added} new)",
                details={"url": target_url, "count": len(entries), "new": added},
            )
        return RedirectResponse(url="/ui/catalog", status_code=status.HTTP_303_SEE_OTHER)

    # ---------- settings pane ---------------------------------------

    def _settings_context(request: Request, flash_error: str | None = None) -> dict[str, Any]:
        """Build the render context for /ui/settings. Each row exposes
        the effective value (what pixie will use), the stored override
        (blank when unset), and the source bucket (override / env /
        default) so the operator sees the provenance chain at a
        glance."""
        store: SettingsStore = request.app.state.settings_store
        tz_override = store.get(KEY_DISPLAY_TZ) or ""
        try:
            tz_effective = str(store.resolve_display_timezone())
        except SettingValueError as exc:
            tz_effective = f"(invalid: {exc})"
        fmt_override = store.get(KEY_DATETIME_FORMAT) or ""
        fmt_effective = store.resolve_datetime_format()
        deployment_envvars = _deployment_envvar_docs()
        deployment_state = _deployment_state()
        return {
            "version": pixie.__version__,
            "authed": True,
            "page": "settings",
            "deployment_envvars": deployment_envvars,
            "deployment": deployment_state,
            "display_tz": {
                "override": tz_override,
                "effective": tz_effective,
                "default": "UTC",
                "env": "PIXIE_DISPLAY_TZ",
                "updated_at": store.updated_at(KEY_DISPLAY_TZ) or "",
            },
            "datetime_format": {
                "override": fmt_override,
                "effective": fmt_effective,
                "default": "%Y-%m-%d %H:%M:%S %Z",
                "env": "PIXIE_DATETIME_FORMAT",
                "updated_at": store.updated_at(KEY_DATETIME_FORMAT) or "",
            },
            "flash_error": flash_error,
        }

    @app.get("/ui/settings", response_class=HTMLResponse)
    def ui_settings(
        request: Request,
        _auth: None = Depends(_require_ui_auth),
    ) -> HTMLResponse:
        return templates.TemplateResponse(request, "settings.html", _settings_context(request))

    @app.post("/ui/settings/display/edit", response_model=None)
    def ui_settings_display_edit(
        request: Request,
        timezone: str = Form(""),
        datetime_format: str = Form(""),
        _auth: None = Depends(_require_ui_auth),
    ) -> HTMLResponse | RedirectResponse:
        """Persist the two Display settings. Blank inputs CLEAR the
        override so the value falls back to env / default. Both fields
        are validated BEFORE any write so a bad tz + a good format
        don't leave the DB in a half-updated state."""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        store: SettingsStore = request.app.state.settings_store
        tz_raw = (timezone or "").strip()
        fmt_raw = (datetime_format or "").strip()
        # Validate tz + fmt up-front so a bad value on either side
        # rejects the whole submit rather than partially applying.
        if tz_raw:
            try:
                ZoneInfo(tz_raw)
            except ZoneInfoNotFoundError:
                return templates.TemplateResponse(
                    request,
                    "settings.html",
                    _settings_context(
                        request,
                        flash_error=f"'{tz_raw}' is not a known IANA timezone.",
                    ),
                    status_code=400,
                )
        if fmt_raw:
            try:
                datetime.now(UTC).strftime(fmt_raw)
            except ValueError as exc:
                return templates.TemplateResponse(
                    request,
                    "settings.html",
                    _settings_context(
                        request,
                        flash_error=f"invalid datetime format: {exc}",
                    ),
                    status_code=400,
                )
        if tz_raw:
            store.set_value(KEY_DISPLAY_TZ, tz_raw)
        else:
            store.clear(KEY_DISPLAY_TZ)
        if fmt_raw:
            store.set_value(KEY_DATETIME_FORMAT, fmt_raw)
        else:
            store.clear(KEY_DATETIME_FORMAT)
        return RedirectResponse(url="/ui/settings", status_code=status.HTTP_303_SEE_OTHER)

    def _live_env_context(request: Request, flash_error: str | None = None) -> dict[str, Any]:
        """Render context for the dedicated /ui/live-env pane: staged
        media state (ready + per-file sizes), the fetch source + extra
        cmdline overrides (with provenance), and any in-flight fetch."""
        state = request.app.state
        store: SettingsStore = state.settings_store
        live_env_dir = state.live_env_dir
        files: dict[str, int | None] = {"vmlinuz": None, "initrd": None, "live.squashfs": None}
        if live_env_dir is not None:
            for name in files:
                p = live_env_dir / name
                try:
                    files[name] = p.stat().st_size if p.is_file() else None
                except OSError:
                    files[name] = None
        return {
            "version": pixie.__version__,
            "authed": True,
            "page": "live-env",
            "live_env_ready": all(v is not None for v in files.values()),
            "live_env_dir": str(live_env_dir) if live_env_dir is not None else "",
            "live_env_files": files,
            "live_env_fetch_state": dict(state.live_env_fetch_state),
            "live_env_src": {
                "override": store.get(KEY_LIVE_ENV_SRC) or "",
                "effective": store.resolve_live_env_src(),
                "default": DEFAULT_LIVE_ENV_SRC,
                "env": "PIXIE_LIVE_ENV_SRC",
                "updated_at": store.updated_at(KEY_LIVE_ENV_SRC) or "",
            },
            "live_env_extra_cmdline": {
                "override": store.get(KEY_LIVE_ENV_EXTRA_CMDLINE) or "",
                "effective": store.resolve_live_env_extra_cmdline(),
                "default": "",
                "env": "PIXIE_LIVE_ENV_EXTRA_CMDLINE",
                "updated_at": store.updated_at(KEY_LIVE_ENV_EXTRA_CMDLINE) or "",
            },
            "flash_error": flash_error,
        }

    @app.get("/ui/live-env", response_class=HTMLResponse)
    def ui_live_env(
        request: Request,
        _auth: None = Depends(_require_ui_auth),
    ) -> HTMLResponse:
        return templates.TemplateResponse(request, "live_env.html", _live_env_context(request))

    @app.post("/ui/live-env/cmdline/edit", response_model=None)
    def ui_live_env_cmdline_edit(
        request: Request,
        extra_cmdline: str = Form(""),
        _auth: None = Depends(_require_ui_auth),
    ) -> HTMLResponse | RedirectResponse:
        """Persist the live-env extra cmdline. Blank clears the
        override so the value falls back to $PIXIE_LIVE_ENV_EXTRA_CMDLINE
        then empty. Rejects any newline in the input -- the tokens go
        onto a single-line iPXE ``kernel`` directive and a newline
        would truncate the render before the ``initrd``/``boot`` lines
        that follow."""
        store: SettingsStore = request.app.state.settings_store
        raw = (extra_cmdline or "").strip()
        if "\n" in raw or "\r" in raw:
            return templates.TemplateResponse(
                request,
                "live_env.html",
                _live_env_context(
                    request,
                    flash_error=(
                        "Live-env extra cmdline must be a single line "
                        "(newlines truncate the iPXE render)."
                    ),
                ),
                status_code=400,
            )
        if raw:
            store.set_value(KEY_LIVE_ENV_EXTRA_CMDLINE, raw)
        else:
            store.clear(KEY_LIVE_ENV_EXTRA_CMDLINE)
        return RedirectResponse(url="/ui/live-env", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/ui/live-env/src/edit", response_model=None)
    def ui_live_env_src_edit(
        request: Request,
        live_env_src: str = Form(""),
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        """Persist the live-env fetch-src override. Blank clears it so
        the value falls back to $PIXIE_LIVE_ENV_SRC then the default
        GitHub-release URL."""
        store: SettingsStore = request.app.state.settings_store
        raw = (live_env_src or "").strip()
        if raw:
            store.set_value(KEY_LIVE_ENV_SRC, raw)
        else:
            store.clear(KEY_LIVE_ENV_SRC)
        return RedirectResponse(url="/ui/live-env", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/ui/live-env/fetch")
    def ui_live_env_fetch(
        request: Request,
        _auth: None = Depends(_require_ui_auth),
    ) -> RedirectResponse:
        """Download the live-env tarball (from PIXIE_LIVE_ENV_SRC or its
        override) and stage vmlinuz + initrd + live.squashfs under
        PIXIE_LIVE_ENV_DIR, on the fetch pool. The in-app replacement
        for a manual ``make build VARIANT=netboot-pc`` + copy. Progress
        lands on ``app.state.live_env_fetch_state`` for the dashboard to
        poll; idempotent while a fetch is already running."""
        state = request.app.state
        live_env_dir = state.live_env_dir
        fs = state.live_env_fetch_state
        if live_env_dir is None:
            fs.clear()
            fs.update(
                {
                    "state": "error",
                    "error": "no live-env dir (PIXIE_LIVE_ENV_DIR unset or unwritable)",
                    "at_iso": _now_iso(),
                }
            )
            return RedirectResponse(url="/ui/live-env", status_code=status.HTTP_303_SEE_OTHER)
        if fs.get("state") == "fetching":
            return RedirectResponse(url="/ui/live-env", status_code=status.HTTP_303_SEE_OTHER)

        src = state.settings_store.resolve_live_env_src()
        fs.clear()
        fs.update({"state": "fetching", "src": src, "phase": "starting", "at_iso": _now_iso()})
        events = getattr(state, "events_log", None)

        def _run() -> None:
            from pixie.catalog._live_env import stage_live_env

            def _report(payload: dict[str, Any]) -> None:
                # Layer phase / byte counters onto the "fetching" pill.
                fs.update(payload)

            try:
                result = stage_live_env(src, live_env_dir, progress=_report)
            except Exception as exc:
                fs.clear()
                fs.update({"state": "error", "src": src, "error": str(exc), "at_iso": _now_iso()})
                if events is not None:
                    events.emit(
                        LIVE_ENV_FETCH_FAILED,
                        summary=f"live-env fetch failed: {exc}",
                        details={"src": src, "error": str(exc)},
                    )
                return
            fs.clear()
            fs.update({"state": "done", "src": src, "sha256": result.sha256, "at_iso": _now_iso()})
            if events is not None:
                events.emit(
                    LIVE_ENV_FETCH_DONE,
                    summary=f"live-env staged from {src} (sha {result.sha256[:12]})",
                    details={"src": src, "sha256": result.sha256, "bytes": result.size},
                )

        state.fetch_pool.submit(_run)
        return RedirectResponse(url="/ui/live-env", status_code=status.HTTP_303_SEE_OTHER)

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
