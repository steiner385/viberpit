"""/info carries the capacity signal the fleet ranker consumes.

Extending /info rather than adding an endpoint keeps the probe a single
round-trip, and means any daemon old enough to answer /info at all stays visible
to the fleet — it just reports no metrics.

The NUMBER matters as much as the envelope. `_dispatchable` excludes a host on
``active_sessions >= max_sessions``, so an over-count takes the host out of the
fleet permanently and silently — the only symptom is a user-facing "no enabled,
healthy host with capacity". Observed 2026-09-09: dobby reported 11/6 with a
single live driver, because /info published ``len(orch._records)`` — every record,
of every status, and (on the primary, which deliberately holds every host's rows
for the fleet dashboard) of every host. The tests below pin both filters.
"""
from __future__ import annotations

from server.ipc.daemon import _capacity_snapshot


def test_capacity_snapshot_shape():
    snap = _capacity_snapshot(active_sessions=3)
    assert snap["activeSessions"] == 3
    assert isinstance(snap["loadAvg"], float)
    assert isinstance(snap["memAvailableMb"], int)
    assert snap["memAvailableMb"] > 0, "a running host has some available memory"
    assert snap["loadAvg"] >= 0.0


def test_capacity_snapshot_survives_unreadable_meminfo(monkeypatch):
    """Degrade to None, never raise — unreadable /proc must not take /info down."""
    import server.ipc.daemon as d

    monkeypatch.setattr(d, "_read_mem_available_mb", lambda: None)
    assert d._capacity_snapshot(active_sessions=0)["memAvailableMb"] is None


# ---------------------------------------------------------------------------
# The number /info publishes: slots occupied ON THIS DAEMON.
# ---------------------------------------------------------------------------

def _rec(sid, *, status, host, kind=None):
    from server.sessions.engine import AgentEngine
    from server.sessions.types import SessionRecord
    return SessionRecord(id=sid, name=sid, account="acct",
                         engine=AgentEngine.CLAUDE_CODE, engine_session_id="",
                         cols=80, rows=24, cwd="/tmp",
                         status=status, host=host, kind=kind)


def _primary(records, monkeypatch, *, me="dobby", enabled=frozenset({"dobby", "rocky"})):
    """A primary orchestrator holding *records*, with the registry stubbed."""
    import server.hostname as hostname_mod
    import server.sessions.orchestrator as om

    monkeypatch.setattr(hostname_mod, "self_host_name", lambda: me)
    monkeypatch.setattr(om, "self_host_name", lambda: me)
    monkeypatch.setattr(om, "daemon_role", lambda: "primary")

    orch = om.SessionOrchestrator()
    orch._records = {r.id: r for r in records}
    orch._drivers = {}

    async def _hosts():
        return enabled
    monkeypatch.setattr(orch, "_enabled_host_names", _hosts)
    return orch


def _count(orch) -> int:
    import asyncio
    return asyncio.run(orch.owned_active_session_count())


def test_complete_sessions_do_not_hold_a_capacity_slot(monkeypatch):
    """`complete` is in CAPACITY_EXEMPT_STATUSES — it must not reach the fleet gate.

    This is the 2026-09-09 outage in one assertion: ten finished automation
    sessions pinned dobby over its cap of 6 while one driver was running.
    """
    orch = _primary([_rec(f"c{i}", status="complete", host="dobby") for i in range(10)]
                    + [_rec("live", status="in_progress", host="dobby")], monkeypatch)
    assert _count(orch) == 1


def test_another_hosts_sessions_do_not_count_against_this_daemon(monkeypatch):
    """The primary holds every host's rows for the dashboard; capacity is per-host."""
    orch = _primary([_rec("mine", status="in_progress", host="dobby"),
                     _rec("theirs1", status="in_progress", host="rocky"),
                     _rec("theirs2", status="idle", host="rocky")], monkeypatch)
    assert _count(orch) == 1


def test_live_sessions_on_this_host_still_count(monkeypatch):
    """The guard must not swing the other way — a real slot must be reported."""
    orch = _primary([_rec("a", status="in_progress", host="dobby"),
                     _rec("b", status="idle", host="dobby"),
                     _rec("c", status="done", host="dobby")], monkeypatch)
    assert _count(orch) == 3, "idle and done hold a warm driver — they occupy a slot"


def test_the_assistant_drawer_never_consumes_a_coding_slot(monkeypatch):
    """Same carve-out _active_session_count already makes; it must not regress here."""
    orch = _primary([_rec("drawer", status="in_progress", host="dobby", kind="assistant"),
                     _rec("real", status="in_progress", host="dobby")], monkeypatch)
    assert _count(orch) == 1
