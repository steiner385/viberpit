"""A resume may take a daemon past its cap (#3255); the daemon has to say how many.

Resumes are admitted without the launch gate, so the over-cap count they leave is
expected. ``resumed_over_cap_count`` is what /info publishes so the fleet alarm can
excuse exactly that much overage, and nothing else. The count is pruned to sessions that
still hold a slot, so an ended session stops being excused.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from server.ipc import daemon
from server.sessions.orchestrator import SessionOrchestrator


class _Orch:
    """Just enough of SessionOrchestrator for the resume-tracking methods."""

    _consumes_capacity_slot = SessionOrchestrator._consumes_capacity_slot
    resumed_over_cap_count = SessionOrchestrator.resumed_over_cap_count

    def __init__(self, records, resumed):
        self._records = records
        self._resumed_over_cap = set(resumed)


def _rec(status="done", kind="coding"):
    return SimpleNamespace(status=status, kind=kind)


def test_counts_resumed_sessions_that_still_hold_a_slot():
    orch = _Orch({"a": _rec(), "b": _rec("running")}, {"a", "b"})
    assert asyncio.run(orch.resumed_over_cap_count()) == 2


def test_an_ended_session_stops_being_excused():
    orch = _Orch({"a": _rec(), "b": _rec("complete")}, {"a", "b"})
    assert asyncio.run(orch.resumed_over_cap_count()) == 1
    # Pruned for good: a session that stopped holding a slot does not come back.
    assert orch._resumed_over_cap == {"a"}


def test_a_session_no_longer_on_this_daemon_is_dropped():
    orch = _Orch({"a": _rec()}, {"a", "gone"})
    assert asyncio.run(orch.resumed_over_cap_count()) == 1


def test_resumed_over_cap_count_degrades_to_zero_on_failure():
    class _Broken:
        async def resumed_over_cap_count(self):
            raise RuntimeError("store unavailable")

    # Reporting too few keeps the alarm's WARNING in place; too many would hide a fault.
    assert asyncio.run(daemon._resumed_over_cap_count(_Broken())) == 0
    assert asyncio.run(daemon._resumed_over_cap_count(None)) == 0
