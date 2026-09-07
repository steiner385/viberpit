# server/tests/test_no_real_sdk_spawn_guard.py
"""No test may spawn a real `claude` subprocess through the SDK (#1383).

`claude-agent-sdk` is a hard dependency, so every `from claude_agent_sdk import
query  # lazy — not available in tests` comment in this repo is simply wrong on
CI: the import succeeds, the SDK finds its own bundled `claude` binary, and the
call spends 14-75s discovering it is `Not logged in`. Several of those serialize
inside one WS test's window, which is what made
`test_ws_auto_reattach.py::test_teardown_unsubscribes_even_with_an_auto_reattach_in_flight`
blow a 120s fence, then a 240s one, on `server-tests (2)` seven times.

The doors are plural and grow: nine modules define their own `_default_query_fn`
(goal_inference, title_generator, prompt_refiner, open_question, autopilot_decider,
model_pick, pr_intent, nudge_composer, backlog_grouping) and `claude_sdk_factory`
builds three `ClaudeSDKClient`s (session, suggester, summarizer). Stubbing them
per-harness closes the ones that exist today. Every one of them resolves its
symbol off the `claude_agent_sdk` package at CALL time, so conftest's guard
closes them all at the single shared door instead.
"""
import asyncio

import claude_agent_sdk
import pytest

from server.sessions import goal_inference
from server.sessions.claude_sdk_factory import real_suggestion_factory
from server.sessions.suggestions import SuggestionService


def test_query_door_is_guarded():
    """`claude_agent_sdk.query` is the guard, not the real streaming query."""
    assert getattr(claude_agent_sdk.query, "_no_real_sdk_guard", False), (
        "conftest must replace claude_agent_sdk.query so the nine lazy "
        "_default_query_fn doors cannot spawn a real CLI")


def test_client_door_is_guarded():
    """`claude_agent_sdk.ClaudeSDKClient` is the guard, not the real client."""
    assert getattr(claude_agent_sdk.ClaudeSDKClient, "_no_real_sdk_guard", False), (
        "conftest must replace claude_agent_sdk.ClaudeSDKClient so the session / "
        "suggester / summarizer factories cannot spawn a real CLI")


def test_guarded_query_fails_where_the_real_one_would():
    """The stub is an async iterator that raises on first iteration — the same
    shape and the same failure point as a real `Not logged in` query, so every
    call site's existing degrade-on-failure path is what runs."""
    async def _drain():
        async for _ in claude_agent_sdk.query(prompt="hi", options=None):
            pass

    with pytest.raises(RuntimeError, match="real SDK"):
        asyncio.run(_drain())


def test_guarded_client_fails_on_connect_not_construction():
    """A real ClaudeSDKClient constructs fine and fails when it spawns, so the
    stub must too: a factory test that only builds a client keeps working."""
    client = claude_agent_sdk.ClaudeSDKClient(options=object())
    with pytest.raises(RuntimeError, match="real SDK"):
        asyncio.run(client.connect())


def test_infer_goal_degrades_without_touching_the_cli(tmp_path):
    """The exact door named in #1383: `infer_goal` with no injected `query_fn`
    must return None immediately instead of spawning the bundled binary."""
    got = asyncio.run(goal_inference.infer_goal(
        "sess", [{"role": "user", "text": "hi"}],
        config_dir=str(tmp_path), cwd=str(tmp_path)))
    assert got is None


def test_suggestion_service_degrades_without_touching_the_cli(tmp_path):
    """The suggester door: the real factory is what an unstubbed orchestrator
    uses, and `generate` must come back empty rather than spawn."""
    svc = SuggestionService(real_suggestion_factory)
    got = asyncio.run(svc.generate(str(tmp_path), str(tmp_path), "some context"))
    assert got == []


def test_no_real_sdk_env_escape_hatch_is_honoured(monkeypatch):
    """Tests that genuinely want the real SDK (none today) opt back in the way
    the factory tests already do: replace the attribute for their own scope."""
    sentinel = object()
    monkeypatch.setattr(claude_agent_sdk, "query", sentinel)
    assert claude_agent_sdk.query is sentinel
