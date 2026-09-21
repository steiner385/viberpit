"""A sub-agent's permission / question card carries the spawning Task's tool-use id
(spec §4.3): ToolPermissionContext.agent_id == task_started.task_id (claude 2.1.268)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from server.sessions import events as ev
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient

TID = "toolu_01YNkJcCo55UaJospWhvnJMx"
TASK = "abb42ada82e1382fe"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _driver(clients, mode="interactive"):
    def make(cwd, config_dir, can_use_tool, m, resume, extra_env=None, *,
             on_pre_compact=None, options_overlay=None):
        c = FakeSDKClient([], on_can_use_tool=can_use_tool, startup_frames=[])
        clients.append(c)
        return c
    return ClaudeAgentDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode=mode,
                             sdk_factory=make, registry=PermissionRegistry(), identity="me")


async def _settle(pred, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


def _drain(q: asyncio.Queue) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_permission_from_a_live_sub_agent_carries_its_tool_use_id():
    clients: list[FakeSDKClient] = []
    d = _driver(clients)
    out_q: asyncio.Queue = asyncio.Queue()
    d._subscribers.add(out_q)

    async def run():
        await d.start()
        clients[0].inject([f.task_started(TASK, TID)])
        await _settle(lambda: d.subagent_task_id(TID) == TASK)
        ctx = SimpleNamespace(agent_id=TASK, tool_use_id="toolu_inner")
        task = asyncio.create_task(d._can_use_tool("Write", {"file_path": "/tmp/x"}, ctx))
        await _settle(lambda: any(isinstance(e, ev.PermissionRequest) for e in list(out_q._queue)))
        req = next(e for e in _drain(out_q) if isinstance(e, ev.PermissionRequest))
        assert req.parent_tool_use_id == TID
        assert req.to_wire()["parentToolUseId"] == TID
        d.resolve_permission(req.perm_id, "deny", "me")
        await task
        await d.aclose()
    _run(run())


def test_permission_with_unknown_agent_id_is_unattributed():
    clients: list[FakeSDKClient] = []
    d = _driver(clients)
    out_q: asyncio.Queue = asyncio.Queue()
    d._subscribers.add(out_q)

    async def run():
        await d.start()
        ctx = SimpleNamespace(agent_id="never-started", tool_use_id="toolu_inner")
        task = asyncio.create_task(d._can_use_tool("Write", {"file_path": "/tmp/x"}, ctx))
        await _settle(lambda: any(isinstance(e, ev.PermissionRequest) for e in list(out_q._queue)))
        req = next(e for e in _drain(out_q) if isinstance(e, ev.PermissionRequest))
        assert req.parent_tool_use_id is None
        assert req.to_wire()["parentToolUseId"] is None
        d.resolve_permission(req.perm_id, "deny", "me")
        await task
        await d.aclose()
    _run(run())


def test_pending_permission_reattach_rebuild_carries_sub_agent_tool_use_id():
    """#1560: a full-disk-replay reattach rebuilds the pending card from
    ``PermissionRegistry.pending()``, not from the live event — that payload must
    still carry parent_tool_use_id or the re-emitted card is not mirrored into the
    sub-agent's own thread."""
    clients: list[FakeSDKClient] = []
    d = _driver(clients)
    out_q: asyncio.Queue = asyncio.Queue()
    d._subscribers.add(out_q)

    async def run():
        await d.start()
        clients[0].inject([f.task_started(TASK, TID)])
        await _settle(lambda: d.subagent_task_id(TID) == TASK)
        ctx = SimpleNamespace(agent_id=TASK, tool_use_id="toolu_inner")
        task = asyncio.create_task(d._can_use_tool("Write", {"file_path": "/tmp/x"}, ctx))
        await _settle(lambda: any(isinstance(e, ev.PermissionRequest) for e in list(out_q._queue)))
        _drain(out_q)
        pending = d._reg.pending(d.session_id)
        assert len(pending) == 1
        assert pending[0]["parent_tool_use_id"] == TID
        d.resolve_permission(pending[0]["permId"], "deny", "me")
        await task
        await d.aclose()
    _run(run())


def test_question_from_a_live_sub_agent_carries_its_tool_use_id():
    clients: list[FakeSDKClient] = []
    d = _driver(clients)
    out_q: asyncio.Queue = asyncio.Queue()
    d._subscribers.add(out_q)
    tool_input = {"questions": [{"header": "Pick", "question": "Which?", "multiSelect": False,
                                 "options": [{"label": "A", "description": ""},
                                             {"label": "B", "description": ""}]}]}

    async def run():
        await d.start()
        clients[0].inject([f.task_started(TASK, TID)])
        await _settle(lambda: d.subagent_task_id(TID) == TASK)
        ctx = SimpleNamespace(agent_id=TASK, tool_use_id="toolu_q")
        task = asyncio.create_task(d._can_use_tool("AskUserQuestion", tool_input, ctx))
        await _settle(lambda: any(isinstance(e, ev.Question) for e in list(out_q._queue)))
        q = next(e for e in _drain(out_q) if isinstance(e, ev.Question))
        assert q.parent_tool_use_id == TID and q.to_wire()["parentToolUseId"] == TID
        d.answer_question(q.question_id, ["A"], "me")
        await task
        await d.aclose()
    _run(run())


def test_pending_question_reattach_rebuild_carries_sub_agent_tool_use_id():
    """#1560: same regression as the permission case, for AskUserQuestion — the
    reattach rebuild reads QuestionRegistry.pending(), which must also carry
    parent_tool_use_id."""
    clients: list[FakeSDKClient] = []
    d = _driver(clients)
    out_q: asyncio.Queue = asyncio.Queue()
    d._subscribers.add(out_q)
    tool_input = {"questions": [{"header": "Pick", "question": "Which?", "multiSelect": False,
                                 "options": [{"label": "A", "description": ""},
                                             {"label": "B", "description": ""}]}]}

    async def run():
        await d.start()
        clients[0].inject([f.task_started(TASK, TID)])
        await _settle(lambda: d.subagent_task_id(TID) == TASK)
        ctx = SimpleNamespace(agent_id=TASK, tool_use_id="toolu_q")
        task = asyncio.create_task(d._can_use_tool("AskUserQuestion", tool_input, ctx))
        await _settle(lambda: any(isinstance(e, ev.Question) for e in list(out_q._queue)))
        _drain(out_q)
        pending = d.pending_questions()
        assert len(pending) == 1
        assert pending[0]["parent_tool_use_id"] == TID
        d.answer_question(pending[0]["question_id"], ["A"], "me")
        await task
        await d.aclose()
    _run(run())
