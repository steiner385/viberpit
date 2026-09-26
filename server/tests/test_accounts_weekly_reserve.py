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

# One spelling of the DB-URL override for the whole file (brand ratchet
# env-prefix, #1591): every test below patches the same in-memory DB via this
# constant instead of repeating the literal.
_MEMORY_DB_ENV = {"VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:"}


@pytest.fixture(autouse=True)
def _fresh_db(isolated_db):
    """Per-test DB (#728): several tests here exit their VIBERPIT_DB_URL patch
    before `with client:`, so the app lifespan resolves the AMBIENT env var at
    init_db time — without this fixture that was the worker-shared session DB,
    coupling these files to whatever ran (or leaked a writer) before them."""



def _seed_slot(name: str) -> None:
    from viberpit import paths
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
    # Always inject a fake usage_fn: the poller's startup sweep otherwise runs the
    # real usage_for() on a detached thread that outlives the TestClient and writes
    # into the next test's temp_env mid-rmtree — Errno 39 (#1647).
    orch = SessionOrchestrator(
        sdk_factory=fake_sdk_factory(),
        usage_fn=usage_fn or (lambda name: make_usage(session=10.0)),
        poll_interval=9999.0,
    )
    app = create_app(orchestrator=orch)
    return TestClient(app, raise_server_exceptions=False)


def test_patch_sets_reserve_and_returns_it():
    with temp_env():
        _seed_slot("alice")
        with patch.dict(os.environ, _MEMORY_DB_ENV):
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
        with patch.dict(os.environ, _MEMORY_DB_ENV):
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
        with patch.dict(os.environ, _MEMORY_DB_ENV):
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
        with patch.dict(os.environ, _MEMORY_DB_ENV):
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
        with patch.dict(os.environ, _MEMORY_DB_ENV):
            client = _make_client(usage_fn=lambda name: make_usage(session=10.0))
        with client:
            client.patch("/api/accounts/eve", json={"weeklyReservePct": 90},
                         headers=IDENTITY_HEADERS)
            response = client.get("/api/accounts", headers=IDENTITY_HEADERS)
    assert response.status_code == 200
    [acct] = response.json()
    assert acct["weeklyReservePct"] == 90.0


def test_get_accounts_ranks_over_reserve_account_as_fallback():
    """Display ranking must match real selection: over-reserve → last + fallback reason.

    Reserve 20 = keep 20% back (ceiling 80): hot at 95% is over it, cool at 20% is not.
    """
    def usage_fn(name):
        return make_usage(session=10.0, weekly=95.0 if name == "hot" else 20.0)

    with temp_env():
        _seed_slot("hot")
        _seed_slot("cool")
        with patch.dict(os.environ, _MEMORY_DB_ENV):
            client = _make_client(usage_fn=usage_fn)
        with client:
            client.patch("/api/accounts/hot", json={"weeklyReservePct": 20},
                         headers=IDENTITY_HEADERS)
            response = client.get("/api/accounts", headers=IDENTITY_HEADERS)
    assert response.status_code == 200
    by_name = {a["name"]: a for a in response.json()}
    assert by_name["cool"]["rank"] == 1
    assert by_name["hot"]["rank"] == 2
    assert by_name["hot"]["rankReason"] == "over weekly reserve — fallback"


def test_get_accounts_carries_the_effective_and_default_reserve():
    """Spec 2026-09-20 D4/§7: effectiveReservePct = the account's override, else the
    push level's RESERVE_PCT (steady 20 on a fresh governor row); pushLevelReservePct
    is that default, so the Accounts page can label a blank override."""
    with temp_env():
        _seed_slot("plain")
        _seed_slot("kept")
        with patch.dict(os.environ, _MEMORY_DB_ENV):
            client = _make_client()
        with client:
            client.patch("/api/accounts/kept", json={"weeklyReservePct": 60},
                         headers=IDENTITY_HEADERS)
            response = client.get("/api/accounts", headers=IDENTITY_HEADERS)
    assert response.status_code == 200
    by_name = {a["name"]: a for a in response.json()}
    assert by_name["plain"]["weeklyReservePct"] is None
    assert by_name["plain"]["effectiveReservePct"] == 20.0
    assert by_name["plain"]["pushLevelReservePct"] == 20.0
    assert by_name["kept"]["effectiveReservePct"] == 60.0
    assert by_name["kept"]["pushLevelReservePct"] == 20.0


def test_get_accounts_reserve_default_follows_the_governor_push_level():
    """The other test in this file (`..._carries_the_effective_and_default_reserve`)
    passes even if the governor row is never read at all: a fresh governor row's
    push level is "steady", which is also `_push_level()`'s degrade fallback. Seed a
    REAL governor row via the automations API so a regression back to a hardcoded
    "steady" default would actually fail this test."""
    with temp_env():
        _seed_slot("blank")
        _seed_slot("overridden")
        with patch.dict(os.environ, _MEMORY_DB_ENV):
            client = _make_client()
        with client:
            gov = client.patch("/api/automations/capacity-governor",
                               json={"config": {"push_level": "max"}},
                               headers=IDENTITY_HEADERS)
            assert gov.status_code == 200
            client.patch("/api/accounts/overridden", json={"weeklyReservePct": 60},
                         headers=IDENTITY_HEADERS)
            response = client.get("/api/accounts", headers=IDENTITY_HEADERS)
    assert response.status_code == 200
    by_name = {a["name"]: a for a in response.json()}
    assert by_name["blank"]["effectiveReservePct"] == 5.0
    assert by_name["blank"]["pushLevelReservePct"] == 5.0
    assert by_name["overridden"]["effectiveReservePct"] == 60.0
    assert by_name["overridden"]["pushLevelReservePct"] == 5.0


async def _boom(*a, **k):
    raise RuntimeError("boom")


def test_get_accounts_reserve_default_degrades_to_steady_when_the_governor_read_fails(monkeypatch, caplog):
    """A config-read exception must degrade the reserve default to steady (20%),
    never fail the whole accounts list, and must log `accounts.push_level_read_failed`
    so a wedged governor read is visible without breaking GET /api/accounts."""
    import server.api.accounts as accounts_api
    monkeypatch.setattr(accounts_api, "read_automation_config", _boom)
    with temp_env():
        _seed_slot("plain")
        with patch.dict(os.environ, _MEMORY_DB_ENV):
            client = _make_client()
        with client:
            with caplog.at_level("WARNING"):
                response = client.get("/api/accounts", headers=IDENTITY_HEADERS)
    assert response.status_code == 200
    by_name = {a["name"]: a for a in response.json()}
    assert by_name["plain"]["pushLevelReservePct"] == 20.0
    assert "accounts.push_level_read_failed" in caplog.text
