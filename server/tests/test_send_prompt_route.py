"""REST endpoint tests: POST /api/sessions/{id}/prompt (quick-resume).

Tests:
  200 — live session: prompt forwarded, record returned
  200 — detached session: lazily RESUMED (ensure_driver) then prompt forwarded
  409 — session exists but cannot be resumed
  404 — unknown session: 404 not found

Auth approach: mirror test_adopt_api.py — real SessionOrchestrator + fake SDK,
CCSWITCH_TAILNET_ALLOWLIST="" accepts any identity, send Tailscale-User-Login header.
TestClient (sync, lifespan-aware) runs init_db() before the route's get_db() is hit.
app.state.sessions is replaced after create_app() with a lightweight fake so
the route tests don't need a live driver.
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
    """Minimal sessions facade for the send-prompt route."""

    def __init__(self, record, resumable=True):
        self._record = record
        self._resumable = resumable
        self.sent: list[tuple[str, str]] = []
        self.ensured: list[tuple[str, str]] = []

    async def get_session(self, sid):
        return self._record if (self._record and self._record.id == sid) else None

    async def ensure_driver(self, sid, identity="system"):
        # Mirrors orchestrator.ensure_driver: resumes a detached session, returns a
        # live driver (or None if it can't be resumed).
        self.ensured.append((sid, identity))
        if self._record and self._record.id == sid and self._resumable:
            return object()
        return None

    async def send_prompt(self, sid, text, attachment_ids=None):
        self.sent.append((sid, text))

    async def latest_suggestions(self, sid):
        return []


def _rec(detached=False):
    from server.sessions.types import SessionRecord
    return SessionRecord(
        id="s1", name="n", account="a", claude_session_id="c",
        cols=80, rows=24, cwd="/x", detached=detached,
    )


def _make_client(record, resumable=True):
    """Build a TestClient with a real orchestrator (for lifespan) and a fake sessions facade."""
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    # Replace sessions with fake after create_app (orch is still used by lifespan)
    fake = _FakeSessions(record, resumable=resumable)
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


def test_send_prompt_200_live():
    client, fake = _make_client(_rec(detached=False))
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/s1/prompt", json={"text": "continue"}, headers=_H)
    assert r.status_code == 200
    assert fake.ensured == [("s1", "testuser@example.com")]
    assert fake.sent == [("s1", "continue")]


def test_send_prompt_resumes_detached():
    # The whole point of quick-resume: a detached (idle/"Ready") session is lazily
    # resumed via ensure_driver, then the prompt is delivered — no 409.
    client, fake = _make_client(_rec(detached=True))
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/s1/prompt", json={"text": "continue"}, headers=_H)
    assert r.status_code == 200
    assert fake.ensured == [("s1", "testuser@example.com")]
    assert fake.sent == [("s1", "continue")]


def test_send_prompt_409_resume_failed():
    # Session exists but ensure_driver can't bring it live → 409, nothing sent.
    client, fake = _make_client(_rec(detached=True), resumable=False)
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/s1/prompt", json={"text": "continue"}, headers=_H)
    assert r.status_code == 409
    assert fake.sent == []


def test_send_prompt_404_unknown():
    client, fake = _make_client(None)
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/missing/prompt", json={"text": "x"}, headers=_H)
    assert r.status_code == 404
    assert fake.ensured == []  # never tried to resume an unknown session
