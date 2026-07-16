"""Contract tests for the closed event-kind registry.

Every action pixie takes carries an event log entry with a
well-defined identifier from :mod:`pixie.events._kinds`. Two things
have to hold:

1. Every constant declared in the module is included in the
   ``KNOWN_EVENT_KINDS`` frozenset (so a caller importing the
   constant can trust the emit call will not raise).
2. :meth:`EventsLog.emit` rejects any string not in the frozenset
   with :class:`UnknownEventKind` (so a new mutation site cannot
   ship without registering its identifier first).
"""

from __future__ import annotations

import pytest

from pixie.events import KNOWN_EVENT_KINDS, EventsLog, UnknownEventKind
from pixie.events import _kinds as kind_constants


def test_every_module_constant_is_registered() -> None:
    """Every all-caps string attribute exported by
    :mod:`pixie.events._kinds` must appear in ``KNOWN_EVENT_KINDS``.
    Prevents drift when a caller imports a constant that was defined
    but forgotten in the registry."""
    for name in dir(kind_constants):
        if name.startswith("_") or not name.isupper():
            continue
        value = getattr(kind_constants, name)
        if not isinstance(value, str):
            continue
        assert value in KNOWN_EVENT_KINDS, (
            f"{name}={value!r} declared in _kinds.py but missing from KNOWN_EVENT_KINDS"
        )


def test_emit_rejects_unknown_kind(tmp_path) -> None:
    """The closed-set enforcement is the contract users rely on. A
    caller passing a string not in the registry must fail loud."""
    log = EventsLog(tmp_path / "state.db")
    with pytest.raises(UnknownEventKind):
        log.emit("not.a.real.kind", summary="should not land")


def test_emit_accepts_every_registered_kind(tmp_path) -> None:
    """Round-trip check: emitting each registered kind writes a row
    and ``list()`` returns them. Catches a typo where a constant
    string diverges from what the frozenset carries."""
    log = EventsLog(tmp_path / "state.db")
    for kind in sorted(KNOWN_EVENT_KINDS):
        log.emit(kind, summary=f"round-trip: {kind}")
    rows = log.list(limit=len(KNOWN_EVENT_KINDS) + 10)
    emitted = {r.kind for r in rows}
    assert emitted == KNOWN_EVENT_KINDS
