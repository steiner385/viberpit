"""GET /api/config exposes adoptionEnabled, off by default."""
from __future__ import annotations

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

H = {"Tailscale-User-Login": "testuser@example.com"}


def _client():
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        app = create_app(orchestrator=orch)
        return TestClient(app, raise_server_exceptions=False)


def test_config_adoption_disabled_by_default():
    client = _client()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CCSWITCH_ENABLE_ADOPTION", None)
        r = client.get("/api/config", headers=H)
    assert r.status_code == 200
    assert r.json()["adoptionEnabled"] is False


def test_config_adoption_enabled_when_flag_set():
    client = _client()
    with patch.dict(os.environ, {"CCSWITCH_ENABLE_ADOPTION": "1"}):
        r = client.get("/api/config", headers=H)
    assert r.json()["adoptionEnabled"] is True
