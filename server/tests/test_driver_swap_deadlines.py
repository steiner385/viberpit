"""Driver swaps are bounded (#2967).

Incident (2026-10-11, rocky): a setup-token 401 rotated session f094e5d2 to another
account. The swap (_reassign) took about 3 minutes — 80 s to close the old driver and
claim the lease, then 85 s no log line accounted for, then the CLI start. `_swapping`
stayed set for all of it, and send_prompt refuses a prompt once it has held it for
_SWAP_PROMPT_HOLD_S (20 s), so every prompt the user sent was refused.

Each step of a rebuild now runs under a deadline: one driver start() under
DRIVER_START_TIMEOUT_S (the half-built driver is closed on overrun), the rebuild as
a whole under DRIVER_SWAP_TIMEOUT_S, and _reassign's wait for a prior swap of the
same session under the same budget. Each step's wall time is logged.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

import server.sessions.orchestrator as orch_mod
from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import DriverStartTimeout, SessionOrchestrator
from server.sessions.types import SessionRecord


def _make_orch():
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o._clock = lambda: 1000.0
    o.paused = []

    async def spy_pause(record, reason, earliest_reset=None):
        o.paused.append((record.id, reason))

    o._pause_session = spy_pause
    o._copilot = None
    o._suggester = None
    o._session_factory = lambda sid: lambda *a, **k: None
    o._install_driver = lambda sid, drv: o._drivers.__setitem__(sid, drv)
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
        self.closed = False

    async def interrupt(self): pass
    async def aclose(self): self.closed = True


def _install_new_driver(monkeypatch, *, start):
    built = []

    class _NewDriver:
        engine_session_id = None
        mode = "interactive"

        def __init__(self):
            self.closed = False
            built.append(self)

        async def start(self): await start()
        async def interrupt(self): pass
        async def aclose(self): self.closed = True
        async def set_model(self, m): pass

    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_create_driver",
                        lambda self, **kw: _NewDriver())
    return built


def _rec(o, tmp_path):
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                        engine_session_id="c1", cols=80, rows=24, cwd=str(tmp_path))
    o._records[rec.id] = rec
    return rec


async def _hang():
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_reassign_new_driver_start_overrun_closes_it_and_parks(
        monkeypatch, tmp_path, caplog):
    o = _make_orch()
    rec = _rec(o, tmp_path)
    o._drivers[rec.id] = _OldDriver(str(tmp_path))
    _stub_env(monkeypatch)
    monkeypatch.setattr(orch_mod, "DRIVER_START_TIMEOUT_S", 0.05)
    built = _install_new_driver(monkeypatch, start=_hang)
    o._publish_event = lambda e: None

    with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        result = await asyncio.wait_for(o._reassign(rec, "b", reason="auth_401"), 5.0)

    assert result == "failed"
    assert built and built[0].closed, "the half-built driver must be closed on overrun"
    assert o._drivers.get(rec.id) is not built[0], (
        "a driver that never started must not be installed")
    assert o.paused == [(rec.id, orch_mod._REASSIGN_FAILED_PAUSE_REASON)]
    assert rec.id not in o._swapping
    assert rec.id not in o._overlays
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("reassign.start session=s1 a→b reason=auth_401")
               for m in messages), messages
    timeout_lines = [m for m in messages if m.startswith("reassign.timeout")]
    assert timeout_lines and "'claim'" in timeout_lines[0], (
        f"the timeout must name the steps that finished: {messages}")


@pytest.mark.asyncio
async def test_reassign_overall_deadline_bounds_pre_start_steps(monkeypatch, tmp_path):
    """The 85 s gap on 2026-10-11 sat between the claim and the CLI start — steps
    the per-start budget does not cover. The swap budget does."""
    o = _make_orch()
    rec = _rec(o, tmp_path)
    o._drivers[rec.id] = _OldDriver(str(tmp_path))
    _stub_env(monkeypatch)
    monkeypatch.setattr(orch_mod, "DRIVER_SWAP_TIMEOUT_S", 0.05)
    started = []

    async def _start():
        started.append(True)

    built = _install_new_driver(monkeypatch, start=_start)
    o._publish_event = lambda e: None

    async def _hanging_overlay(*a, **k):
        await _hang()

    o._setup_overlay_for_kind = _hanging_overlay

    result = await asyncio.wait_for(o._reassign(rec, "b", reason="auth_401"), 5.0)

    assert result == "failed"
    assert not built and not started
    assert o.paused == [(rec.id, orch_mod._REASSIGN_FAILED_PAUSE_REASON)]
    assert rec.id not in o._swapping


@pytest.mark.asyncio
async def test_reassign_deadline_during_start_still_closes_the_new_driver(
        monkeypatch, tmp_path):
    """The swap deadline can land inside start() (cancelling it) before the per-start
    budget does — the half-built driver is closed either way."""
    o = _make_orch()
    rec = _rec(o, tmp_path)
    o._drivers[rec.id] = _OldDriver(str(tmp_path))
    _stub_env(monkeypatch)
    # Long enough for the (instant) pre-start steps, far short of the start budget.
    monkeypatch.setattr(orch_mod, "DRIVER_SWAP_TIMEOUT_S", 0.5)
    built = _install_new_driver(monkeypatch, start=_hang)
    o._publish_event = lambda e: None

    result = await asyncio.wait_for(o._reassign(rec, "b", reason="auth_401"), 5.0)

    assert result == "failed"
    assert built and built[0].closed
    assert o._drivers.get(rec.id) is not built[0]
    assert o.paused == [(rec.id, orch_mod._REASSIGN_FAILED_PAUSE_REASON)]


@pytest.mark.asyncio
async def test_reassign_complete_logs_step_timings(monkeypatch, tmp_path, caplog):
    o = _make_orch()
    rec = _rec(o, tmp_path)
    o._drivers[rec.id] = _OldDriver(str(tmp_path))
    _stub_env(monkeypatch)

    async def _start(): pass

    _install_new_driver(monkeypatch, start=_start)
    o._publish_event = lambda e: None

    with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        assert await o._reassign(rec, "b", reason="rate_limit") == "rotated"

    complete = [r.getMessage() for r in caplog.records
                if r.getMessage().startswith("reassign.complete")]
    assert complete, "reassign.complete was not logged"
    for step in ("close", "claim", "cwd", "prepare", "start"):
        assert f"'{step}'" in complete[0], complete[0]


@pytest.mark.asyncio
async def test_reassign_prior_swap_wait_is_bounded_and_never_races_it(
        monkeypatch, tmp_path):
    o = _make_orch()
    rec = _rec(o, tmp_path)
    old = _OldDriver(str(tmp_path))
    o._drivers[rec.id] = old
    _stub_env(monkeypatch)
    monkeypatch.setattr(orch_mod, "DRIVER_SWAP_TIMEOUT_S", 0.05)

    async def _start(): pass

    built = _install_new_driver(monkeypatch, start=_start)
    wedged = asyncio.get_running_loop().create_future()
    o._swapping[rec.id] = wedged

    result = await asyncio.wait_for(o._reassign(rec, "b", reason="rate_limit"), 5.0)

    assert result == "failed"
    assert not built, "a second build must never start beside a live swap (#1040)"
    assert not old.closed
    assert o._swapping[rec.id] is wedged, "the other swap's entry is not ours to clear"
    assert rec.account == "a"
    wedged.set_result(None)


def test_ensure_driver_start_overrun_raises_without_fresh_start(tmp_path, monkeypatch):
    """A resume start() that never finishes is closed and surfaced as a timeout. It
    says nothing about the resume handle, so even with no transcript on disk the
    fresh-start fallback (which would orphan the conversation) must not run."""
    from server.tests.fakes.fake_sdk import FakeSDKClient
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'viberpit.db'}"
    cwd = str(tmp_path)
    session_id = "s-start-hang"
    csid = "csid-start-hang"
    resume_calls = []
    monkeypatch.setattr(orch_mod, "DRIVER_START_TIMEOUT_S", 0.1)

    def factory(sdk_cwd, cfg_dir, can_use_tool, mode, resume, *, on_pre_compact=None,
                options_overlay=None):
        resume_calls.append(resume)
        c = FakeSDKClient([], on_can_use_tool=can_use_tool)
        c.__aenter__ = lambda: _hang()
        return c

    async def _run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id=session_id, name="t", account_name="acct1",
                             engine_session_id=csid, status="idle", cwd=cwd))
        orch = SessionOrchestrator(sdk_factory=factory)
        await orch.rehydrate()
        cfg_dir = tmp_path / "cfg" / "acct1"
        cfg_dir.mkdir(parents=True)
        with patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
             patch("server.sessions.orchestrator.configdir"):
            try:
                await asyncio.wait_for(orch.ensure_driver(session_id), 10.0)
            except DriverStartTimeout as raised:
                return orch, raised
        return orch, None

    orch, raised = asyncio.run(_run())

    assert isinstance(raised, TimeoutError), "callers map a TimeoutError already"
    assert resume_calls == [csid], resume_calls
    assert orch.driver(session_id) is None
    assert session_id not in orch._swapping
