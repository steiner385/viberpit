"""Server-level tests for PATCH /api/accounts/{name} modelApprovalAction field."""
from __future__ import annotations

import os
import time
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from fastapi.testclient import TestClient

from tests.helpers import temp_env
from server.tests.fakes.fake_factory import fake_sdk_factory

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


def _seed_slot(name: str) -> None:
    from ccswitch import paths
    store = paths.store_dir()
    store.mkdir(parents=True, exist_ok=True)
    slot = store / f"{name}.cred.json"
    now_ms = int(time.time() * 1000)
    slot.write_text(
        f'{{"accessToken":"{name}-tok","refreshToken":"{name}-ref",'
        f'"expiresAt":{now_ms + 9_000_000},"subscriptionType":"max"}}'
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
# PATCH /api/accounts/{name} — modelApprovalAction round-trip
# ---------------------------------------------------------------------------


def test_set_account_model_approval_action_allow():
    """PATCH sets modelApprovalAction=allow; response reflects it."""
    with temp_env():
        _seed_slot("alice")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            response = client.patch(
                "/api/accounts/alice",
                json={"modelApprovalAction": "allow"},
                headers=IDENTITY_HEADERS,
            )

    assert response.status_code == 200
    data = response.json()
    assert data["accountDefaults"]["modelApprovalAction"] == "allow"


def test_set_account_model_approval_action_block():
    """PATCH sets modelApprovalAction=block; response reflects it."""
    with temp_env():
        _seed_slot("bob")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            response = client.patch(
                "/api/accounts/bob",
                json={"modelApprovalAction": "block"},
                headers=IDENTITY_HEADERS,
            )

    assert response.status_code == 200
    data = response.json()
    assert data["accountDefaults"]["modelApprovalAction"] == "block"


def test_set_account_model_approval_action_require_approval():
    """PATCH sets modelApprovalAction=require_approval; response reflects it."""
    with temp_env():
        _seed_slot("carol")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            response = client.patch(
                "/api/accounts/carol",
                json={"modelApprovalAction": "require_approval"},
                headers=IDENTITY_HEADERS,
            )

    assert response.status_code == 200
    data = response.json()
    assert data["accountDefaults"]["modelApprovalAction"] == "require_approval"


def test_set_account_model_approval_action_invalid_rejected():
    """PATCH with an invalid modelApprovalAction returns 400 or 422."""
    with temp_env():
        _seed_slot("dave")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            response = client.patch(
                "/api/accounts/dave",
                json={"modelApprovalAction": "nope"},
                headers=IDENTITY_HEADERS,
            )

    assert response.status_code in (400, 422)


def test_clear_account_model_approval_action_with_null():
    """PATCH with null modelApprovalAction clears the per-account override."""
    with temp_env():
        _seed_slot("eve")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            # First set it
            client.patch(
                "/api/accounts/eve",
                json={"modelApprovalAction": "allow"},
                headers=IDENTITY_HEADERS,
            )
            # Then clear it
            response = client.patch(
                "/api/accounts/eve",
                json={"modelApprovalAction": None},
                headers=IDENTITY_HEADERS,
            )

    assert response.status_code == 200
    data = response.json()
    assert data["accountDefaults"]["modelApprovalAction"] is None


def test_account_defaults_always_includes_model_approval_action_key():
    """accountDefaults always has a modelApprovalAction key (even when unset)."""
    with temp_env():
        _seed_slot("frank")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            # A plain GET /api/accounts includes the key
            response = client.get("/api/accounts", headers=IDENTITY_HEADERS)

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert "modelApprovalAction" in data[0]["accountDefaults"]
    assert data[0]["accountDefaults"]["modelApprovalAction"] is None
