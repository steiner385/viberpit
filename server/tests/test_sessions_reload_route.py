"""REST endpoint tests: POST /api/sessions/{id}/reload ("Reload session").

  200 — known session: reload_session returns "reloaded", session dict returned,
        the caller's identity is threaded through
  404 — unknown session
  409 — reload_session declines ("skipped:archived" / "no_driver"): code in detail

Auth approach mirrors test_sessions_fix_pr.py.
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
    """Minimal sessions facade for the reload route."""

    def __init__(self, record, result="reloaded"):
        self._record = record
        self._result = result
        self.calls: list[tuple[str, str]] = []

    async def get_session(self, sid):
        return self._record if (self._record and self._record.id == sid) else None

    async def reload_session(self, sid, identity="system"):
        self.calls.append((sid, identity))
        return self._result

    async def latest_suggestions(self, sid):
        return []


def _rec():
    from server.sessions.engine import AgentEngine
    from server.sessions.types import SessionRecord
    return SessionRecord(
        id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c",
        cols=80, rows=24, cwd="/x", detached=False,
    )


def _make_client(record, result="reloaded"):
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    fake = _FakeSessions(record, result=result)
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


def test_reload_known_session_returns_200_and_threads_identity():
    client, fake = _make_client(_rec())
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/s1/reload", headers=_H)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "s1"
    assert fake.calls == [("s1", "testuser@example.com")]


def test_reload_unknown_session_returns_404():
    client, fake = _make_client(_rec())
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/nope/reload", headers=_H)
    assert r.status_code == 404
    assert fake.calls == []


def test_reload_declined_returns_409_with_reason():
    for reason in ("skipped:archived", "no_driver"):
        client, _ = _make_client(_rec(), result=reason)
        with patch.dict(os.environ, _ENV), client:
            r = client.post("/api/sessions/s1/reload", headers=_H)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == reason
