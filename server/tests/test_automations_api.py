"""/api/automations: list, events, sessions, PATCH validation, auth."""
from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import patch

H = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client():
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        app = create_app(orchestrator=orch)
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


def test_list_includes_pr_doctor():
    with _client() as c:
        r = c.get("/api/automations", headers=H)
        assert r.status_code == 200
        ids = {a["id"] for a in r.json()}
        assert "pr-doctor" in ids
        pd = next(a for a in r.json() if a["id"] == "pr-doctor")
        assert pd["modes"][0] == "off"
        assert "configSchema" in pd and "status" in pd


def test_requires_auth():
    with _client() as c:
        assert c.get("/api/automations").status_code == 401


def test_patch_valid_mode_persists():
    with _client() as c:
        r = c.patch("/api/automations/pr-doctor", json={"enabled": True, "mode": "auto"}, headers=H)
        assert r.status_code == 200
        assert c.get("/api/automations", headers=H).json()[0]  # smoke
        pd = next(a for a in c.get("/api/automations", headers=H).json() if a["id"] == "pr-doctor")
        assert pd["enabled"] is True and pd["mode"] == "auto"


def test_patch_bad_mode_422():
    with _client() as c:
        assert c.patch("/api/automations/pr-doctor", json={"mode": "nope"}, headers=H).status_code == 422


def test_patch_bad_config_422():
    with _client() as c:
        r = c.patch("/api/automations/pr-doctor", json={"config": {"unknown": 1}}, headers=H)
        assert r.status_code == 422


def test_patch_unknown_automation_404():
    with _client() as c:
        assert c.patch("/api/automations/nope", json={"enabled": True}, headers=H).status_code == 404


def test_events_endpoint_empty_ok():
    with _client() as c:
        r = c.get("/api/automations/pr-doctor/events", headers=H)
        assert r.status_code == 200 and r.json() == []
