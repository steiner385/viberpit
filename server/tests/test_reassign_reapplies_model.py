import pytest


@pytest.mark.asyncio
async def test_reassign_reapplies_persisted_model(monkeypatch):
    # Capture the set_model calls on the rebuilt driver.
    from server.sessions import orchestrator as orch_mod

    calls = {}

    class _NewDriver:
        def __init__(self, *a, **k):
            self.cwd = "/tmp"; self.mode = "interactive"; self.identity = None
            self.claude_session_id = "c1"
        async def start(self): pass
        async def send_prompt(self, p): pass
        async def set_model(self, m): calls["model"] = m

    class _OldDriver:
        cwd = "/tmp"; mode = "interactive"; identity = None; claude_session_id = "c1"
        async def interrupt(self): pass
        async def aclose(self): pass

    # Stub the collaborators _reassign reaches for.
    monkeypatch.setattr(orch_mod, "SessionDriver", _NewDriver)
    monkeypatch.setattr(orch_mod, "compose", lambda a: "/cfg")
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a, **k: None)

    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions.types import SessionRecord
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id="s1", name="n", account="a", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp", model="sonnet")
    orch._records = {"s1": rec}
    orch._drivers = {"s1": _OldDriver()}
    orch._last_prompt = {}
    orch._registry = None
    orch._suggester = None
    orch._session_factory = lambda sid: None
    orch._persist_claude_session_id = lambda *a, **k: None
    orch._persist_status = lambda *a, **k: None
    orch._handle_recoverable_failure = lambda *a, **k: None
    # Stub DB write + event publish so we test only the model re-apply.
    async def _noop_db(self, *a, **k): pass
    orch._publish_event = lambda e: None
    monkeypatch.setattr(orch_mod, "db_session", lambda: _NullCtx())

    await orch._reassign(rec, "b", reason="manual")
    assert calls["model"] == "sonnet"


class _NullCtx:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def execute(self, *a, **k): return None
