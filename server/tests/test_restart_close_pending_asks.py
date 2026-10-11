"""A daemon restart must not read to the agent as the user refusing (#2922).

Closing a driver for a daemon shutdown used to settle a pending AskUserQuestion
with "[User dismissed the question UI] … user did not answer" and a pending
permission with "denied by user". The agent then acted on a refusal nobody gave.
A restart close now settles both with the restart-specific message, and the
question's resolution frame says ``interrupted`` rather than ``dismissed``.
"""
from __future__ import annotations

import asyncio

import server.sessions.events as ev
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.orphaned_asks import (
    CLOSE_REASON_RESTART,
    QUESTION_MODE_INTERRUPTED,
    RESTART_ASK_MESSAGE,
)
from server.sessions.permissions import PermissionRegistry
from server.sessions.questions import QuestionRegistry
from server.tests.fakes.fake_sdk import FakeSDKClient


def _factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
    return FakeSDKClient([], on_can_use_tool=can_use_tool)


def _driver(sid: str) -> ClaudeAgentDriver:
    return ClaudeAgentDriver(sid, "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                             sdk_factory=_factory, registry=PermissionRegistry(),
                             identity="me")


_Q = {"questions": [{"header": "Pick", "question": "Which?",
                     "options": [{"label": "A"}, {"label": "B"}], "multiSelect": False}]}


def _drain(q: asyncio.Queue) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_restart_close_settles_a_pending_question_with_the_restart_text():
    from claude_agent_sdk import PermissionResultDeny

    async def run():
        d = _driver("rq")
        await d.start()
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        cb = asyncio.create_task(d._can_use_tool("AskUserQuestion", _Q))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert d.awaiting_user_input

        await d.aclose(reason=CLOSE_REASON_RESTART)
        result = await asyncio.wait_for(cb, timeout=2.0)
        assert isinstance(result, PermissionResultDeny)
        assert RESTART_ASK_MESSAGE in result.message
        assert "dismissed" not in result.message.lower()
        assert "did not answer" not in result.message.replace("did NOT answer", "")
        resolved = [e for e in _drain(out_q) if isinstance(e, ev.QuestionResolved)]
        assert len(resolved) == 1
        assert resolved[0].mode == QUESTION_MODE_INTERRUPTED
        assert resolved[0].selected is None

    asyncio.run(run())


def test_restart_close_multi_question_uses_the_restart_text_for_every_line():
    from claude_agent_sdk import PermissionResultDeny

    two = {"questions": [dict(_Q["questions"][0]),
                         {**_Q["questions"][0], "question": "And?"}]}

    async def run():
        d = _driver("rq2")
        await d.start()
        cb = asyncio.create_task(d._can_use_tool("AskUserQuestion", two))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await d.aclose(reason=CLOSE_REASON_RESTART)
        result = await asyncio.wait_for(cb, timeout=2.0)
        assert isinstance(result, PermissionResultDeny)
        assert RESTART_ASK_MESSAGE in result.message
        assert "dismissed" not in result.message.lower()

    asyncio.run(run())


def test_a_user_close_keeps_the_dismissed_text():
    from claude_agent_sdk import PermissionResultDeny

    async def run():
        d = _driver("uq")
        await d.start()
        cb = asyncio.create_task(d._can_use_tool("AskUserQuestion", _Q))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await d.aclose()
        result = await asyncio.wait_for(cb, timeout=2.0)
        assert isinstance(result, PermissionResultDeny)
        assert "dismissed" in result.message

    asyncio.run(run())


def test_restart_close_denies_a_pending_permission_with_the_restart_text():
    from claude_agent_sdk import PermissionResultDeny

    async def run():
        d = _driver("rp")
        await d.start()
        cb = asyncio.create_task(d._can_use_tool("Bash", {"command": "make test"}))
        for _ in range(5):
            await asyncio.sleep(0)
        assert d._reg.pending("rp")
        await d.aclose(reason=CLOSE_REASON_RESTART)
        result = await asyncio.wait_for(cb, timeout=2.0)
        assert isinstance(result, PermissionResultDeny)
        assert result.message == RESTART_ASK_MESSAGE

    asyncio.run(run())


def test_question_registry_deny_all_with_a_mode_resolves_to_that_mode():
    async def run():
        reg = QuestionRegistry()
        _qid, fut = reg.request("s1", "t1", "H", "Q?", [{"label": "A"}], False, "me")
        reg.deny_all("s1", mode=QUESTION_MODE_INTERRUPTED)
        return await fut

    assert asyncio.run(run()) == ([], QUESTION_MODE_INTERRUPTED)


def test_orchestrator_stop_closes_drivers_for_a_restart():
    from server.sessions.orchestrator import SessionOrchestrator

    seen: list = []

    class _D:
        async def aclose(self, *, reason=None):
            seen.append(reason)

    async def run():
        o = SessionOrchestrator.__new__(SessionOrchestrator)
        o._bg_tasks = set()
        o._drivers = {"s1": _D()}

        class _Stopper:
            async def stop(self):
                return None

        o._poller = o._process_quota = o._pr_poller = _Stopper()
        o._pr_population = o._deploy_watcher = _Stopper()
        o._pr_branch_task = None
        await o.stop()

    asyncio.run(run())
    assert seen == [CLOSE_REASON_RESTART]


def test_restart_close_keeps_an_answer_given_before_the_restart():
    from claude_agent_sdk import PermissionResultDeny

    two = {"questions": [dict(_Q["questions"][0]),
                         {**_Q["questions"][0], "question": "And?"}]}

    async def run():
        d = _driver("rq3")
        await d.start()
        cb = asyncio.create_task(d._can_use_tool("AskUserQuestion", two))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        first = d.pending_questions()[0]["question_id"]
        assert d.answer_question(first, ["B"], "me")
        await d.aclose(reason=CLOSE_REASON_RESTART)
        result = await asyncio.wait_for(cb, timeout=2.0)
        assert isinstance(result, PermissionResultDeny)
        assert "- Which? → B" in result.message
        assert "- And? → (not answered yet)" in result.message

    asyncio.run(run())
