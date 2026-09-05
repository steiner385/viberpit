"""_reassign must survive a pre-close interrupt that fails or hangs.

Incident (2026-09-03, dobby): a session's ``claude`` CLI stopped answering the SDK
control channel after emitting its "You've hit your session limit" result. Every
rotation attempt — the usage-ceiling monitor, the stuck-session sweep, and the user's
manual account switch — ran ``_reassign``, whose first step was an UNBOUNDED
``await old_driver.interrupt()``. The SDK raised its generic
``Exception("Control request timeout: interrupt")`` after 60 s, which is not a
``CLIConnectionError`` and so escaped ``driver.interrupt()``; ``_reassign`` aborted
before ``aclose()`` or the relaunch, the wedged driver stayed installed, and the
session re-paused as ``reassign_failed:*`` once a minute until a human killed the CLI.

The pre-close interrupt is a courtesy to the driver being REPLACED. It must be
best-effort and bounded, like ``ClaudeAgentDriver.aclose`` already is (#846).
"""
import asyncio
from pathlib import Path

import pytest

from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
import server.sessions.orchestrator as orch_mod


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
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a: None)


def _new_driver_cls(started):
    class _NewDriver:
        engine_session_id = None
        mode = "interactive"

        def __init__(self, *a, **kw): pass
        async def start(self): started.append(True)
        async def interrupt(self): pass
        async def aclose(self): pass
        async def set_model(self, m): pass

    return _NewDriver


def _old_driver(tmp_path, closed, *, interrupt):
    class _OldDriver:
        engine_session_id = "old-csid"
        cwd = str(tmp_path)
        mode = "interactive"
        identity = "user@example.com"

        async def interrupt(self): await interrupt()
        async def aclose(self): closed.append(True)

    return _OldDriver()


async def _run(o, rec, monkeypatch, tmp_path, *, interrupt):
    closed, started, events = [], [], []
    o._drivers[rec.id] = _old_driver(tmp_path, closed, interrupt=interrupt)
    driver_cls = _new_driver_cls(started)
    monkeypatch.setattr(
        orch_mod.SessionOrchestrator,
        "_create_driver",
        lambda self, **kw: driver_cls(resume=kw.get("resume"), identity=kw.get("identity")),
    )
    o._publish_event = events.append
    _stub_env(monkeypatch)
    await o._reassign(rec, "b", reason="session limit reached")
    return closed, started, events


def _rec(o, tmp_path):
    rec = SessionRecord(id="s1", name="n", account="a", claude_session_id="c1",
                        cols=80, rows=24, cwd=str(tmp_path))
    o._records[rec.id] = rec
    return rec


@pytest.mark.asyncio
async def test_reassign_proceeds_when_interrupt_raises(monkeypatch, tmp_path):
    """The SDK's control-request timeout is a plain Exception — it must not abort
    the reassign: the old driver is still closed and the new one started."""
    o = _make_orch()
    rec = _rec(o, tmp_path)

    async def _raise():
        raise Exception("Control request timeout: interrupt")

    closed, started, events = await _run(o, rec, monkeypatch, tmp_path, interrupt=_raise)

    assert closed, "old driver was never aclose()d after the interrupt failed"
    assert started, "new driver was never started after the interrupt failed"
    assert rec.account == "b"
    assert [e["type"] for e in events if e.get("type") == "rotated"] == ["rotated"]
    assert o.paused == [], f"session was paused instead of rotated: {o.paused}"


@pytest.mark.asyncio
async def test_reassign_proceeds_when_interrupt_hangs(monkeypatch, tmp_path):
    """A CLI that never answers the control channel must cost the reassign the
    bounded grace, not block it forever."""
    o = _make_orch()
    rec = _rec(o, tmp_path)
    monkeypatch.setattr(orch_mod, "_DRIVER_CONTROL_TIMEOUT_S", 0.05)

    async def _hang():
        await asyncio.Event().wait()

    closed, started, events = await asyncio.wait_for(
        _run(o, rec, monkeypatch, tmp_path, interrupt=_hang), timeout=5.0)

    assert closed and started
    assert rec.account == "b"
    assert o.paused == []
