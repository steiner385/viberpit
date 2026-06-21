"""Server-level tests for PATCH /api/accounts/{name} (nickname feature)."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests.helpers import temp_env, usage as make_usage
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
# PATCH /api/accounts/{name} — nickname round-trip
# ---------------------------------------------------------------------------


def test_patch_account_nickname_sets_and_returns_it():
    """PATCH /api/accounts/{name} sets a nickname; response includes it."""
    with temp_env():
        _seed_slot("alice")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            response = client.patch(
                "/api/accounts/alice",
                json={"nickname": "Work"},
                headers=IDENTITY_HEADERS,
            )

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "alice"
    assert data["nickname"] == "Work"
    assert "usage" in data
    assert "status" in data


def test_patch_account_nickname_clears_with_empty_string():
    """PATCH with empty nickname clears an existing nickname."""
    with temp_env():
        _seed_slot("bob")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            # Set
            client.patch(
                "/api/accounts/bob",
                json={"nickname": "Personal"},
                headers=IDENTITY_HEADERS,
            )
            # Clear
            response = client.patch(
                "/api/accounts/bob",
                json={"nickname": ""},
                headers=IDENTITY_HEADERS,
            )

    assert response.status_code == 200
    data = response.json()
    assert data["nickname"] is None


def test_patch_account_nickname_clears_with_null():
    """PATCH with null nickname clears an existing nickname."""
    with temp_env():
        _seed_slot("carol")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            client.patch(
                "/api/accounts/carol",
                json={"nickname": "Label"},
                headers=IDENTITY_HEADERS,
            )
            response = client.patch(
                "/api/accounts/carol",
                json={"nickname": None},
                headers=IDENTITY_HEADERS,
            )

    assert response.status_code == 200
    data = response.json()
    assert data["nickname"] is None


def test_patch_account_nickname_404_for_unknown():
    """PATCH /api/accounts/{unknown} returns 404."""
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            response = client.patch(
                "/api/accounts/does-not-exist",
                json={"nickname": "Ghost"},
                headers=IDENTITY_HEADERS,
            )

    assert response.status_code == 404


def test_patch_account_nickname_404_for_hidden_name_with_dot():
    """PATCH /api/accounts/{name} rejects names starting with dot."""
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            response = client.patch(
                "/api/accounts/.hidden",
                json={"nickname": "Secret"},
                headers=IDENTITY_HEADERS,
            )

    assert response.status_code == 404


def test_patch_account_nickname_requires_auth():
    """PATCH /api/accounts/{name} without identity header returns 401."""
    with temp_env():
        _seed_slot("dave")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            response = client.patch(
                "/api/accounts/dave",
                json={"nickname": "Secret"},
            )

    assert response.status_code == 401


def test_get_accounts_includes_nickname_in_response():
    """GET /api/accounts includes the nickname field for each account."""
    with temp_env():
        _seed_slot("eve")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.app import create_app
            orch = SessionOrchestrator(
                sdk_factory=fake_sdk_factory(),
                usage_fn=lambda name: make_usage(session=10.0),
                poll_interval=9999.0,
            )
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)
        with client:
            # Set nickname first
            client.patch(
                "/api/accounts/eve",
                json={"nickname": "Primary"},
                headers=IDENTITY_HEADERS,
            )
            response = client.get("/api/accounts", headers=IDENTITY_HEADERS)

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["nickname"] == "Primary"
