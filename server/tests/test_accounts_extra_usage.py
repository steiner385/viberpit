"""Server tests for the per-account extra-usage-credits opt-in.

Covers the PATCH toggle, GET exposure (including the upstream ``extraUsageEnabled``
flag the UI warns on), and display ranking parity with real selection.
"""
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
    """Per-test DB (#728) — see test_accounts_weekly_reserve for the rationale."""


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


def test_patch_toggles_allow_extra_usage():
    with temp_env():
        _seed_slot("alice")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            on = client.patch("/api/accounts/alice", json={"allowExtraUsage": True},
                              headers=IDENTITY_HEADERS)
            off = client.patch("/api/accounts/alice", json={"allowExtraUsage": False},
                               headers=IDENTITY_HEADERS)
    assert on.status_code == 200
    assert on.json()["allowExtraUsage"] is True
    assert off.status_code == 200
    assert off.json()["allowExtraUsage"] is False


def test_get_accounts_defaults_allow_extra_usage_false():
    """An account nobody has touched must never be opted into spending."""
    with temp_env():
        _seed_slot("bob")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(usage_fn=lambda name: make_usage(session=10.0))
        with client:
            response = client.get("/api/accounts", headers=IDENTITY_HEADERS)
    assert response.status_code == 200
    [acct] = response.json()
    assert acct["allowExtraUsage"] is False


def test_patch_leaves_other_fields_untouched():
    """Fields absent from the body must not be clobbered (partial-update contract)."""
    with temp_env():
        _seed_slot("carol")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            client.patch("/api/accounts/carol",
                         json={"nickname": "Carol", "weeklyReservePct": 80},
                         headers=IDENTITY_HEADERS)
            after = client.patch("/api/accounts/carol", json={"allowExtraUsage": True},
                                 headers=IDENTITY_HEADERS)
            # …and toggling something else must not clear the opt-in.
            final = client.patch("/api/accounts/carol", json={"nickname": "Carol II"},
                                 headers=IDENTITY_HEADERS)
    assert after.json()["nickname"] == "Carol"
    assert after.json()["weeklyReservePct"] == 80.0
    assert after.json()["allowExtraUsage"] is True
    assert final.json()["allowExtraUsage"] is True


def test_get_accounts_exposes_upstream_extra_usage_enabled():
    """The UI warns when the opt-in is on but the provider has extra usage off, so
    the upstream flag must survive the wire format."""
    def usage_fn(name):
        return make_usage(session=10.0, extra=(name == "paid"))

    with temp_env():
        _seed_slot("paid")
        _seed_slot("free")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(usage_fn=usage_fn)
        with client:
            response = client.get("/api/accounts", headers=IDENTITY_HEADERS)
    assert response.status_code == 200
    by_name = {a["name"]: a for a in response.json()}
    assert by_name["paid"]["usage"]["extraUsageEnabled"] is True
    assert by_name["free"]["usage"]["extraUsageEnabled"] is False


def test_get_accounts_ranks_extra_usage_account_last():
    """Display ranking must match real selection: an opted-in at-ceiling account
    appears as the final tier rather than vanishing from the ranking."""
    def usage_fn(name):
        return make_usage(session=100.0 if name == "maxed" else 10.0, extra=True)

    with temp_env():
        _seed_slot("maxed")
        _seed_slot("cool")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(usage_fn=usage_fn)
        with client:
            client.patch("/api/accounts/maxed", json={"allowExtraUsage": True},
                         headers=IDENTITY_HEADERS)
            response = client.get("/api/accounts", headers=IDENTITY_HEADERS)
    assert response.status_code == 200
    by_name = {a["name"]: a for a in response.json()}
    assert by_name["cool"]["rank"] == 1
    assert by_name["maxed"]["rank"] == 2
    assert by_name["maxed"]["rankReason"] == "at ceiling — burning extra usage credits"


def test_get_accounts_at_ceiling_unranked_without_opt_in():
    """Without the opt-in the at-ceiling account stays out of the ranking entirely —
    proves the rank above is attributable to the toggle, not to usage shape."""
    def usage_fn(name):
        return make_usage(session=100.0 if name == "maxed" else 10.0, extra=True)

    with temp_env():
        _seed_slot("maxed")
        _seed_slot("cool")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(usage_fn=usage_fn)
        with client:
            response = client.get("/api/accounts", headers=IDENTITY_HEADERS)
    by_name = {a["name"]: a for a in response.json()}
    assert by_name["cool"]["rank"] == 1
    assert by_name["maxed"]["rank"] is None


def test_patch_unknown_account_is_404():
    with temp_env():
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client()
        with client:
            response = client.patch("/api/accounts/ghost", json={"allowExtraUsage": True},
                                    headers=IDENTITY_HEADERS)
    assert response.status_code == 404
