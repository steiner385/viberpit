"""POST /api/accounts/{name}/browser-login and the accounts payload's ``remoteControlLogin``.

Remote Control needs the account's full claude.ai browser login in its store slot (plan
docs/superpowers/plans/2026-11-24-remote-control-full-login.md, Task A). ``/reauth`` only
ever mints a setup-token, and ``/accounts/add`` 409s on an existing slot, so an operator
had no way to re-run the browser login for an account whose slot grant had lapsed.
"""
from __future__ import annotations

import os
import time
from unittest.mock import patch

import pytest

from server.tests.test_reauth_api import (
    IDENTITY_HEADERS, FakeReauthManager, _make_client,
)
from tests.helpers import temp_env

FULL_SCOPES = ["user:inference", "user:profile", "user:sessions:claude_code"]


@pytest.fixture(autouse=True)
def _fresh_db(isolated_db):
    """Per-test DB, as in test_reauth_api.py."""


def _write_slot(name: str, **overrides) -> None:
    from viberpit import paths
    from viberpit.creds import write_json_secure
    now_ms = int(time.time() * 1000)
    blob = {
        "accessToken": f"{name}-tok", "refreshToken": f"{name}-ref",
        "expiresAt": now_ms + 9_000_000, "refreshTokenExpiresAt": now_ms + 86_400_000,
        "scopes": FULL_SCOPES, "subscriptionType": "max",
    }
    blob.update(overrides)
    write_json_secure(paths.slot_path(name), blob)


def _client(fake_rm: FakeReauthManager | None = None):
    with patch.dict(os.environ, {"VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:"}):
        return _make_client(fake_rm or FakeReauthManager())


def test_browser_login_starts_the_login_mode_for_an_existing_account():
    fake_rm = FakeReauthManager()
    with temp_env():
        _write_slot("primary")
        with _client(fake_rm) as client:
            resp = client.post("/api/accounts/primary/browser-login", headers=IDENTITY_HEADERS)
    assert resp.status_code == 202
    assert resp.json()["state"] == "running"
    assert fake_rm.last_start_mode == "login"


def test_browser_login_404_for_unknown_account():
    fake_rm = FakeReauthManager()
    with temp_env():
        with _client(fake_rm) as client:
            resp = client.post("/api/accounts/ghost/browser-login", headers=IDENTITY_HEADERS)
    assert resp.status_code == 404
    assert fake_rm.last_start_mode is None


def test_browser_login_404_for_dotted_name():
    with temp_env():
        with _client() as client:
            resp = client.post("/api/accounts/..secret/browser-login", headers=IDENTITY_HEADERS)
    assert resp.status_code == 404


def test_browser_login_409_when_another_login_is_running():
    fake_rm = FakeReauthManager()
    fake_rm.raise_busy = True
    fake_rm.holding_identity = "other@example.com"
    with temp_env():
        _write_slot("primary")
        with _client(fake_rm) as client:
            resp = client.post("/api/accounts/primary/browser-login", headers=IDENTITY_HEADERS)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "reauth_busy"


def test_browser_login_503_without_an_encryption_key():
    fake_rm = FakeReauthManager()
    fake_rm.raise_no_key = True
    with temp_env():
        _write_slot("primary")
        with _client(fake_rm) as client:
            resp = client.post("/api/accounts/primary/browser-login", headers=IDENTITY_HEADERS)
    assert resp.status_code == 503


def test_browser_login_refused_for_a_non_person(monkeypatch):
    """An unattested local caller (an agent on the host) cannot sign an account in."""
    from server.auth import identity
    monkeypatch.setattr(identity, "attested_front_end_hop", lambda _conn: False)
    fake_rm = FakeReauthManager()
    with temp_env():
        _write_slot("primary")
        with _client(fake_rm) as client:
            resp = client.post("/api/accounts/primary/browser-login", headers=IDENTITY_HEADERS)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "SESSION_CONTROL_HUMAN_ONLY"
    assert fake_rm.last_start_mode is None


def test_browser_login_refused_for_a_copilot_account():
    from viberpit import paths
    fake_rm = FakeReauthManager()
    with temp_env():
        paths.copilot_token_dir("gh").mkdir(parents=True)
        with patch("server.api.accounts._provider_or_none", return_value="copilot"):
            with _client(fake_rm) as client:
                resp = client.post("/api/accounts/gh/browser-login", headers=IDENTITY_HEADERS)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "browser_login_unsupported"
    assert fake_rm.last_start_mode is None


def test_browser_login_writes_an_audit_event_without_token_material():
    from unittest.mock import AsyncMock
    audit = AsyncMock()
    with temp_env():
        _write_slot("primary")
        with patch("server.api.accounts.record_audit", audit):
            with _client() as client:
                resp = client.post("/api/accounts/primary/browser-login",
                                   headers=IDENTITY_HEADERS)
    assert resp.status_code == 202
    audit.assert_awaited_once()
    kw = audit.await_args.kwargs
    assert kw["type"] == "account.browser_login.start"
    assert kw["detail"] == {"account": "primary"}


def test_accounts_list_reports_remote_control_login_per_slot():
    with temp_env():
        _write_slot("good")
        _write_slot("lapsed", refreshTokenExpiresAt=int(time.time() * 1000) - 1)
        _write_slot("narrow", scopes=["user:inference"])
        with _client() as client:
            resp = client.get("/api/accounts", headers=IDENTITY_HEADERS)
    assert resp.status_code == 200
    by_name = {a["name"]: a for a in resp.json()}
    assert by_name["good"]["remoteControlLogin"] == "ok"
    assert by_name["lapsed"]["remoteControlLogin"] == "login_needed"
    assert by_name["narrow"]["remoteControlLogin"] == "login_needed"
    body = resp.text
    for secret in ("good-tok", "good-ref", "lapsed-tok", "lapsed-ref"):
        assert secret not in body
