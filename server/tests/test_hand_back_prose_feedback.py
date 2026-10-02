"""The driver's `signal_message_prose` probe behind the hand_back acknowledgement.

The SDK queues a message's stream events and its consolidated AssistantMessage
BEFORE it spawns the in-process MCP handler for a tool call in that message
(`claude_agent_sdk/_internal/query.py:_read_messages`), but the driver's consumer
may not have processed them yet when the handler runs. The probe therefore waits
for the Translator to have READ the call — the signal is recorded from the
tool-use block — and only then reports the prose of the message that carried it.
"""
from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk import TextBlock, ThinkingBlock, ToolUseBlock

from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk as f

HAND_BACK = "mcp__viberpit__hand_back"


def _driver() -> ClaudeAgentDriver:
    return ClaudeAgentDriver("s1", "acct", "/tmp/r", "/tmp/cfg", mode="interactive",
                             sdk_factory=lambda *a, **k: None, registry=PermissionRegistry(),
                             identity="me")


@pytest.mark.asyncio
async def test_the_probe_reports_the_prose_of_the_message_that_carried_the_call():
    d = _driver()
    d._tr.feed(f.assistant(ThinkingBlock(thinking="weigh", signature="s"),
                           TextBlock(text="Options:\n1. A\n2. B"),
                           ToolUseBlock(id="toolu_hb", name=HAND_BACK, input={"reason": "pick"}),
                           uuid="m1"))
    assert await d.signal_message_prose("hand_back", timeout=0.2) == "Options:\n1. A\n2. B"


@pytest.mark.asyncio
async def test_the_probe_reports_empty_prose_when_the_message_had_only_thinking():
    """The 6ab5c602 shape: two thinking blocks and the call, no text."""
    d = _driver()
    d._tr.feed(f.assistant(ThinkingBlock(thinking="", signature="s"),
                           ThinkingBlock(thinking="the five options...", signature="t"),
                           ToolUseBlock(id="toolu_hb", name=HAND_BACK, input={"reason": "pick"}),
                           uuid="m1"))
    assert await d.signal_message_prose("hand_back", timeout=0.2) == ""


@pytest.mark.asyncio
async def test_the_probe_waits_for_the_translator_to_catch_up():
    d = _driver()

    async def late_feed():
        await asyncio.sleep(0.05)
        d._tr.feed(f.assistant(TextBlock(text="late prose"),
                               ToolUseBlock(id="toolu_hb", name=HAND_BACK, input={"reason": "r"}),
                               uuid="m1"))

    task = asyncio.create_task(late_feed())
    assert await d.signal_message_prose("hand_back", timeout=1.0) == "late prose"
    await task


@pytest.mark.asyncio
async def test_the_probe_answers_none_when_the_call_never_shows_up():
    """An earlier message's prose must not be mistaken for the call's: with no
    recorded signal the answer is "unknown", never the stale `_turn_text`."""
    d = _driver()
    d._tr.feed(f.assistant(TextBlock(text="I'll check what landed first."), uuid="m0"))
    assert await d.signal_message_prose("hand_back", timeout=0.05) is None
