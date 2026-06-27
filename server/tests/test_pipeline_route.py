"""Route tests: GET /api/prs — unified pipeline view.

Auth approach: mirrors test_send_prompt_route.py — create_app() with
CCSWITCH_DEV=1 and CCSWITCH_TAILNET_ALLOWLIST="" so any identity passes,
then replace app.state.sessions with a lightweight fake after construction.
Send Tailscale-User-Login header for authenticated requests; omit for 401 test.
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
    """Minimal sessions facade for the pipeline route tests."""

    def __init__(self):
        self.all_prs_result: list = []

    async def all_prs(self):
        return self.all_prs_result


def _make_client():
    """Build a TestClient with the fake sessions facade injected."""
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
            usage_fn=lambda name: None,
        )
        app = create_app(orchestrator=orch)
    fake = _FakeSessions()
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


def test_get_prs_requires_identity():
    client, _ = _make_client()
    with patch.dict(os.environ, _ENV), client:
        resp = client.get("/api/prs")
    assert resp.status_code == 401


def test_get_prs_returns_population():
    from server.github.pr_status import PipelinePr, PullRequestInfo

    client, fake = _make_client()
    fake.all_prs_result = [
        PipelinePr(
            pr=PullRequestInfo(
                number=3, url="u3", state="open", checks="failing",
                title="t", repo="o/n", stage="parked",
            ),
            session_id="s1", session_name="Fix it",
        ),
        PipelinePr(
            pr=PullRequestInfo(
                number=4, url="u4", state="merged", checks="none",
                title="m", repo="o/n", stage="merged",
            ),
        ),
    ]
    with patch.dict(os.environ, _ENV), client:
        resp = client.get("/api/prs", headers=_H)
    assert resp.status_code == 200
    body = resp.json()
    assert [p["number"] for p in body] == [3, 4]
    assert body[0]["repo"] == "o/n" and body[0]["stage"] == "parked"
    assert body[0]["session"] == {"id": "s1", "name": "Fix it"}
    assert body[1]["session"] is None


def test_get_prs_carries_node_states():
    """Real GET /api/prs — response rows must include node_states (spec §7)."""
    from server.github.pr_status import PipelinePr, PullRequestInfo
    from server.pipeline.track import NodeStatus

    client, fake = _make_client()
    fake.all_prs_result = [
        PipelinePr(
            pr=PullRequestInfo(
                number=7, url="u7", state="open", checks="pending",
                title="feat", repo="o/r", stage="ci",
            ),
            session_id="s2", session_name="Track me",
            node_states=(NodeStatus("ci", "active"),),
        ),
    ]
    with patch.dict(os.environ, _ENV), client:
        resp = client.get("/api/prs", headers=_H)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["node_states"] == [{"id": "ci", "state": "active"}]
