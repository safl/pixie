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

from pixie.exports._store import Overlay, OverlaysStore
from pixie.machines._store import OVERLAY_ALIAS_RE
from pixie.pxe._renderer import overlay_qcow2_path


def resolve_overlay_bind(
    *,
    overlays: OverlaysStore,
    overlays_dir: Path,
    mac: str,
    boot_mode: str,
    image_sha: str,
    alias: str,
) -> tuple[str, str]:
    """Resolve a machine's requested overlay attachment.

    Returns ``(effective_image_sha, resolved_alias)``: the image the
    machine should bind (implied by the alias when attaching an existing
    one) and the alias actually attached ("" for ephemeral). Side
    effects: creates the overlay row for a new alias, claims the
    single-writer hold on the resolved alias, and releases every other
    alias this machine held. Raises :class:`ValueError` if the alias is
    held by a different machine (single-writer), leaving all state
    untouched.

    ``alias`` is honoured only for ``boot_mode == 'nbdboot'``; every
    other mode folds back to ephemeral and this releases any hold the
    machine had.
    """
    alias = (alias or "").strip()
    if boot_mode != "nbdboot" or not alias:
        # Ephemeral (or a non-nbdboot mode): the machine writes no
        # overlay, so drop any hold it still carries.
        overlays.detach_mac(mac, keep="")
        return (image_sha, "")

    # Validate BEFORE any row/path is written: a new alias becomes
    # ``<overlays_dir>/<alias>.qcow2``, so a ``..``/slash name must be
    # refused here rather than escaping the storage tree. Same rule the
    # machines store enforces on the persisted binding.
    if not OVERLAY_ALIAS_RE.match(alias):
        raise ValueError(
            "overlay_alias must be alphanumeric-leading; a-z / A-Z / 0-9 / . _ - (max 64 chars)"
        )

    existing = overlays.get(alias)
    if existing is not None:
        # Single-writer: refuse to hand an alias held by another machine.
        if existing.attached_mac and existing.attached_mac != mac:
            raise ValueError(f"overlay {alias!r} held by {existing.attached_mac}; detach first")
        # Alias implies image: bind to the overlay's base image.
        resolved_image = existing.image_sha
    else:
        # New alias needs a base image; without one there is nothing to
        # layer over, so fold back to ephemeral.
        if not image_sha:
            overlays.detach_mac(mac, keep="")
            return (image_sha, "")
        overlays.upsert(
            Overlay(
                alias=alias,
                image_sha=image_sha,
                qcow2_path=str(overlay_qcow2_path(overlays_dir, alias)),
            )
        )
        resolved_image = image_sha

    overlays.attach(alias, mac)
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
