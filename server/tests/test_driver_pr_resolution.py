"""Driver captures pr_resolution from TurnEnd and exposes it via last_pr_resolution."""
from __future__ import annotations

import pytest
from claude_agent_sdk import TextBlock

from server.sessions.completion import NO_PR_MARKER
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f


class _NoPRTurnClient(FakeSDKClient):
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


class _PlainTurnClient(FakeSDKClient):
    """A FakeSDKClient whose single turn has NO marker."""
    async def receive_response(self):
        yield f.stream_start(uuid="t1")
        yield f.stream_text("all done, please open a PR", uuid="t1")
        yield f.assistant(TextBlock(text="all done, please open a PR"), uuid="t1")
        yield f.result(session_id="sess-1", uuid="t1")


def _make_driver(client_cls=_NoPRTurnClient):
    def factory(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
                on_pre_compact=None):
        return client_cls([], on_can_use_tool=can_use_tool)
    return SessionDriver(
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
