from server.sessions.completion import COMPLETION_MARKER


def _captured_options(monkeypatch):
    captured = {}

    class _FakeClient:
        def __init__(self, options=None):
            captured["options"] = options

    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _FakeClient)

    from server.sessions.sdk_factory import real_sdk_factory
    real_sdk_factory("/tmp/cwd", "/tmp/cfg", lambda *a, **k: None, "interactive", None)
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
