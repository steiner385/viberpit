"""GET /api/sessions/{id}/transcript — must handle BOTH event shapes.

In-process replay yields event objects (`.to_wire()`); a remote/daemon
orchestrator may hand back already-serialised wire dicts. The endpoint 500'd on
dicts (`'dict' object has no attribute 'to_wire'`) — this guards the daemon path
through the (now unified) transcript_window flow.

Auth/harness mirrors test_send_prompt_route.py.
"""
from __future__ import annotations

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {
    "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    "CCSWITCH_DEV": "1",
    "CCSWITCH_TAILNET_ALLOWLIST": "",
}


class _Evt:
    """An in-process-style event with a to_wire()."""

    def __init__(self, d):
        self._d = d

    def to_wire(self):
        return dict(self._d)


class _FakeSessions:
    def __init__(self, record, events):
        self._record = record
        self._events = events

    async def get_session(self, sid):
        return self._record if (self._record and self._record.id == sid) else None

    async def transcript_window(self, sid, *, before=None, limit=None):
        from server.sessions.replay import ReplayWindow
        return ReplayWindow(events=list(self._events), earliest_turn=0,
                            total_turns=len(self._events))


def _rec():
    from server.sessions.engine import AgentEngine
    from server.sessions.types import SessionRecord
    return SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c",
                         cols=80, rows=24, cwd="/x")


def _make_client(record, events):
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    app.state.sessions = _FakeSessions(record, events)
    return TestClient(app, raise_server_exceptions=False)


def test_transcript_handles_dict_events_daemon_mode():
    # Remote/daemon mode: transcript_events returns wire dicts, not objects.
    events = [{"type": "user", "text": "hi"}, {"type": "assistant", "text": "yo"}]
    client = _make_client(_rec(), events)
    with patch.dict(os.environ, _ENV), client:
        r = client.get("/api/sessions/s1/transcript", headers=_H)
    assert r.status_code == 200
    out = r.json()["events"]
    assert [e["type"] for e in out] == ["user", "assistant"]


def test_transcript_handles_object_events_inprocess():
    # In-process mode: event objects with to_wire() still work.
    events = [_Evt({"type": "user", "text": "hi"})]
    client = _make_client(_rec(), events)
    with patch.dict(os.environ, _ENV), client:
        r = client.get("/api/sessions/s1/transcript", headers=_H)
    assert r.status_code == 200
    assert r.json()["events"][0]["type"] == "user"
