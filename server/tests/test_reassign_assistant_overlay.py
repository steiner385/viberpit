"""#1125: _reassign is a third assistant-driver-building site (alongside _spawn_session
and ensure_driver) — it must rebuild the AssistantApi/overlay rather than reuse the one
old_driver.aclose() just tore down, or every mcp__ccswitch__* call on the rebuilt driver
raises against a closed httpx.AsyncClient.
"""
import time

import pytest


class _NullCtx:
    async def __aenter__(self):
        class _Db:
            async def execute(self, *a, **k): pass
        return _Db()

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_reassign_rebuilds_assistant_api_for_assistant_kind(monkeypatch):
    from server.sessions import orchestrator as orch_mod

    captured = {}

    class _NewDriver:
        def __init__(self, *a, assistant_api=None, **k):
            self.cwd = "/tmp"; self.mode = "interactive"; self.identity = None
            self.engine_session_id = "c1"
            captured["assistant_api"] = assistant_api

        async def start(self): pass
        async def send_prompt(self, p): pass
        async def set_model(self, m): pass

    class _OldDriver:
        cwd = "/tmp"; mode = "interactive"; identity = None; engine_session_id = "c1"

        async def interrupt(self): pass

        async def aclose(self): pass

    monkeypatch.setattr(
        orch_mod.SessionOrchestrator,
        "_create_driver",
        lambda self, **kw: _NewDriver(
            resume=kw.get("resume"),
            identity=kw.get("identity"),
            assistant_api=kw.get("assistant_api"),
        ),
    )
    monkeypatch.setattr(orch_mod, "compose", lambda a: "/cfg")
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a, **k: None)
    monkeypatch.setattr(orch_mod, "db_session", lambda: _NullCtx())

    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions.engine import AgentEngine
    from server.sessions.types import ASSISTANT_KIND, SessionRecord

    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp", model=None, kind=ASSISTANT_KIND)
    old_overlay = {"stale": True}
    orch._records = {"s1": rec}
    orch._drivers = {"s1": _OldDriver()}
    orch._assistant_overlays = {"s1": old_overlay}
    orch._last_prompt = {}
    orch._turn_restart_at = {}
    orch._swapping = {}
    orch._clock = time.time
    orch._registry = None
    orch._suggester = None
    orch._session_factory = lambda sid: None
    orch._persist_engine_session_id = lambda *a, **k: None
    orch._persist_status = lambda *a, **k: None
    orch._handle_recoverable_failure = lambda *a, **k: None
    orch._publish_event = lambda e: None

    await orch._reassign(rec, "b", reason="manual")

    assistant_api = captured["assistant_api"]
    assert assistant_api is not None
    assert assistant_api._client.is_closed is False
    # The overlay registered for the new driver must wrap the FRESH client, not the
    # stale one old_driver.aclose() already tore down.
    assert orch._assistant_overlays["s1"] is not old_overlay
