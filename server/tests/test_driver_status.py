from __future__ import annotations
import asyncio

import pytest
from claude_agent_sdk import TextBlock

from server.sessions.completion import COMPLETION_MARKER as _MARK
from server.sessions.driver import SessionDriver
from server.sessions import events as ev
from server.sessions.events import TurnEnd
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f


class _OneTurnClient(FakeSDKClient):
    """A FakeSDKClient that always yields one clean turn (no messages list needed)."""
    async def receive_response(self):
        yield f.stream_start(uuid="t1")
        yield f.stream_text("hi", uuid="t1")
        yield f.result(session_id="sess-1", uuid="t1")


def _make_driver():
    def factory(cwd, config_dir, can_use_tool, mode, resume):
        return _OneTurnClient([], on_can_use_tool=can_use_tool)
    return SessionDriver(
        "sid", "alpha", "/tmp", "/tmp/cfg", mode="default",
        sdk_factory=factory, registry=PermissionRegistry(),
        identity="wstest@example.com",
    )


@pytest.mark.asyncio
async def test_live_status_uses_rich_vocabulary():
    d = _make_driver()
    await d.start()
    statuses: list[str] = []

    async def collect():
        async for e in d.events():
            if isinstance(e, ev.Status):
                statuses.append(e.state)
            if isinstance(e, ev.TurnEnd):
                break

    await d.send_prompt("go")
    await asyncio.wait_for(collect(), timeout=5.0)
    await d.aclose()

    # No legacy 'working'/'idle' wire values; the active turn reports in_progress.
    assert "working" not in statuses and "idle" not in statuses
    assert "in_progress" in statuses


@pytest.mark.asyncio
async def test_current_status_reflects_idle_before_first_turn():
    d = _make_driver()
    await d.start()
    assert d.current_status() == "idle"
    await d.aclose()


class _RaisingClient(FakeSDKClient):
    async def receive_response(self):
        yield f.sys_init("s1")
        raise RuntimeError("stream died")


def _factory(msgs):
    def make(cwd, config_dir, can_use_tool, mode, resume):
        return FakeSDKClient(msgs, on_can_use_tool=can_use_tool)
    return make


def test_status_progresses_in_progress_then_done():
    msgs = [f.sys_init("sess-1"), f.stream_start(uuid="t1"),
            f.stream_text("hi", uuid="t1"),
            f.assistant(TextBlock(text="hi"), uuid="t1"),
            f.result(session_id="sess-1", uuid="t1")]
    captured: list[str] = []

    async def on_status(sid, status):
        captured.append(status)

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(),
                          identity="me", on_status=on_status)
        await d.start()
        await d.send_prompt("hello")
        async for e in d.events():
            if isinstance(e, TurnEnd):
                break
        # Allow the drain's finally block (which emits the terminal status) to run.
        if d._pump:
            await d._pump
        await d.aclose()
        return captured

    seen = asyncio.run(run())
    assert "in_progress" in seen
    assert seen[-1] == "done"


def _run_capture(msgs):
    captured: list[str] = []

    async def on_status(sid, status):
        captured.append(status)

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(),
                          identity="me", on_status=on_status)
        await d.start()
        await d.send_prompt("hello")
        async for e in d.events():
            if isinstance(e, TurnEnd):
                break
        if d._pump:
            await d._pump
        await d.aclose()
        return captured

    return asyncio.run(run())


def test_status_complete_when_marker_present():
    msgs = [f.sys_init("s1"), f.stream_start(uuid="t1"),
            f.assistant(TextBlock(text=f"done {_MARK}"), uuid="t1"),
            f.result(session_id="s1", uuid="t1")]
    assert _run_capture(msgs)[-1] == "complete"


def test_status_needs_attention_on_error_result():
    msgs = [f.sys_init("s1"), f.stream_start(uuid="t1"),
            f.assistant(TextBlock(text="boom"), uuid="t1"),
            f.result(session_id="s1", uuid="t1", subtype="error_during_execution", is_error=True)]
    assert _run_capture(msgs)[-1] == "needs_attention"


def test_status_needs_attention_when_stream_raises():
    def factory(cwd, config_dir, can_use_tool, mode, resume):
        return _RaisingClient([f.sys_init("s1")], on_can_use_tool=can_use_tool)
    captured: list[str] = []

    async def on_status(sid, status):
        captured.append(status)

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(),
                          identity="me", on_status=on_status)
        await d.start()
        await d.send_prompt("hello")
        if d._pump:
            await d._pump
        await d.aclose()
        return captured

    assert asyncio.run(run())[-1] == "needs_attention"
