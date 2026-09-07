"""GET /api/automations/backlog-runner/groups — plan visibility endpoint."""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

H = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client():
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url,
                                     "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


async def _seed():
    from server.automations.backlog_grouping import (
        load_plan, mark_consumed, store_plan)
    candidates = [
        {"ref": "o/r#2", "number": 2, "repo": "o/r", "title": "Fix two"},
        {"ref": "o/r#5", "number": 5, "repo": "o/r", "title": "Fix five"},
        {"ref": "o/r#9", "number": 9, "repo": "o/r", "title": "Fix nine"},
    ]
    await store_plan([
        {"refs": ["o/r#9", "o/r#5"], "rationale": "same subsystem"},
        {"refs": ["o/r#2"], "rationale": None},
    ], candidates)
    plan = await load_plan()
    await mark_consumed(plan[0]["id"])


def test_groups_shape_priority_order_and_consumed_stamp():
    with _client() as c:
        # Seed on the app's own loop: a bare asyncio.run shares the engine pool
        # across two loops and dies once startup saturates it (#1386).
        assert c.portal is not None
        c.portal.call(_seed)
        body = c.get("/api/automations/backlog-runner/groups", headers=H).json()
        assert body["generation"] and body["createdAt"]
        groups = body["groups"]
        assert [g["refs"] for g in groups] == [["o/r#9", "o/r#5"], ["o/r#2"]]
        first = groups[0]
        assert first["repo"] == "o/r"
        assert first["rationale"] == "same subsystem"
        assert first["titles"] == {"o/r#9": "Fix nine", "o/r#5": "Fix five"}
        assert first["consumedAt"], "consumed stamp must surface"
        assert groups[1]["consumedAt"] is None


def test_groups_empty_plan_returns_empty_shape():
    with _client() as c:
        body = c.get("/api/automations/backlog-runner/groups", headers=H).json()
        assert body == {"generation": None, "createdAt": None, "groups": []}


def test_groups_requires_auth():
    with _client() as c:
        r = c.get("/api/automations/backlog-runner/groups")
        assert r.status_code == 401
