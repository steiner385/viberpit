"""A turn dropped by daemon teardown must NOT run failure recovery.

When the daemon is shutting down (deploy or watchdog restart), closing a live
driver mid-turn fires the recoverable-failure callback. Before this guard, that
escalated through rotation/recompose to needs_attention, stranding every
in-flight session on a restart (incident 2026-07-02). While shutting down the
callback is a no-op; rehydrate() on the next boot restores the row as idle.
"""
import asyncio


from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


def _make_orch():
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    rec = SessionRecord(id="s1", name="n", account="kindash", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp/x")
    o._records["s1"] = rec
    o._drivers["s1"] = object()
    calls = []

    async def spy_recover(record, kind, retry_after):
        calls.append((record.id, kind))

    o._recover_session = spy_recover
    return o, calls


def test_recovery_skipped_while_shutting_down():
    async def go():
        o, calls = _make_orch()
        o._shutting_down = True
        await o._handle_recoverable_failure("s1", "auth_401", None)
        await asyncio.sleep(0)  # let any (wrongly) spawned recover task run
        return calls

    assert asyncio.run(go()) == []


def test_recovery_runs_when_not_shutting_down():
    async def go():
        o, calls = _make_orch()
        await o._handle_recoverable_failure("s1", "auth_401", None)
        await asyncio.sleep(0)  # the recover task is spawned onto the loop
        return calls

    assert asyncio.run(go()) == [("s1", "auth_401")]


def test_stop_sets_shutting_down_flag():
    async def go():
        o, _ = _make_orch()
        assert o._shutting_down is False
        await o.stop()
        return o._shutting_down

    assert asyncio.run(go()) is True
