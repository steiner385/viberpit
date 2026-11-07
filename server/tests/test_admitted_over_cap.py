"""An override may take a daemon past its cap (#3255, #3358); the daemon has to say
how many.

Resumes and "Start now" are admitted without the launch gate, so the over-cap count
they leave is expected. ``admitted_over_cap_count`` is what /info publishes so the fleet alarm can
excuse exactly that much overage, and nothing else. The count is pruned to sessions that
still hold a slot, so an ended session stops being excused.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from server.ipc import daemon
from server.sessions.orchestrator import SessionOrchestrator


class _Orch:
    """Just enough of SessionOrchestrator for the override-tracking methods."""

    _consumes_capacity_slot = SessionOrchestrator._consumes_capacity_slot
    admitted_over_cap_count = SessionOrchestrator.admitted_over_cap_count
    _note_admitted_over_cap = SessionOrchestrator._note_admitted_over_cap

    def __init__(self, records, resumed, *, owned=0, local_cap=None, fleet_cap=11):
        self._records = records
        self._admitted_over_cap = set(resumed)
        self._owned, self._local_cap, self._fleet_cap = owned, local_cap, fleet_cap

    async def owned_active_session_count(self):
        return self._owned

    async def _local_host_cap(self):
        return self._local_cap

    async def _max_active_sessions(self):
        return self._fleet_cap


def _rec(status="done", kind="coding"):
    return SimpleNamespace(status=status, kind=kind)


def test_counts_resumed_sessions_that_still_hold_a_slot():
    orch = _Orch({"a": _rec(), "b": _rec("running")}, {"a", "b"})
    assert asyncio.run(orch.admitted_over_cap_count()) == 2


def test_an_ended_session_stops_being_excused():
    orch = _Orch({"a": _rec(), "b": _rec("complete")}, {"a", "b"})
    assert asyncio.run(orch.admitted_over_cap_count()) == 1
    # Pruned for good: a session that stopped holding a slot does not come back.
    assert orch._admitted_over_cap == {"a"}


def test_a_session_no_longer_on_this_daemon_is_dropped():
    orch = _Orch({"a": _rec()}, {"a", "gone"})
    assert asyncio.run(orch.admitted_over_cap_count()) == 1


def test_admitted_over_cap_count_degrades_to_zero_on_failure():
    class _Broken:
        async def admitted_over_cap_count(self):
            raise RuntimeError("store unavailable")

    # Reporting too few keeps the alarm's WARNING in place; too many would hide a fault.
    assert asyncio.run(daemon._admitted_over_cap_count(_Broken())) == 0
    assert asyncio.run(daemon._admitted_over_cap_count(None)) == 0


def test_an_override_past_the_host_cap_is_recorded_below_the_fleet_cap():
    """dobby 6/5 inside a fleet cap of 11 (#3358): the HOST cap is what the alarm
    compares against, so that is the one an override is measured by."""
    orch = _Orch({"a": _rec()}, set(), owned=6, local_cap=5, fleet_cap=11)
    asyncio.run(orch._note_admitted_over_cap("a"))
    assert orch._admitted_over_cap == {"a"}


def test_an_override_within_the_host_cap_is_not_recorded():
    orch = _Orch({"a": _rec()}, set(), owned=5, local_cap=5)
    asyncio.run(orch._note_admitted_over_cap("a"))
    assert orch._admitted_over_cap == set()


def test_an_uncapped_host_records_nothing():
    orch = _Orch({"a": _rec()}, set(), owned=50, local_cap=None)
    asyncio.run(orch._note_admitted_over_cap("a"))
    assert orch._admitted_over_cap == set()
