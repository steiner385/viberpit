"""Async one-shot helpers: canonical binary probes stay off the event loop, and the
options they build are tool-free and transcript-free (#2706)."""
import importlib
import threading
from types import SimpleNamespace

import pytest

from server.sessions.repo_match import RepoVocabulary


class _StopAfterOptions(Exception):
    """Sentinel the fake options factory raises to end the helper right after it
    builds real options. Helpers differ in what they do with a failed model call
    — most swallow it into a neutral result, ``propose_groups`` re-raises it as
    ``GroupingModelError`` so the runner can name the cause — and this test is
    about the probe/options threading boundary, not that policy, so it accepts
    either as long as what comes back out is this sentinel."""


@pytest.mark.asyncio
@pytest.mark.parametrize("module_name,function_name,args,kwargs", [
    ("sessions.title_generator", "generate_title", (["Fix login"],),
     {"config_dir": "/test/config", "cwd": "/tmp"}),
    ("sessions.goal_inference", "infer_goal", ("Fix login", []),
     {"config_dir": "/test/config"}),
    ("sessions.prompt_refiner", "refine_prompt", ("Fix login",), {}),
    ("sessions.repo_match", "suggest_repo",
     ("Fix login", None, [RepoVocabulary("org/repo", [])]), {}),
    ("sessions.open_question", "completion_awaits_user",
     ("Would you like me to proceed?",), {"config_dir": "/test/config"}),
    ("automations.model_pick", "assess_model", ("Fix login",), {}),
    ("automations.backlog_grouping", "propose_groups", ([{"number": 1}], {}), {}),
    ("automations.pr_intent", "classify_pr_intent", ([], "Fix login"), {}),
    ("automations.autopilot_decider", "decide", (SimpleNamespace(session_id="s1"),), {}),
    ("automations.nudge_composer", "compose_nudge", (SimpleNamespace(session_id="s1"),),
     {"intent_hint": "", "verdict_reason": "", "verdict_remedy": "",
      "recent_nudges": [], "rich_tail": [], "facts_verified": False}),
])
async def test_helper_option_probe_thread(monkeypatch, module_name, function_name, args, kwargs):
    import claude_agent_sdk
    from server.sessions import claude_bin

    module = importlib.import_module(f"server.{module_name}")
    loop_thread = threading.get_ident()
    probe_threads = []
    options_seen = []
    original_options = claude_agent_sdk.ClaudeAgentOptions
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/test/config")
    monkeypatch.setattr(claude_bin, "_min_version", lambda: (2, 1, 0))
    monkeypatch.setattr(claude_bin.shutil, "which", lambda _: "/test/claude")
    monkeypatch.setattr(module, "helper_env", lambda _: {})
    if hasattr(module, "pick_helper_config_dir"):
        monkeypatch.setattr(module, "pick_helper_config_dir", lambda: "/test/config")
    if hasattr(module, "helper_cwd"):
        monkeypatch.setattr(module, "helper_cwd", lambda: "/tmp")
    if hasattr(module, "repo_check_enabled"):
        monkeypatch.setattr(module, "repo_check_enabled", lambda: True)

    def probe(path):
        probe_threads.append(threading.get_ident())
        return (2, 2, 0)

    def options(**kw):
        assert threading.get_ident() == loop_thread
        options_seen.append(original_options(**kw))
        # Stop immediately after real options construction: no model turn or
        # unrelated response-parser inputs are needed to exercise this boundary.
        raise _StopAfterOptions("test stops after options construction")

    monkeypatch.setattr(claude_bin, "_probe_version", probe)
    monkeypatch.setattr(claude_agent_sdk, "ClaudeAgentOptions", options)
    if function_name in {"assess_model", "propose_groups"}:
        # These helpers always build options, including their injected-query path;
        # the suite's outer no-model guards require injection to reach that code.
        kwargs = {**kwargs, "_query_fn": claude_agent_sdk.query}
    try:
        await getattr(module, function_name)(*args, **kwargs)
    except Exception as exc:  # a helper that re-raises a failed model call
        assert isinstance(exc, _StopAfterOptions) or isinstance(
            exc.__cause__, _StopAfterOptions), f"unexpected failure: {exc!r}"
    assert probe_threads and all(t != loop_thread for t in probe_threads)
    assert len(options_seen) == 1
    assert options_seen[0].cli_path == "/test/claude"
    _assert_stateless(options_seen[0])


def _assert_stateless(opts):
    """No inherited built-ins, MCP servers or settings, and no native transcript (#2706).

    ``allowed_tools=[]`` alone is NOT asserted: it is an auto-approve list, and the old
    helper shape that relied on it initialised the real CLI with 26 built-in tools."""
    assert opts.tools == []
    assert opts.strict_mcp_config is True
    assert opts.mcp_servers == {}
    assert opts.setting_sources == []
    assert opts.extra_args == {"no-session-persistence": None}
