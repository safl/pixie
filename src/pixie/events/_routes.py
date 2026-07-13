"""HTTP routes for the events log.

Read routes are OPEN because the on-call operator glances at
``GET /events`` from a workstation curl; the events themselves are
just names + timestamps + already-visible fields, no secrets.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from pixie.events._log import EventsLog

router = APIRouter()


def _get_log(request: Request) -> EventsLog:
    log: EventsLog | None = getattr(request.app.state, "events_log", None)
    if log is None:
        raise HTTPException(status_code=503, detail="events log not initialised")
    return log


@router.get("/events")
def list_events(
    request: Request,
    kind: str = Query("", description="filter by exact kind (empty = all kinds)"),
    subject_kind: str = Query("", description="filter by subject_kind"),
    subject_id: str = Query("", description="filter by subject_id"),
    since_id: int = Query(0, ge=0, description="return only events with id > since_id"),
    limit: int = Query(100, ge=1, le=1000, description="max rows"),
) -> dict[str, list[dict[str, Any]]]:
    rows = _get_log(request).list(
        kind=kind,
        subject_kind=subject_kind,
        subject_id=subject_id,
        since_id=since_id,
        limit=limit,
    )
    return {"events": [r.to_dict() for r in rows]}
