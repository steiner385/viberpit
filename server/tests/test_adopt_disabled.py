"""When CCSWITCH_ENABLE_ADOPTION is off (default), adoption routes 404."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

H = {"Tailscale-User-Login": "testuser@example.com"}


@pytest.fixture(autouse=True)
def _disable_adoption(monkeypatch):
    # Guarantee the flag is off at request time regardless of ambient shell env.
    monkeypatch.delenv("CCSWITCH_ENABLE_ADOPTION", raising=False)


def _client():
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    env = {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
           "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}
    with patch.dict(os.environ, env):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        app = create_app(orchestrator=orch)
        return TestClient(app, raise_server_exceptions=False)


def test_adoptable_404_when_disabled():
    client = _client()
    r = client.get("/api/sessions/adoptable", headers=H)
    assert r.status_code == 404


def test_adopt_404_when_disabled():
    client = _client()
    r = client.post("/api/sessions/adopt", json={"claudeSessionId": "x"}, headers=H)
    assert r.status_code == 404
