"""API surface for consolidation. Uses the app factory + a stub orchestrator on
app.state.sessions, mirroring test_pr_doctor_actions_api.py."""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

H = {"Tailscale-User-Login": "testuser@example.com"}


class _StubOrch:
    async def consolidation_candidates(self, repo=None):
        return {"repos": {"o/r": {"candidates": [{"number": 1}], "clusters": []}}}

    async def automation_consolidate(self, repo, numbers):
        return {"status": "spawned", "level": "ok", "message": "Spawned consolidation session"}


@contextmanager
def _client():
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url,
                                     "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            real = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            app = create_app(orchestrator=real)
            app.state.sessions = _StubOrch()
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


def test_get_consolidation_returns_repos():
    with _client() as c:
        r = c.get("/api/automations/pr-doctor/consolidation", headers=H)
    assert r.status_code == 200
    assert "o/r" in r.json()["repos"]


def test_post_consolidate_returns_trigger_result():
    with _client() as c:
        r = c.post("/api/automations/pr-doctor/consolidate",
                   json={"repo": "o/r", "numbers": [1, 2]}, headers=H)
    assert r.status_code == 200
    assert r.json()["status"] == "spawned"


def test_post_consolidate_422_on_malformed_body():
    with _client() as c:
        r = c.post("/api/automations/pr-doctor/consolidate",
                   json={"repo": "o/r"}, headers=H)
    assert r.status_code == 422       # numbers missing → pydantic rejects
