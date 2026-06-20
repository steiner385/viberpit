"""Tests for GET/PUT /api/settings (AppSettings singleton row)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests.helpers import temp_env

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


def _make_app_and_client():
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app

    with patch.dict(
        os.environ,
        {
            "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
        },
    ):
        orch = SessionOrchestrator(poll_interval=9999.0)
        app = create_app(orchestrator=orch)
        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)
        return app, client


# ---------------------------------------------------------------------------
# GET /api/settings
# ---------------------------------------------------------------------------


def test_get_settings_returns_defaults():
    """GET /api/settings returns the singleton row with defaults on first call."""
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            _, client = _make_app_and_client()
            with client:
                resp = client.get("/api/settings", headers=IDENTITY_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert data["defaultPermissionMode"] == "interactive"
    assert data["thinkingCollapsed"] is True
    assert data["repoRoot"] is None


# ---------------------------------------------------------------------------
# PUT /api/settings round-trip
# ---------------------------------------------------------------------------


def test_put_settings_round_trips():
    """PUT {defaultPermissionMode: 'auto'} then GET → 'auto'."""
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            _, client = _make_app_and_client()
            with client:
                put_resp = client.put(
                    "/api/settings",
                    json={"defaultPermissionMode": "auto"},
                    headers=IDENTITY_HEADERS,
                )
                assert put_resp.status_code == 200
                assert put_resp.json()["defaultPermissionMode"] == "auto"

                get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
                assert get_resp.status_code == 200
                assert get_resp.json()["defaultPermissionMode"] == "auto"


# ---------------------------------------------------------------------------
# PUT /api/settings rejects invalid mode
# ---------------------------------------------------------------------------


def test_put_rejects_invalid_mode():
    """PUT {defaultPermissionMode: 'bogus'} → value silently stays 'interactive'."""
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            _, client = _make_app_and_client()
            with client:
                put_resp = client.put(
                    "/api/settings",
                    json={"defaultPermissionMode": "bogus"},
                    headers=IDENTITY_HEADERS,
                )
                assert put_resp.status_code == 200
                assert put_resp.json()["defaultPermissionMode"] == "interactive"

                get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
                assert get_resp.status_code == 200
                assert get_resp.json()["defaultPermissionMode"] == "interactive"
