"""HTTP-level tests for the Task 3.1 session-action routes:

  POST /api/sessions/{id}/interrupt
  POST /api/sessions/{id}/permissions/{pid}
  POST /api/sessions/{id}/questions/{qid}
  POST /api/sessions/{id}/permission-mode
  POST /api/sessions/{id}/model

plus the `interrupt` field on POST /api/sessions/{id}/prompt.

Harness mirrors test_send_prompt_route.py: a real SessionOrchestrator only for the
app lifespan (init_db), with `app.state.sessions` replaced by a lightweight fake
sessions facade so route tests don't need a live driver. Audit rows are asserted
by querying the real (in-memory sqlite) DB the lifespan set up.
"""
from __future__ import annotations

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.orchestrator import ModelBlockedError, SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}

_ENV = {
    "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    "CCSWITCH_DEV": "1",
    "CCSWITCH_TAILNET_ALLOWLIST": "",
    # Needed for the assistant-bearer-token test (mint_token / verify_token sign
    # with this secret); harmless for every other test in this file.
    "CCSWITCH_SESSION_SECRET": "x" * 32,
}


class _FakeSessions:
    """Minimal sessions facade for the session-action routes."""

    def __init__(self, record, resumable=True):
        self._record = record
        self._resumable = resumable
        self.sent: list[tuple] = []
        self.ensured: list[tuple] = []
        self.interrupted: list[str] = []
        self.resolve_permission_calls: list[tuple] = []
        self.resolve_permission_result = True
        self.note_user_interaction_calls: list[str] = []
        self.answer_question_calls: list[tuple] = []
        self.answer_question_result = True
        self.set_permission_mode_calls: list[tuple] = []
        self.set_model_calls: list[tuple] = []
        self.set_model_result: bool = False
        self.set_model_error: Exception | None = None

    async def get_session(self, sid):
        return self._record if (self._record and self._record.id == sid) else None

    async def ensure_driver(self, sid, identity="system"):
        self.ensured.append((sid, identity))
        if self._record and self._record.id == sid and self._resumable:
            return object()
        return None

    async def send_prompt(self, sid, text, interrupt=False):
        self.sent.append((sid, text, interrupt))
        return "sent"

    async def latest_suggestions(self, sid):
        return []

    async def interrupt(self, sid):
        self.interrupted.append(sid)

    async def resolve_permission(self, sid, perm_id, decision, identity):
        self.resolve_permission_calls.append((sid, perm_id, decision, identity))
        return self.resolve_permission_result

    async def note_user_interaction(self, sid):
        self.note_user_interaction_calls.append(sid)

    async def answer_question(self, sid, question_id, selected, identity, mode):
        self.answer_question_calls.append((sid, question_id, selected, identity, mode))
        return self.answer_question_result

    async def set_permission_mode(self, sid, mode):
        self.set_permission_mode_calls.append((sid, mode))

    async def set_model_for_session(self, sid, model, *, when="now", identity):
        self.set_model_calls.append((sid, model, identity))
        if self.set_model_error is not None:
            raise self.set_model_error
        return self.set_model_result


def _rec():
    from server.sessions.engine import AgentEngine
    from server.sessions.types import SessionRecord
    return SessionRecord(
        id="11111111-1111-4111-8111-111111111111", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c",
        cols=80, rows=24, cwd="/x", detached=False,
    )


def _make_client():
    """Build a TestClient with a real orchestrator (for lifespan) and a fake sessions
    facade — mirrors test_send_prompt_route.py."""
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    fake = _FakeSessions(_rec())
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


async def _audit_events():
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import AuditEvent
    async with db_session() as db:
        rows = (await db.execute(select(AuditEvent))).scalars().all()
        return [(r.type, r.identity, r.detail, r.via) for r in rows]


# ---------------------------------------------------------------------------
# POST /{id}/interrupt
# ---------------------------------------------------------------------------


def test_interrupt_route_calls_orchestrator_and_records_audit():
    client, fake = _make_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/11111111-1111-4111-8111-111111111111/interrupt", headers=_H)
        assert r.status_code == 200
        assert fake.interrupted == ["11111111-1111-4111-8111-111111111111"]
        events = client.portal.call(_audit_events)
    assert ("session.interrupt", "testuser@example.com", {"sessionId": "11111111-1111-4111-8111-111111111111"}, None) in events


# ---------------------------------------------------------------------------
# POST /{id}/permissions/{pid}
# ---------------------------------------------------------------------------


def test_permission_route_allow_200():
    client, fake = _make_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/11111111-1111-4111-8111-111111111111/permissions/p1", json={"decision": "allow"}, headers=_H)
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert fake.resolve_permission_calls == [("11111111-1111-4111-8111-111111111111", "p1", "allow", "testuser@example.com")]
        assert fake.note_user_interaction_calls == ["11111111-1111-4111-8111-111111111111"]
        events = client.portal.call(_audit_events)
    assert ("permission.allow", "testuser@example.com",
            {"sessionId": "11111111-1111-4111-8111-111111111111", "permId": "p1"}, None) in events


def test_permission_route_not_pending_is_404():
    client, fake = _make_client()
    fake.resolve_permission_result = False
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/11111111-1111-4111-8111-111111111111/permissions/p1", json={"decision": "deny"}, headers=_H)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "PERMISSION_NOT_PENDING"
    # Matches WS behaviour: the audit trail still records the rejected decision.
    assert fake.note_user_interaction_calls == ["11111111-1111-4111-8111-111111111111"]


def test_permission_always_forbidden_via_assistant():
    from server.assistant.token import mint_token

    client, fake = _make_client()
    with patch.dict(os.environ, _ENV), client:
        tok = mint_token("testuser@example.com", "asst-1")
        lc = TestClient(client.app, client=("127.0.0.1", 5000), raise_server_exceptions=False)
        r = lc.post("/api/sessions/11111111-1111-4111-8111-111111111111/permissions/p1", json={"decision": "always"},
                     headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "ALWAYS_FORBIDDEN_VIA_ASSISTANT"
        assert fake.resolve_permission_calls == []
        # A direct human request for "always" is unaffected.
        r2 = client.post("/api/sessions/11111111-1111-4111-8111-111111111111/permissions/p1", json={"decision": "always"}, headers=_H)
        assert r2.status_code == 200
        assert fake.resolve_permission_calls == [("11111111-1111-4111-8111-111111111111", "p1", "always", "testuser@example.com")]


# ---------------------------------------------------------------------------
# POST /{id}/questions/{qid}
# ---------------------------------------------------------------------------


def test_question_route_answer_200():
    client, fake = _make_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/11111111-1111-4111-8111-111111111111/questions/q1",
                         json={"selected": ["Option A"], "mode": "option"}, headers=_H)
        assert r.status_code == 200
        assert fake.answer_question_calls == [
            ("11111111-1111-4111-8111-111111111111", "q1", ["Option A"], "testuser@example.com", "option")]
        events = client.portal.call(_audit_events)
    assert ("question.answer", "testuser@example.com",
            {"sessionId": "11111111-1111-4111-8111-111111111111", "questionId": "q1", "selected": ["Option A"], "mode": "option"},
            None) in events


def test_question_route_not_pending_is_404():
    client, fake = _make_client()
    fake.answer_question_result = False
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/11111111-1111-4111-8111-111111111111/questions/q1", json={"selected": []}, headers=_H)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "QUESTION_NOT_PENDING"


# ---------------------------------------------------------------------------
# POST /{id}/permission-mode
# ---------------------------------------------------------------------------


def test_permission_mode_route():
    client, fake = _make_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/11111111-1111-4111-8111-111111111111/permission-mode", json={"mode": "acceptEdits"},
                         headers=_H)
        assert r.status_code == 200
        assert fake.set_permission_mode_calls == [("11111111-1111-4111-8111-111111111111", "acceptEdits")]
        events = client.portal.call(_audit_events)
    assert ("permission_mode.change", "testuser@example.com",
            {"sessionId": "11111111-1111-4111-8111-111111111111", "mode": "acceptEdits"}, None) in events


# ---------------------------------------------------------------------------
# POST /{id}/model
# ---------------------------------------------------------------------------


def test_model_route_success():
    client, fake = _make_client()
    fake.set_model_result = True
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/11111111-1111-4111-8111-111111111111/model", json={"model": "opus"}, headers=_H)
        assert r.status_code == 200
        assert r.json() == {"ok": True, "restarted": True}
        assert fake.set_model_calls == [("11111111-1111-4111-8111-111111111111", "opus", "testuser@example.com")]
        events = client.portal.call(_audit_events)
    assert ("session.model_change", "testuser@example.com",
            {"sessionId": "11111111-1111-4111-8111-111111111111", "model": "opus"}, None) in events


def test_model_route_blocked_is_409():
    client, fake = _make_client()
    fake.set_model_error = ModelBlockedError("haiku")
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/11111111-1111-4111-8111-111111111111/model", json={"model": "haiku"}, headers=_H)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "MODEL_BLOCKED"


# ---------------------------------------------------------------------------
# POST /{id}/prompt — interrupt field
# ---------------------------------------------------------------------------


def test_prompt_route_forwards_interrupt_field():
    client, fake = _make_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/11111111-1111-4111-8111-111111111111/prompt", json={"text": "stop and do X",
                                                           "interrupt": True}, headers=_H)
    assert r.status_code == 200
    assert fake.sent == [("11111111-1111-4111-8111-111111111111", "stop and do X", True)]


def test_prompt_route_defaults_interrupt_false():
    client, fake = _make_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/11111111-1111-4111-8111-111111111111/prompt", json={"text": "continue"}, headers=_H)
    assert r.status_code == 200
    assert fake.sent == [("11111111-1111-4111-8111-111111111111", "continue", False)]
