"""pixie.tui - terminal UI for image inspection and flashing.

Rich-based. No Textual. No event loop. No alt-screen.

Each "screen" is a sequence of Rich-rendered Panels followed by a
``Prompt.ask`` input. Boring, stable, fast: the screen draws once
(~30-100ms on a kernel framebuffer console), the operator types a
choice, the next screen draws. No reactive properties, no compose
tree, no CSS cascading, no DataTable layout passes.

The wizard flow is a plain Python ``while True`` loop dispatching
on the current ``_WizardStage``. Esc-back-nav is the literal
``b`` / ``back`` token returned from the prompt; Enter-to-advance
is the number the operator types.

Performance design notes:

  * Rich prints are synchronous one-shot writes -- no per-frame
    diffing, no allocation of intermediate render trees.
  * The only Live-update region is the flash-progress bar, and
    even that is bounded: one update per second from the
    ``FlashProgress`` callback, no more.
  * Lists are static tables rendered once. Filter? The operator
    reads the list; on framebuffer console with <30 entries the
    cost of "live filter" isn't worth its complexity.
  * No modal overlays: "confirm before flash" is a panel printed
    inline, followed by a y/N prompt. Visually identical to the
    operator (one focused question at a time); zero alt-screen
    overhead.

Catalog sources (same as the old Textual UI):

  * Local image-root (always scanned).
  * Optional ``--catalog SOURCE`` overlay (local TOML, http(s),
    or oras://).

PXE-interactive use: ``--catalog http://pixie:8080/catalog.toml``
plus ``--mac <MAC>`` so the TUI POSTs ``/pxe/<mac>/status`` after a
flash (derived from the catalog URL's scheme+host).

Public surface preserved from the prior Textual implementation:

  * ``BtyTui`` class with ``run()``.
  * ``_TuiImage`` dataclass (catalog row shape).
  * ``load_catalog_from_source(...)``,
    ``post_pxe_status(...)``, ``post_inventory(...)`` helpers.
  * ``_format_mib``, ``_parse_size_to_bytes`` formatters.
  * ``_WizardStage`` enum.

This module no longer imports textual; pure-rich + stdlib.
"""

from __future__ import annotations

import contextlib
import json as _json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.prompt import Prompt
from rich.table import Table

import pixie
from pixie import disks, flash, images
from pixie import tui_catalog as _catalog
from pixie.tui import DEFAULT_SERVER as _DEFAULT_SERVER

# ---------------------------------------------------------------------------
# Public-API helpers (preserved across the textual -> rich rewrite so
# external callers + the test suite's model layer don't have to change).
# ---------------------------------------------------------------------------


class _WizardStage(IntEnum):
    """The five wizard stages, derived from selection state.

    Forward advance: an operator commit (Enter on an image / disk
    / confirm) sets the corresponding state field, which flips the
    derived stage. Esc / ``b`` back-nav clears the most-recent
    commit, dropping the stage by one.

    ``SELECT_CATALOG`` is auto-skipped on startup when either a
    ``--catalog`` URL was passed OR the local image-root already
    contains images -- the operator has data to work with, no
    catalog selection needed up front. Back-nav from ``SELECT_IMAGE``
    re-enters ``SELECT_CATALOG`` so the operator can switch source
    mid-session (e.g. plug-in to a USB stick but then realize they
    want the remote release catalog).
    """

    SELECT_CATALOG = 1
    SELECT_IMAGE = 2
    SELECT_DISK = 3
    CONFIRM_FLASH = 4
    REBOOT_OR_DONE = 5


@dataclass
class _TuiImage:
    """Unified catalog row.

    Either ``path`` (local file) or ``url`` (remote / oras / catalog
    pointer) is populated. The rest of the TUI consumes this
    shape uniformly so local + remote sources blend into one list.
    """

    name: str
    fmt: str | None
    size_bytes: int
    path: Path | None = None
    url: str | None = None
    # Declared content sha256 (bare hex) for a URL source, when the
    # catalog or PXE plan committed to one. Threaded into the flash so
    # the bytes are verified on the wire. ``None`` for local files and
    # sources with no declared digest.
    sha: str | None = None
    # Informational architecture hint (``x86_64`` / ``arm64`` / ...).
    # Shown as a column in the image table; never restricts flash
    # eligibility -- pixie writes whatever bytes the operator picks.
    arch: str | None = None


def _normalise_server_url(server: str) -> str:
    """Turn a bare hostname or full URL into a normalised base URL.

    Operator convenience: ``--server pixie`` should work as
    well as ``--server http://pixie:8080``. When no scheme is
    present we default to ``http://``. Trailing slashes are stripped
    so concatenations like ``f"{server}/pxe/{mac}/plan"`` produce
    a clean path.
    """
    server = server.strip().rstrip("/")
    if "://" not in server:
        server = f"http://{server}"
    return server


def _basename_from_url(url: str) -> str | None:
    """Last path segment of ``url``, url-decoded, or ``None`` if the
    URL has no useful filename component.

    Used by the auto-flash path to derive a display name for the
    image being flashed. The URL may have a query string and
    arbitrary percent-encoded characters; we want the human-
    readable last segment.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    name = Path(parsed.path).name
    if not name:
        return None
    return urllib.parse.unquote(name)


def _format_mib(size_bytes: int | None) -> str:
    """Format a byte count as comma-grouped MiB.

    Negative / None render as ``?`` so a probe that couldn't
    determine a virtual size (e.g. a streamed raw URL whose
    Content-Length the server didn't advertise) shows a clean
    placeholder rather than crashing the prompt.
    """
    if size_bytes is None or size_bytes < 0:
        return "?"
    return f"{size_bytes / (1 << 20):,.1f} MiB"


_SIZE_SUFFIX_MULTIPLIERS = {
    "K": 1 << 10,
    "M": 1 << 20,
    "G": 1 << 30,
    "T": 1 << 40,
    "P": 1 << 50,
}


def _parse_size_to_bytes(s: str) -> int:
    """Parse an lsblk-style human-readable size (``500G``, ``1.5T``)
    to bytes. Empty / unrecognised input returns 0 (caller can
    render as ``?``).
    """
    s = s.strip().upper()
    if not s:
        return 0
    if s[-1] in _SIZE_SUFFIX_MULTIPLIERS:
        try:
            n = float(s[:-1])
        except ValueError:
            return 0
        return int(n * _SIZE_SUFFIX_MULTIPLIERS[s[-1]])
    try:
        return int(s)
    except ValueError:
        return 0


def load_catalog_from_source(source: str, *, timeout: float = 30.0) -> list[_TuiImage]:
    """Load catalog rows from a path / URL into the TUI shape.

    Thin projection over :func:`pixie.catalog.load_source`. Same
    accepted sources as before: local TOML path, http(s):// URL,
    oras:// reference.
    """
    parsed_catalog = _catalog.load_source(source, timeout=timeout)
    return [
        _TuiImage(
            name=entry.name,
            fmt=entry.format,
            size_bytes=entry.size_bytes or 0,
            url=entry.src,
            sha=entry.sha256,
            arch=entry.arch,
        )
        for entry in parsed_catalog.entries
    ]


def post_pxe_status(
    pxe_done_base: str, mac: str, status: str, reason: str = "", *, timeout: float = 10.0
) -> None:
    """POST ``<base>/pxe/{mac}/status`` with ``{"status": ..., "reason": ...}``
    -- the live env's terminal flash signal.

    ``status`` is ``"done"`` or ``"failed"``; ``reason`` (optional, capped)
    rides only with a failure so the pixie can show it on the machine's
    timeline instead of the box sitting at "awaiting flash" forever. Silent
    on success; raises ``urllib.error.URLError`` on transport failure (callers
    decide whether to surface, the failure paths wrap in
    ``contextlib.suppress``).
    """
    base = pxe_done_base.rstrip("/")
    payload: dict[str, str] = {"status": status}
    reason = reason.strip()
    if reason:
        payload["reason"] = reason[:500]
    data = _json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/pxe/{mac}/status",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout):
        pass


def post_inventory(
    pxe_done_base: str,
    mac: str,
    disks_payload: list[dict[str, object]],
    *,
    lshw: object | None = None,
    timeout: float = 10.0,
) -> None:
    """POST ``<pxe_done_base>/pxe/{mac}/inventory`` with the live env's
    local disk inventory, plus the optional full ``lshw -json`` tree.

    ``lshw`` (when supplied) is the parsed JSON from ``lshw -json``;
    pixie stores it as a supplementary hardware blob. The flasher
    never consumes it -- ``disks_payload`` (from lsblk) is the contract.
    """
    base = pxe_done_base.rstrip("/")
    payload: dict[str, object] = {"disks": disks_payload}
    if lshw is not None:
        payload["lshw"] = lshw
    body = _json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/pxe/{mac}/inventory",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout):
        pass


def collect_lshw(*, timeout: float = 30.0) -> object | None:
    """Best-effort full hardware inventory via ``lshw -json``.

    Returns the parsed JSON (lshw emits an object, or a list on some
    versions), or ``None`` if lshw is missing, errors, times out, or
    emits unparseable output. Bounded so a slow probe can't wedge the
    inventory post; lshw needs root, which the live env has.
    """
    try:
        proc = subprocess.run(
            ["lshw", "-json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        parsed: object = _json.loads(proc.stdout)
    except (ValueError, TypeError):
        return None
    return parsed


# v0.46 stopped publishing a pixie-side catalog.toml mirror and pointed
# pixie at the upstream image-builder (``safl/nosi``); the wizard's
# ``[d] default`` shortcut tracks the same source so both consumers
# resolve to one catalog. The pixie release no longer ships a
# catalog.toml asset, so the old URL would 404.
_BTY_DEFAULT_CATALOG_URL = "https://github.com/safl/nosi/releases/latest/download/catalog.toml"


# ---------------------------------------------------------------------------
# Rendering style: blue/gray dominant, with a sparing dash of
# muted yellow. Pure-text fallback works on the framebuffer
# console too; the colour bytes are ANSI escapes the kernel
# terminal driver understands. Names are kept in the 16-colour
# set so the look is identical across SSH, serial, and the live
# env's framebuffer tty1 (where any 256-colour mapping collapses
# to its nearest neighbour and the design intent gets lost).
# ---------------------------------------------------------------------------

_PRIMARY = "bright_blue"  # dominant -- headers, table titles, primary columns.
# ``bright_blue`` (16-colour canonical, ANSI code 94 fg / xterm
# default #5555ff) was chosen over plain ``blue`` (#0000aa) so the
# non-bold instances (column data: path / format / size / etc.)
# stay readable against the framebuffer console's black background
# -- plain blue rendered too dark for non-bold body text. Bold
# headers still pop because of the weight.
_MUTED = "bright_black"  # secondary -- byline columns, parenthesised hints. Canonical
# 16-colour ANSI gray with no 256-colour tint (grey62 read as
# teal-ish on dark dev terminals); renders identically across SSH,
# serial, and the live env's framebuffer.
_ACCENT = "yellow"  # the dash: row indices + prompts + stage breadcrumb only
_DANGER = "red"
_OK = "green"
# Very dark grey for subtle zebra striping. On 256-colour terminals
# (SSH, dev consoles) renders as a faint band; on the live env's
# 16-colour framebuffer it down-converts to black and disappears,
# which is the desired behaviour -- the stripe is a nicety on
# capable terminals, not a feature anyone should depend on.
_STRIPE = "grey11"


# ---------------------------------------------------------------------------
# Wizard state. Tiny dataclass; no reactive properties, just fields.
# ---------------------------------------------------------------------------


@dataclass
class _State:
    image_root: Path
    catalog_source: str | None = None
    mac: str | None = None
    pxe_done_base: str | None = None

    selected_image: _TuiImage | None = None
    selected_disk: dict[str, Any] | None = None
    post_flash: bool = False

    # ``True`` iff the operator has either explicitly chosen a
    # catalog source via the SELECT_CATALOG screen (``[d]`` /
    # ``[c]`` / ``[l] local-only``) OR the wizard auto-skipped that
    # step at startup because ``--catalog`` was set OR the local
    # ``image_root`` had images at startup. Back-nav from
    # SELECT_IMAGE clears this flag so the operator can re-enter
    # SELECT_CATALOG mid-session.
    catalog_chosen: bool = False

    # Cached lists; refreshed on demand.
    _images: list[_TuiImage] = field(default_factory=list)
    _disks: list[dict[str, Any]] = field(default_factory=list)

    def stage(self) -> _WizardStage:
        if self.post_flash:
            return _WizardStage.REBOOT_OR_DONE
        if not self.catalog_chosen:
            return _WizardStage.SELECT_CATALOG
        if self.selected_image is None:
            return _WizardStage.SELECT_IMAGE
        if self.selected_disk is None:
            return _WizardStage.SELECT_DISK
        return _WizardStage.CONFIRM_FLASH

    def back(self) -> None:
        """Clear the most-recent commit. Esc / ``b`` from a prompt
        calls this. SELECT_CATALOG (top stage) -> no-op.
        """
        if self.post_flash:
            self.post_flash = False
            self.selected_disk = None
            return
        if self.selected_disk is not None:
            self.selected_disk = None
            return
        if self.selected_image is not None:
            self.selected_image = None
            return
        if self.catalog_chosen:
            # Back from SELECT_IMAGE -> SELECT_CATALOG. Clear the
            # chosen flag so stage() returns SELECT_CATALOG;
            # ``catalog_source`` itself stays so the catalog screen
            # shows the previous choice as the implicit default.
            self.catalog_chosen = False
            return
        # SELECT_CATALOG: no-op (top of wizard).


# ---------------------------------------------------------------------------
# Local-side enumeration: images + disks. Pure functions that build
# the TUI shape. The catalog overlay is loaded separately via
# ``load_catalog_from_source``.
# ---------------------------------------------------------------------------


def _list_local_images(image_root: Path) -> list[_TuiImage]:
    """Local image-root scan -> TUI rows. Each file with a recognised
    image extension (``.qcow2`` / ``.img`` / ``.img.{gz,zst,xz,bz2}`` /
    ``.iso`` / ``.iso.gz``) surfaces as a local row (path-bearing).
    Catalog entries (the pixie's remote images, oras://, http://)
    overlay on top via ``--catalog URL`` -- they are NOT discovered on
    the local filesystem; the PIXIE_IMAGES partition is plain local
    image files only.
    """
    if not image_root.exists() or not image_root.is_dir():
        return []
    return [
        _TuiImage(
            name=img.name,
            fmt=img.format,
            size_bytes=img.size_bytes or 0,
            path=img.path,
            arch=img.arch,
        )
        for img in images.list_images(image_root)
    ]


def _list_disks() -> list[dict[str, Any]]:
    """Disk inventory via ``disks.list_disks``. Filters down to
    flash-eligible candidates: must be a block device, not
    read-only, not a loop / ram device.
    """
    try:
        all_disks = disks.list_disks()
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return []
    return [d for d in all_disks if d.get("type") == "disk" and not d.get("ro")]


def _uefi_boot_registration_enabled() -> bool:
    """Whether to register a UEFI NVRAM boot entry after a flash.

    OFF by default. Most firmware boots a freshly-flashed disk on its
    own, and writing NVRAM on every flash proved risky on server boards
    -- it once reordered an EPYC box's BootOrder so it stopped
    netbooting. Opt in by setting ``PIXIE_REGISTER_UEFI_BOOT`` to a truthy
    value (``1`` / ``true`` / ``yes`` / ``on``) only for firmware that
    won't boot the disk without an explicit entry.
    """
    return os.environ.get("PIXIE_REGISTER_UEFI_BOOT", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# ---------------------------------------------------------------------------
# The TUI itself. Sequential-screen wizard driven by a plain loop.
# ---------------------------------------------------------------------------


class BtyTui:
    """The pixie terminal UI -- Rich-based, no event loop.

    ``run()`` is the entry point. The wizard advances through five
    stages (SELECT_CATALOG, SELECT_IMAGE, SELECT_DISK, CONFIRM_FLASH,
    REBOOT_OR_DONE) until the operator quits. ``SELECT_CATALOG`` is
    auto-skipped at launch when ``--catalog`` was passed or the local
    image-root already has images; see :class:`_WizardStage` for the
    derivation rules.

    Each screen is a method that renders + prompts + returns a
    string token. The dispatcher uses the token to advance, back,
    quit, refresh, or switch catalog source.
    """

    def __init__(
        self,
        server: str = _DEFAULT_SERVER,
        mac: str | None = None,
        catalog: str | None = None,
    ) -> None:
        """Two-mode TUI:

        * ``mac`` unset -> interactive wizard, local image-root only.
          PIXIE_IMAGE_ROOT env var still picks the root. ``catalog``,
          if given, pre-fills the catalog source and skips the
          SELECT_CATALOG screen (equivalent to picking ``[c]`` and
          typing the URL).
        * ``mac`` set -> server-driven mode. ``run()`` GETs
          ``<server>/pxe/<mac>/plan`` and dispatches:
            - ``plan.mode == "flash"``     -> ``_run_auto`` (no prompts)
            - ``plan.mode == "interactive"`` -> interactive wizard with
              the catalog the server suggests
            - ``plan.mode == "inventory"`` -> post disk inventory, then
              reboot (boot_mode=pixie-inventory; next contact serves the ipxe-exit chain)
            - ``plan.mode == "exit"``    -> exit cleanly (nothing to do
              from pixie's side; firmware sanboot handles the rest)
            - 404 / network failure       -> interactive wizard with
              server's ``/catalog.toml`` as the catalog source
          ``catalog`` is ignored in server-driven mode -- the server's
          plan response carries the catalog the operator should see.
        """
        self._console = Console(highlight=False)
        # Single source of truth for the PIXIE_IMAGE_ROOT -> default
        # resolution; mirrors what pixie uses rather than re-hardcoding
        # the default path here.
        resolved_root = images.default_image_root()
        # ``server`` is the pixie URL or hostname (bare hostnames
        # get an ``http://`` scheme defaulted in below). Stored on the
        # state so the header Panel can show it; also the base for
        # ``/pxe/<mac>/done`` POST after a successful flash.
        self._server_url = _normalise_server_url(server)
        pxe_done = self._server_url if mac else None
        # Three startup paths:
        # 1. ``mac`` set -> server-driven mode. catalog_chosen=True so
        #    the interactive fallback lands on SELECT_IMAGE directly
        #    with the server's catalog pre-loaded. The --catalog flag
        #    is ignored here; the server's plan dictates source.
        # 2. ``catalog`` set (no mac) -> hand-driven interactive run
        #    with a known catalog. Skip SELECT_CATALOG; behave as if
        #    the operator typed ``[c]`` + the URL.
        # 3. Neither set -> auto-skip SELECT_CATALOG only when the
        #    local image-root already has images; otherwise prompt.
        if mac:
            catalog_chosen = True
            catalog_source: str | None = self._server_url.rstrip("/") + "/catalog.toml"
        elif catalog:
            catalog_chosen = True
            catalog_source = catalog
        else:
            catalog_source = None
            catalog_chosen = bool(_list_local_images(resolved_root))
        self._state = _State(
            image_root=resolved_root,
            catalog_source=catalog_source,
            mac=mac,
            pxe_done_base=pxe_done,
            catalog_chosen=catalog_chosen,
        )
        # Catalog load errors (transient network / bad TOML) -- surface
        # via a soft banner on the image-pick screen rather than
        # aborting the TUI.
        self._catalog_load_error: str | None = None
        # ``_auto`` is flipped True later by ``run()`` if the
        # server's plan says ``mode=flash``. Without a mac, stays False.
        self._auto = False
        self._auto_image: str | None = None
        # Declared content sha256 from the plan (``disk_image_sha``), so
        # the auto-flash verifies even when the image URL is a withcache
        # or direct origin that doesn't embed the digest in its path.
        self._auto_image_sha: str | None = None
        self._auto_target_disk_serial: str | None = None
        # Image format from the plan. The image URL's name segment can be
        # a descriptive title (oras) with no extension, so format can't be
        # detected from the URL -- the server passes it explicitly.
        self._auto_format: str | None = None
        # Descriptive image name from the plan (the catalog title). The
        # image URL's basename can be a synthesised "image.<fmt>", so use
        # this for the flash-screen display instead.
        self._auto_name: str | None = None

    # ---------- entry --------------------------------------------------

    def run(self) -> None:
        """Drive the wizard until the operator quits, OR run the
        server-driven path when a ``mac`` was supplied.
        """
        # Best-effort inventory post at startup. Network failures are
        # non-fatal; the TUI is still useful even if pixie can't be
        # reached.
        if self._state.pxe_done_base and self._state.mac:
            self._auto_post_inventory()

        # Server-driven mode: fetch the plan from
        # <server>/pxe/<mac>/plan and dispatch.
        #
        # For every plan we print a banner BEFORE the dispatch so the
        # operator (or anyone watching the live env's tty1) sees what
        # the server said and what ``pixie`` is about to do. A silent
        # exit / silent wizard launch is indistinguishable from a
        # crash on a framebuffer console.
        if self._state.mac:
            plan_action = self._fetch_and_dispatch_plan()
            if plan_action == "flash":
                self._console.print(
                    Panel(
                        f"Server reports [{_ACCENT}]mode=flash[/] for "
                        f"[{_PRIMARY}]{self._state.mac}[/]:\n\n"
                        f"  image  : {self._auto_image}\n"
                        f"  serial : {self._auto_target_disk_serial}\n\n"
                        f"[{_MUTED}]Running the flash without prompts; "
                        "the same chrome the interactive wizard uses.[/]",
                        title="Plan: flash",
                        border_style=_PRIMARY,
                    )
                )
                rc = self._run_auto()
                if rc == 0:
                    sys.exit(0)
                # Deterministic auto-flash failure (target disk not
                # found / plan rejected / flash error). Do NOT sys.exit
                # non-zero: pixie-on-tty1.service has Restart=on-failure,
                # so that relaunches pixie every few seconds in a tight
                # reject -> exit -> relaunch loop (the operator just sees
                # the same rejection flash by). Fall back to the wizard
                # instead -- the box stays up, the operator sees the
                # rejection reasons printed above and can pick by hand or
                # fix the assignment on the pixie.
                self._auto = False
                self._catalog_load_error = (
                    "Auto-flash could not proceed (see the panel above). "
                    "Pick an image and target disk by hand, or fix the "
                    "assignment in the pixie UI (/ui/machines)."
                )
            if plan_action == "exit":
                # The server says nothing to do here (boot_mode=ipxe-exit
                # or an unrecognised policy). Print a Panel so an operator
                # hand-running ``pixie --mac X`` from a workstation sees WHY
                # the tool is exiting -- a silent ``sys.exit(0)`` looks
                # like a crash. The live env never reaches this path:
                # ipxe-exit short-circuits at the iPXE chain (boots the
                # local disk directly, no live-env chain).
                self._console.print(
                    Panel(
                        f"Server reports [{_ACCENT}]mode=exit[/] for "
                        f"[{_PRIMARY}]{self._state.mac}[/] -- nothing for "
                        "pixie to do here.\n\n"
                        f"[{_MUTED}]The firmware / local disk boots directly "
                        "(ipxe-exit or already provisioned); no flash, no wizard.[/]",
                        title="Plan: nothing to do",
                        border_style=_PRIMARY,
                    )
                )
                sys.exit(0)
            if plan_action == "inventory":
                # boot_mode=pixie-inventory: the box booted the live env
                # only to (re)report its disks. Post inventory
                # synchronously so it lands, then reboot -- the next PXE
                # contact (saw_flasher_boot armed by the /boot fetch)
                # serves the ipxe-exit chain to boot the local disk.
                # No wizard, no flash.
                self._console.print(
                    Panel(
                        f"Server reports [{_ACCENT}]mode=inventory[/] for "
                        f"[{_PRIMARY}]{self._state.mac}[/].\n\n"
                        f"[{_MUTED}]Reporting this box's disks to pixie, then "
                        "rebooting to boot the local disk.[/]",
                        title="Plan: inventory + reboot",
                        border_style=_PRIMARY,
                    )
                )
                self._post_inventory_sync()
                self._do_reboot()  # exits via systemctl reboot
                sys.exit(0)
            # ``interactive`` falls through to the wizard below. Print a
            # banner first so the operator knows the server delegated
            # the pick to them (vs. having auto-flashed). The auto-flash
            # FAILURE fall-through reaches the wizard too, but it already
            # printed its own rejection panel, so skip this banner there.
            if plan_action == "interactive":
                self._console.print(
                    Panel(
                        f"Server reports [{_ACCENT}]mode=interactive[/] for "
                        f"[{_PRIMARY}]{self._state.mac}[/]:\n\n"
                        f"  catalog : {self._state.catalog_source or '(none)'}\n\n"
                        f"[{_MUTED}]Dropping into the wizard so you can pick "
                        "the image + target disk by hand.[/]",
                        title="Plan: interactive",
                        border_style=_PRIMARY,
                    )
                )

        try:
            self._main_loop()
        except KeyboardInterrupt:
            self._console.print()
            self._console.print(
                f"[{_MUTED}]Interrupted -- exiting.[/]",
            )
        except SystemExit:
            raise
        except Exception:  # pragma: no cover - last-resort safety net
            self._console.print_exception(show_locals=False)
            sys.exit(1)

    def _fetch_and_dispatch_plan(self) -> str:
        """GET ``<server>/pxe/<mac>/plan`` and prep the wizard for
        dispatch. Returns one of:

        * ``"flash"`` -- plan is an auto-flash; ``_run_auto`` should
          run next. ``_auto_image`` + ``_auto_target_disk_serial``
          are populated.
        * ``"interactive"`` -- plan asks the operator to pick; fall
          through to the interactive wizard. The catalog source on
          state may be updated to the server's suggestion.
        * ``"exit"`` -- plan is "nothing to do here"; exit cleanly.

        Network / parse failures fall through to ``"interactive"``
        with the previously-set catalog (the server's
        ``/catalog.toml`` -- set in ``__init__``). The operator can
        retry from the interactive screen or re-run with the same
        cmdline.
        """
        assert self._state.mac is not None
        url = f"{self._server_url.rstrip('/')}/pxe/{self._state.mac}/plan"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = _json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            # Soft failure: surface as a transient catalog-load
            # error and fall through to interactive. Operator sees
            # the banner on the image-select screen.
            self._catalog_load_error = f"plan fetch failed: {exc}"
            return "interactive"

        mode = payload.get("mode")
        if mode == "flash":
            self._auto_image = payload.get("image")
            self._auto_target_disk_serial = payload.get("target_disk_serial")
            self._auto_format = payload.get("format")
            self._auto_name = payload.get("name")
            self._auto_image_sha = payload.get("disk_image_sha")
            if not self._auto_image or not self._auto_target_disk_serial:
                self._catalog_load_error = (
                    f"server returned mode=flash but missing image/target_disk_serial: {payload!r}"
                )
                return "interactive"
            self._auto = True
            return "flash"
        if mode == "interactive":
            # Server may suggest a specific catalog. If it does, use
            # it; if not, keep whatever was set in __init__ (which
            # defaults to ``<server>/catalog.toml`` for server-driven
            # mode). Crucially, ``pxe_done_base`` stays at
            # ``self._server_url`` regardless of what the plan's
            # catalog field points at -- the completion POST goes
            # back to the pixie that handed us the plan, NOT
            # to whichever (possibly third-party) host hosts the
            # catalog TOML.
            suggested = payload.get("catalog")
            if isinstance(suggested, str) and suggested:
                self._state.catalog_source = suggested
            return "interactive"
        if mode == "exit":
            return "exit"
        if mode == "inventory":
            return "inventory"
        # Unknown mode -- treat as interactive so the operator gets
        # SOMETHING they can act on, plus a banner explaining why.
        self._catalog_load_error = f"unknown plan mode {mode!r}; falling back to interactive"
        return "interactive"

    def _post_auto_failure(self, reason: str) -> None:
        """Tell the pixie an unattended (netboot) flash failed.

        "When it knows and it is capable": the failure paths below call this
        the moment they detect a non-recoverable failure, and ``mac`` +
        ``pxe_done_base`` (the netboot context) being set IS the capability
        check -- a USB/interactive run has neither and stays silent. Without
        this the server sits at "awaiting flash" forever even though the live
        env knew. Best-effort: a server we cannot reach must not mask the
        on-console failure (the red Panel was already printed).
        """
        if not (self._state.pxe_done_base and self._state.mac):
            return
        with contextlib.suppress(urllib.error.URLError, OSError, TimeoutError):
            post_pxe_status(self._state.pxe_done_base, self._state.mac, "failed", reason)

    def _run_auto(self) -> int:
        """Scripted flash path: plan-driven, no prompts.

        The pixie's /pxe/<mac>/plan response provides every
        argument needed for the flash. ``pixie``'s job is to:

        1. Look up the disk whose serial matches the plan's
           ``target_disk_serial`` (the operator picked it in
           /ui/machines).
        2. Build the flash plan.
        3. Run the flash with the standard Rich progress bar.
        4. POST ``/pxe/<mac>/done`` to the pixie.
        5. Reboot.

        On any failure: prints a red Panel + exits non-zero, no reboot.
        Uses the same screens/panels the interactive wizard uses, so
        operator-visible chrome is identical between scripted +
        interactive runs.
        """
        assert self._auto_image is not None
        assert self._auto_target_disk_serial is not None

        # Stable serial-console marker for the PXE chain test
        # (cijoe/configs/test-pxe.toml chain_markers). Pinned plain-
        # text so downstream observers (test scripts, BMC serial-log
        # tailers, operators inspecting journalctl) can detect that
        # pixie entered auto-flash mode rather than the wizard.
        _emit_console_marker("pixie: auto-flash starting")

        # Pre-fill wizard state from plan-fetched values. ``--image``
        # accepts a path or URL; ``_TuiImage`` keys differ.
        image_arg = self._auto_image
        if "://" in image_arg:
            self._state.selected_image = _TuiImage(
                name=self._auto_name or _basename_from_url(image_arg) or "auto-flash-image",
                fmt=self._auto_format,
                size_bytes=0,
                url=image_arg,
                sha=self._auto_image_sha,
            )
        else:
            image_path = Path(image_arg)
            self._state.selected_image = _TuiImage(
                name=self._auto_name or image_path.name or "auto-flash-image",
                fmt=self._auto_format,
                size_bytes=0,
                path=image_path,
            )

        # Look up the disk by serial. lsblk-via-disks.list_disks
        # returns dicts with a ``serial`` field; the pixie
        # picked this serial when the operator chose a target in
        # /ui/machines. A no-match means the drive was swapped or
        # the inventory is stale -- refuse rather than guess.
        target_serial = self._auto_target_disk_serial
        try:
            all_disks = disks.list_disks()
        except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
            self._console.clear()
            self._print_header(stage=3, title="Auto-flash: disk lookup failed")
            self._console.print(
                Panel(
                    f"[{_DANGER}]lsblk failed: {exc}[/]",
                    border_style=_DANGER,
                    title="Disk inventory failed",
                )
            )
            self._post_auto_failure(f"disk inventory failed: {exc}")
            return 1
        matched: dict[str, Any] | None = None
        for disk in all_disks:
            raw = disk.get("serial")
            present = raw.strip() if isinstance(raw, str) else raw
            # ``present`` guards the catastrophic case: never match a
            # serial-less disk (present None/"") even if target_serial
            # were somehow empty -- flashing the wrong disk is total
            # data loss. Upstream guarantees target_serial is truthy;
            # this is defence in depth.
            if present and present == target_serial:
                matched = disk
                break
        if matched is None:
            self._console.clear()
            self._print_header(stage=3, title="Auto-flash: target disk not found")
            visible = (
                ", ".join(
                    f"{d.get('path')} (serial={d.get('serial')!r})"
                    for d in all_disks
                    if d.get("type") == "disk"
                )
                or "(none)"
            )
            self._console.print(
                Panel(
                    f"[{_DANGER}]No disk on this host has serial="
                    f"{target_serial!r}.[/]\n\n"
                    f"Current disks: {visible}\n\n"
                    "The operator's pick in /ui/machines is stale; "
                    "re-pick after the next inventory and retry.",
                    border_style=_DANGER,
                    title="No matching disk",
                )
            )
            self._post_auto_failure(
                f"target disk serial {target_serial!r} not present on this host"
            )
            return 2
        self._state.selected_disk = matched

        # Probe + plan (same code path the interactive screen 4
        # uses). Errors surface as a red Panel and a non-zero exit.
        self._console.clear()
        self._print_header(stage=4, title="Auto-flash: probing")
        plan_or_error = self._probe_and_plan(
            self._state.selected_image,
            Path(str(matched.get("path"))),
        )
        if isinstance(plan_or_error, str):
            self._console.print(
                Panel(
                    f"[{_DANGER}]Probe failed:[/]\n\n{plan_or_error}",
                    border_style=_DANGER,
                    title="Plan rejected",
                )
            )
            self._post_auto_failure(f"plan rejected on probe: {plan_or_error}")
            return 3
        plan, errors = plan_or_error
        if errors:
            self._print_flash_plan(plan, errors)
            self._post_auto_failure("plan validation failed: " + "; ".join(errors))
            return 4

        # Run the flash with the same Rich progress bar interactive
        # operators see. ``_screen_flash_running`` sets
        # ``self._state.post_flash`` on success.
        self._screen_flash_running(plan)
        if not self._state.post_flash:
            self._post_auto_failure("flash did not complete (write or verify failed)")
            return 5

        # Best-effort completion signal (pixie's per-MAC timeline).
        if self._state.pxe_done_base and self._state.mac:
            with contextlib.suppress(urllib.error.URLError, OSError, TimeoutError):
                post_pxe_status(self._state.pxe_done_base, self._state.mac, "done")

        # Stable serial-console marker for the PXE chain test
        # (cijoe/configs/test-pxe.toml chain_markers). Rich Panels
        # vary across terminal widths; this plain-text line is the
        # contract downstream observers (test scripts, CI dashboards,
        # operators tailing the BMC serial log) can pin against.
        _emit_console_marker("pixie: flash complete; rebooting")

        # Always reboot on success -- auto-mode exists for the
        # unattended netboot flow where reboot is the whole point.
        self._do_reboot()  # exits via systemctl reboot; unreachable on success
        return 0

    # ---------- main loop ----------------------------------------------

    def _main_loop(self) -> None:
        while True:
            stage = self._state.stage()
            if stage is _WizardStage.SELECT_CATALOG:
                action = self._screen_select_catalog()
            elif stage is _WizardStage.SELECT_IMAGE:
                action = self._screen_select_image()
            elif stage is _WizardStage.SELECT_DISK:
                action = self._screen_select_disk()
            elif stage is _WizardStage.CONFIRM_FLASH:
                action = self._screen_confirm_flash()
            else:
                action = self._screen_reboot_or_done()

            if action == "quit":
                return
            # All other actions (back / continue / refresh) loop.

    # ---------- screens ------------------------------------------------

    def _screen_select_catalog(self) -> str:
        """Stage 1: pick the image source.

        Three options: pixie's default release catalog, a custom
        catalog URL (http(s):// or oras://), or local-only (no
        remote overlay, just scan ``image_root``). The screen is
        auto-skipped at startup when ``--catalog`` was set or the
        local image-root already has images; back-nav from
        SELECT_IMAGE re-enters this screen so the operator can
        switch source mid-session.
        """
        self._console.clear()
        self._print_header(stage=1, title="Pick an image source")
        self._console.print(
            Panel(
                f"[{_ACCENT}]d[/]  load pixie's [bold]default[/] catalog "
                "(published with pixie as a release artifact)\n"
                f"[{_ACCENT}]c[/]  provide a [bold]custom[/] http(s):// or oras:// URL "
                "to a catalog that you host\n"
                f"[{_ACCENT}]l[/]  [bold]local only[/] -- skip remote catalog, "
                "use images already in the image-root",
                title="How should pixie find images?",
                border_style=_PRIMARY,
            )
        )
        prompt_text = self._render_prompt_line(
            title="Pick an image source",
            extras=(
                ("d", "default"),
                ("c", "custom"),
                ("l", "local only"),
                ("q", "quit"),
            ),
        )
        choice = self._ask(prompt_text)
        if choice in ("q", "quit"):
            return "quit"
        if choice in ("d", "default"):
            self._state.catalog_source = _BTY_DEFAULT_CATALOG_URL
            self._state.catalog_chosen = True
            return "continue"
        if choice in ("c", "custom"):
            self._screen_change_catalog()
            # Mark chosen even if the operator cancelled the URL
            # input -- ``catalog_source`` either got set (success)
            # or stayed at whatever it was (cancel). Either way the
            # operator decided to proceed; back-nav from image
            # screen re-enters this stage if they change their mind.
            self._state.catalog_chosen = True
            return "continue"
        if choice in ("l", "local"):
            self._state.catalog_source = None
            self._state.catalog_chosen = True
            return "continue"
        self._console.print(f"[{_DANGER}]Unrecognised choice {choice!r}; type d, c, l, or q.[/]")
        self._pause_for_ack()
        return "continue"

    def _screen_select_image(self) -> str:
        """Stage 2: pick an image.

        Combines local image-root scan + the optional ``--catalog``
        overlay into one numbered list. Operator types a number or
        a single-letter command. The catalog source itself was
        chosen in stage 1; ``b`` here re-enters that screen.
        """
        self._refresh_images()
        self._console.clear()
        self._print_header(stage=2, title="Pick an image to flash")
        if self._state._images:
            self._print_image_table(self._state._images)
        else:
            self._print_empty_catalog_panel()

        prompt_text = self._render_prompt_line(
            title="Pick an image to flash",
            extras=(
                ("#", "pick"),
                ("b", "back (change catalog)"),
                ("r", "refresh"),
                ("q", "quit"),
            ),
        )
        choice = self._ask(prompt_text)
        if choice in ("q", "quit"):
            return "quit"
        if choice in ("", "r", "refresh"):
            return "continue"
        if choice in ("b", "back"):
            self._state.back()
            return "continue"
        idx = self._parse_index(choice, len(self._state._images))
        if idx is not None:
            self._state.selected_image = self._state._images[idx]
        else:
            self._console.print(
                f"[{_DANGER}]"
                + self._describe_index_miss(choice, len(self._state._images), "image")
                + "[/]"
            )
            self._pause_for_ack()
        return "continue"

    def _screen_select_disk(self) -> str:
        """Stage 2: pick a disk.

        Refreshed every entry to catch hotplug. Filtered to block
        devices of type ``disk`` (skips loop / ram / partitions).
        """
        self._refresh_disks()
        self._console.clear()
        self._print_header(stage=3, title="Pick a target disk")
        if self._state._disks:
            self._print_disk_table(self._state._disks)
        else:
            self._console.print(
                Panel(
                    f"[{_DANGER}]No flash-eligible disks detected.[/]\n\n"
                    f"[{_MUTED}]Check ``lsblk`` on tty2 to see what the kernel sees.[/]",
                    border_style=_DANGER,
                    title="No disks",
                )
            )

        # ``[r]`` is not advertised here: Enter re-runs
        # ``_refresh_disks`` (called on every screen entry), so an
        # explicit refresh key is redundant. Hot-plugged disks show
        # up on the next Enter.
        prompt_text = self._render_prompt_line(
            title="Pick a target disk",
            extras=(
                ("#", "pick"),
                ("b", "back"),
                ("q", "quit"),
            ),
        )
        choice = self._ask(prompt_text)
        if choice in ("q", "quit"):
            return "quit"
        if choice in ("b", "back", "esc"):
            self._state.back()
            return "continue"
        if choice in ("r", "refresh", ""):
            return "continue"
        idx = self._parse_index(choice, len(self._state._disks))
        if idx is not None:
            self._state.selected_disk = self._state._disks[idx]
        else:
            self._console.print(
                f"[{_DANGER}]"
                + self._describe_index_miss(choice, len(self._state._disks), "disk")
                + "[/]"
            )
            self._pause_for_ack()
        return "continue"

    def _screen_confirm_flash(self) -> str:
        """Stage 3: probe image + target, render plan, y/N confirm.

        Probing runs synchronously with a Rich Status spinner so
        the operator sees something during the 1-3s of subprocess
        calls (``lsblk``, ``qemu-img info``, etc.).
        """
        image = self._state.selected_image
        disk = self._state.selected_disk
        assert image is not None and disk is not None  # stage gate

        disk_path = Path(str(disk.get("path") or disk.get("name") or ""))
        self._console.clear()
        self._print_header(stage=4, title="Confirm flash plan")

        # Probe both ends with a spinner so the screen isn't blank
        # during the lsblk + qemu-img info round-trips.
        plan_or_error = self._probe_and_plan(image, disk_path)
        if isinstance(plan_or_error, str):
            self._console.print(
                Panel(
                    f"[{_DANGER}]Probe failed:[/]\n\n{plan_or_error}",
                    border_style=_DANGER,
                    title="Plan rejected",
                )
            )
            choice = self._ask(
                self._render_prompt_line(
                    title="Probe failed",
                    extras=(("b", "back"), ("q", "quit")),
                )
            )
            if choice in ("q", "quit"):
                return "quit"
            self._state.back()
            return "continue"

        plan, errors = plan_or_error
        self._print_flash_plan(plan, errors)

        if errors:
            self._console.print(
                Panel(
                    f"[{_DANGER}]Validation FAILED:[/]\n" + "\n".join(f"  - {e}" for e in errors),
                    border_style=_DANGER,
                    title="Plan rejected",
                )
            )
            choice = self._ask(
                self._render_prompt_line(
                    title="Plan rejected",
                    extras=(("b", "back"), ("q", "quit")),
                )
            )
            if choice in ("q", "quit"):
                return "quit"
            self._state.back()
            return "continue"

        prompt_text = self._render_prompt_line(
            title="Confirm flash plan",
            extras=(
                ("y", "yes, flash"),
                ("b", "back"),
                ("q", "quit"),
            ),
        )
        choice = self._ask(prompt_text)
        if choice in ("q", "quit"):
            return "quit"
        if choice in ("b", "back", "n", "no", ""):
            self._state.back()
            return "continue"
        if choice in ("y", "yes"):
            self._screen_flash_running(plan)
            return "continue"
        self._console.print(f"[{_DANGER}]Unrecognised choice {choice!r}.[/]")
        self._pause_for_ack()
        return "continue"

    def _screen_flash_running(self, plan: flash.FlashPlan) -> None:
        """Run the flash in a background thread; the main thread
        sits in a Rich Live() with a Progress bar updated from the
        ``FlashProgress`` callback.

        On success, sets ``self._state.post_flash = True`` so the
        next ``stage()`` returns REBOOT_OR_DONE.
        """
        self._console.clear()
        self._print_header(stage=4, title="Flashing...")

        progress = Progress(
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=None),
            TaskProgressColumn(),
            TextColumn("[{task.fields[bytes_human]}]"),
            TransferSpeedColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=self._console,
            transient=False,
            expand=True,
        )

        # Shared state between the flash worker thread and the
        # rendering loop. Only the worker writes; the main thread
        # reads via the Progress callback shim below.
        shared: dict[str, Any] = {"result": None, "error": None, "stage": "starting"}

        with progress:
            # The write bar is always indeterminate. The "total" for
            # the writer is fundamentally unreliable: gzip wraps its
            # uncompressed-size trailer mod 2^32, qcow2 virtual size
            # need not equal the bytes dd ends up writing, and the
            # ``size_bytes`` fallback is the COMPRESSED upstream size
            # which is always smaller than the decompressed write
            # count. Rather than show a percentage that misleads,
            # leave ``total=None`` so Rich's BarColumn draws the
            # pulsing scanner block; the bytes-human counter shows
            # the running write count so the operator can still see
            # the disk is being written. The download bar IS
            # determinate (Content-Length is reliable) and gets added
            # lazily on the first ``downloading_progress`` event so
            # local-file flashes do not show a permanently-empty
            # network bar.
            task_id = progress.add_task(
                "queued",
                total=None,
                bytes_human="0",
            )
            download_task_id: TaskID | None = None

            # SoL-friendly milestone markers. The Rich progress bars
            # above are rendered to /dev/tty1 (the framebuffer VT) and
            # invisible to an operator watching via IPMI SoL or
            # following journalctl. The milestone emitter writes plain
            # ``pixie: download NN%`` / ``pixie: write NN%`` lines via
            # /dev/kmsg at 25/50/75/100 crossings so the same operator
            # gets at most four heartbeats per stage on whichever
            # serial console the kernel is registered with. Skipped
            # silently when the total is unknown (some write paths
            # can't pre-compute decompressed size); the ``starting``
            # and ``flash complete; rebooting`` bookends still fire.
            download_emitter = _MilestoneEmitter("download")
            write_emitter = _MilestoneEmitter("write")

            def _on_progress(ev: flash.FlashProgress) -> None:
                # Called from the flash thread. ``progress`` is
                # thread-safe (Rich's Progress mutex), so direct
                # updates are fine.
                nonlocal download_task_id
                shared["stage"] = ev.event
                if ev.event == "started":
                    progress.update(task_id, description="starting flash")
                elif ev.event == "writing":
                    progress.update(task_id, description=f"writing ({ev.note or '?'})")
                elif ev.event == "writing_progress":
                    if ev.bytes_written is not None:
                        # Indeterminate bar; just surface the running
                        # byte count alongside the pulse.
                        progress.update(
                            task_id,
                            completed=ev.bytes_written,
                            bytes_human=_format_mib(ev.bytes_written),
                        )
                        write_emitter.update(ev.bytes_written, ev.total_bytes)
                elif ev.event == "downloading_progress":
                    if ev.bytes_downloaded is None:
                        return
                    if download_task_id is None:
                        # Rich renders tasks in insertion order; we
                        # want download above writing, but the write
                        # task was created first. Workaround: a new
                        # task added at runtime lands at the bottom,
                        # which is fine; the operator still sees
                        # both bars and the labels disambiguate.
                        download_task_id = progress.add_task(
                            "downloading",
                            total=ev.total_bytes,
                            bytes_human=_format_progress_bytes(ev.bytes_downloaded, ev.total_bytes),
                        )
                    progress.update(
                        download_task_id,
                        completed=ev.bytes_downloaded,
                        total=ev.total_bytes,
                        bytes_human=_format_progress_bytes(ev.bytes_downloaded, ev.total_bytes),
                    )
                    download_emitter.update(ev.bytes_downloaded, ev.total_bytes)
                elif ev.event == "synced":
                    progress.update(task_id, description="syncing buffers")
                elif ev.event == "partprobed":
                    progress.update(task_id, description="partprobed")
                elif ev.event == "done":
                    progress.update(task_id, description="done")
                    if download_task_id is not None:
                        progress.update(download_task_id, description="downloaded")
                elif ev.event == "failed":
                    progress.update(task_id, description=f"FAILED: {ev.note}")
                elif ev.event == "subprocess_log":
                    # Rich's Progress is a Live; ``console.print``
                    # inside the live context erases the live
                    # region, prints the line, and redraws -- so
                    # the log line lands above the progress widget
                    # without corrupting it.
                    self._console.print(f"[{_MUTED}]{ev.note}[/]")

            # ``cancel_event`` lets the main thread (which holds
            # KeyboardInterrupt) tell the worker thread's flash pipeline
            # to SIGTERM its subprocesses. Without this, hitting Ctrl+C
            # during a flash interrupts only the main thread's join();
            # the daemon worker keeps its dd/curl/zstd children running
            # and the operator's "abort" leaves a partial-write in flight
            # on the target disk with no cleanup.
            cancel_event = threading.Event()

            def _runner() -> None:
                try:
                    flash.execute_plan(
                        plan,
                        progress=_on_progress,
                        cancel=cancel_event.is_set,
                    )
                    shared["result"] = "ok"
                except flash.FlashCancelled as exc:
                    shared["result"] = "cancelled"
                    shared["error"] = str(exc)
                except flash.FlashError as exc:
                    shared["result"] = "failed"
                    shared["error"] = str(exc)
                except Exception as exc:
                    shared["result"] = "failed"
                    shared["error"] = f"unexpected: {exc!r}"

            t = threading.Thread(target=_runner, name="pixie-flash", daemon=True)
            t.start()
            try:
                t.join()
            except KeyboardInterrupt:
                # Operator pressed Ctrl+C while the flash was running.
                # Set the cancel flag; ``execute_plan``'s watchdog will
                # SIGTERM its subprocesses (1s grace then SIGKILL) and
                # the runner will land ``FlashCancelled`` on shared.
                # Re-join with no timeout so we don't return while the
                # subprocesses are still tearing down: a partially-killed
                # dd that's still flushing the page cache wedges the
                # screen redraw if we race past it.
                cancel_event.set()
                self._console.print(f"[{_MUTED}]Cancelling -- waiting for dd/curl to exit...[/]")
                t.join()

        # Defensive: Rich's ``Live`` (which ``Progress`` uses) hides
        # the cursor while running via ANSI ``\033[?25l`` and is
        # supposed to restore it on ``with`` exit. On the live env's
        # framebuffer console and BMC virtual KVMs the restore is
        # sometimes lost (terminal state desync). Explicitly re-show
        # the cursor here so the next screen's prompt is visible.
        self._console.show_cursor(True)

        if shared["result"] == "ok":
            self._console.print(
                Panel(
                    f"[{_OK}]Flash completed.[/]",
                    border_style=_OK,
                    title="Done",
                )
            )
            self._register_uefi_boot_entry(plan)
            self._post_pxe_done_if_configured()
            self._state.post_flash = True
        elif shared["result"] == "cancelled":
            # Surface cancellation distinctly from a pipeline failure
            # so the operator knows the disk is in a partial-write
            # state that needs a re-flash (vs. a true error they'd
            # report). dd's subprocesses were SIGTERM'd; the on-disk
            # state is the prefix that dd had written + flushed.
            self._console.print(
                Panel(
                    f"[{_DANGER}]Flash cancelled.[/]\n\n"
                    f"[{_MUTED}]The target disk holds a partial write. "
                    f"Re-flash before booting.[/]",
                    border_style=_DANGER,
                    title="Cancelled",
                )
            )
            self._pause_for_ack()
        else:
            self._console.print(
                Panel(
                    f"[{_DANGER}]Flash FAILED.[/]\n\n"
                    f"[{_MUTED}]{shared.get('error') or 'unknown error'}[/]",
                    border_style=_DANGER,
                    title="Flash failed",
                )
            )
            self._pause_for_ack()

    def _screen_reboot_or_done(self) -> str:
        """Stage 4: flash succeeded. Offer reboot.

        Esc / ``b`` from here goes back to Stage 2 (same disk, pick
        again) so the operator can flash another disk with the same
        image without re-selecting the image. Full reset happens on
        a further ``b``.
        """
        self._console.clear()
        self._print_header(stage=5, title="Flash complete -- ready to reboot")
        d = self._state.selected_disk or {}
        disk_brief = f"{d.get('path', '?')} ({d.get('size', '?')} {d.get('model') or ''})".strip()
        self._console.print(
            Panel(
                f"[{_OK}]Image written to {disk_brief}.[/]\n\n"
                f"Reboot now to boot the freshly flashed disk.",
                border_style=_OK,
                title="Done",
            )
        )

        prompt_text = self._render_prompt_line(
            title="Reboot to boot from the freshly flashed disk",
            extras=(
                ("Enter/y", "reboot now (default)"),
                ("n", "don't reboot, stay"),
                ("b", "back"),
                ("q", "quit"),
            ),
        )
        choice = self._ask(prompt_text)
        # Enter (empty) defaults to REBOOT: the operator just flashed and
        # the obvious next step is to boot the new disk. Defaulting Enter
        # to "quit" surprised operators (flashed box sat un-rebooted).
        # The destructive step already happened, so reboot-on-Enter is
        # safe -- explicit n/q/back still opt out.
        if choice in ("n", "no"):
            return "quit"  # stay; don't reboot
        if choice in ("q", "quit"):
            return "quit"
        if choice in ("b", "back"):
            self._state.back()
            return "continue"
        if choice in ("y", "yes", "r", "reboot", ""):
            self._do_reboot()
            return "quit"  # unreachable on success; defensive
        self._console.print(f"[{_DANGER}]Unrecognised choice {choice!r}.[/]")
        self._pause_for_ack()
        return "continue"

    # ---------- auxiliary screens -------------------------------------

    def _screen_change_catalog(self) -> None:
        """Switch the catalog source. Prompt for a new URL / path.

        Empty input = clear catalog (local-only mode). Invalid
        sources surface as a soft banner on the next image-pick
        screen rather than crashing.
        """
        self._console.clear()
        self._print_header(stage=1, title="Switch catalog source")
        self._console.print(
            Panel(
                f"Current source: [{_PRIMARY}]{self._state.catalog_source or '(local only)'}[/]\n\n"
                "Enter a new source:\n"
                "  - local TOML path:    ``/etc/pixie/catalog.toml``\n"
                "  - HTTP URL:           ``http://pixie:8080/catalog.toml``\n"
                "  - ORAS reference:     ``oras://ghcr.io/owner/repo:tag``\n"
                "  - empty:              clear catalog (local image-root only)",
                title="Catalog source",
            )
        )
        prompt = "[bold]>[/] [new catalog source, empty to clear, q to abort]"
        new_source = self._ask(prompt).strip()
        if new_source in ("q", "quit"):
            return
        self._state.catalog_source = new_source or None
        # ``pxe_done_base`` stays anchored to ``self._server_url``
        # (set in __init__ when ``--mac`` was supplied) regardless
        # of what catalog source the operator picks here. The
        # completion POST goes to the pixie we got the plan
        # from, NOT to whichever host the catalog TOML happens to
        # live on. See the matching guard in
        # ``_fetch_and_dispatch_plan`` for the same bug pattern.

    # ---------- rendering helpers -------------------------------------

    def _print_header(self, *, stage: int, title: str) -> None:
        """Single Rich-Panel header carrying everything that's "world
        state" for the screen.

        Title (on the top border): ``-={[ pixie vX.Y.Z ]}=-`` -- the
        decoration that used to live in the standalone ASCII banner.
        Same two-tone treatment (blue brackets, yellow version) via
        Rich markup; the Panel itself uses ``border_style=_PRIMARY``
        so the box-drawing renders blue. Rich's ``safe_box`` falls
        back to ASCII frames on dumb / monochrome terminals.

        Body lines:

        * ``Steps:`` -- the wizard breadcrumb. Active stage in
          accent yellow + bold, others muted. The active-step
          highlight tracks the ``stage`` arg.
        * ``image_root:`` -- always shown.
        * ``catalog:`` -- always shown; reads ``local only``
          (italic) when no ``--catalog`` source is set.
        * ``mac:`` -- shown only when set (PXE-driven runs).

        The per-screen ``title`` is NOT rendered here -- it surfaces
        attached to the leader on the prompt line
        (``> <title>: _``, via :meth:`_render_prompt_line`). The
        arg is kept on this method for symmetry with the call
        sites; every screen passes the same title to both this
        method and the prompt builder.
        """
        del title  # surfaced near the prompt instead
        labels = ("Catalog", "Image", "Disk", "Flash", "Reboot")

        # Steps breadcrumb with active-step highlight.
        crumb_parts = []
        for n, label in enumerate(labels, start=1):
            if n == stage:
                crumb_parts.append(f"[bold {_ACCENT}]{n}.{label}[/]")
            else:
                crumb_parts.append(f"[{_MUTED}]{n}.{label}[/]")
        crumb = " -> ".join(crumb_parts)

        # Source-summary lines. Catalog is always shown so an
        # operator scanning the header never wonders whether the
        # state is missing.
        catalog_value = (
            f"[{_PRIMARY}]{self._state.catalog_source}[/]"
            if self._state.catalog_source
            else "[italic]local only[/italic]"
        )
        # ``image_root:`` is the longest label; pad shorter ones so
        # the colons align in the rendered body. Body groups two
        # sections, separated by a divider line:
        #   1. Wizard process: steps + data sources
        #   2. Wizard state:   what the operator has committed so far
        body_lines = [
            f"[{_MUTED}]Steps:     [/] {crumb}",
            f"[{_MUTED}]image_root:[/] [{_PRIMARY}]{self._state.image_root}[/]",
            f"[{_MUTED}]catalog:   [/] {catalog_value}",
        ]
        if self._state.mac:
            body_lines.append(f"[{_MUTED}]mac:       [/] [{_PRIMARY}]{self._state.mac}[/]")

        # State-collected lines (selected image / disk). Only emit
        # the divider + section when there's at least one commit;
        # stage-1 boots with nothing selected, no need for an
        # empty section.
        state_lines: list[str] = []
        if self._state.selected_image:
            state_lines.append(
                f"[{_MUTED}]image:     [/] [{_PRIMARY}]{self._state.selected_image.name}[/]"
            )
        if self._state.selected_disk:
            d = self._state.selected_disk
            disk_brief = (
                f"{d.get('path', '?')} ({d.get('size', '?')} {d.get('model') or ''})".strip()
            )
            state_lines.append(f"[{_MUTED}]disk:      [/] [{_PRIMARY}]{disk_brief}[/]")
        if state_lines:
            body_lines.append("")  # blank divider line inside the panel
            body_lines.extend(state_lines)

        # Panel title: ``-={[ pixie vX.Y.Z ]}=-`` with blue brackets +
        # yellow version. Rich preserves markup in the title slot.
        panel_title = (
            f"[bold {_PRIMARY}]-={{[ [/]"
            f"[bold {_ACCENT}]pixie v{pixie.__version__}[/]"
            f"[bold {_PRIMARY}] ]}}=-[/]"
        )
        self._console.print(
            Panel(
                "\n".join(body_lines),
                title=panel_title,
                border_style=_PRIMARY,
            )
        )
        if self._catalog_load_error:
            self._console.print(f"[{_DANGER}]catalog load failed: {self._catalog_load_error}[/]")
        self._console.print()

    def _print_image_table(self, rows: list[_TuiImage]) -> None:
        table = Table(
            show_header=True,
            header_style=f"bold {_PRIMARY}",
            row_styles=("", f"on {_STRIPE}"),
            expand=True,
        )
        table.add_column("#", justify="right", style=_ACCENT, no_wrap=True)
        table.add_column("Name")
        table.add_column("Format", style=_PRIMARY, no_wrap=True)
        table.add_column("Arch", style=_PRIMARY, no_wrap=True)
        table.add_column("Size", justify="right", no_wrap=True)
        table.add_column("Source", style=_MUTED)
        for i, row in enumerate(rows, start=1):
            source = "local" if row.path else "remote"
            table.add_row(
                str(i),
                row.name,
                row.fmt or "?",
                row.arch or "?",
                _format_mib(row.size_bytes) if row.size_bytes else "-",
                source,
            )
        self._console.print(table)
        self._console.print()

    def _print_disk_table(self, rows: list[dict[str, Any]]) -> None:
        table = Table(
            show_header=True,
            header_style=f"bold {_PRIMARY}",
            row_styles=("", f"on {_STRIPE}"),
            expand=True,
        )
        table.add_column("#", justify="right", style=_ACCENT, no_wrap=True)
        table.add_column("Path", style=_PRIMARY, no_wrap=True)
        table.add_column("Size", justify="right", no_wrap=True)
        table.add_column("Model")
        table.add_column("Transport", style=_MUTED, no_wrap=True)
        table.add_column("Serial", style=_MUTED, no_wrap=True)
        for i, d in enumerate(rows, start=1):
            table.add_row(
                str(i),
                str(d.get("path") or d.get("name") or "?"),
                str(d.get("size") or "?"),
                str(d.get("model") or ""),
                str(d.get("tran") or d.get("transport") or ""),
                str(d.get("serial") or ""),
            )
        self._console.print(table)
        self._console.print()

    def _print_empty_catalog_panel(self) -> None:
        body = (
            f"No images visible.\n\n"
            f"[{_MUTED}]Add some via:[/]\n"
            f"  - drop files into [{_PRIMARY}]{self._state.image_root}[/]\n"
            f"  - [{_ACCENT}]d[/] load pixie's default catalog "
            f"(published with pixie as a release artifact)\n"
            f"  - [{_ACCENT}]c[/] provide an http(s):// or oras:// URL "
            f"to a catalog that you host"
        )
        self._console.print(Panel(body, title="Catalog is empty"))
        self._console.print()

    def _print_flash_plan(self, plan: flash.FlashPlan, errors: list[str]) -> None:
        """Rich rendering of the plan -- replaces the
        FlashConfirmScreen modal's body.
        """
        image_lines = [
            f"  image:        {plan.image.url or plan.image.path}",
            f"  format:       {plan.image.format or '?'}",
            f"  size on disk: {_format_mib(plan.image.size_bytes)}"
            f" ({plan.image.size_bytes or 0} bytes)",
        ]
        if plan.image.virtual_size_bytes is not None:
            image_lines.append(f"  virtual size: {_format_mib(plan.image.virtual_size_bytes)}")
        target_lines = [
            f"  target:       {plan.target.path}",
            f"  size:         {_format_mib(plan.target.size_bytes)}",
        ]
        body = "[bold]Image[/]\n" + "\n".join(image_lines)
        body += "\n\n[bold]Target[/]\n" + "\n".join(target_lines)
        if errors:
            # Show WHY it was rejected -- the caller passes the reasons
            # but the panel used to drop them, leaving the operator with
            # a bare "rejected" and no clue (e.g. mounted partitions /
            # image-too-big / unrecognised format).
            body += f"\n\n[bold {_DANGER}]Rejected:[/]\n" + "\n".join(f"  - {e}" for e in errors)
        border_style = _DANGER if errors else _OK
        title = "[red]Flash plan (rejected)[/]" if errors else "[green]Flash plan[/]"
        self._console.print(Panel(body, border_style=border_style, title=title))

    def _render_prompt_line(
        self,
        *,
        extras: tuple[tuple[str, str], ...],
        title: str,
    ) -> str:
        """Build the prompt label shown by ``Prompt.ask``.

        Layout: keybinding guide on the line above, action statement
        attached to the leader on the prompt line itself:

          \\[k] label   \\[k] label  ...     <- keybinding guide
          > <title>: _                    <- leader + action + cursor

        ``>`` marks the action point; the ``title`` immediately after
        describes the action (``Pick an image to flash``, ``Confirm
        flash plan``, ...). For screens whose input is a choice from
        a set (``y/b/q`` etc.), the options should live in ``extras``
        as keybindings -- the prompt line itself stays focused on the
        action, not on enumerating valid keys.
        """
        self._print_keybindings(extras)
        return f"[bold]>[/] [bold]{title}[/]"

    def _print_keybindings(self, extras: tuple[tuple[str, str], ...]) -> None:
        """Print the secondary-key guide above the prompt.

        Renders as one dim line: ``[k] label    [k] label   ...``.
        Skips printing when ``extras`` is empty (the confirm screens
        encode their keys in the choice_hint itself).
        """
        if not extras:
            return
        # ``\[[accent]K[/]] label`` -> renders as ``[K] label`` with K
        # in the accent colour. ``\[`` is Rich's escape for a literal
        # bracket; ``[/]`` closes the most-recent open tag.
        cells = [f"\\[[{_ACCENT}]{key}[/{_ACCENT}]] {label}" for key, label in extras]
        self._console.print(f"[{_MUTED}]" + "   ".join(cells) + "[/]")

    def _sanitize_tty(self) -> None:
        """Restore the controlling tty to canonical mode + echo before
        reading a prompt.

        Companion to ``show_cursor(True)``: that fixes an invisible
        *cursor*, this fixes *dead input*. After a long flash (Rich
        ``Live`` region + ``curl``/``dd`` subprocesses writing to the
        same tty), the live env's framebuffer console / BMC KVM has
        been seen to come back with ``ECHO``/``ICANON`` cleared -- the
        operator types ``y``/Enter and nothing reaches ``input()``, so
        the post-flash reboot prompt looks wedged (the symptom that
        forced a Ctrl+Alt+Del). Re-asserting ICANON+ECHO+ISIG (and
        ICRNL so Enter delivers ``\\n``) makes the prompt readable
        again regardless of what disturbed the tty.

        Best-effort: silently no-ops when stdin isn't a real tty
        (tests, piped input) or ``termios`` is unavailable.
        """
        try:
            import termios
        except ImportError:
            return
        with contextlib.suppress(OSError, termios.error, ValueError):
            fd = sys.stdin.fileno()
            if not os.isatty(fd):
                return
            attrs = termios.tcgetattr(fd)
            attrs[0] |= termios.ICRNL  # iflag: CR -> NL so Enter submits
            attrs[3] |= (  # lflag: canonical line editing + echo + signals
                termios.ICANON | termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ISIG
            )
            termios.tcsetattr(fd, termios.TCSADRAIN, attrs)

    def _ask(self, prompt_text: str) -> str:
        """Single-line prompt with a leading newline so it's clearly
        separated from the rendered panel above. ``show_default=False``
        suppresses Rich's ``()`` annotation after the prompt label --
        the empty-string default is still honoured (Enter returns
        ``""`` which the screens map to ``refresh``); we just don't
        render the parens.

        Belt-and-braces ``show_cursor(True)`` + ``_sanitize_tty()``
        before every prompt: a prior screen's Rich ``Live`` region
        (the flash progress bar) plus the flash subprocesses can
        leave the live env's framebuffer console / BMC KVM with the
        cursor hidden AND input echo/canonical-mode disabled. The
        operator then types at an invisible cursor (or types and
        nothing happens) and thinks the TUI is wedged. Re-asserting
        both costs two cheap syscalls and removes the failure mode.
        """
        self._console.show_cursor(True)
        self._sanitize_tty()
        try:
            answer = (
                Prompt.ask(
                    prompt_text,
                    console=self._console,
                    default="",
                    show_default=False,
                )
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            return "q"
        return answer

    def _pause_for_ack(self) -> None:
        """Tiny ``press Enter to continue``. Used after an error
        message so the operator sees it before the screen redraws.
        """
        self._sanitize_tty()
        with contextlib.suppress(EOFError, KeyboardInterrupt):
            Prompt.ask(
                f"[{_MUTED}](press Enter to continue)[/]",
                console=self._console,
                default="",
                show_default=False,
            )

    # ---------- model helpers (probe, plan, post) ---------------------

    def _refresh_images(self) -> None:
        """Combine local + (optional) catalog overlay into one
        sorted list. Catalog load errors surface via
        ``self._catalog_load_error``.

        Prints a one-line ``loading catalog ...`` indicator BEFORE
        the blocking fetch so an operator on a slow / broken network
        sees where the wait is going. On a healthy LAN the fetch
        finishes inside a second and the indicator scrolls past
        instantly; on a stuck DNS / slow server it tells the
        operator the box is waiting on the network, not wedged.
        """
        local = _list_local_images(self._state.image_root)
        remote: list[_TuiImage] = []
        self._catalog_load_error = None
        if self._state.catalog_source:
            self._console.print(
                f"[{_MUTED}]loading catalog from {self._state.catalog_source} (timeout 30s) ...[/]"
            )
            try:
                remote = load_catalog_from_source(self._state.catalog_source)
            except (
                _catalog.CatalogError,
                urllib.error.URLError,
                OSError,
                ValueError,
            ) as exc:
                self._catalog_load_error = f"{type(exc).__name__}: {exc}"
        # Dedup local + remote by the canonical ``bty_image_ref``
        # (``sha256(canonicalise_src(url))``). Local rows take
        # precedence: a catalog entry (operator-set --catalog overlay)
        # and a catalog entry that target the SAME upstream
        # (typically ``oras://...``) collapse to one row instead of
        # showing both. Plain local image files without a ``url``
        # (raw ``.img.gz`` etc. dropped into PIXIE_IMAGES) skip the
        # dedup -- they're unique by filesystem identity.
        seen_refs: set[str] = set()
        merged: list[_TuiImage] = []
        for img in local:
            if img.url:
                # Malformed URL (not http/https/oras/file): let the
                # row through but don't gate dedup on it.
                with contextlib.suppress(ValueError):
                    seen_refs.add(_catalog.image_ref_for_src(img.url))
            merged.append(img)
        for img in remote:
            if img.url:
                try:
                    ref = _catalog.image_ref_for_src(img.url)
                except ValueError:
                    merged.append(img)
                    continue
                if ref in seen_refs:
                    continue
                seen_refs.add(ref)
            merged.append(img)
        self._state._images = merged

    def _refresh_disks(self) -> None:
        self._state._disks = _list_disks()

    def _parse_index(self, choice: str, n: int) -> int | None:
        """Parse a 1-based numeric choice into a 0-based list index.
        Returns ``None`` for non-numeric / out-of-range input.
        """
        if not choice:
            return None
        try:
            idx = int(choice) - 1
        except ValueError:
            return None
        if 0 <= idx < n:
            return idx
        return None

    def _describe_index_miss(self, choice: str, n: int, kind: str) -> str:
        """Compose a context-specific error message for a failed index parse.

        Distinguishes "empty list" (catalog or disk inventory has no rows --
        no number is valid), "out of range" (a number was typed but the
        list is shorter), and "not a number" (everything else).
        ``kind`` is the singular noun the screen is selecting (``"image"``
        or ``"disk"``); ``n`` is the current list length.
        """
        # Backslash-escape ``[k]`` brackets so Rich renders them as
        # literal text rather than swallowing them as unknown style
        # tags (the strings below all flow through the
        # ``[{_DANGER}]...[/]`` wrapper at the call site).
        if n == 0:
            if kind == "image":
                return (
                    f"No images available; {choice!r} can't pick one. "
                    f"Press Enter to re-scan, \\[c] for a custom catalog source, "
                    f"or \\[d] for the pixie default catalog."
                )
            return (
                f"No disks available; {choice!r} can't pick one. "
                f"Press Enter to re-scan, or check that a target disk is attached."
            )
        if choice.lstrip("-").isdigit():
            return f"{choice!r} is out of range; valid {kind} numbers are 1..{n}."
        return f"Unrecognised choice {choice!r}; type a number 1..{n} or one of the listed keys."

    def _probe_and_plan(
        self,
        image: _TuiImage,
        disk_path: Path,
    ) -> tuple[flash.FlashPlan, list[str]] | str:
        """Probe both ends + build + validate. Returns ``(plan,
        errors)`` on success, or a string error message on probe
        failure (image URL unreachable, target gone, etc.).

        Rendered with a Rich Status spinner so the 1-3s of
        subprocess calls don't look like a wedge.
        """
        from rich.status import Status

        with Status(
            f"[{_ACCENT}]probing image + target ...[/]",
            console=self._console,
        ):
            try:
                if image.url is not None:
                    image_info = flash.probe_image_url(
                        image.url, format_hint=image.fmt, expected_sha=image.sha
                    )
                else:
                    assert image.path is not None  # local row guarantees a path
                    image_info = flash.probe_image(image.path)
            except (FileNotFoundError, ValueError) as exc:
                return f"image probe failed: {exc}"

            try:
                target_info = flash.probe_target(disk_path)
            except (FileNotFoundError, ValueError) as exc:
                return f"target probe failed: {exc}"

        plan = flash.make_plan(image_info, target_info)
        errors = flash.validate_plan(plan)
        return plan, errors

    def _register_uefi_boot_entry(self, plan: flash.FlashPlan) -> None:
        """After a successful flash, optionally register a UEFI NVRAM
        boot entry for the freshly-written disk.

        OFF by default (opt in via ``PIXIE_REGISTER_UEFI_BOOT``): most
        firmware boots the flashed disk on its own, and touching NVRAM
        proved risky on server boards. When enabled it's best-effort and
        UEFI-only (no-op on BIOS); the outcome is printed to the console
        and never blocks the post-flash transition.
        """
        if not _uefi_boot_registration_enabled():
            return
        try:
            msg = flash.register_uefi_boot_entry(plan.target.path)
        except Exception as exc:  # boot-entry setup must never fail the flash
            self._console.print(
                f"[{_DANGER}]pixie: could not register UEFI boot entry: {exc}[/] "
                f"[{_MUTED}](flash succeeded; firmware may not boot the disk)[/]"
            )
            return
        style = _OK if msg.startswith("registered") else _MUTED
        self._console.print(f"[{style}]pixie: {msg}[/]")

    def _post_pxe_done_if_configured(self) -> None:
        """Best-effort: POST ``/pxe/<mac>/done`` after a successful
        flash so the pixie server's last_flashed_at + pixie-flash-once
        flip can fire. Failure is logged via the soft banner; does
        NOT block the post-flash transition (lesson from v0.20.1).
        """
        if self._state.pxe_done_base is None or self._state.mac is None:
            return
        try:
            post_pxe_status(self._state.pxe_done_base, self._state.mac, "done")
        except urllib.error.URLError as exc:
            self._console.print(
                f"[{_DANGER}]post-flash signal failed:[/] {exc} "
                f"[{_MUTED}](flash succeeded; pixie didn't update)[/]"
            )

    def _post_inventory_sync(self) -> None:
        """Post the disk inventory and block until it completes (or
        fails), so a ``mode=inventory`` boot doesn't reboot before
        pixie has the disks. Best-effort (the box reboots either way),
        but the outcome is printed to the console so a failed post isn't
        invisible -- a silent swallow here used to leave operators with
        a box that re-armed the ipxe-exit chain yet reported no disks."""
        if self._state.pxe_done_base is None or self._state.mac is None:
            return
        base = self._state.pxe_done_base
        try:
            payload = disks.list_disks()
        except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
            self._console.print(f"[{_DANGER}]pixie: lsblk failed; no inventory to post: {exc}[/]")
            return
        lshw = collect_lshw()
        try:
            post_inventory(base, self._state.mac, payload, lshw=lshw)
            self._console.print(
                f"[{_OK}]pixie: posted inventory -- {len(payload)} disk(s)"
                f"{', + lshw' if lshw is not None else ', no lshw'} -> {base}[/]"
            )
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            self._console.print(
                f"[{_DANGER}]pixie: inventory POST to {base}/pxe/{self._state.mac}/inventory "
                f"FAILED: {exc}[/]"
            )

    def _auto_post_inventory(self) -> None:
        """Background-thread post of the disk inventory so a slow
        pixie doesn't delay the first paint.
        """
        if self._state.pxe_done_base is None or self._state.mac is None:
            return
        base = self._state.pxe_done_base
        mac = self._state.mac

        def _runner() -> None:
            try:
                payload = disks.list_disks()
            except (FileNotFoundError, subprocess.SubprocessError, OSError):
                return
            lshw = collect_lshw()
            with contextlib.suppress(urllib.error.URLError, ConnectionError, TimeoutError):
                post_inventory(base, mac, payload, lshw=lshw)

        t = threading.Thread(target=_runner, name="pixie-inventory", daemon=True)
        t.start()

    def _do_reboot(self) -> None:
        self._console.print(f"[{_ACCENT}]Rebooting now ...[/]")
        # ``systemctl reboot`` returns promptly after handing off to
        # systemd; bound it so a wedged systemd can't hang the live env
        # at the flash-done screen forever (the box reboots out from
        # under us on success, so a generous cap is fine).
        with contextlib.suppress(FileNotFoundError, OSError, subprocess.TimeoutExpired):
            subprocess.run(["systemctl", "reboot"], check=False, timeout=15)


# ---------------------------------------------------------------------------
# Module-level helpers used by the screens but exposed for test
# isolation.
# ---------------------------------------------------------------------------


def _format_progress_bytes(written: int | None, total: int | None) -> str:
    """Format ``{written} / {total}`` in MiB. Either side may be
    None; ``_format_mib`` renders that (and negatives) as ``?``.
    """
    return f"{_format_mib(written)} / {_format_mib(total)}"


def _emit_console_marker(line: str, *, local_tty: bool = True) -> None:
    """Write a chain-test marker line to every kernel console.

    The PXE chain test (cijoe/configs/test-pxe.toml) reads the
    client VM's QEMU serial log -- which is whatever the kernel
    cmdline names with the LAST ``console=ttyS*,115200`` token.
    Writing only to ``stderr`` would stay on /dev/tty1 because
    pixie-on-tty1.service routes ``StandardError=tty`` ->
    ``TTYPath=/dev/tty1``.

    Three sinks, each best-effort (any can be missing or
    unwritable on a workstation run):

    * ``stderr`` -- the operator-facing path; appears on /dev/tty1
      under the service, on the terminal under a bare ``pixie`` run.
    * ``/dev/kmsg`` -- the kernel log device. Writes here go
      through ``printk`` which broadcasts to ALL registered
      consoles regardless of which one /dev/console happens to
      resolve to. This is the one the chain test actually needs
      when the cmdline lists more than one ``console=ttyS*`` and
      Linux's preferred-console-vs-/dev/console picks an
      unexpected sink. Prefixed with a syslog priority so kmsg
      accepts it as a single line.
    * ``/dev/console`` -- the historical path; kept because on a
      single-serial-console live env it's the most direct route
      to the captured log and one fewer hop than printk.

    ``local_tty=False`` skips the stderr + /dev/console sinks and
    leaves only /dev/kmsg active. Used by the in-flash milestone
    emitter so its lines don't scramble the Rich Live progress
    bar painted on the same /dev/tty1 that stderr resolves to
    under pixie-on-tty1.service. The kmsg path still fans out via
    ``printk`` to every registered serial console, which is what
    a SoL / IPMI observer needs.
    """
    if local_tty:
        print(line, file=sys.stderr, flush=True)
    # ``<6>`` = LOG_INFO; ``/dev/kmsg`` parses a leading
    # ``<prio>`` token so the line shows up as a normal kernel-log
    # entry on every registered console without raising the log
    # level. Without the prefix the write still succeeds but the
    # priority defaults to LOG_NOTICE.
    with contextlib.suppress(OSError), open("/dev/kmsg", "w", encoding="utf-8") as kmsg:
        kmsg.write("<6>" + line + "\n")
    if local_tty:
        with contextlib.suppress(OSError), open("/dev/console", "w", encoding="utf-8") as console:
            console.write(line + "\n")
            console.flush()


class _MilestoneEmitter:
    """Emit a percentage milestone marker once per 25 / 50 / 75 / 100
    crossing, via :func:`_emit_console_marker`.

    The flash progress callback fires many times per second. Calling
    ``_emit_console_marker`` on every event would flood the kernel log
    and the SoL stream. This helper fires AT MOST four times per stage
    (25, 50, 75, 100), at the first event that crosses each boundary.

    Skipped silently when the total is unknown (``None`` or ``<= 0``)
    -- some write paths (gzip, qcow2) can't pre-compute the post-
    decompression size, so we emit just the ``starting`` / ``complete``
    bookends in that case. Cheap enough to construct unconditionally.
    """

    def __init__(self, stage: str) -> None:
        self._stage = stage
        # Milestones to fire, in order. Popped from the front as
        # each one fires so a single ``update`` that jumps past two
        # thresholds at once still emits both in order.
        self._pending = [25, 50, 75, 100]

    def update(self, done: int, total: int | None) -> None:
        if not total or total <= 0:
            return
        pct = min(100, (done * 100) // total)
        while self._pending and pct >= self._pending[0]:
            # Write the milestone DIRECTLY to /dev/console, which
            # resolves to the LAST ``console=`` cmdline target
            # (ttyS0 on every pixie cmdline -- USB, PXE, chain
            # test). That means the bytes hit the serial UART
            # only: a SoL / IPMI operator still sees the heartbeat
            # via the same UART they're watching for everything
            # else, and the chain test still captures it via
            # QEMU's ``-serial file:``.
            #
            # v0.55.11 routed milestones through /dev/kmsg to
            # reach all registered consoles, but kmsg goes
            # through ``printk`` which fans out to /dev/tty0 too.
            # The kernel timestamp + line text WAS landing on
            # the framebuffer console, just covered by Rich's
            # next paint within ~100ms -- invisible to the
            # operator's eye but enough to displace the
            # framebuffer cursor by one line. Rich's internal
            # cursor tracker then thought the top of its render
            # region was higher than it was, and the next bar
            # render landed below the prior one. Three milestones
            # = three stacked bar pairs.
            #
            # Direct /dev/console write skips printk entirely,
            # so /dev/tty0 (and Rich's render region) is never
            # touched. Best-effort: a workstation run without
            # /dev/console writable swallows the OSError.
            line = f"pixie: {self._stage} {self._pending[0]}%\n"
            with (
                contextlib.suppress(OSError),
                open("/dev/console", "w", encoding="utf-8") as console,
            ):
                console.write(line)
                console.flush()
            self._pending.pop(0)


__all__ = [
    "BtyTui",
    "_TuiImage",
    "_WizardStage",
    "_format_mib",
    "_parse_size_to_bytes",
    "load_catalog_from_source",
    "post_inventory",
    "post_pxe_status",
]
