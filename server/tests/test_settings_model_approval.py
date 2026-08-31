"""Tests for model-approval action + denylist settings (GET/PUT /api/settings)."""
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


def test_get_settings_model_approval_defaults_null():
    """GET /api/settings returns modelApprovalAction and modelApprovalDenylist as null by default."""
    with _client() as client:
        resp = client.get("/api/settings", headers=IDENTITY_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert "modelApprovalAction" in data
    assert data["modelApprovalAction"] is None
    assert "modelApprovalDenylist" in data
    assert data["modelApprovalDenylist"] is None


# ---------------------------------------------------------------------------
# Round-trip: PUT action + denylist → GET returns them
# ---------------------------------------------------------------------------


def test_put_model_approval_round_trips():
    """PUT {modelApprovalAction: 'block', modelApprovalDenylist: 'fable,haiku'} then GET → same."""
    with _client() as client:
        put_resp = client.put(
            "/api/settings",
            json={"modelApprovalAction": "block", "modelApprovalDenylist": "fable,haiku"},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["modelApprovalAction"] == "block"
        assert put_resp.json()["modelApprovalDenylist"] == "fable,haiku"

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.status_code == 200
        assert get_resp.json()["modelApprovalAction"] == "block"
        assert get_resp.json()["modelApprovalDenylist"] == "fable,haiku"


def test_put_model_approval_action_only_round_trips():
    """PUT with only modelApprovalAction (require_approval) round-trips correctly."""
    with _client() as client:
        put_resp = client.put(
            "/api/settings",
            json={"modelApprovalAction": "require_approval"},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["modelApprovalAction"] == "require_approval"

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["modelApprovalAction"] == "require_approval"


def test_put_model_approval_allow_round_trips():
    """PUT {modelApprovalAction: 'allow'} round-trips correctly."""
    with _client() as client:
        put_resp = client.put(
            "/api/settings",
            json={"modelApprovalAction": "allow"},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["modelApprovalAction"] == "allow"

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["modelApprovalAction"] == "allow"


# ---------------------------------------------------------------------------
# Invalid action → 422
# ---------------------------------------------------------------------------


def test_put_invalid_model_approval_action_rejected():
    """PUT {modelApprovalAction: 'nope'} → 422 (not a valid VALID_ACTIONS member)."""
    with _client() as client:
        resp = client.put(
            "/api/settings",
            json={"modelApprovalAction": "nope"},
            headers=IDENTITY_HEADERS,
        )
        assert resp.status_code in (400, 422)


def test_put_invalid_action_does_not_persist():
    """After a rejected invalid action PUT, the stored value remains null."""
    with _client() as client:
        resp = client.put(
            "/api/settings",
            json={"modelApprovalAction": "bad-value"},
            headers=IDENTITY_HEADERS,
        )
        assert resp.status_code in (400, 422)

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["modelApprovalAction"] is None


# ---------------------------------------------------------------------------
# Null clears the stored value (model_fields_set idiom)
# ---------------------------------------------------------------------------


def test_put_null_action_clears_stored_value():
    """Explicitly sending null for modelApprovalAction clears a previously set value."""
    with _client() as client:
        # Set a value first
        client.put(
            "/api/settings",
            json={"modelApprovalAction": "block"},
            headers=IDENTITY_HEADERS,
        )
        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["modelApprovalAction"] == "block"

        # Now explicitly null it out
        put_resp = client.put(
            "/api/settings",
            json={"modelApprovalAction": None},
            headers=IDENTITY_HEADERS,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["modelApprovalAction"] is None

        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["modelApprovalAction"] is None


def test_put_omitted_action_leaves_stored_value_unchanged():
    """Omitting modelApprovalAction from PUT body leaves the stored value intact."""
    with _client() as client:
        client.put(
            "/api/settings",
            json={"modelApprovalAction": "block"},
            headers=IDENTITY_HEADERS,
        )
        # PUT something else, no modelApprovalAction key
        client.put(
            "/api/settings",
            json={"thinkingCollapsed": False},
            headers=IDENTITY_HEADERS,
        )
        get_resp = client.get("/api/settings", headers=IDENTITY_HEADERS)
        assert get_resp.json()["modelApprovalAction"] == "block"
