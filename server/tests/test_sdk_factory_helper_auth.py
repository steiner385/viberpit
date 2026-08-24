"""Helper SDK factories (suggestions/summaries) must inject CLAUDE_CODE_OAUTH_TOKEN for an
account whose browser-OAuth refresh is dead but which holds a live setup-token — the same auth
resolution session launch already gets via server.sessions.account_auth.oauth_env_for.
"""
import server.sessions.sdk_factory as sf


def _patch_sdk_client(monkeypatch):
    captured = {}

    class _FakeClient:
        def __init__(self, options=None):
            captured["options"] = options

    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _FakeClient)
    return captured


def test_real_suggestion_factory_injects_oauth_token_for_account(monkeypatch):
    captured = _patch_sdk_client(monkeypatch)
    monkeypatch.setattr(
        sf, "oauth_env_for",
        lambda account: {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-TOK"} if account == "primary" else {},
    )
    sf.real_suggestion_factory("/tmp/r", "/tmp/cfg-acct", "primary")
    assert captured["options"].env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-TOK"


def test_real_suggestion_factory_no_setup_token_omits_env(monkeypatch):
    # The factory starts from dict(os.environ, ...), so this assertion only means what it
    # claims if the *test process's own* environment doesn't already export the var (plausible
    # on a dev box for this project).
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    captured = _patch_sdk_client(monkeypatch)
    monkeypatch.setattr(sf, "oauth_env_for", lambda account: {})
    sf.real_suggestion_factory("/tmp/r", "/tmp/cfg-acct", "primary")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in captured["options"].env


def test_real_summary_factory_injects_oauth_token_for_account(monkeypatch):
    captured = _patch_sdk_client(monkeypatch)
    monkeypatch.setattr(
        sf, "oauth_env_for",
        lambda account: {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-TOK"} if account == "primary" else {},
    )
    sf.real_summary_factory("/tmp/r", "/tmp/cfg-acct", "primary")
    assert captured["options"].env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-TOK"


def test_real_summary_factory_no_setup_token_omits_env(monkeypatch):
    # See the matching comment on test_real_suggestion_factory_no_setup_token_omits_env.
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    captured = _patch_sdk_client(monkeypatch)
    monkeypatch.setattr(sf, "oauth_env_for", lambda account: {})
    sf.real_summary_factory("/tmp/r", "/tmp/cfg-acct", "primary")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in captured["options"].env


def test_real_suggestion_factory_never_logs_token(monkeypatch, caplog):
    captured = _patch_sdk_client(monkeypatch)
    monkeypatch.setattr(
        sf, "oauth_env_for", lambda account: {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-super-secret"}
    )
    with caplog.at_level("DEBUG"):
        sf.real_suggestion_factory("/tmp/r", "/tmp/cfg-acct", "primary")
    assert "sk-ant-oat01-super-secret" not in caplog.text
    assert captured["options"].env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-super-secret"
