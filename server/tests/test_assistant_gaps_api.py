"""API tests for GET /api/assistant/gaps (plan Task 10.4, issue #1249): the capability
gap table — `assistant.gap` audit rows (written by the `report_gap` tool) aggregated by
capability over a trailing window. Mirrors test_assistant_feedback_api.py's lightweight
`_client` fixture; rows are seeded straight into the DB through the client's portal so
`at` can be set explicitly for the window test.
"""
from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import timedelta
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
        db_url = f"sqlite+aiosqlite:///{Path(d) / 'gaps.db'}"
        full_env = {"VIBERPIT_DB_URL": db_url, **(env or {})}
        with patch.dict(os.environ, full_env):
            orch = SessionOrchestrator(poll_interval=9999.0)
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


def _seed(c, rows):
    """Insert `(type, capability | None, age)` audit rows; `age` is a timedelta before now."""
    from server.db import db_session
    from server.db.clock import naive_utc_now
    from server.db.models import AuditEvent

    async def _insert():
        now = naive_utc_now()
        async with db_session() as db:
            for i, (typ, cap, age) in enumerate(rows):
                detail = {"detail": f"secret words {i}"}
                if cap is not None:
                    detail["capability"] = cap
                db.add(AuditEvent(type=typ, identity=f"user{i}@example.com", detail=detail,
                                  via="assistant", at=now - age))
            await db.commit()

    # A context-managed TestClient owns the app's loop; run DB work through its portal.
    c.portal.call(_insert)


def test_gaps_aggregates_by_capability_orders_and_excludes_old_rows():
    with _client() as c:
        _seed(c, [
            ("assistant.gap", "deploy", timedelta(days=1)),
            ("assistant.gap", "deploy", timedelta(hours=2)),
            ("assistant.gap", "edit-code", timedelta(days=3)),
            ("assistant.gap", "edit-code", timedelta(days=5)),
            ("assistant.gap", "merge-pr", timedelta(days=2)),
            ("assistant.gap", None, timedelta(days=4)),
            # Outside the default 30-day window.
            ("assistant.gap", "merge-pr", timedelta(days=40)),
            ("assistant.gap", "ancient", timedelta(days=90)),
            # Not a gap row at all.
            ("assistant.feedback", "deploy", timedelta(days=1)),
        ])
        r = c.get("/api/assistant/gaps", headers=IDENTITY)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["days"] == 30
        assert body["total"] == 6
        assert [(g["capability"], g["count"]) for g in body["gaps"]] == [
            ("deploy", 2), ("edit-code", 2), ("merge-pr", 1), ("unknown", 1)]
        # lastSeen is the newest row per capability, ISO UTC.
        deploy, edit = body["gaps"][0], body["gaps"][1]
        assert deploy["lastSeen"].endswith("Z") or deploy["lastSeen"].endswith("+00:00")
        assert deploy["lastSeen"] > edit["lastSeen"]
        # Aggregated across identities: never leak who reported it or their words.
        assert "secret words" not in r.text
        assert "@example.com" not in r.text

        wide = c.get("/api/assistant/gaps", params={"days": 60}, headers=IDENTITY).json()
        assert wide["days"] == 60
        assert wide["total"] == 7
        assert ("merge-pr", 2) in [(g["capability"], g["count"]) for g in wide["gaps"]]
        assert "ancient" not in [g["capability"] for g in wide["gaps"]]


def test_gaps_empty():
    with _client() as c:
        r = c.get("/api/assistant/gaps", headers=IDENTITY)
        assert r.status_code == 200, r.text
        assert r.json() == {"days": 30, "total": 0, "gaps": []}


def test_gaps_days_bounds_are_422():
    with _client() as c:
        for days in (0, -1, 366):
            r = c.get("/api/assistant/gaps", params={"days": days}, headers=IDENTITY)
            assert r.status_code == 422, (days, r.text)
        assert c.get("/api/assistant/gaps", params={"days": 365},
                     headers=IDENTITY).status_code == 200
        assert c.get("/api/assistant/gaps", params={"days": 1},
                     headers=IDENTITY).status_code == 200


def test_gaps_route_is_not_on_the_assistant_allowlist():
    from server.assistant.policy import route_allowed

    assert not route_allowed("GET", "/api/assistant/gaps")
    assert not route_allowed("GET", "/api/assistant/gaps", operator=True)


def test_gaps_rejects_the_assistant_token_with_403():
    """The gap table aggregates every identity's reports; the assistant's loopback
    `ccas1:` bearer must never read it. `/api/assistant/gaps` is absent from
    ASSISTANT_ROUTE_ALLOW, so the auth middleware 403s it before routing."""
    from server.assistant.token import mint_token
    from fastapi.testclient import TestClient

    with _client() as c:
        _seed(c, [("assistant.gap", "deploy", timedelta(days=1))])
        tok = mint_token("testuser@example.com", "asst-sess-1")
        loopback = TestClient(c.app, client=("127.0.0.1", 5000))
        assert c.portal is not None
        loopback.portal = c.portal
        r = loopback.get("/api/assistant/gaps", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403
        assert "deploy" not in r.text
