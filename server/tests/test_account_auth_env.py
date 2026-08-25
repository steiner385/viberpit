"""Tests for server.sessions.account_auth.oauth_env_for — the single shared builder of the
model-auth env (CLAUDE_CODE_OAUTH_TOKEN) used by both session launch and helper SDK factories.
"""
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
