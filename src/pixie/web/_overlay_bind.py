"""Shared overlay-attachment resolution for the bind routes.

Both the operator form (``POST /ui/machines/bind``) and the JSON API
(``PUT /machines/{mac}``) let a machine attach a persistent overlay by
alias. The rules -- single-writer enforcement, alias-implies-image, and
lazy row-creation for a brand-new alias -- must be identical on both
paths, so they live here rather than duplicated per route.

An overlay is a globally-unique writable volume over ONE base image
(see :class:`pixie.exports._store.Overlay`). Attaching an existing alias
binds the machine to that overlay's base image; creating a new alias
takes the base image the operator selected. At most one machine may hold
an alias at a time -- attaching one held elsewhere raises.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pixie.exports._store import OverlaysStore
from pixie.machines._store import OVERLAY_ALIAS_RE


def resolve_overlay_bind(
    *,
    overlays: OverlaysStore,
    mac: str,
    boot_mode: str,
    image_sha: str,
    alias: str,
) -> tuple[str, str]:
    """Resolve a machine's requested overlay attachment.

    Returns ``(effective_image_sha, resolved_alias)``: the image the
    machine should bind (implied by the alias for ``nbdboot-overlay``)
    and the alias actually attached ("" for every other mode). Side
    effects: claims the single-writer hold on the resolved alias and
    releases every other alias this machine held. Raises
    :class:`ValueError` if the alias does not exist, is malformed, or is
    held by a different machine, leaving all state untouched.

    Selecting an overlay never CREATES it -- the overlay must already
    exist (materialized on the Overlays page). ``alias`` is honoured
    only for ``boot_mode == 'nbdboot-overlay'``; every other mode
    (including ``nbdboot-ephemeral``) folds back to no overlay and this
    releases any hold the machine had.
    """
    alias = (alias or "").strip()
    if boot_mode != "nbdboot-overlay":
        # Ephemeral nbdboot or any non-overlay mode: the machine writes
        # no persistent overlay, so drop any hold it still carries.
        overlays.detach_mac(mac, keep="")
        return (image_sha, "")

    # nbdboot-overlay requires an alias: without one there is nothing to
    # attach, so the operator must pick an existing overlay.
    if not alias:
        raise ValueError("nbdboot-overlay requires an overlay; select one or create it first")

    # Defence-in-depth: a malformed alias should never have reached a row
    # (creation validates the same rule), but reject it here too so the
    # error is legible rather than a bare not-found.
    if not OVERLAY_ALIAS_RE.match(alias):
        raise ValueError(
            "overlay_alias must be alphanumeric-leading; a-z / A-Z / 0-9 / . _ - (max 64 chars)"
        )

    existing = overlays.get(alias)
    if existing is None:
        # No lazy creation: an overlay must exist before it can be
        # selected, exactly like an image must be fetched before it can
        # be bound. Point the operator at the one place overlays are made.
        raise ValueError(f"overlay {alias!r} does not exist; create it on the Overlays page first")
    # Single-writer: refuse to hand an alias held by another machine.
    if existing.attached_mac and existing.attached_mac != mac:
        raise ValueError(f"overlay {alias!r} held by {existing.attached_mac}; detach first")
    # Alias implies image: bind to the overlay's base image.
    resolved_image = existing.image_sha

    if not overlays.try_claim(alias, mac):
        # Lost the race: another machine claimed this alias between the
        # get() above and here. Refuse rather than last-write-win over
        # its hold (the atomic claim is what closes the window).
        raise ValueError(f"overlay {alias!r} was just claimed by another machine; retry")
    overlays.detach_mac(mac, keep=alias)
    return (resolved_image, alias)


def overlay_state(state: Any) -> tuple[OverlaysStore, Path]:
    """Pull ``(overlays_store, overlays_dir)`` off ``app.state`` with a
    clear error if the app wasn't fully wired (keeps the bind routes from
    raising a bare ``AttributeError``)."""
    overlays = getattr(state, "overlays_store", None)
    overlays_dir = getattr(state, "overlays_dir", None)
    if overlays is None or overlays_dir is None:
        raise RuntimeError("overlays store not initialised")
    return (overlays, Path(overlays_dir))
