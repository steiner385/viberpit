"""Server-level tests for the Copilot device-flow add-account REST endpoints.

POST /api/accounts/add/copilot         → start device flow (202; 409 exists; 400 bad name)
GET  /api/accounts/add/copilot?name=   → status of in-flight device flow
POST /api/accounts/add/copilot/cancel  → cancel a running device flow

The orchestrator's copilot_add_start/status/cancel are replaced with a fake
facade injected on app.state.sessions — no real CopilotAuthManager needed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests.helpers import temp_env

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}

_CANNED_START = {
    "state": "awaiting_authorization",
    "userCode": "ABCD-1234",
    "verificationUri": "https://github.com/login/device",
}
_CANNED_STATUS = {
    "state": "awaiting_authorization",
    "userCode": "ABCD-1234",
    "verificationUri": "https://github.com/login/device",
}
_CANNED_CANCEL = {"state": "idle"}

_ENV = {
    "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    "CCSWITCH_DEV": "1",
}


# ---------------------------------------------------------------------------
# Fake sessions facade
# ---------------------------------------------------------------------------

class _FakeSessions:
    """Minimal sessions facade exposing only the copilot device-flow methods."""

    def __init__(self):
        self.copilot_add_start = AsyncMock(return_value=_CANNED_START)
        self.copilot_add_status = AsyncMock(return_value=_CANNED_STATUS)
        self.copilot_add_cancel = AsyncMock(return_value=_CANNED_CANCEL)
        # Stub usage so the accounts-list route (if hit) doesn't crash
        self.usage = AsyncMock(return_value=None)

    # Keep the facade interface parity: daemon_epoch is sync
    def daemon_epoch(self):
        return None


# ---------------------------------------------------------------------------
# App/client factory + seed helper
# ---------------------------------------------------------------------------

def _seed_copilot_token_dir(name: str) -> None:
    """Create the copilot token directory so the slot appears to exist."""
    from ccswitch import paths
    d = paths.copilot_token_dir(name)
    d.mkdir(parents=True, exist_ok=True)


def _make_client() -> tuple[TestClient, _FakeSessions]:
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
        )
        app = create_app(orchestrator=orch)

    fake = _FakeSessions()
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


# ---------------------------------------------------------------------------
# POST /api/accounts/add/copilot — start
# ---------------------------------------------------------------------------


def test_copilot_add_start_returns_202_with_state():
    """POST /api/accounts/add/copilot returns 202 with the device-flow state dict."""
    with temp_env():
        with patch.dict(os.environ, _ENV):
            client, fake = _make_client()
        with client:
            resp = client.post(
                "/api/accounts/add/copilot",
                json={"name": "gh-copilot"},
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 202
    data = resp.json()
    assert data["state"] == "awaiting_authorization"
    assert data["userCode"] == "ABCD-1234"
    assert "verificationUri" in data
    fake.copilot_add_start.assert_awaited_once_with("gh-copilot")


def test_copilot_add_start_409_when_token_dir_exists():
    """POST /api/accounts/add/copilot returns 409 when the token dir already exists."""
    with temp_env():
        _seed_copilot_token_dir("existing-copilot")
        with patch.dict(os.environ, _ENV):
            client, _ = _make_client()
        with client:
            resp = client.post(
                "/api/accounts/add/copilot",
                json={"name": "existing-copilot"},
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "account_exists"
    assert "existing-copilot" in detail["message"]


def test_copilot_add_start_400_invalid_name_with_space():
    """POST /api/accounts/add/copilot returns 400 for a name containing spaces."""
    with temp_env():
        with patch.dict(os.environ, _ENV):
            client, _ = _make_client()
        with client:
            resp = client.post(
                "/api/accounts/add/copilot",
                json={"name": "has space"},
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_name"
    assert isinstance(detail.get("message"), str) and detail["message"].strip()


def test_copilot_add_start_400_invalid_name_email():
    """POST /api/accounts/add/copilot returns 400 for an email-style name."""
    with temp_env():
        with patch.dict(os.environ, _ENV):
            client, _ = _make_client()
        with client:
            resp = client.post(
                "/api/accounts/add/copilot",
                json={"name": "tony@github.com"},
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 400


def test_copilot_add_start_requires_auth():
    """POST /api/accounts/add/copilot without an identity header returns 401."""
    with temp_env():
        with patch.dict(os.environ, _ENV):
            client, _ = _make_client()
        with client:
            resp = client.post(
                "/api/accounts/add/copilot",
                json={"name": "gh-copilot"},
            )

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/accounts/add/copilot?name= — status
# ---------------------------------------------------------------------------


def test_copilot_add_status_returns_state_dict():
    """GET /api/accounts/add/copilot?name=… returns the current device-flow state."""
    with temp_env():
        with patch.dict(os.environ, _ENV):
            client, fake = _make_client()
        with client:
            resp = client.get(
                "/api/accounts/add/copilot",
                params={"name": "gh-copilot"},
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "awaiting_authorization"
    assert data["userCode"] == "ABCD-1234"
    fake.copilot_add_status.assert_awaited_once_with("gh-copilot")


def test_copilot_add_status_requires_auth():
    """GET /api/accounts/add/copilot without an identity header returns 401."""
    with temp_env():
        with patch.dict(os.environ, _ENV):
            client, _ = _make_client()
        with client:
            resp = client.get(
                "/api/accounts/add/copilot",
                params={"name": "gh-copilot"},
            )

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/accounts/add/copilot/cancel
# ---------------------------------------------------------------------------


def test_copilot_add_cancel_returns_idle():
    """POST /api/accounts/add/copilot/cancel cancels the flow and returns idle."""
    with temp_env():
        with patch.dict(os.environ, _ENV):
            client, fake = _make_client()
        with client:
            resp = client.post(
                "/api/accounts/add/copilot/cancel",
                json={"name": "gh-copilot"},
                headers=IDENTITY_HEADERS,
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "idle"
    fake.copilot_add_cancel.assert_awaited_once_with("gh-copilot")


def test_copilot_add_cancel_requires_auth():
    """POST /api/accounts/add/copilot/cancel without an identity header returns 401."""
    with temp_env():
        with patch.dict(os.environ, _ENV):
            client, _ = _make_client()
        with client:
            resp = client.post(
                "/api/accounts/add/copilot/cancel",
                json={"name": "gh-copilot"},
            )

    assert resp.status_code == 401
