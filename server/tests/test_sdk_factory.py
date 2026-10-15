import pytest
from server.sessions.completion import COMPLETION_MARKER
from server.sessions.claude_sdk_factory import real_sdk_factory


async def _captured_options(monkeypatch, mode="interactive"):
    captured = {}

    class _FakeClient:
        def __init__(self, options=None):
            captured["options"] = options

    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _FakeClient)

    from server.sessions.claude_sdk_factory import real_sdk_factory as _rsf
    await _rsf("/tmp/cwd", "/tmp/cfg", lambda *a, **k: None, mode, None)
    return captured["options"]


@pytest.mark.asyncio
async def test_factory_appends_completion_instruction(monkeypatch):
    """With the session-tools surface on by default, Claude's `durable_tool_surface`
    means the append teaches `mark_complete` INSTEAD of the marker — never both
    (task-8, marker-to-tool migration)."""
    opts = await _captured_options(monkeypatch)
    sp = opts.system_prompt
    assert isinstance(sp, dict)
    assert sp["type"] == "preset" and sp["preset"] == "claude_code"
    assert "mark_complete" in sp["append"]
    assert COMPLETION_MARKER not in sp["append"]


@pytest.mark.asyncio
async def test_factory_still_sets_core_options(monkeypatch):
    opts = await _captured_options(monkeypatch)
    assert opts.cwd == "/tmp/cwd"
    assert opts.permission_mode == "default"


@pytest.mark.asyncio
async def test_factory_maps_permission_mode_via_helper(monkeypatch):
    """The spawn factory routes mode → SDK permission_mode through the single
    to_sdk_permission_mode helper (issue #750): SDK-vocabulary names pass through,
    everything else collapses to "default"."""
    assert (await _captured_options(monkeypatch, "plan")).permission_mode == "plan"
    assert (await _captured_options(monkeypatch, "acceptEdits")).permission_mode == "acceptEdits"
    assert (await _captured_options(monkeypatch, "bypassPermissions")).permission_mode == "bypassPermissions"
    assert (await _captured_options(monkeypatch, "full-auto")).permission_mode == "default"
    assert (await _captured_options(monkeypatch, "auto")).permission_mode == "default"


async def _cb(*a, **k):
    return {}


@pytest.mark.asyncio
async def test_factory_registers_precompact_hook_when_callback_given():
    client = await real_sdk_factory("/tmp", "/tmp/cfg", None, "default", None, on_pre_compact=_cb)
    hooks = client.options.hooks
    assert hooks is not None and "PreCompact" in hooks
    matcher = hooks["PreCompact"][0]
    assert _cb in matcher.hooks


@pytest.mark.asyncio
async def test_factory_without_precompact_callback_still_guards_human_hold():
    """No PreCompact callback → no PreCompact hook, but the `hold:human` guard is
    registered on every spawn (#3088): it is a PreToolUse hook so no permission
    mode or allow rule can skip it."""
    from server.sessions.human_hold_guard import human_hold_pre_tool_use
    client = await real_sdk_factory("/tmp", "/tmp/cfg", None, "bypassPermissions", None)
    hooks = client.options.hooks
    assert "PreCompact" not in hooks
    (matcher,) = hooks["PreToolUse"]
    assert matcher.matcher == "Bash" and human_hold_pre_tool_use in matcher.hooks
    # …and so is the draft-PR guard; with no ownership check, no draft is the session's.
    draft_guard = matcher.hooks[1]
    assert draft_guard.__name__ == "draft_ready_pre_tool_use"
    out = await draft_guard({"tool_input": {"command": "gh pr ready 1 --undo"}})
    assert out == {}


@pytest.mark.asyncio
async def test_factory_hands_the_ownership_check_to_the_draft_guard(monkeypatch):
    """The session's ``owns_pr`` is the draft guard's ownership check."""
    from server.sessions import draft_hold_guard
    asked = []

    async def owns_pr(repo, number, head_ref):
        asked.append((repo, number, head_ref))
        return True
    monkeypatch.setattr(draft_hold_guard, "resolve_pr", lambda call, cwd:
                        draft_hold_guard.TargetPr("o/r", 5, "feat/x", True))
    client = await real_sdk_factory("/tmp", "/tmp/cfg", None, "default", None,
                                    owns_pr=owns_pr)
    (matcher,) = client.options.hooks["PreToolUse"]
    assert await matcher.hooks[1]({"tool_input": {"command": "gh pr ready 5"}}) == {}
    assert asked == [("o/r", 5, "feat/x")]


@pytest.mark.asyncio
async def test_factory_pins_cli_path_to_system_binary(monkeypatch):
    """Sessions must run the auto-updating system `claude`, never the SDK's stale
    bundled CLI (claude_agent_sdk/_bundled/claude, frozen behind the API's minimum
    version). The factory sets cli_path to the resolved system binary."""
    import server.sessions.claude_bin as cb
    monkeypatch.setattr(cb, "resolve_claude_bin", lambda: "/usr/local/bin/claude")
    opts = await _captured_options(monkeypatch)
    assert str(opts.cli_path) == "/usr/local/bin/claude"


@pytest.mark.asyncio
async def test_factory_leaves_cli_path_unset_when_no_system_binary(monkeypatch):
    """When no system claude can be resolved, cli_path stays None so the SDK falls
    back to its own discovery rather than crashing on a bogus path."""
    import server.sessions.claude_bin as cb
    monkeypatch.setattr(cb, "resolve_claude_bin", lambda: None)
    opts = await _captured_options(monkeypatch)
    assert opts.cli_path is None


@pytest.mark.asyncio
async def test_factory_auto_trusts_project_mcp_servers(monkeypatch):
    """The session must never hit the binary's project-`.mcp.json` trust gate.

    The gate (`LX1()` in the CLI) leaves a project server `"pending"` — i.e. prompts —
    unless `enableAllProjectMcpServers` is set in the flag-settings layer.  The SDK only
    loads filesystem settings when `setting_sources` is passed (we don't), so we inject the
    flag inline via the `--settings` channel (`ClaudeAgentOptions.settings`)."""
    import json
    opts = await _captured_options(monkeypatch)
    assert opts.settings is not None, "factory must pass an inline --settings payload"
    parsed = json.loads(opts.settings)
    assert parsed.get("enableAllProjectMcpServers") is True


@pytest.mark.asyncio
async def test_factory_raises_the_sdk_line_buffer_above_the_1mib_default(monkeypatch):
    """The CLI echoes the whole ``originalFile`` in an Edit result on stdout. The SDK's
    default ``max_buffer_size`` is 1 MiB per JSON line, and one over it kills the
    reader for good while the process lives on (2026-09-16: a 1,023,418-byte
    orchestrator.py edit → "JSON message exceeded maximum buffer size" → two hours of
    a session the daemon could write to but never hear). The cap must comfortably
    clear any source file a session will edit."""
    from server.sessions.claude_sdk_factory import SDK_MAX_BUFFER_BYTES
    opts = await _captured_options(monkeypatch)
    assert opts.max_buffer_size == SDK_MAX_BUFFER_BYTES
    assert opts.max_buffer_size >= 16 * 1024 * 1024
