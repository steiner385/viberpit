"""Tests for the assistant enabled/model settings (GET/PUT /api/settings), spec §4.11.

Mirrors test_settings_model_approval.py's structure exactly: GET defaults null, PUT
round-trips, explicit null clears via model_fields_set, an omitted field leaves the
stored value untouched.
"""
from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests.helpers import temp_env

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client():
    """Live-app TestClient over an isolated file-backed SQLite DB (mirrors test_settings.py)."""
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    from fastapi.testclient import TestClient
    from unittest.mock import patch

    with temp_env(), tempfile.TemporaryDirectory() as d:
        db_url = f"sqlite+aiosqlite:///{Path(d) / 'settings.db'}"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}):
            orch = SessionOrchestrator(poll_interval=9999.0)
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)
            with client:
                yield client


# ---------------------------------------------------------------------------
# GET defaults
# ---------------------------------------------------------------------------


def test_get_settings_assistant_defaults_null():
    """GET /api/settings returns assistantEnabled and assistantModel as null by default."""
    with _client() as client:
        resp = client.get("/api/settings", headers=IDENTITY_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert "assistantEnabled" in data
    assert data["assistantEnabled"] is None
    assert "assistantModel" in data
    assert data["assistantModel"] is None


# ---------------------------------------------------------------------------
# Round-trip: PUT assistantEnabled + assistantModel → GET returns them
# ---------------------------------------------------------------------------


def test_put_assistant_settings_round_trips():
    """PUT {assistantEnabled: false, assistantModel: 'opus'} then GET → same."""
    with _client() as client:
        put_resp = client.put(
            "/api/settings",
            json={"assistantEnabled": False, "assistantModel": "opus"},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["assistantEnabled"] is False
        assert put_resp.json()["assistantModel"] == "opus"

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.status_code == 200
        assert get_resp.json()["assistantEnabled"] is False
        assert get_resp.json()["assistantModel"] == "opus"


def test_put_assistant_enabled_only_round_trips():
    """PUT with only assistantEnabled round-trips correctly, leaving assistantModel null."""
    with _client() as client:
        put_resp = client.put(
            "/api/settings",
            json={"assistantEnabled": True},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["assistantEnabled"] is True
        assert put_resp.json()["assistantModel"] is None

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["assistantEnabled"] is True


def test_put_assistant_model_only_round_trips():
    """PUT with only assistantModel round-trips correctly, leaving assistantEnabled null."""
    with _client() as client:
        put_resp = client.put(
            "/api/settings",
            json={"assistantModel": "haiku"},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["assistantModel"] == "haiku"
        assert put_resp.json()["assistantEnabled"] is None

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["assistantModel"] == "haiku"


# ---------------------------------------------------------------------------
# Null clears the stored value (model_fields_set idiom)
# ---------------------------------------------------------------------------


def test_put_null_assistant_enabled_clears_stored_value():
    """Explicitly sending null for assistantEnabled clears a previously set value."""
    with _client() as client:
        client.put(
            "/api/settings",
            json={"assistantEnabled": False},
            headers=IDENTITY_HEADERS,
        )
        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["assistantEnabled"] is False

        put_resp = client.put(
            "/api/settings",
            json={"assistantEnabled": None},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["assistantEnabled"] is None

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["assistantEnabled"] is None


def test_put_null_assistant_model_clears_stored_value():
    """Explicitly sending null for assistantModel clears a previously set value."""
    with _client() as client:
        client.put(
            "/api/settings",
            json={"assistantModel": "opus"},
            headers=IDENTITY_HEADERS,
        )
        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["assistantModel"] == "opus"

        put_resp = client.put(
            "/api/settings",
            json={"assistantModel": None},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["assistantModel"] is None

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["assistantModel"] is None


def test_put_omitted_assistant_fields_leave_stored_value_unchanged():
    """Omitting assistantEnabled/assistantModel from PUT body leaves the stored values intact."""
    with _client() as client:
        client.put(
            "/api/settings",
            json={"assistantEnabled": True, "assistantModel": "sonnet"},
            headers=IDENTITY_HEADERS,
        )
        # PUT something else, no assistant keys
        client.put(
            "/api/settings",
            json={"thinkingCollapsed": False},
            headers=IDENTITY_HEADERS,
        )
        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["assistantEnabled"] is True
        assert get_resp.json()["assistantModel"] == "sonnet"
