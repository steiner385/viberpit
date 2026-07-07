"""Tests for driverless session recovery.

Three gaps that left sessions unrecoverable when a Copilot proxy died:

1. _reassign() silently returned when no driver existed — every driverless
   rotation path (including _restart_driverless from PR #531) was a no-op.
2. switch_account_manual() rejected sessions with no driver — the UI account
   dropdown couldn't rescue an idle session whose driver had crashed.
3. ensure_driver() never scheduled rotation when the proxy failed to start —
   the WS attach path showed an error but left the session stranded.
"""
import asyncio
from pathlib import Path
from unittest import mock

import pytest

from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
import server.sessions.orchestrator as orch_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_orch():
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o._clock = lambda: 1000.0
    o.calls = []

    async def spy_rotate(record, reason):
        o.calls.append(("rotate", record.id, reason))

    async def spy_pause(record, reason, earliest_reset=None):
        o.calls.append(("pause", record.id, reason))

    o._rotate_away = spy_rotate
    o._pause_session = spy_pause
    return o


def _rec(o, session_id="s1", account="copilot_acct", *,
         with_driver=True, cwd="/tmp/x", claude_session_id="c1"):
    rec = SessionRecord(id=session_id, name="n", account=account,
                        claude_session_id=claude_session_id,
                        cols=80, rows=24, cwd=cwd)
    o._records[session_id] = rec
    if with_driver:
        o._drivers[session_id] = object()
    return rec


# ---------------------------------------------------------------------------
# Fix 1: _reassign works when old_driver is None
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reassign_builds_driver_when_no_old_driver(monkeypatch, tmp_path):
    """_reassign on a driverless session must build a new driver, not silently bail."""
    o = _make_orch()
    rec = _rec(o, with_driver=False, cwd=str(tmp_path))

    built = []
    started = []

    class _FakeDriver:
        claude_session_id = None
        mode = "interactive"

        def __init__(self, *a, **kw):
            built.append(kw.get("resume"))

        async def start(self):
            started.append(True)

        async def interrupt(self): pass
        async def aclose(self): pass
        async def set_model(self, m): pass

    monkeypatch.setattr(orch_mod, "SessionDriver", _FakeDriver)

    events = []
    o._publish_event = events.append
    o._install_driver = lambda sid, drv: o._drivers.__setitem__(sid, drv)

    # Stub out DB write and compose
    monkeypatch.setattr(orch_mod, "compose", lambda acct: Path("/fake/cfg"))
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a: None)

    class _MockDB:
        async def execute(self, *a): pass

    class _FakeDbCtx:
        async def __aenter__(self): return _MockDB()
        async def __aexit__(self, *a): pass

    monkeypatch.setattr(orch_mod, "db_session", lambda: _FakeDbCtx())

    # copilot is None so the proxy ensure branch is skipped
    o._copilot = None
    o._suggester = None
    o._session_factory = lambda sid: lambda *a, **k: None

    await o._reassign(rec, "new_account", reason="test")

    assert started, "_FakeDriver.start() was never called — driver was not built"
    assert rec.account == "new_account"
    rotated = [e for e in events if e.get("type") == "rotated"]
    assert rotated, "rotated WS event was not published"
    assert rotated[0]["toAccount"] == "new_account"


@pytest.mark.asyncio
async def test_reassign_with_live_driver_closes_old_and_starts_new(monkeypatch, tmp_path):
    """With a live driver, _reassign closes old + starts fresh — existing behaviour."""
    o = _make_orch()
    rec = _rec(o, with_driver=False, cwd=str(tmp_path))

    interrupted = []
    closed = []
    started = []

    class _OldDriver:
        claude_session_id = "old-csid"
        cwd = str(tmp_path)
        mode = "interactive"
        identity = "user@example.com"

        async def interrupt(self): interrupted.append(True)
        async def aclose(self): closed.append(True)

    class _NewDriver:
        claude_session_id = None
        mode = "interactive"

        def __init__(self, *a, **kw): pass
        async def start(self): started.append(True)
        async def interrupt(self): pass
        async def aclose(self): pass
        async def set_model(self, m): pass

    o._drivers["s1"] = _OldDriver()
    monkeypatch.setattr(orch_mod, "SessionDriver", _NewDriver)
    o._publish_event = lambda e: None
    o._install_driver = lambda sid, drv: o._drivers.__setitem__(sid, drv)
    monkeypatch.setattr(orch_mod, "compose", lambda acct: Path("/fake/cfg"))
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a: None)

    class _MockDB2:
        async def execute(self, *a): pass

    class _FakeDbCtx2:
        async def __aenter__(self): return _MockDB2()
        async def __aexit__(self, *a): pass

    monkeypatch.setattr(orch_mod, "db_session", lambda: _FakeDbCtx2())
    o._copilot = None
    o._suggester = None
    o._session_factory = lambda sid: lambda *a, **k: None

    await o._reassign(rec, "new_account", reason="test")

    assert interrupted, "old driver was not interrupted"
    assert closed, "old driver was not closed"
    assert started, "new driver was not started"


# ---------------------------------------------------------------------------
# Fix 2: switch_account_manual works without a live driver
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_switch_account_manual_works_when_no_driver(monkeypatch, tmp_path):
    """switch_account_manual must not raise KeyError when the session has no driver."""
    o = _make_orch()
    _rec(o, with_driver=False, cwd=str(tmp_path))

    reassigned = []

    async def spy_reassign(self, record, new_account, *, reason):
        reassigned.append((record.id, new_account, reason))

    async def spy_persist_pin(self, sid, pinned):
        pass

    monkeypatch.setattr(SessionOrchestrator, "_reassign", spy_reassign)
    monkeypatch.setattr(SessionOrchestrator, "_persist_pin", spy_persist_pin)
    monkeypatch.setattr(orch_mod.ccswitch_accounts, "list_accounts",
                        lambda: [{"name": "copilot_acct"}, {"name": "tony_kindash"}])

    await o.switch_account_manual("s1", "tony_kindash")

    assert reassigned == [("s1", "tony_kindash", "manual")]


@pytest.mark.asyncio
async def test_switch_account_manual_raises_for_unknown_session(monkeypatch):
    """Missing session_id still raises KeyError regardless of driver state."""
    o = _make_orch()
    with pytest.raises(KeyError):
        await o.switch_account_manual("no-such-session", "any_account")


# ---------------------------------------------------------------------------
# Fix 3: ensure_driver schedules rotation when proxy fails to start
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_driver_rotates_on_proxy_start_failure(monkeypatch, tmp_path):
    """When the Copilot proxy fails to start, ensure_driver must schedule _rotate_away."""
    o = _make_orch()
    rec = _rec(o, with_driver=False, cwd=str(tmp_path))

    class _DeadProxy:
        async def ensure(self, account):
            raise RuntimeError("proxy quota exhausted")

    o._copilot = _DeadProxy()
    monkeypatch.setattr(orch_mod, "_provider_of",
                        lambda acct: orch_mod._copilot_mod.PROVIDER)
    monkeypatch.setattr(orch_mod, "compose", lambda acct: Path("/fake/cfg"))
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a: None)

    spawned = []

    def _fake_spawn(coro, *, name=None):
        spawned.append(name or "")
        asyncio.ensure_future(coro)
        return mock.MagicMock()

    o._spawn = _fake_spawn
    o._shutting_down = False

    with pytest.raises(RuntimeError, match="proxy quota exhausted"):
        await o.ensure_driver("s1")

    # Yield to the event loop so the scheduled rotation coroutine can run
    await asyncio.sleep(0)

    assert any("rotate" in n for n in spawned), (
        "ensure_driver should have spawned a rotation task when the proxy failed"
    )
    assert ("rotate", "s1", "proxy_start_failed") in o.calls


# ---------------------------------------------------------------------------
# Fix 4: run_command /account works for driverless sessions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_command_account_works_without_driver(monkeypatch):
    """/account must switch the session even when no driver is present.

    run_command previously checked for a live driver first, returning
    'This session isn't live' before reaching the /account handler.
    The fix hoists /account above the driver guard."""
    o = _make_orch()
    _rec(o, with_driver=False)

    reassigned = []

    async def spy_reassign(self, record, new_account, *, reason):
        reassigned.append((record.id, new_account, reason))

    async def spy_persist_pin(self, sid, pinned):
        pass

    monkeypatch.setattr(SessionOrchestrator, "_reassign", spy_reassign)
    monkeypatch.setattr(SessionOrchestrator, "_persist_pin", spy_persist_pin)
    monkeypatch.setattr(orch_mod.ccswitch_accounts, "list_accounts",
                        lambda: [{"name": "copilot_acct"}, {"name": "tony_kindash"}])

    result = await o.run_command("s1", "/account", "tony_kindash")

    assert result is not None
    assert result.get("data", {}).get("text") == "Account → tony_kindash", repr(result)
    assert reassigned == [("s1", "tony_kindash", "manual")]


@pytest.mark.asyncio
async def test_run_command_other_commands_still_blocked_without_driver():
    """Non-account commands still return 'not live' when there is no driver."""
    o = _make_orch()
    _rec(o, with_driver=False)

    result = await o.run_command("s1", "/compact", "")

    assert result is not None
    assert "isn't live" in result.get("data", {}).get("text", ""), repr(result)
