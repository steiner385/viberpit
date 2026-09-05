"""set_account_pin — the unpin button. Unpinning a `limited` session rotates it now."""
import asyncio

import pytest

from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def _orch(rec):
    o = SessionOrchestrator.__new__(SessionOrchestrator)
    o._records = {rec.id: rec}
    o.calls = []

    async def _persist_pin(sid, pinned): o.calls.append(("persist", pinned))

    async def _rotate_away(record, *, reason): o.calls.append(("rotate", reason))

    o._persist_pin = _persist_pin
    o._rotate_away = _rotate_away
    o._bg_tasks = set()
    o._spawn = lambda coro, name=None: o._bg_tasks.add(asyncio.get_running_loop().create_task(coro))
    return o


def _rec(status="idle", pinned=True):
    return SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                         cols=80, rows=24, cwd="/tmp", status=status, account_pinned=pinned)


@pytest.mark.asyncio
async def test_unpin_persists_and_rotates_a_limited_session():
    rec = _rec(status="limited")
    o = _orch(rec)
    assert await o.set_account_pin("s1", False) == "unpinned"
    await asyncio.gather(*o._bg_tasks)
    assert rec.account_pinned is False
    assert ("persist", False) in o.calls
    assert ("rotate", "unpinned") in o.calls


@pytest.mark.asyncio
async def test_unpin_idle_session_does_not_rotate():
    rec = _rec(status="idle")
    o = _orch(rec)
    assert await o.set_account_pin("s1", False) == "unpinned"
    assert not any(c[0] == "rotate" for c in o.calls)


@pytest.mark.asyncio
async def test_pin_persists_current_account():
    rec = _rec(status="idle", pinned=False)
    o = _orch(rec)
    assert await o.set_account_pin("s1", True) == "pinned"
    assert rec.account_pinned is True and ("persist", True) in o.calls


@pytest.mark.asyncio
async def test_unknown_session():
    o = _orch(_rec())
    assert await o.set_account_pin("zz", False) == "skipped:not-found"
