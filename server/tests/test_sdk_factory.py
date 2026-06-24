from server.sessions.completion import COMPLETION_MARKER
from server.sessions.sdk_factory import real_sdk_factory


def _captured_options(monkeypatch):
    captured = {}

    class _FakeClient:
        def __init__(self, options=None):
            captured["options"] = options

    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _FakeClient)

    from server.sessions.sdk_factory import real_sdk_factory as _rsf
    _rsf("/tmp/cwd", "/tmp/cfg", lambda *a, **k: None, "interactive", None)
    return captured["options"]


def test_factory_appends_completion_instruction(monkeypatch):
    opts = _captured_options(monkeypatch)
    sp = opts.system_prompt
    assert isinstance(sp, dict)
    assert sp["type"] == "preset" and sp["preset"] == "claude_code"
    assert COMPLETION_MARKER in sp["append"]


def test_factory_still_sets_core_options(monkeypatch):
    opts = _captured_options(monkeypatch)
    assert opts.cwd == "/tmp/cwd"
    assert opts.permission_mode == "default"


async def _cb(*a, **k):
    return {}


def test_factory_registers_precompact_hook_when_callback_given():
    client = real_sdk_factory("/tmp", "/tmp/cfg", None, "default", None, on_pre_compact=_cb)
    hooks = client.options.hooks
    assert hooks is not None and "PreCompact" in hooks
    matcher = hooks["PreCompact"][0]
    assert _cb in matcher.hooks


def test_factory_no_hooks_without_callback():
    client = real_sdk_factory("/tmp", "/tmp/cfg", None, "default", None)
    assert client.options.hooks is None


def test_factory_auto_trusts_project_mcp_servers(monkeypatch):
    """The session must never hit the binary's project-`.mcp.json` trust gate.

    The gate (`LX1()` in the CLI) leaves a project server `"pending"` — i.e. prompts —
    unless `enableAllProjectMcpServers` is set in the flag-settings layer.  The SDK only
    loads filesystem settings when `setting_sources` is passed (we don't), so we inject the
    flag inline via the `--settings` channel (`ClaudeAgentOptions.settings`)."""
    import json
    opts = _captured_options(monkeypatch)
    assert opts.settings is not None, "factory must pass an inline --settings payload"
    parsed = json.loads(opts.settings)
    assert parsed.get("enableAllProjectMcpServers") is True
