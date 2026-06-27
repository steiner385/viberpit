"""Route tests: GET /api/pipeline/repos — per-repo StageModel endpoint.

Auth approach: mirrors test_pipeline_route.py — create_app() with
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
from server.pipeline.track import StageModel, StageNode

_H = {"Tailscale-User-Login": "testuser@example.com"}

_ENV = {
    "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    "CCSWITCH_DEV": "1",
    "CCSWITCH_TAILNET_ALLOWLIST": "",
}


class _FakeSessions:
    """Minimal sessions facade for the pipeline repos route tests."""

    def __init__(self):
        self._models = {}

    def all_repos_stage_models(self):
        return self._models


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


def test_pipeline_repos_requires_identity():
    client, _ = _make_client()
    with patch.dict(os.environ, _ENV), client:
        assert client.get("/api/pipeline/repos").status_code == 401


def test_pipeline_repos_returns_models():
    client, fake = _make_client()
    fake._models = {
        "o/r": StageModel(
            repo="o/r",
            nodes=(
                StageNode(id="ci", label="CI", kind="ci", order=0),
                StageNode(id="ready", label="Ready", kind="gate", order=1),
                StageNode(id="merged", label="Merged", kind="merged", order=2),
            ),
        )
    }
    with patch.dict(os.environ, _ENV), client:
        r = client.get("/api/pipeline/repos", headers=_H)
    assert r.status_code == 200
    body = r.json()
    assert body["o/r"]["nodes"][0]["id"] == "ci"
    assert body["o/r"]["repo"] == "o/r"
