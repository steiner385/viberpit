"""Follow-up suggestion chips for the assistant kind (spec v2 §10.3, #1248).

The assistant used to be excluded from suggestions outright (``_suggester_for``
returned None). It now gets its own suggester: same one-shot helper path, but a
frozen assistant-specific prompt asking for up to 3 follow-up QUESTIONS about the
fleet rather than imperative coding prompts. Titles/summaries stay excluded.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from server.assistant.suggest_prompt import (
    ASSISTANT_SUGGEST_LIMIT, ASSISTANT_SUGGEST_SYSTEM_PROMPT, build_assistant_suggest_prompt,
)
from server.sessions.suggestions import SUGGEST_SYSTEM_PROMPT, SuggestionService, build_prompt
from server.sessions.types import ASSISTANT_KIND
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient


def _recording_factory(reply: str):
    clients: list[FakeSDKClient] = []

    def make(cwd, config_dir, account=None):
        c = FakeSDKClient([f.stream_start(uuid="h"), f.stream_text(reply, uuid="h"),
                           f.result(uuid="h")])
        clients.append(c)
        return c
    return make, clients


def test_assistant_prompt_is_frozen_and_distinct_from_the_coding_prompt():
    assert "follow-up question" in ASSISTANT_SUGGEST_SYSTEM_PROMPT
    assert ASSISTANT_SUGGEST_SYSTEM_PROMPT != SUGGEST_SYSTEM_PROMPT
    assert ASSISTANT_SUGGEST_LIMIT == 3
    prompt = build_assistant_suggest_prompt("User: q\n\nAssistant: a")
    assert "User: q\n\nAssistant: a" in prompt
    assert "coding agent" not in prompt


def test_service_uses_the_injected_prompt_builder_and_limit():
    make, clients = _recording_factory('["A?", "B?", "C?", "D?", "E?"]')
    svc = SuggestionService(make, build=build_assistant_suggest_prompt,
                            limit=ASSISTANT_SUGGEST_LIMIT)
    items = asyncio.run(svc.generate("/tmp", "/tmp/cfg", "User: hi\n\nAssistant: yo"))
    assert items == ["A?", "B?", "C?"]
    assert clients[0].sent == [build_assistant_suggest_prompt("User: hi\n\nAssistant: yo")]


def test_default_service_keeps_the_coding_prompt():
    make, clients = _recording_factory('["Run the tests"]')
    items = asyncio.run(SuggestionService(make).generate("/tmp", "/tmp/cfg", "ctx"))
    assert items == ["Run the tests"]
    assert clients[0].sent == [build_prompt("ctx")]


def test_real_factory_honours_a_system_prompt_override(monkeypatch):
    """The assistant suggester reaches the SDK with the assistant system prompt."""
    from server.sessions import claude_sdk_factory as fac

    captured: list[dict] = []
    fake_sdk_mod = types.SimpleNamespace(
        ClaudeAgentOptions=lambda **kw: captured.append(kw) or kw,
        ClaudeSDKClient=lambda options: options,
    )
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk_mod)
    monkeypatch.setattr(fac, "helper_env", lambda config_dir, account: {})
    monkeypatch.setattr(fac, "_cli_path_kwargs", lambda: {})

    asyncio.run(fac.real_suggestion_factory("/tmp", "/tmp/cfg"))
    asyncio.run(fac.real_suggestion_factory(
        "/tmp", "/tmp/cfg", system_prompt=ASSISTANT_SUGGEST_SYSTEM_PROMPT))
    assert captured[0]["system_prompt"] == SUGGEST_SYSTEM_PROMPT
    assert captured[1]["system_prompt"] == ASSISTANT_SUGGEST_SYSTEM_PROMPT


def test_orchestrator_gives_the_assistant_its_own_suggester(tmp_path, monkeypatch):
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    make, clients = _recording_factory('["Which is oldest?"]')
    monkeypatch.setenv("VIBERPIT_SUGGESTIONS", "1")
    orch._init_suggesters(make)

    coding = orch._suggester_for(None)
    assistant = orch._suggester_for(ASSISTANT_KIND)
    assert coding is orch._suggester
    assert assistant is not None and assistant is not coding

    items = asyncio.run(assistant.generate(str(tmp_path), str(tmp_path), "User: q\n\nAssistant: a"))
    assert items == ["Which is oldest?"]
    assert clients[0].sent == [build_assistant_suggest_prompt("User: q\n\nAssistant: a")]


@pytest.mark.parametrize("kind", [None, ASSISTANT_KIND])
def test_suggestions_kill_switch_disables_both(monkeypatch, kind):
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    monkeypatch.setenv("VIBERPIT_SUGGESTIONS", "0")
    orch._init_suggesters(None)
    assert orch._suggester_for(kind) is None
