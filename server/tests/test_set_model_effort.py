"""`POST /sessions/{id}/model` carries an optional reasoning ``effort`` (D-1 prompt row).

The prompt row's effort picker re-sends the session's current model with a new
effort. The route threads it into ``set_model_for_session(reasoning_effort=…)``
only when present — an ordinary model switch must keep ``EFFORT_UNCHANGED`` so it
never touches a Codex session's effort — and refuses it for a Claude session with
the same ``unsupported_reasoning_effort`` code session creation uses. The remote
hop (RemoteOrchestrator → daemon RPC) carries it too, so a session on another
fleet host behaves the same.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.engine import EFFORT_UNCHANGED, AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_SID = "11111111-1111-4111-8111-111111111111"
_H = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {
    "VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:",
    "VIBERPIT_DEV": "1",
    "VIBERPIT_TAILNET_ALLOWLIST": "",
}


class _FakeSessions:
    def __init__(self, record):
        self._record = record
        self.calls: list[dict] = []

    async def get_session(self, sid):
        return self._record if self._record.id == sid else None

    async def set_model_for_session(self, sid, model, *, when="now", identity,
                                    reasoning_effort=EFFORT_UNCHANGED):
        self.calls.append({"sid": sid, "model": model, "when": when,
                           "reasoning_effort": reasoning_effort})
        return False


def _rec(engine: AgentEngine):
    from server.sessions.types import SessionRecord
    return SessionRecord(
        id=_SID, name="n", account="a", engine=engine, engine_session_id="c",
        cols=80, rows=24, cwd="/x", detached=False,
    )


def _make_client(engine: AgentEngine):
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    fake = _FakeSessions(_rec(engine))
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


def test_effort_sent_reaches_set_model_for_session():
    client, fake = _make_client(AgentEngine.CODEX)
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{_SID}/model",
                        json={"model": "gpt-5", "effort": "high"}, headers=_H)
    assert r.status_code == 200, r.text
    assert fake.calls == [{"sid": _SID, "model": "gpt-5", "when": "now",
                           "reasoning_effort": "high"}]


def test_effort_omitted_leaves_effort_unchanged():
    client, fake = _make_client(AgentEngine.CODEX)
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{_SID}/model", json={"model": "gpt-5"}, headers=_H)
    assert r.status_code == 200, r.text
    assert len(fake.calls) == 1
    assert fake.calls[0]["reasoning_effort"] is EFFORT_UNCHANGED


def test_effort_on_a_claude_session_is_refused():
    client, fake = _make_client(AgentEngine.CLAUDE_CODE)
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{_SID}/model",
                        json={"model": "opus", "effort": "high"}, headers=_H)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "unsupported_reasoning_effort"
    assert fake.calls == []


class _RecordingClient:
    def __init__(self):
        self.last: tuple | None = None

    async def call(self, method, **kwargs):
        self.last = (method, kwargs)
        return True


@pytest.mark.asyncio
async def test_remote_orchestrator_forwards_effort_over_rpc():
    from server.sessions.remote import RemoteOrchestrator
    client = _RecordingClient()
    await RemoteOrchestrator(client).set_model_for_session(
        _SID, "gpt-5", when="now", identity="u", reasoning_effort="high")
    assert client.last == ("set_model_for_session", {
        "session_id": _SID, "model": "gpt-5", "when": "now", "identity": "u",
        "reasoning_effort": "high"})


@pytest.mark.asyncio
async def test_remote_orchestrator_omits_unchanged_effort():
    """A daemon that predates the kwarg must keep accepting an ordinary switch."""
    from server.sessions.remote import RemoteOrchestrator
    client = _RecordingClient()
    await RemoteOrchestrator(client).set_model_for_session(_SID, "gpt-5", identity="u")
    assert client.last == ("set_model_for_session", {
        "session_id": _SID, "model": "gpt-5", "when": "now", "identity": "u"})
