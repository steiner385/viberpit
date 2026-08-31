"""Server-level tests for PATCH /api/accounts/{name}/enabled (enable/disable)."""
from __future__ import annotations

import os
import pytest
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


@pytest.fixture(autouse=True)
def _fresh_db(isolated_db):
    """Per-test DB (#728): several tests here exit their CCSWITCH_DB_URL patch
    before `with client:`, so the app lifespan resolves the AMBIENT env var at
    init_db time — without this fixture that was the worker-shared session DB,
    coupling these files to whatever ran (or leaked a writer) before them."""



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


def _make_client(usage_fn=None):
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    orch = SessionOrchestrator(
        sdk_factory=fake_sdk_factory(),
        usage_fn=usage_fn or (lambda name: make_usage(session=10.0)),
        poll_interval=9999.0,
    )
    app = create_app(orchestrator=orch)
    return TestClient(app, raise_server_exceptions=False)


def test_patch_enabled_disables_and_returns_state():
    with temp_env():
        _seed_slot("alice")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            response = client.patch(
                "/api/accounts/alice/enabled",
                json={"enabled": False},
                headers=IDENTITY_HEADERS,
            )

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "alice"
    assert data["enabled"] is False


def test_patch_enabled_reenables():
    with temp_env():
        _seed_slot("bob")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            client.patch("/api/accounts/bob/enabled",
                         json={"enabled": False}, headers=IDENTITY_HEADERS)
            response = client.patch("/api/accounts/bob/enabled",
                                    json={"enabled": True}, headers=IDENTITY_HEADERS)

    assert response.status_code == 200
    assert response.json()["enabled"] is True


def test_patch_enabled_persists_to_ledger():
    """Disabling via the API writes the ledger, which the selector reads to skip
    the account (selector-skip behavior itself is covered in test_pick_account_detailed)."""
    with temp_env():
        _seed_slot("carol")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            client.patch("/api/accounts/carol/enabled",
                         json={"enabled": False}, headers=IDENTITY_HEADERS)
        from ccswitch import ledger
        with ledger.locked_ledger() as led:
            assert ledger.is_account_enabled(led, "carol") is False


def test_patch_enabled_404_for_unknown():
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            response = client.patch("/api/accounts/nope/enabled",
                                    json={"enabled": False}, headers=IDENTITY_HEADERS)
    assert response.status_code == 404


def test_patch_enabled_requires_auth():
    with temp_env():
        _seed_slot("dave")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            response = client.patch("/api/accounts/dave/enabled",
                                    json={"enabled": False})
    assert response.status_code == 401


def test_get_accounts_includes_enabled_default_true():
    with temp_env():
        _seed_slot("eve")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            response = client.get("/api/accounts", headers=IDENTITY_HEADERS)

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["enabled"] is True


def test_get_accounts_never_ranks_disabled_account():
    """A disabled account must not carry a display rank (issue #736).

    Selection can never pick a disabled account, so ranking it — especially as
    "#1 next" — mislabels where the next session will land and shifts every
    other visible rank by one. The best-looking account is disabled here: if
    the display ranking included it, it would take rank 1.
    """
    def usage_fn(name):
        return make_usage(session=10.0 if name == "dead" else 50.0,
                          weekly=10.0 if name == "dead" else 50.0)

    with temp_env():
        _seed_slot("dead")
        _seed_slot("live")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(usage_fn=usage_fn)
        with client:
            client.patch("/api/accounts/dead/enabled",
                         json={"enabled": False}, headers=IDENTITY_HEADERS)
            response = client.get("/api/accounts", headers=IDENTITY_HEADERS)

    assert response.status_code == 200
    by_name = {a["name"]: a for a in response.json()}
    assert by_name["dead"]["enabled"] is False
    assert by_name["dead"]["rank"] is None
    assert by_name["dead"]["rankReason"] is None
    assert by_name["live"]["rank"] == 1
