"""Route tests: GET /api/pipeline/trains — merge-train state per repo.

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
from server.pipeline.merge_train import MergeTrainState, TrainBuild

_H = {"Tailscale-User-Login": "testuser@example.com"}

_ENV = {
    "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    "CCSWITCH_DEV": "1",
    "CCSWITCH_TAILNET_ALLOWLIST": "",
}


class _FakeSessions:
    """Minimal sessions facade for the merge-trains route tests."""

    async def all_merge_trains(self):
        return {
            "cairnea/KinDash": MergeTrainState(
                repo="cairnea/KinDash",
                depth=5,
                trains_per_hour=0.8,
                batch_success_pct=38.0,
                ejects_24h=46,
                oldest_wait_seconds=5760,
                failing=(10823,),
                building=TrainBuild(10824, None, 720),
                blocked=(10805,),
                awaiting_qa=1,
                awaiting_prod=42,
                merges_next=TrainBuild(10824, None, 720),
            )
        }


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
    return TestClient(app, raise_server_exceptions=False)


def test_trains_requires_identity():
    client = _make_client()
    with patch.dict(os.environ, _ENV), client:
        resp = client.get("/api/pipeline/trains")
    assert resp.status_code == 401


def test_trains_endpoint_returns_serialized_state():
    client = _make_client()
    with patch.dict(os.environ, _ENV), client:
        resp = client.get("/api/pipeline/trains", headers=_H)
    assert resp.status_code == 200
    body = resp.json()
    assert body["cairnea/KinDash"]["depth"] == 5
    assert body["cairnea/KinDash"]["building"]["pr_number"] == 10824
    assert body["cairnea/KinDash"]["blocked"] == [10805]
    assert body["cairnea/KinDash"]["awaiting_qa"] == 1
    assert body["cairnea/KinDash"]["awaiting_prod"] == 42
