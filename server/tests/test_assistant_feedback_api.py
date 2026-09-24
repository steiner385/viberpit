"""API tests for POST /api/assistant/feedback (plan Task 10.1, issue #1246): per-turn
thumbs feedback on the assistant drawer. Mirrors test_assistant_watches_api.py's
lightweight `_client` fixture (no real session/account plumbing needed — this route
never touches SessionOrchestrator) and test_session_feedback.py's counter-delta
assertion style for `assistant_feedback_total`.
"""
from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests.helpers import temp_env

IDENTITY = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client(env=None):
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    from fastapi.testclient import TestClient

    with temp_env(), tempfile.TemporaryDirectory() as d:
        db_url = f"sqlite+aiosqlite:///{Path(d) / 'feedback.db'}"
        full_env = {"VIBERPIT_DB_URL": db_url, **(env or {})}
        with patch.dict(os.environ, full_env):
            orch = SessionOrchestrator(poll_interval=9999.0)
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


def test_feedback_up_records_audit_and_metric():
    from server.obs.metrics import assistant_feedback_total
    before = assistant_feedback_total.value("up")
    with _client() as c:
        r = c.post("/api/assistant/feedback",
                   json={"sessionId": "s1", "turnId": "t1", "vote": "up"},
                   headers=IDENTITY)
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True}
    assert assistant_feedback_total.value("up") == before + 1


def test_feedback_down_records_audit_and_metric():
    from server.obs.metrics import assistant_feedback_total
    before_up = assistant_feedback_total.value("up")
    before_down = assistant_feedback_total.value("down")
    with _client() as c:
        r = c.post("/api/assistant/feedback",
                   json={"sessionId": "s2", "turnId": "t2", "vote": "down"},
                   headers=IDENTITY)
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True}
    assert assistant_feedback_total.value("down") == before_down + 1
    assert assistant_feedback_total.value("up") == before_up


def test_feedback_writes_audit_event_with_expected_detail():
    with _client() as c:
        r = c.post("/api/assistant/feedback",
                   json={"sessionId": "s3", "turnId": "t3", "vote": "up", "note": "great answer"},
                   headers=IDENTITY)
        assert r.status_code == 200, r.text

        from sqlalchemy import select
        from server.db import db_session
        from server.db.models import AuditEvent

        async def _get():
            async with db_session() as db:
                return (await db.execute(
                    select(AuditEvent).where(AuditEvent.type == "assistant.feedback")
                )).scalars().all()

        # A TestClient entered as a context manager reuses the app's own event loop
        # via its portal (see test_assistant_watches_api.py's loopback note) — plain
        # asyncio.run() here would spin up a foreign loop and hang against the pool,
        # so route the query through the same portal the client already established.
        rows = c.portal.call(_get)
        row = next(r for r in rows if r.detail.get("sessionId") == "s3")
        assert row.identity == "testuser@example.com"
        assert row.via is None
        assert row.detail == {"sessionId": "s3", "turnId": "t3", "vote": "up", "note": "great answer"}


def test_feedback_without_note_omits_note_key():
    with _client() as c:
        r = c.post("/api/assistant/feedback",
                   json={"sessionId": "s4", "turnId": "t4", "vote": "down"},
                   headers=IDENTITY)
        assert r.status_code == 200, r.text

        from sqlalchemy import select
        from server.db import db_session
        from server.db.models import AuditEvent

        async def _get():
            async with db_session() as db:
                return (await db.execute(
                    select(AuditEvent).where(AuditEvent.type == "assistant.feedback")
                )).scalars().all()

        rows = c.portal.call(_get)
        row = next(r for r in rows if r.detail.get("sessionId") == "s4")
        assert "note" not in row.detail


def test_feedback_invalid_vote_is_422():
    with _client() as c:
        r = c.post("/api/assistant/feedback",
                   json={"sessionId": "s5", "turnId": "t5", "vote": "sideways"},
                   headers=IDENTITY)
        assert r.status_code == 422

        from sqlalchemy import select
        from server.db import db_session
        from server.db.models import AuditEvent

        async def _get():
            async with db_session() as db:
                return (await db.execute(
                    select(AuditEvent).where(AuditEvent.type == "assistant.feedback")
                )).scalars().all()

        rows = c.portal.call(_get)
        assert not any(r.detail.get("sessionId") == "s5" for r in rows)


def test_feedback_rejects_the_assistant_token_with_403():
    """A `ccas1:` bearer — the assistant's own loopback token — must never be able to
    vote on its own turns. `/api/assistant/feedback` is deliberately absent from
    ASSISTANT_ROUTE_ALLOW (server/assistant/policy.py), so the auth middleware itself
    403s the call before the route's own `via == "assistant"` guard would ever run;
    either layer catching it satisfies "the assistant cannot use this endpoint",
    which is what this test pins end to end."""
    from server.assistant.token import mint_token
    from fastapi.testclient import TestClient
    from server.obs.metrics import assistant_feedback_total

    before = assistant_feedback_total.value("up")
    with _client() as c:
        tok = mint_token("testuser@example.com", "asst-sess-1")
        loopback = TestClient(c.app, client=("127.0.0.1", 5000))
        assert c.portal is not None
        loopback.portal = c.portal
        r = loopback.post("/api/assistant/feedback",
                          json={"sessionId": "s6", "turnId": "t6", "vote": "up"},
                          headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403

        from sqlalchemy import select
        from server.db import db_session
        from server.db.models import AuditEvent

        async def _get():
            async with db_session() as db:
                return (await db.execute(
                    select(AuditEvent).where(AuditEvent.type == "assistant.feedback")
                )).scalars().all()

        rows = c.portal.call(_get)
        assert not any(row.detail.get("sessionId") == "s6" for row in rows)
    assert assistant_feedback_total.value("up") == before
