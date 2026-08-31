"""Server tests for the per-account weekly reserve (PATCH + GET exposure + ranking)."""
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
    kwargs = dict(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
    if usage_fn is not None:
        kwargs["usage_fn"] = usage_fn
    orch = SessionOrchestrator(**kwargs)
    app = create_app(orchestrator=orch)
    return TestClient(app, raise_server_exceptions=False)


def test_patch_sets_reserve_and_returns_it():
    with temp_env():
        _seed_slot("alice")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            response = client.patch(
                "/api/accounts/alice",
                json={"weeklyReservePct": 90},
                headers=IDENTITY_HEADERS,
            )
    assert response.status_code == 200
    assert response.json()["weeklyReservePct"] == 90.0


def test_patch_null_clears_reserve():
    with temp_env():
        _seed_slot("bob")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            client.patch("/api/accounts/bob", json={"weeklyReservePct": 85},
                         headers=IDENTITY_HEADERS)
            response = client.patch("/api/accounts/bob", json={"weeklyReservePct": None},
                                    headers=IDENTITY_HEADERS)
    assert response.status_code == 200
    assert response.json()["weeklyReservePct"] is None


def test_patch_out_of_range_reserve_is_422():
    with temp_env():
        _seed_slot("carol")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            low = client.patch("/api/accounts/carol", json={"weeklyReservePct": 0.5},
                               headers=IDENTITY_HEADERS)
            high = client.patch("/api/accounts/carol", json={"weeklyReservePct": 101},
                                headers=IDENTITY_HEADERS)
    assert low.status_code == 422
    assert high.status_code == 422


def test_nickname_patch_preserves_reserve_and_vice_versa():
    with temp_env():
        _seed_slot("dave")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            client.patch("/api/accounts/dave", json={"weeklyReservePct": 75},
                         headers=IDENTITY_HEADERS)
            after_nick = client.patch("/api/accounts/dave", json={"nickname": "Work"},
                                      headers=IDENTITY_HEADERS)
            after_reserve = client.patch("/api/accounts/dave", json={"weeklyReservePct": 80},
                                         headers=IDENTITY_HEADERS)
    assert after_nick.json()["weeklyReservePct"] == 75.0
    assert after_nick.json()["nickname"] == "Work"
    assert after_reserve.json()["nickname"] == "Work"
    assert after_reserve.json()["weeklyReservePct"] == 80.0


def test_get_accounts_includes_reserve_field():
    with temp_env():
        _seed_slot("eve")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(usage_fn=lambda name: make_usage(session=10.0))
        with client:
            client.patch("/api/accounts/eve", json={"weeklyReservePct": 90},
                         headers=IDENTITY_HEADERS)
            response = client.get("/api/accounts", headers=IDENTITY_HEADERS)
    assert response.status_code == 200
    [acct] = response.json()
    assert acct["weeklyReservePct"] == 90.0


def test_get_accounts_ranks_over_reserve_account_as_fallback():
    """Display ranking must match real selection: over-reserve → last + fallback reason."""
    def usage_fn(name):
        return make_usage(session=10.0, weekly=95.0 if name == "hot" else 20.0)

    with temp_env():
        _seed_slot("hot")
        _seed_slot("cool")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(usage_fn=usage_fn)
        with client:
            client.patch("/api/accounts/hot", json={"weeklyReservePct": 90},
                         headers=IDENTITY_HEADERS)
            response = client.get("/api/accounts", headers=IDENTITY_HEADERS)
    assert response.status_code == 200
    by_name = {a["name"]: a for a in response.json()}
    assert by_name["cool"]["rank"] == 1
    assert by_name["hot"]["rank"] == 2
    assert by_name["hot"]["rankReason"] == "over weekly reserve — fallback"
