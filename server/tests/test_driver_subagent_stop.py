"""The driver keeps tool_use_id -> task_id for live Task sub-agents and stops one
through the SDK's stop_task control request (spec §4.3). Verified against claude
2.1.268: stop_task kills a FOREGROUND sub-agent at its next tool boundary."""
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


def _driver(clients: list[FakeSDKClient], session_id="s1"):
    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
             on_pre_compact=None, options_overlay=None):
        c = FakeSDKClient([], on_can_use_tool=can_use_tool, startup_frames=[])
        clients.append(c)
        return c
    return ClaudeAgentDriver(session_id, "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                             sdk_factory=make, registry=PermissionRegistry(), identity="me")


async def _settle(d: ClaudeAgentDriver, pred, timeout=1.0):
    """Poll a driver predicate — the idle listener consumes injected frames on the loop."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("driver never reached the expected state")
        await asyncio.sleep(0.01)


def test_started_registers_and_ended_unregisters_the_task():
    clients: list[FakeSDKClient] = []
    d = _driver(clients)

    async def run():
        await d.start()
        clients[0].inject([f.task_started(TASK, TID)])
        await _settle(d, lambda: d.subagent_task_id(TID) == TASK)
        assert d.subagent_tool_use_id(TASK) == TID
        clients[0].inject([f.task_notification(TASK, TID, status="completed")])
        await _settle(d, lambda: d.subagent_task_id(TID) is None)
        assert d.subagent_tool_use_id(TASK) is None
        await d.aclose()
    _run(run())


def test_stop_subagent_calls_stop_task_with_the_mapped_id():
    clients: list[FakeSDKClient] = []
    d = _driver(clients)

    async def run():
        await d.start()
        clients[0].inject([f.task_started(TASK, TID)])
        await _settle(d, lambda: d.subagent_task_id(TID) == TASK)
        assert await d.stop_subagent(TID) == TASK
        assert clients[0].stopped_tasks == [TASK]
        await d.aclose()
    _run(run())


def test_stop_subagent_unknown_returns_none_and_calls_nothing():
    clients: list[FakeSDKClient] = []
    d = _driver(clients)

    async def run():
        await d.start()
        assert await d.stop_subagent("toolu_missing") is None
        assert clients[0].stopped_tasks == []
        await d.aclose()
    _run(run())


def test_stop_subagent_before_start_returns_none():
    d = _driver([])
    assert _run(d.stop_subagent(TID)) is None


def test_background_shell_registers_in_the_task_map_and_stop_kills_it():
    """A backgrounded Bash shares the Task map so POST /agents/{toolUseId}/stop stops it."""
    clients: list[FakeSDKClient] = []
    d = _driver(clients)
    seen: list[str] = []
    d._on_background_work_changed = seen.append

    async def run():
        await d.start()
        clients[0].inject([f.task_started(TASK, TID, task_type="local_bash", backgrounded=True)])
        await _settle(d, lambda: d.subagent_task_id(TID) == TASK)
        assert seen == ["s1"]
        assert await d.stop_subagent(TID) == TASK
        assert clients[0].stopped_tasks == [TASK]
        clients[0].inject([f.task_notification(TASK, TID, status="stopped", summary="")])
        await _settle(d, lambda: d.subagent_task_id(TID) is None)
        assert seen == ["s1", "s1"]
        await d.aclose()
    _run(run())


def test_foreground_bash_task_neither_registers_nor_fires_the_callback():
    """A foreground Bash is a local_bash task too (is_backgrounded false) — not a snapshot change."""
    clients: list[FakeSDKClient] = []
    d = _driver(clients)
    seen: list[str] = []
    d._on_background_work_changed = seen.append

    async def run():
        await d.start()
        clients[0].inject([f.task_started(TASK, TID, task_type="local_bash", backgrounded=False),
                           f.task_notification(TASK, TID, status="completed", summary="")])
        await asyncio.sleep(0.2)
        assert d.subagent_task_id(TID) is None
        assert seen == []
        await d.aclose()
    _run(run())
