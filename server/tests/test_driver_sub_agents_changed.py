"""A Task sub-agent starting or ending fires the driver's on_sub_agents_changed callback
so the orchestrator can refresh the persisted per-session summary mid-turn (the board
card otherwise only catches up on the next status change)."""
from __future__ import annotations

import asyncio

from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient

TID = "toolu_01YNkJcCo55UaJospWhvnJMx"
TASK = "abb42ada82e1382fe"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _driver(clients, calls):
    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
             on_pre_compact=None, options_overlay=None):
        c = FakeSDKClient([], on_can_use_tool=can_use_tool, startup_frames=[])
        clients.append(c)
        return c
    return ClaudeAgentDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                             sdk_factory=make, registry=PermissionRegistry(), identity="me",
                             on_sub_agents_changed=lambda sid: calls.append(sid))


async def _settle(pred, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


def test_start_and_end_each_fire_the_callback_with_the_session_id():
    clients: list[FakeSDKClient] = []
    calls: list[str] = []
    d = _driver(clients, calls)

    async def run():
        await d.start()
        clients[0].inject([f.task_started(TASK, TID)])
        await _settle(lambda: len(calls) == 1)
        clients[0].inject([f.task_notification(TASK, TID, status="completed")])
        await _settle(lambda: len(calls) == 2)
        assert calls == ["s1", "s1"]
        await d.aclose()
    _run(run())


def test_a_failing_callback_never_breaks_the_pump():
    clients: list[FakeSDKClient] = []
    calls: list[str] = []

    def boom(sid):
        calls.append(sid)
        raise RuntimeError("refresh exploded")

    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
             on_pre_compact=None, options_overlay=None):
        c = FakeSDKClient([], on_can_use_tool=can_use_tool, startup_frames=[])
        clients.append(c)
        return c
    d = ClaudeAgentDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=make, registry=PermissionRegistry(), identity="me",
                          on_sub_agents_changed=boom)

    async def run():
        await d.start()
        clients[0].inject([f.task_started(TASK, TID)])
        await _settle(lambda: len(calls) == 1)
        assert d.subagent_task_id(TID) == TASK      # the map update still happened
        await d.aclose()
    _run(run())


def test_no_callback_is_fine():
    clients: list[FakeSDKClient] = []

    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
             on_pre_compact=None, options_overlay=None):
        c = FakeSDKClient([], on_can_use_tool=can_use_tool, startup_frames=[])
        clients.append(c)
        return c
    d = ClaudeAgentDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=make, registry=PermissionRegistry(), identity="me")

    async def run():
        await d.start()
        clients[0].inject([f.task_started(TASK, TID)])
        await _settle(lambda: d.subagent_task_id(TID) == TASK)
        await d.aclose()
    _run(run())
