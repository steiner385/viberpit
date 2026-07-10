import time

import pytest


@pytest.mark.asyncio
async def test_reassign_sets_account_before_driver_start(monkeypatch):
    """Regression for #346: record.account must be the NEW account when the driver starts,
    so _oat_env injects the correct account's token (not the stale old account's)."""
    from server.sessions import orchestrator as orch_mod

    seen = {}

    class _NewDriver:
        def __init__(self, sid, account, *a, **k):
            self._sid = sid
            self.cwd = "/tmp"; self.mode = "interactive"; self.identity = None
            self.claude_session_id = "c1"

        async def start(self):
            # _oat_env reads orch._records[sid].account at this point in production.
            seen["account_at_start"] = _orch._records[self._sid].account

        async def send_prompt(self, p): pass

        async def set_model(self, m): pass

    class _OldDriver:
        cwd = "/tmp"; mode = "interactive"; identity = None; claude_session_id = "c1"

        async def interrupt(self): pass

        async def aclose(self): pass

    monkeypatch.setattr(orch_mod, "SessionDriver", _NewDriver)
    monkeypatch.setattr(orch_mod, "compose", lambda a: "/cfg")
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a, **k: None)

    class _NullCtx:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, *a, **k): return None

    monkeypatch.setattr(orch_mod, "db_session", lambda: _NullCtx())

    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions.types import SessionRecord

    global _orch
    _orch = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id="s1", name="n", account="secondary", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp", model=None)
    _orch._records = {"s1": rec}
    _orch._drivers = {"s1": _OldDriver()}
    _orch._last_prompt = {}
    _orch._turn_restart_at = {}
    _orch._clock = time.time
    _orch._registry = None
    _orch._suggester = None
    _orch._session_factory = lambda sid: None
    _orch._publish_event = lambda e: None
    _orch._persist_claude_session_id = lambda *a, **k: None
    _orch._persist_status = lambda *a, **k: None
    _orch._handle_recoverable_failure = lambda *a, **k: None
    _orch._persist_context_usage = lambda *a, **k: None
    _orch._record_condense_ms = lambda *a, **k: None
    _orch._condense_estimate = lambda *a, **k: None

    await _orch._reassign(rec, "primary", reason="rotate")   # reassign secondary -> primary
    assert seen["account_at_start"] == "primary", (
        f"Expected 'primary' but got {seen.get('account_at_start')!r}: "
        f"record.account was not updated before driver.start() (#346)"
    )
