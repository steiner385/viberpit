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


@pytest.mark.asyncio
async def test_reassign_fires_driver_replaced_hook(monkeypatch):
    """A recovery reassign must notify on_driver_replaced with the NEW driver so the
    daemon RPC layer restarts the per-session pump (an attached viewer would otherwise
    freeze until reattach — the whole point of the hook)."""
    from server.sessions import orchestrator as orch_mod

    class _NewDriver:
        def __init__(self, *a, **k):
            self.cwd = "/tmp"; self.mode = "interactive"; self.identity = None
            self.claude_session_id = "c1"
        async def start(self): pass
        async def send_prompt(self, p): pass
        async def set_model(self, m): pass

    class _OldDriver:
        cwd = "/tmp"; mode = "interactive"; identity = None; claude_session_id = "c1"
        async def interrupt(self): pass
        async def aclose(self): pass

    monkeypatch.setattr(orch_mod, "SessionDriver", _NewDriver)
    monkeypatch.setattr(orch_mod, "compose", lambda a: "/cfg")
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a, **k: None)
    monkeypatch.setattr(orch_mod, "db_session", lambda: _NullCtx())

    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions.types import SessionRecord
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id="s1", name="n", account="a", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp", model=None)
    orch._records = {"s1": rec}
    old = _OldDriver()
    orch._drivers = {"s1": old}
    orch._last_prompt = {}
    orch._registry = None
    orch._suggester = None
    orch._session_factory = lambda sid: None
    orch._persist_claude_session_id = lambda *a, **k: None
    orch._persist_status = lambda *a, **k: None
    orch._handle_recoverable_failure = lambda *a, **k: None
    orch._publish_event = lambda e: None

    captured = []
    orch.on_driver_replaced = lambda sid, drv: captured.append((sid, drv))

    await orch._reassign(rec, "b", reason="manual")

    assert len(captured) == 1
    sid, new_driver = captured[0]
    assert sid == "s1"
    assert isinstance(new_driver, _NewDriver)          # the swapped-in driver, not the old one
    assert new_driver is not old
    assert orch._drivers["s1"] is new_driver           # dict updated in lockstep with the hook


class _NullCtx:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def execute(self, *a, **k): return None
