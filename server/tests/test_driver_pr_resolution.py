"""Driver captures pr_resolution from TurnEnd and exposes it via last_pr_resolution."""
from __future__ import annotations

import pytest
from claude_agent_sdk import TextBlock

from server.sessions.completion import NO_PR_MARKER
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import PerQueryScript

MARK_NO_PR = "mcp__viberpit__mark_no_pr"


class _NoPRTurnClient(PerQueryScript, FakeSDKClient):
    """A FakeSDKClient whose single turn includes the no-pr marker in assistant text.

    The Translator builds _turn_text from AssistantMessage TextBlock content (not
    StreamEvent deltas), so we must emit a proper AssistantMessage whose TextBlock
    text contains the marker alongside the streaming events.
    """
    async def receive_response(self):
        marker_text = f"done {NO_PR_MARKER}"
        yield f.stream_start(uuid="t1")
        yield f.stream_text(marker_text, uuid="t1")
        yield f.assistant(TextBlock(text=marker_text), uuid="t1")
        yield f.result(session_id="sess-1", uuid="t1")


class _PlainTurnClient(PerQueryScript, FakeSDKClient):
    """A FakeSDKClient whose single turn has NO marker."""
    async def receive_response(self):
        yield f.stream_start(uuid="t1")
        yield f.stream_text("all done, please open a PR", uuid="t1")
        yield f.assistant(TextBlock(text="all done, please open a PR"), uuid="t1")
        yield f.result(session_id="sess-1", uuid="t1")


class _NoPRToolTurnClient(PerQueryScript, FakeSDKClient):
    """A FakeSDKClient whose single turn calls `mark_no_pr` instead of emitting text.

    Mirrors _NoPRTurnClient on the other transport: the Translator turns the tool-use
    block into TurnEnd(pr_resolution="not-needed", pr_resolution_source="tool").
    """
    async def receive_response(self):
        yield f.stream_start(uuid="t1")
        yield f.stream_text("nothing to ship here", uuid="t1")
        yield f.tool_use("tc1", MARK_NO_PR, {"resolution": "not-needed"}, uuid="t1")
        yield f.result(session_id="sess-1", uuid="t1")


class _NoPRToolThenPlainClient(PerQueryScript, FakeSDKClient):
    """Turn 1 calls `mark_no_pr`; turn 2 carries no resolution at all."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._turns = 0

    async def receive_response(self):
        self._turns += 1
        if self._turns == 1:
            yield f.stream_start(uuid="t1")
            yield f.tool_use("tc1", MARK_NO_PR, {"resolution": "not-needed"}, uuid="t1")
            yield f.result(session_id="sess-1", uuid="t1")
        else:
            yield f.stream_start(uuid="t2")
            yield f.stream_text("one more thought", uuid="t2")
            yield f.assistant(TextBlock(text="one more thought"), uuid="t2")
            yield f.result(session_id="sess-1", uuid="t2")


def _make_driver(client_cls=_NoPRTurnClient):
    def factory(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
                on_pre_compact=None):
        return client_cls([], on_can_use_tool=can_use_tool)
    return ClaudeAgentDriver(
        "sid", "alpha", "/tmp", "/tmp/cfg", mode="default",
        sdk_factory=factory, registry=PermissionRegistry(),
        identity="wstest@example.com",
    )


@pytest.mark.asyncio
async def test_driver_captures_last_pr_resolution():
    """A turn whose text contains the no-pr marker sets last_pr_resolution."""
    d = _make_driver(_NoPRTurnClient)
    await d.start()
    await d.send_prompt("go")
    if d._pump:
        await d._pump
    await d.aclose()

    assert d.last_pr_resolution == "not-needed"


@pytest.mark.asyncio
async def test_driver_last_pr_resolution_none_without_marker():
    """A turn without the no-pr marker leaves last_pr_resolution as None."""
    d = _make_driver(_PlainTurnClient)
    await d.start()
    await d.send_prompt("go")
    if d._pump:
        await d._pump
    await d.aclose()

    assert d.last_pr_resolution is None


async def _run_one_turn(client_cls):
    d = _make_driver(client_cls)
    await d.start()
    await d.send_prompt("go")
    if d._pump:
        await d._pump
    await d.aclose()
    return d


@pytest.mark.asyncio
async def test_the_driver_reports_the_source_alongside_the_resolution():
    d = await _run_one_turn(_NoPRToolTurnClient)
    assert d.last_pr_resolution == "not-needed"
    assert d.last_pr_resolution_source == "tool"


@pytest.mark.asyncio
async def test_a_marker_turn_still_reports_a_marker_source():
    d = await _run_one_turn(_NoPRTurnClient)
    assert d.last_pr_resolution_source == "marker"


@pytest.mark.asyncio
async def test_no_resolution_means_no_source():
    d = await _run_one_turn(_PlainTurnClient)
    assert d.last_pr_resolution_source is None


@pytest.mark.asyncio
async def test_a_later_resolution_free_turn_does_not_wipe_the_source():
    """The source latch must mirror last_pr_resolution's stickiness exactly. An
    unconditional assignment would reset "tool" to None on the next turn, and the
    orchestrator's `or "marker"` default would then file a tool call as a marker."""
    d = _make_driver(_NoPRToolThenPlainClient)
    await d.start()
    await d.send_prompt("go")
    if d._pump:
        await d._pump
    assert d.last_pr_resolution_source == "tool"
    await d.send_prompt("and again")
    if d._pump:
        await d._pump
    await d.aclose()

    assert d.last_pr_resolution == "not-needed"
    assert d.last_pr_resolution_source == "tool"


def test_a_driver_stub_without_the_new_attribute_still_persists_as_marker():
    """`getattr(..., None) or "marker"` in the orchestrator — a partial test double
    must keep behaving exactly as it did before this change."""
    class Stub:
        last_pr_resolution = "not-needed"
    assert (getattr(Stub(), "last_pr_resolution_source", None) or "marker") == "marker"
