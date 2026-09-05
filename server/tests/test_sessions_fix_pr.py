"""REST endpoint tests: POST /api/sessions/{id}/fix-pr (manual propose tap).

Tests:
  200 — known session: request_pr_fix returns "sent", session dict returned
  404 — unknown session: 404 not found
  422 — request_pr_fix returns "not_actionable": 422 with code in detail

Auth approach: mirrors test_send_prompt_route.py — real SessionOrchestrator +
fake SDK, CCSWITCH_TAILNET_ALLOWLIST="" accepts any identity, send
Tailscale-User-Login header. TestClient (sync, lifespan-aware) + fake sessions
facade replacing app.state.sessions after create_app().
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


class _FakeSessions:
    """Minimal sessions facade for the fix-pr route."""

    def __init__(self, record, resumable=True, fix_result="sent"):
        self._record = record
        self._resumable = resumable
        self._fix_result = fix_result
        self.fixed: list[tuple[str, int]] = []
        self.ensured: list[tuple[str, str]] = []

    async def get_session(self, sid):
        return self._record if (self._record and self._record.id == sid) else None

    async def ensure_driver(self, sid, identity="system"):
        self.ensured.append((sid, identity))
        if self._record and self._record.id == sid and self._resumable:
            return object()
        return None

    async def request_pr_fix(self, sid, number):
        self.fixed.append((sid, number))
        return self._fix_result

    async def send_prompt(self, sid, text):
        pass

    async def latest_suggestions(self, sid):
        return []


def _rec():
    from server.sessions.engine import AgentEngine
    from server.sessions.types import SessionRecord
    return SessionRecord(
        id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c",
        cols=80, rows=24, cwd="/x", detached=False,
    )


def _make_client(record, resumable=True, fix_result="sent"):
    """Build a TestClient with a real orchestrator (for lifespan) and a fake sessions facade."""
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    fake = _FakeSessions(record, resumable=resumable, fix_result=fix_result)
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


def test_fix_pr_200_returns_session():
    client, fake = _make_client(_rec())
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/s1/fix-pr", json={"number": 7}, headers=_H)
    assert r.status_code == 200
    assert r.json()["id"] == "s1"
    assert fake.fixed == [("s1", 7)]
    assert fake.ensured == [("s1", "testuser@example.com")]


def test_fix_pr_404_unknown_session():
    client, fake = _make_client(None)
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/nope/fix-pr", json={"number": 7}, headers=_H)
    assert r.status_code == 404
    assert fake.ensured == []
    assert fake.fixed == []


def test_fix_pr_422_not_actionable():
    client, fake = _make_client(_rec(), fix_result="not_actionable")
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/s1/fix-pr", json={"number": 7}, headers=_H)
    assert r.status_code == 422
    body = r.json()
    assert body["detail"]["code"] == "not_actionable"
