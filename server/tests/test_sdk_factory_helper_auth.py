"""Helper SDK factories (suggestions/summaries) must inject CLAUDE_CODE_OAUTH_TOKEN for an
account whose browser-OAuth refresh is dead but which holds a live setup-token — the same auth
resolution session launch already gets via server.sessions.account_auth.oauth_env_for.

The factories build their env through ``account_auth.helper_env`` (#1005 moved them onto that
shared builder, which also swaps in the host-local scratch CLAUDE_CONFIG_DIR), so the seam these
tests patch is ``account_auth.oauth_env_for`` — the actual source of truth — rather than a
re-export on the factory module.
"""
import json

import server.sessions.account_auth as aa
import server.sessions.claude_sdk_factory as sf
from ccswitch.configdir import CLAUDE_JSON_NAME


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
        aa, "oauth_env_for",
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
    monkeypatch.setattr(aa, "oauth_env_for", lambda account: {})
    sf.real_suggestion_factory("/tmp/r", "/tmp/cfg-acct", "primary")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in captured["options"].env


def test_real_summary_factory_injects_oauth_token_for_account(monkeypatch):
    captured = _patch_sdk_client(monkeypatch)
    monkeypatch.setattr(
        aa, "oauth_env_for",
        lambda account: {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-TOK"} if account == "primary" else {},
    )
    sf.real_summary_factory("/tmp/r", "/tmp/cfg-acct", "primary")
    assert captured["options"].env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-TOK"


def test_real_summary_factory_no_setup_token_omits_env(monkeypatch):
    # See the matching comment on test_real_suggestion_factory_no_setup_token_omits_env.
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    captured = _patch_sdk_client(monkeypatch)
    monkeypatch.setattr(aa, "oauth_env_for", lambda account: {})
    sf.real_summary_factory("/tmp/r", "/tmp/cfg-acct", "primary")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in captured["options"].env


def test_real_suggestion_factory_never_logs_token(monkeypatch, caplog):
    captured = _patch_sdk_client(monkeypatch)
    monkeypatch.setattr(
        aa, "oauth_env_for", lambda account: {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-super-secret"}
    )
    with caplog.at_level("DEBUG"):
        sf.real_suggestion_factory("/tmp/r", "/tmp/cfg-acct", "primary")
    assert "sk-ant-oat01-super-secret" not in caplog.text
    assert captured["options"].env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-super-secret"


def test_helper_factories_spawn_against_a_host_local_config_dir(monkeypatch, tmp_path):
    """#1005: a suggestion/summary spawn must not point at the shared account dir.

    These are the ``latest_suggestions_many`` / ``latest_summary_many`` spawns that fired
    dozens of times an hour on rocky — each a fresh cold read of dobby's ``.claude.json``
    through a stale attribute cache, and each able to "repair" it back to defaults.
    """
    monkeypatch.setenv("CCSWITCH_HELPER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    monkeypatch.setattr(aa, "oauth_env_for", lambda account: {})
    cfg = tmp_path / "cfg" / "primary"
    cfg.mkdir(parents=True)
    (cfg / CLAUDE_JSON_NAME).write_text(json.dumps({"userID": "u"}))

    for factory in (sf.real_suggestion_factory, sf.real_summary_factory):
        captured = _patch_sdk_client(monkeypatch)
        factory("/tmp/r", str(cfg), "primary")
        spawned = captured["options"].env["CLAUDE_CONFIG_DIR"]
        assert spawned != str(cfg)
        assert str(tmp_path / "scratch") in spawned
