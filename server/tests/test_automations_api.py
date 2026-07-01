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


def test_sessions_endpoint_returns_only_origin_matched():
    """GET /api/automations/{id}/sessions returns only sessions with matching origin."""
    import asyncio
    from server.db import db_session
    from server.db.models import Session as DbSession

    async def _seed():
        async with db_session() as db:
            # Session spawned by pr-doctor automation
            db.add(DbSession(
                id="s-origin-match",
                name="pr-fix-session",
                account_name="acct1",
                cwd="/tmp",
                origin="pr-doctor",
                origin_ref="owner/repo#42",
            ))
            # Session with no origin (human-created) — must NOT appear
            db.add(DbSession(
                id="s-no-origin",
                name="manual-session",
                account_name="acct2",
                cwd="/tmp",
                origin=None,
            ))

    with _client() as c:
        # Seed rows now that init_db has run (in-memory DB is alive)
        asyncio.run(_seed())

        r = c.get("/api/automations/pr-doctor/sessions", headers=H)
        assert r.status_code == 200
        sessions = r.json()
        ids = [s["id"] for s in sessions]
        assert "s-origin-match" in ids
        assert "s-no-origin" not in ids
        matched = next(s for s in sessions if s["id"] == "s-origin-match")
        assert matched["originRef"] == "owner/repo#42"

        # 401 without auth header
        assert c.get("/api/automations/pr-doctor/sessions").status_code == 401
