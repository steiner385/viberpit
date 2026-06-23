"""Graceful-drain state machine on SessionOrchestrator."""
import asyncio
import pytest
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory


def _rec(sid: str, status: str) -> SessionRecord:
    return SessionRecord(
        id=sid, name=sid, account="acct", claude_session_id="cs",
        cols=80, rows=24, cwd="/tmp", status=status,
    )


def _orch() -> SessionOrchestrator:
    return SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)


def _drain_events_from(q) -> list[dict]:
    out: list[dict] = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


@pytest.mark.asyncio
async def test_drain_status_idle_when_not_draining():
    o = _orch()
    assert o.drain_status()["state"] == "idle"


@pytest.mark.asyncio
async def test_begin_drain_with_no_active_sessions_is_ready():
    o = _orch()
    q = o.subscribe_events()
    st = await o.begin_drain(selfclear_sec=999)
    assert st["state"] == "ready"
    assert st["busySessions"] == 0
    assert any(e["type"] == "updatePending" for e in _drain_events_from(q))
    o.cancel_drain()


@pytest.mark.asyncio
async def test_begin_drain_with_active_session_is_draining():
    o = _orch()
    o._records["a"] = _rec("a", "in_progress")
    st = await o.begin_drain(selfclear_sec=999)
    assert st["state"] == "draining"
    assert st["busySessions"] == 1
    o.cancel_drain()


@pytest.mark.asyncio
async def test_cancel_drain_publishes_cancelled_and_returns_idle():
    o = _orch()
    q = o.subscribe_events()
    await o.begin_drain(selfclear_sec=999)
    st = o.cancel_drain()
    assert st["state"] == "idle"
    assert any(e["type"] == "updateCancelled" for e in _drain_events_from(q))


@pytest.mark.asyncio
async def test_self_clear_cancels_drain_after_timeout():
    o = _orch()
    o._records["a"] = _rec("a", "in_progress")  # stays busy so it can't go ready
    await o.begin_drain(selfclear_sec=0.05)
    await asyncio.sleep(0.15)
    assert o.drain_status()["state"] == "idle"  # self-cleared


@pytest.mark.asyncio
async def test_send_prompt_rejected_while_draining():
    o = _orch()
    q = o.subscribe_events()
    await o.begin_drain(selfclear_sec=999)
    # Drain a fresh queue to ignore the begin_drain updatePending:
    while not q.empty():
        q.get_nowait()
    # No driver registered for this id; the guard must return BEFORE touching _drivers,
    # so this must not raise even though the session is unknown.
    await o.send_prompt("unknown-session", "hello")
    evs = _drain_events_from(q)
    assert any(e["type"] == "updatePending" for e in evs)
    o.cancel_drain()
