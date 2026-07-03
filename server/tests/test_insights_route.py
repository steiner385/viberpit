"""Route tests: GET /api/pipeline/insights/checks — per-repo Check Health insights.

Auth approach: mirrors test_merge_trains_route.py — create_app() with
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

_H = {"Tailscale-User-Login": "insights@example.com"}

_ENV = {
    "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    "CCSWITCH_DEV": "1",
    "CCSWITCH_TAILNET_ALLOWLIST": "",
}


class _FakeSessions:
    """Minimal sessions facade for the insights route tests."""

    def check_insights(self, repo, window):
        return {
            "repo": repo,
            "window": window,
            "slowest": [
                {
                    "check_name": "build",
                    "event": "pr",
                    "p50": 120.0,
                    "p90": 300.0,
                    "ratio": 2.5,
                    "n": 9,
                }
            ],
            "flakiest": [
                {
                    "check_name": "e2e",
                    "event": "pr",
                    "flake_pct": 33.3,
                    "flaky_commits": 1,
                    "total_commits": 3,
                    "n": 4,
                }
            ],
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


def test_insights_checks_returns_payload():
    client = _make_client()
    with patch.dict(os.environ, _ENV), client:
        r = client.get("/api/pipeline/insights/checks?repo=o/n&window=7d", headers=_H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["repo"] == "o/n" and body["window"] == "7d"
    assert body["slowest"][0]["check_name"] == "build"
    assert body["flakiest"][0]["flake_pct"] == 33.3


def test_insights_checks_requires_identity():
    client = _make_client()
    with patch.dict(os.environ, _ENV), client:
        resp = client.get("/api/pipeline/insights/checks?repo=o/n")
    assert resp.status_code == 401
