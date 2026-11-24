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


def test_browser_login_starts_the_code_paste_claude_login_for_an_existing_account():
    fake_rm = FakeReauthManager()
    with temp_env():
        _write_slot("primary")
        with _client(fake_rm) as client:
            resp = client.post("/api/accounts/primary/browser-login", headers=IDENTITY_HEADERS)
    assert resp.status_code == 202
    assert fake_rm.last_start_mode == "claude_login"    # code paste, no login browser


def test_the_pasted_code_goes_to_the_shared_reauth_code_route():
    fake_rm = FakeReauthManager()
    with temp_env():
        _write_slot("primary")
        with _client(fake_rm) as client:
            client.post("/api/accounts/primary/browser-login", headers=IDENTITY_HEADERS)
            resp = client.post("/api/accounts/primary/reauth/code",
                               json={"code": "AUTHCODE#STATE"}, headers=IDENTITY_HEADERS)
    assert resp.status_code == 200
    assert fake_rm.last_submit_code == ("primary", "AUTHCODE#STATE")


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


def test_browser_login_admits_the_trusted_local_browser(monkeypatch):
    """With the operator's VIBERPIT_RULES_TRUST_LOCAL_HOST opt-in, the browser on the
    ViberPit host itself may start the sign-in: it can't complete without a person
    approving at claude.ai, and a mismatched account is refused by the identity check."""
    from server.auth import identity
    # The route imports human_gate_via at call time, so patch it at its source.
    monkeypatch.setattr(identity, "human_gate_via", lambda _req: identity.LOCAL_TRUSTED_VIA)
    fake_rm = FakeReauthManager()
    with temp_env():
        _write_slot("primary")
        with _client(fake_rm) as client:
            resp = client.post("/api/accounts/primary/browser-login", headers=IDENTITY_HEADERS)
    assert resp.status_code == 202
    assert fake_rm.last_start_mode is not None


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


# ── ReauthManager mode="claude_login": the code paste in the operator's own browser ──


def _manager(runner=None):
    from server.reauth import ReauthManager

    async def _no_runner(name: str) -> bool:
        raise AssertionError("claude_login must never start the login-browser runner")

    return ReauthManager(runner=runner or _no_runner, key_available=lambda: True, timeout_s=5)


@pytest.mark.asyncio
async def test_claude_login_publishes_a_full_scope_authorize_url_and_waits_for_the_code():
    from urllib.parse import parse_qs, urlparse
    from viberpit import setup_token_oauth as sto

    m = _manager()
    state = await m.start("primary", "tester@example.com", mode="claude_login")
    assert state.state == "awaiting_code"
    assert state.vnc_session is None                     # no virtual display, no VNC
    q = parse_qs(urlparse(state.auth_url).query)
    assert q["scope"] == [sto.FULL_LOGIN_SCOPE]
    assert "user:sessions:claude_code" in q["scope"][0]   # what Remote Control needs


@pytest.mark.asyncio
async def test_a_pasted_claude_login_code_is_exchanged_and_installed(monkeypatch):
    from viberpit import auth_login, setup_token_oauth as sto

    seen: dict = {}
    monkeypatch.setattr(sto, "exchange_login",
                        lambda code, ch: seen.update(code=code, scope=ch.scope) or {"t": 1})
    monkeypatch.setattr(sto, "exchange",
                        lambda *a: (_ for _ in ()).throw(AssertionError("setup-token path")))
    monkeypatch.setattr(auth_login, "complete_code_login",
                        lambda name, resp: seen.update(name=name, resp=resp))
    m = _manager()
    await m.start("primary", "tester@example.com", mode="claude_login")
    state = await m.submit_code("primary", "AUTHCODE#STATE")
    assert state.state == "success" and state.message is None
    assert seen == {"code": "AUTHCODE#STATE", "scope": sto.FULL_LOGIN_SCOPE,
                    "name": "primary", "resp": {"t": 1}}


@pytest.mark.asyncio
async def test_a_setup_token_code_never_takes_the_claude_login_path(monkeypatch):
    from viberpit import accounts, crypto, setup_token_oauth as sto

    monkeypatch.setattr(sto, "exchange_login",
                        lambda *a: (_ for _ in ()).throw(AssertionError("claude_login path")))
    monkeypatch.setattr(sto, "exchange", lambda code, ch: "sk-ant-oat01-X")
    monkeypatch.setattr(crypto, "store_oauth_token", lambda name, tok: None)
    monkeypatch.setattr(accounts, "mark_reauthenticated", lambda name: None)
    monkeypatch.setattr("server.reauth.ReauthManager._usage_scope_note",
                        staticmethod(lambda *a: None))
    m = _manager()
    await m.start("primary", "tester@example.com", mode="claude_login")
    await m.start("primary", "tester@example.com", mode="setup_token")   # replaces it
    assert (await m.submit_code("primary", "AUTHCODE#STATE")).state == "success"


@pytest.mark.asyncio
async def test_a_wrong_account_sign_in_ends_in_a_coded_error(monkeypatch, caplog):
    from viberpit import auth_login, setup_token_oauth as sto

    def _wrong(name, resp):
        raise auth_login.WrongAccountLogin("a@example.com", "b@example.com")

    monkeypatch.setattr(sto, "exchange_login", lambda code, ch: {"t": 1})
    monkeypatch.setattr(auth_login, "complete_code_login", _wrong)
    m = _manager()
    await m.start("primary", "tester@example.com", mode="claude_login")
    with caplog.at_level("DEBUG"):
        state = (await m.submit_code("primary", "AUTHCODE#STATE")).to_dict()
    assert state["state"] == "error"
    assert state["code"] == "browser_login_wrong_account"
    assert state["message"] == ("You signed in as b@example.com, expected a@example.com. "
                                "Nothing was saved.")
    # The UI gets both addresses; the log gets only the static reason (no PII).
    assert "a@example.com" not in caplog.text and "b@example.com" not in caplog.text
    assert "browser_login_wrong_account" in caplog.text


@pytest.mark.parametrize("exc_name, message", [
    ("SetupTokenNoRefreshToken", "claude.ai did not return a refresh token. Nothing was saved."),
    ("SetupTokenInvalidCode", "Code invalid or expired — start the sign-in again."),
    ("SetupTokenStateMismatch", "Code didn't match this request — start the sign-in again."),
    ("SetupTokenTransient", "Couldn't reach claude.ai — try again."),
])
@pytest.mark.asyncio
async def test_exchange_failures_save_nothing_and_say_why(monkeypatch, caplog, exc_name, message):
    from viberpit import auth_login, setup_token_oauth as sto

    def _raise(code, ch):
        raise getattr(sto, exc_name)("boom")

    installed: list = []
    monkeypatch.setattr(sto, "exchange_login", _raise)
    monkeypatch.setattr(auth_login, "complete_code_login", lambda *a: installed.append(a))
    m = _manager()
    await m.start("primary", "tester@example.com", mode="claude_login")
    state = await m.submit_code("primary", "SECRETCODE#STATE")
    assert state.state == "error" and state.message == message and state.code is None
    assert installed == []
    assert "SECRETCODE" not in caplog.text
