"""POST /api/sessions/{id}/agents/{tool_use_id}/stop (spec §4.4)."""
import os
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:", "CCSWITCH_DEV": "1",
        "CCSWITCH_TAILNET_ALLOWLIST": ""}


class _FakeSessions:
    def __init__(self, task_id):
        self.stop_subagent = AsyncMock(return_value=task_id)


def _client(task_id):
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    fake = _FakeSessions(task_id)
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


def test_stop_route_returns_task_id_and_calls_orchestrator():
    client, fake = _client("abb42ada82e1382fe")
    with patch.dict(os.environ, _ENV), client:
        resp = client.post("/api/sessions/s1/agents/toolu_A/stop", headers=_H)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "taskId": "abb42ada82e1382fe"}
    fake.stop_subagent.assert_awaited_once_with("s1", "toolu_A")


def test_stop_route_404_when_not_running():
    client, _ = _client(None)
    with patch.dict(os.environ, _ENV), client:
        resp = client.post("/api/sessions/s1/agents/toolu_A/stop", headers=_H)
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "SUBAGENT_NOT_RUNNING"


def test_stop_route_reaches_real_facade_in_process():
    """Drive the route through the REAL in-process facade (`app.state.sessions` left
    as the `InProcessOrchestratorAdapter` `create_app` wires up), not a bare fake
    orchestrator. `test_stop_route_returns_task_id_and_calls_orchestrator` above
    stays green even if `InProcessOrchestratorAdapter` never learns to proxy
    `stop_subagent` — this test is the one that would have caught that gap, the way
    it bit `set_model_for_session` in #1199."""
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    orch.stop_subagent = AsyncMock(return_value="abb42ada82e1382fe")
    with patch.dict(os.environ, _ENV), TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/api/sessions/s1/agents/toolu_A/stop", headers=_H)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "taskId": "abb42ada82e1382fe"}
    orch.stop_subagent.assert_awaited_once_with("s1", "toolu_A")
