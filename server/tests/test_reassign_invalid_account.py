"""Regressions for the silent wedge in _reassign().

Production trace (2026-08-31 08:10, session 8220c5ac): a rate-limited turn triggered
_rotate_away(), select_account() returned a PHANTOM account named "" (a stray
`.cred.json` in the store — see tests/test_phantom_account_names.py), and _reassign()
was handed that empty name with no validation. It closed the live driver, then blew
up starting a Copilot proxy for the nameless account. The bare `except Exception:`
swallowed the error, so the session was left with NO driver, NO status change and NO
event — every subsequent prompt spun forever with no reply.

Two defects, both covered here:

1. _reassign() must validate its target BEFORE tearing down the working driver.
   switch_account_manual() validated; the auto-rotation path did not, so the guard
   belonged in the shared primitive all along.
2. A failed _reassign() must leave the session visibly needing attention rather than
   silently driverless.
"""
from pathlib import Path

import pytest

import server.sessions.orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


def _make_orch():
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o._clock = lambda: 1000.0
    o.paused = []

    async def spy_pause(record, *, reason, earliest_reset=None):
        o.paused.append((record.id, reason))

    o._pause_session = spy_pause
    o._copilot = None
    o._suggester = None
    o._session_factory = lambda sid: lambda *a, **k: None
    return o


def _rec(o, cwd, session_id="s1", account="tonystein85_gmail.com"):
    rec = SessionRecord(id=session_id, name="n", account=account,
                        claude_session_id="c1", cols=80, rows=24, cwd=str(cwd))
    o._records[session_id] = rec
    return rec


class _LiveDriver:
    claude_session_id = "old-csid"
    mode = "interactive"
    identity = "user@example.com"

    def __init__(self, cwd):
        self.cwd = str(cwd)
        self.interrupted = False
        self.closed = False

    async def interrupt(self):
        self.interrupted = True

    async def aclose(self):
        self.closed = True


def _stub_db(monkeypatch):
    class _MockDB:
        async def execute(self, *a):
            pass

    class _FakeDbCtx:
        async def __aenter__(self):
            return _MockDB()

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(orch_mod, "db_session", lambda: _FakeDbCtx())


@pytest.mark.parametrize("bad", ["", " ", ".", "..", "../escape"])
@pytest.mark.asyncio
async def test_reassign_rejects_invalid_target_and_keeps_live_driver(
        monkeypatch, tmp_path, bad):
    """An invalid rotation target must not cost the session its working driver."""
    o = _make_orch()
    rec = _rec(o, tmp_path)
    old = _LiveDriver(tmp_path)
    o._drivers[rec.id] = old

    built = []

    class _NewDriver:
        def __init__(self, *a, **kw):
            built.append(kw)

        async def start(self):
            pass

    monkeypatch.setattr(orch_mod, "SessionDriver", _NewDriver)
    monkeypatch.setattr(orch_mod, "compose", lambda acct: Path("/fake/cfg"))
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a: None)
    _stub_db(monkeypatch)

    await o._reassign(rec, bad, reason="usage_ceiling")

    assert not old.closed, "live driver was torn down for an invalid target"
    assert not old.interrupted
    assert not built, "a driver was built for an invalid account name"
    assert o._drivers[rec.id] is old, "session lost its driver"
    assert rec.account == "tonystein85_gmail.com", "record.account was overwritten"
    assert o.paused and o.paused[0][0] == rec.id, "session was not flagged for the user"


@pytest.mark.asyncio
async def test_reassign_failure_pauses_instead_of_silently_stranding(
        monkeypatch, tmp_path):
    """When the relaunch itself fails, the session must not be left silently driverless."""
    o = _make_orch()
    rec = _rec(o, tmp_path)
    o._drivers[rec.id] = _LiveDriver(tmp_path)

    class _ExplodingDriver:
        def __init__(self, *a, **kw):
            pass

        async def start(self):
            raise RuntimeError("proxy never became ready")

        async def interrupt(self):
            pass

        async def aclose(self):
            pass

    monkeypatch.setattr(orch_mod, "SessionDriver", _ExplodingDriver)
    monkeypatch.setattr(orch_mod, "compose", lambda acct: Path("/fake/cfg"))
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a: None)
    _stub_db(monkeypatch)

    await o._reassign(rec, "tony_cairnea.com", reason="usage_ceiling")

    assert o.paused, "a failed reassign left the session with no user-visible signal"
    assert o.paused[0][0] == rec.id


@pytest.mark.asyncio
async def test_reassign_still_accepts_a_valid_target(monkeypatch, tmp_path):
    """The guard must not block ordinary rotation."""
    o = _make_orch()
    rec = _rec(o, tmp_path)
    started = []

    class _NewDriver:
        claude_session_id = None
        mode = "interactive"

        def __init__(self, *a, **kw):
            pass

        async def start(self):
            started.append(True)

        async def interrupt(self):
            pass

        async def aclose(self):
            pass

        async def set_model(self, m):
            pass

    monkeypatch.setattr(orch_mod, "SessionDriver", _NewDriver)
    monkeypatch.setattr(orch_mod, "compose", lambda acct: Path("/fake/cfg"))
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a: None)
    _stub_db(monkeypatch)
    o._publish_event = lambda e: None
    o._install_driver = lambda sid, drv: o._drivers.__setitem__(sid, drv)

    await o._reassign(rec, "tony_cairnea.com", reason="usage_ceiling")

    assert started, "valid rotation was blocked by the guard"
    assert rec.account == "tony_cairnea.com"
    assert not o.paused
