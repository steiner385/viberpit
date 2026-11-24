"""Tests for server.sessions.account_auth.oauth_env_for — the single shared builder of the
model-auth env (CLAUDE_CODE_OAUTH_TOKEN) used by both session launch and helper SDK factories.
"""
import json
import time
from unittest import mock

import pytest

from server.sessions import account_auth


def test_oauth_env_for_returns_token_when_setup_token_exists(monkeypatch):
    monkeypatch.setattr(account_auth.crypto, "read_oauth_token", lambda account: "sk-ant-oat01-secret")
    assert account_auth.oauth_env_for("acct1") == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-secret"}


def test_oauth_env_for_returns_empty_dict_when_no_setup_token(monkeypatch):
    monkeypatch.setattr(account_auth.crypto, "read_oauth_token", lambda account: None)
    assert account_auth.oauth_env_for("acct1") == {}


def test_oauth_env_for_never_logs_token_value(monkeypatch, caplog):
    monkeypatch.setattr(account_auth.crypto, "read_oauth_token", lambda account: "sk-ant-oat01-super-secret")
    with caplog.at_level("DEBUG"):
        account_auth.oauth_env_for("acct1")
    assert "sk-ant-oat01-super-secret" not in caplog.text


def test_runner_env_comes_from_envelope_and_fails_closed(monkeypatch, tmp_path):
    from viberpit import credential_envelope as ce
    monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "worker")
    monkeypatch.setenv("VIBERPIT_CLAUDE_CREDS_DIR", str(tmp_path))
    ce.reset_runner_store()
    monkeypatch.setattr(account_auth.crypto, "read_oauth_token", lambda a: "MOUNT-TOKEN")
    try:
        # Pre-broker: no envelope yet → the existing path.
        assert account_auth.oauth_env_for("a") == {"CLAUDE_CODE_OAUTH_TOKEN": "MOUNT-TOKEN"}
        ce.runner_store().sync([ce.Envelope("a", ce.SETUP_TOKEN, "ENV-TOKEN", None, 1)], 3600)
        assert account_auth.oauth_env_for("a") == {"CLAUDE_CODE_OAUTH_TOKEN": "ENV-TOKEN"}
        ce.runner_store().sync([ce.Envelope("a", ce.SETUP_TOKEN, "ENV-TOKEN", None, 1)], -1)
        assert account_auth.oauth_env_for("a") == {}  # lease lapsed; never the mount token
    finally:
        ce.reset_runner_store()


# ---------------------------------------------------------------------------
# Remote Control launch credential (plan 2026-11-24 Task B): the slot's full-scope ACCESS
# token plus the org UUID — the verified minimum recipe (spike E3o). Primary authority
# only; never a runner, a worker or a Copilot account; fail closed to the setup-token.
# ---------------------------------------------------------------------------

RC_SCOPES = ["user:inference", "user:profile", "user:sessions:claude_code"]
ACCESS = "sk-ant-oat01-SLOT-ACCESS-secret"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A throwaway account store holding one Claude account, ``acct``, whose browser-login
    slot is healthy and whose captured ``.claude.json`` names its org."""
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)
    from viberpit import paths
    paths.store_dir().mkdir(parents=True, exist_ok=True)
    paths.store_claude_json_path("acct").write_text(json.dumps(
        {"oauthAccount": {"organizationUuid": "org-123", "emailAddress": "x@example.com"}}))
    slot = {"accessToken": ACCESS, "refreshToken": "REFRESH-secret",
            "expiresAt": int(time.time() * 1000) + 3_600_000, "scopes": RC_SCOPES}
    paths.slot_path("acct").write_text(json.dumps(slot))
    acc = account_auth._accounts
    monkeypatch.setattr(acc, "provider_or_none", lambda n: "anthropic")
    monkeypatch.setattr(acc, "slot_status", lambda n: "ok")
    monkeypatch.setattr(acc, "ensure_valid_token", lambda n: dict(slot))
    return slot


def test_organization_uuid_comes_from_the_captured_profile(store):
    assert account_auth.organization_uuid("acct") == "org-123"
    assert account_auth.organization_uuid("nobody") is None


def test_remote_control_env_is_the_slot_access_token_and_the_org(store):
    env = account_auth.remote_control_env_for("acct")
    assert env == {"CLAUDE_CODE_OAUTH_TOKEN": ACCESS, "CLAUDE_CODE_ORGANIZATION_UUID": "org-123"}
    assert "REFRESH-secret" not in json.dumps(env)    # Principle II: never the refresh token
    assert account_auth.remote_control_slot_ready("acct") is True


def test_remote_control_env_refreshes_through_the_authority(store, monkeypatch):
    calls = []
    monkeypatch.setattr(account_auth._accounts, "ensure_valid_token",
                        lambda n: calls.append(n) or dict(store))
    account_auth.remote_control_env_for("acct")
    assert calls == ["acct"]


@pytest.mark.parametrize("patch_attr,value", [
    ("slot_status", lambda n: "login_needed"),
    ("slot_status", lambda n: "missing"),
    ("provider_or_none", lambda n: "copilot"),
    ("runner_uses_envelope", lambda n: True),
    ("is_refresh_authority", lambda: False),
])
def test_remote_control_env_fails_closed(store, monkeypatch, patch_attr, value):
    monkeypatch.setattr(account_auth._accounts, patch_attr, value)
    assert account_auth.remote_control_env_for("acct") is None
    assert account_auth.remote_control_slot_ready("acct") is False


def test_remote_control_env_needs_the_org(store):
    from viberpit import paths
    paths.store_claude_json_path("acct").write_text("{}")
    assert account_auth.remote_control_env_for("acct") is None


def test_remote_control_env_needs_the_sessions_scope(store, monkeypatch):
    monkeypatch.setattr(account_auth._accounts, "ensure_valid_token",
                        lambda n: {**store, "scopes": ["user:inference"]})
    assert account_auth.remote_control_env_for("acct") is None


def test_a_failed_refresh_falls_back_without_logging_a_token(store, monkeypatch, caplog):
    from viberpit import oauth

    def _dead(n):
        raise oauth.AuthError(f"rejected {ACCESS}")
    monkeypatch.setattr(account_auth._accounts, "ensure_valid_token", _dead)
    with caplog.at_level("DEBUG"):
        assert account_auth.remote_control_env_for("acct") is None
    assert ACCESS not in caplog.text and "REFRESH-secret" not in caplog.text


def test_without_refresh_only_an_already_fresh_token_is_used(store, monkeypatch):
    refresh = mock.Mock()
    monkeypatch.setattr(account_auth._accounts, "ensure_valid_token", refresh)
    assert account_auth.remote_control_env_for("acct", allow_refresh=False) is not None
    from viberpit import paths
    paths.slot_path("acct").write_text(
        json.dumps({**store, "expiresAt": int(time.time() * 1000) + 10}))
    assert account_auth.remote_control_env_for("acct", allow_refresh=False) is None
    refresh.assert_not_called()


def test_slot_access_token_reads_without_refreshing(store, monkeypatch):
    refresh = mock.Mock()
    monkeypatch.setattr(account_auth._accounts, "ensure_valid_token", refresh)
    assert account_auth.slot_access_token("acct") == ACCESS
    assert account_auth.slot_access_token("nobody") is None
    refresh.assert_not_called()


def test_token_fingerprint_is_stable_and_not_the_token():
    fp = account_auth.token_fingerprint(ACCESS)
    assert fp == account_auth.token_fingerprint(ACCESS) != account_auth.token_fingerprint("other")
    assert ACCESS not in fp and len(fp) == 16


@pytest.mark.parametrize("token,kind", [("SETUP", "setup"), (None, "cfg")])
def test_oauth_launch_env_names_its_kind(monkeypatch, token, kind):
    monkeypatch.setattr(account_auth.crypto, "read_oauth_token", lambda a: token)
    env, got = account_auth.oauth_launch_env("acct1")
    assert got == kind and env == ({"CLAUDE_CODE_OAUTH_TOKEN": token} if token else {})
