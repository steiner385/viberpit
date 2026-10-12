"""A failed reassign must not leave the closed old driver installed (#3007).

Incident (2026-10-12, dobby): after a host-wide OOM, three sessions' stream_lost
recompose failed (the new CLI's ``initialize`` timed out, #3003). `_reassign_locked`
had already closed the old driver but only installs a driver on success, so the dead
one — ``stream_ended is True`` — stayed in `_drivers`. Every prompt then bounced as
"recovering" with a WARNING claiming a recompose was in flight (none was), and
ensure_driver's fast path kept handing the dead driver back, so nothing rebuilt the
session until the 300 s stuck-session sweep.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

import server.sessions.orchestrator as orch_mod
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


def _make_orch():
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o._clock = lambda: 1000.0
    o.paused = []

    async def spy_pause(record, reason, earliest_reset=None):
        o.paused.append((record.id, reason))
        record.paused_reason = reason

    o._pause_session = spy_pause
    o._copilot = None
    o._suggester = None
    o._session_factory = lambda sid: lambda *a, **k: None
    o._install_driver = lambda sid, drv: o._drivers.__setitem__(sid, drv)
    o._publish_event = lambda e: None
    return o


def _stub_env(monkeypatch):
    class _MockDB:
        async def execute(self, *a): pass

    class _FakeDbCtx:
        async def __aenter__(self): return _MockDB()
        async def __aexit__(self, *a): pass

    monkeypatch.setattr(orch_mod, "db_session", lambda: _FakeDbCtx())
    monkeypatch.setattr(orch_mod, "compose", lambda acct: Path("/fake/cfg"))
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a, **k: None)


class _OldDriver:
    engine_session_id = "old-csid"
    mode = "interactive"
    identity = "user@example.com"

    def __init__(self, cwd):
        self.cwd = cwd
        self.stream_ended = True    # the SDK stream died — why we are recomposing

    async def interrupt(self): pass
    async def aclose(self): pass

    async def send_prompt(self, *a, **k):
        raise AssertionError("must not reach a dead driver")


class _NewDriver:
    engine_session_id = None
    mode = "interactive"

    async def start(self):
        raise Exception("Control request timeout: initialize")

    async def interrupt(self): pass
    async def aclose(self): pass
    async def set_model(self, m): pass


def _rec(o, tmp_path):
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                        engine_session_id="c1", cols=80, rows=24, cwd=str(tmp_path))
    o._records[rec.id] = rec
    return rec


@pytest.mark.asyncio
async def test_failed_reassign_releases_the_closed_old_driver(monkeypatch, tmp_path, caplog):
    o = _make_orch()
    rec = _rec(o, tmp_path)
    o._drivers[rec.id] = _OldDriver(str(tmp_path))
    _stub_env(monkeypatch)
    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_create_driver",
                        lambda self, **kw: _NewDriver())

    result = await asyncio.wait_for(
        o._reassign(rec, rec.account, reason="stream_lost", replay_last_prompt=False), 5.0)

    assert result == "failed"
    assert o.paused == [(rec.id, orch_mod._REASSIGN_FAILED_PAUSE_REASON)]
    assert rec.id not in o._drivers, "the closed old driver must not stay installed"

    # The session now reads as detached: a prompt is handed back for a resume, not
    # refused under a "recompose in flight" WARNING.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        assert await o.send_prompt(rec.id, "hi") == "no_driver"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_failure_after_install_keeps_the_new_driver(monkeypatch, tmp_path):
    """Only the CLOSED old driver is released — a failure past the install (here the
    DB write) leaves the new, live driver in place."""
    o = _make_orch()
    rec = _rec(o, tmp_path)
    o._drivers[rec.id] = _OldDriver(str(tmp_path))
    _stub_env(monkeypatch)

    class _Starts(_NewDriver):
        async def start(self): pass

    built = _Starts()
    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_create_driver",
                        lambda self, **kw: built)

    class _BoomCtx:
        async def __aenter__(self): raise RuntimeError("db down")
        async def __aexit__(self, *a): pass

    monkeypatch.setattr(orch_mod, "db_session", lambda: _BoomCtx())

    result = await o._reassign(rec, rec.account, reason="stream_lost",
                               replay_last_prompt=False)

    assert result == "failed"
    assert o._drivers.get(rec.id) is built


@pytest.mark.asyncio
async def test_ensure_driver_does_not_hand_back_a_parked_dead_driver(tmp_path):
    """Backstop: a dead driver on a session recovery already parked is released and
    the session resumed, not returned as if it were live."""
    o = _make_orch()
    rec = _rec(o, tmp_path)
    rec.paused_reason = orch_mod._REASSIGN_FAILED_PAUSE_REASON
    dead = _OldDriver(str(tmp_path))
    o._drivers[rec.id] = dead
    claimed = []

    async def claim(sid, *, why):
        claimed.append(sid)
        return False    # stop the resume here: reaching the claim is the proof

    o._claim_session = claim

    assert await o.ensure_driver(rec.id) is None
    assert claimed == [rec.id], "a parked dead driver must fall through to a resume"
    assert rec.id not in o._drivers


@pytest.mark.asyncio
async def test_ensure_driver_leaves_a_dead_driver_alone_before_recovery_parks(tmp_path):
    """Unparked, a dead driver is in the gap before recovery starts its swap.
    Rebuilding it here would race that swap into a twin CLI (#1040)."""
    o = _make_orch()
    rec = _rec(o, tmp_path)
    dead = _OldDriver(str(tmp_path))
    o._drivers[rec.id] = dead

    assert await o.ensure_driver(rec.id) is dead


@pytest.mark.asyncio
async def test_ensure_driver_leaves_a_dead_driver_alone_during_a_swap(tmp_path):
    o = _make_orch()
    rec = _rec(o, tmp_path)
    rec.paused_reason = orch_mod._REASSIGN_FAILED_PAUSE_REASON
    dead = _OldDriver(str(tmp_path))
    o._drivers[rec.id] = dead
    swap = asyncio.get_running_loop().create_future()
    o._swapping[rec.id] = swap

    assert await o.ensure_driver(rec.id) is dead
    swap.set_result(None)
