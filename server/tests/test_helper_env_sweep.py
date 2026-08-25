"""Tests for server.sessions.account_auth.helper_env — the single shared env builder used by
every LLM helper call site that has only a CLAUDE_CONFIG_DIR (not an account name).

helper_env() reverse-maps config_dir -> account via adopt._map_account (the same reverse
mapping server/sessions/adopt.py already uses for process adoption), then layers in that
account's setup-token env via oauth_env_for — so a helper call against an account whose
browser-OAuth refresh is dead but whose setup-token still works authenticates the same way
session launch does (issue #751).
"""
from server.sessions import account_auth


def test_helper_env_carries_setup_token_for_mapped_account(monkeypatch, tmp_path):
    cfg_dir = tmp_path / "acct1"
    cfg_dir.mkdir()

    monkeypatch.setattr(account_auth._accounts, "list_accounts", lambda: [{"name": "acct1"}])
    monkeypatch.setattr(account_auth._paths, "account_cfg_dir", lambda name: cfg_dir)
    monkeypatch.setattr(account_auth.crypto, "read_oauth_token", lambda account: "sk-ant-oat01-secret")

    env = account_auth.helper_env(str(cfg_dir))

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-secret"


def test_helper_env_no_token_key_when_account_has_none(monkeypatch, tmp_path):
    cfg_dir = tmp_path / "acct1"
    cfg_dir.mkdir()

    monkeypatch.setattr(account_auth._accounts, "list_accounts", lambda: [{"name": "acct1"}])
    monkeypatch.setattr(account_auth._paths, "account_cfg_dir", lambda name: cfg_dir)
    monkeypatch.setattr(account_auth.crypto, "read_oauth_token", lambda account: None)

    env = account_auth.helper_env(str(cfg_dir))

    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_helper_env_unmappable_config_dir_yields_base_env(monkeypatch, tmp_path):
    other_dir = tmp_path / "someone-elses-account"
    other_dir.mkdir()
    cfg_dir = tmp_path / "unmapped"
    cfg_dir.mkdir()

    monkeypatch.setattr(account_auth._accounts, "list_accounts", lambda: [{"name": "acct1"}])
    monkeypatch.setattr(account_auth._paths, "account_cfg_dir", lambda name: other_dir)
    monkeypatch.setattr(
        account_auth.crypto,
        "read_oauth_token",
        lambda account: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    env = account_auth.helper_env(str(cfg_dir))

    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["CLAUDE_CONFIG_DIR"] == str(cfg_dir)


def test_helper_env_lookup_failure_fails_open(monkeypatch, tmp_path):
    cfg_dir = tmp_path / "acct1"
    cfg_dir.mkdir()

    def _boom():
        raise RuntimeError("ledger corrupt")

    monkeypatch.setattr(account_auth._accounts, "list_accounts", _boom)

    env = account_auth.helper_env(str(cfg_dir))

    assert env["CLAUDE_CONFIG_DIR"] == str(cfg_dir)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_helper_env_always_sets_config_dir(monkeypatch, tmp_path):
    cfg_dir = tmp_path / "whatever"
    cfg_dir.mkdir()
    monkeypatch.setattr(account_auth._accounts, "list_accounts", lambda: [])

    env = account_auth.helper_env(str(cfg_dir))

    assert env["CLAUDE_CONFIG_DIR"] == str(cfg_dir)


def test_helper_env_never_logs_token_value(monkeypatch, tmp_path, caplog):
    cfg_dir = tmp_path / "acct1"
    cfg_dir.mkdir()

    monkeypatch.setattr(account_auth._accounts, "list_accounts", lambda: [{"name": "acct1"}])
    monkeypatch.setattr(account_auth._paths, "account_cfg_dir", lambda name: cfg_dir)
    monkeypatch.setattr(account_auth.crypto, "read_oauth_token", lambda account: "sk-ant-oat01-super-secret")

    with caplog.at_level("DEBUG"):
        env = account_auth.helper_env(str(cfg_dir))

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-super-secret"
    assert "sk-ant-oat01-super-secret" not in caplog.text
