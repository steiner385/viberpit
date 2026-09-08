"""Server-level tests for provider-aware account API (Copilot account type).

Verifies:
  - GET /api/accounts returns `provider == "copilot"` for a copilot-seeded account.
  - DELETE /api/accounts/{name} returns 204 (not 404) when only a token-dir exists
    (no .cred.json slot), proving _check_name_and_slot accepts the copilot token-dir.
"""
from __future__ import annotations

import json
import pytest
import os
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from fastapi.testclient import TestClient
from tests.helpers import temp_env
from server.tests.fakes.fake_factory import fake_sdk_factory

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


@pytest.fixture(autouse=True)
def _fresh_db(isolated_db):
    """Per-test DB (#728): several tests here exit their CCSWITCH_DB_URL patch
    before `with client:`, so the app lifespan resolves the AMBIENT env var at
    init_db time — without this fixture that was the worker-shared session DB,
    coupling these files to whatever ran (or leaked a writer) before them."""



def _seed_copilot_account(name: str) -> None:
    """Create a copilot token-dir with a credentials.json (no .cred.json slot)."""
    from ccswitch import paths
    token_dir = paths.copilot_token_dir(name)
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "credentials.json").write_text(
        json.dumps({"access_token": f"{name}-copilot-tok", "expires_at": 9999999999})
    )


def _make_client():
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    orch = SessionOrchestrator(
        sdk_factory=fake_sdk_factory(),
        poll_interval=9999.0,
    )
    app = create_app(orchestrator=orch)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# GET /api/accounts — provider field for copilot accounts
# ---------------------------------------------------------------------------


def test_get_accounts_copilot_provider_field():
    """GET /api/accounts returns provider == 'copilot' for a copilot-seeded account."""
    with temp_env():
        _seed_copilot_account("copilot-work")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            response = client.get("/api/accounts", headers=IDENTITY_HEADERS)

    assert response.status_code == 200
    data = response.json()
    copilot_accounts = [a for a in data if a["name"] == "copilot-work"]
    assert len(copilot_accounts) == 1, f"Expected copilot-work in {[a['name'] for a in data]}"
    assert copilot_accounts[0]["provider"] == "copilot"


# ---------------------------------------------------------------------------
# POST /api/accounts/{name}/refresh — re-reads the proxy, cold if need be
# ---------------------------------------------------------------------------


class _FakeSessions:
    """Only what the refresh route touches; records the forced quota read."""

    def __init__(self) -> None:
        from unittest.mock import AsyncMock
        self.process_quota_refresh = AsyncMock(return_value=None)
        self.usage = AsyncMock(return_value=None)
        self.live_session_counts = AsyncMock(return_value={})

    def daemon_epoch(self):
        return None


def test_refresh_forces_a_process_backed_quota_read():
    """Copilot quota is process-backed: the poller only reads what the refresher
    persisted, so the refresh button must ask the daemon to read the proxy NOW
    (starting it when no session has one up) — the list read never forces."""
    with temp_env():
        _seed_copilot_account("copilot-work")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        fake = _FakeSessions()
        client.app.state.sessions = fake
        with client:
            client.get("/api/accounts", headers=IDENTITY_HEADERS)
            fake.process_quota_refresh.assert_not_awaited()
            resp = client.post("/api/accounts/copilot-work/refresh", headers=IDENTITY_HEADERS)
    assert resp.status_code == 200
    fake.process_quota_refresh.assert_awaited_once_with("copilot-work", force=True)


# ---------------------------------------------------------------------------
# DELETE /api/accounts/{name} — copilot token-dir accepted (not 404)
# ---------------------------------------------------------------------------


def test_delete_copilot_account_returns_204():
    """DELETE /api/accounts/{name} returns 204 for a copilot account (token-dir only)."""
    with temp_env():
        _seed_copilot_account("copilot-delete-me")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            response = client.delete(
                "/api/accounts/copilot-delete-me",
                headers=IDENTITY_HEADERS,
            )

    assert response.status_code == 204
