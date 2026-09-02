"""A prompt sent while an AskUserQuestion is pending IS the answer.

Regression (frozen "Answer needed" sessions, 2026-09-02): the turn is parked inside
``_can_use_tool`` awaiting the question future. ``send_prompt`` gated only on "is a
pump task alive" — true while parked — so a composer message was appended to the
C2 FIFO queue. That queue drains only when the turn ENDS, and the turn cannot end
until the question is answered: a deadlock by construction. The typed reply sat in
the queue strip forever and the model never heard it.

The driver's own state ("questions pending") is the source of truth for what a
user message means — not the client's choice of frame type.
"""
from __future__ import annotations

import asyncio

import server.sessions.events as ev
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes.fake_sdk import FakeSDKClient


def _factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
    return FakeSDKClient([], on_can_use_tool=can_use_tool)


def _question_input(*questions):
    return {"questions": [
        {"header": h, "question": q,
         "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}],
         "multiSelect": False}
        for h, q in questions]}


def _driver(sid: str) -> SessionDriver:
    return SessionDriver(sid, "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                         sdk_factory=_factory, registry=PermissionRegistry(), identity="me")


async def _park_on_question(d: SessionDriver, *questions) -> asyncio.Task:
    cb = asyncio.create_task(d._can_use_tool("AskUserQuestion", _question_input(*questions)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert d.awaiting_user_input
    return cb


def test_send_prompt_while_question_pending_answers_instead_of_queueing():
    from claude_agent_sdk import PermissionResultDeny

    async def run():
        d = _driver("sqp")
        await d.start()
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        cb = await _park_on_question(d, ("Pick", "Which?"))

        await d.send_prompt("all of the above, I'm unsure about the structure")

        # The parked callback returns with the typed text framed as a chat answer.
        result = await asyncio.wait_for(cb, timeout=2.0)
        assert isinstance(result, PermissionResultDeny)
        assert "all of the above" in result.message
        assert "discuss" in result.message          # chat framing, not a picked option
        # Nothing was queued and no new turn was started.
        assert d.queued() == []
        assert d._pump is None
        assert not d.awaiting_user_input
        # The resolution rode the event stream in chat mode.
        events = []
        while not out_q.empty():
            events.append(out_q.get_nowait())
        resolved = [e for e in events if isinstance(e, ev.QuestionResolved)]
        assert len(resolved) == 1
        assert resolved[0].mode == "chat"
        assert resolved[0].selected == ["all of the above, I'm unsure about the structure"]
        await d.aclose()

    asyncio.run(run())


def test_send_prompt_answers_every_pending_question_of_a_multi_question_call():
    async def run():
        d = _driver("sqm")
        await d.start()
        cb = await _park_on_question(d, ("Q1", "First?"), ("Q2", "Second?"))
        assert len(d.pending_questions()) == 2

        await d.send_prompt("both, see my notes")

        result = await asyncio.wait_for(cb, timeout=2.0)
        assert "First?" in result.message and "Second?" in result.message
        assert result.message.count("both, see my notes") == 2
        assert d.pending_questions() == []
        assert d.queued() == []
        await d.aclose()

    asyncio.run(run())


def test_send_prompt_with_images_during_question_answers_text_and_queues_images():
    """The question channel is text-only. Images must not be dropped silently:
    they follow as an attachment-only prompt once the turn ends."""
    async def run():
        d = _driver("sqi")
        await d.start()
        cb = await _park_on_question(d, ("Pick", "Which?"))
        images = [{"url": "/att/1", "mime": "image/png", "name": "shot.png", "data": b""}]

        await d.send_prompt("see screenshot", images)

        result = await asyncio.wait_for(cb, timeout=2.0)
        assert "see screenshot" in result.message
        assert d._queue == [("", images)]
        await d.aclose()

    asyncio.run(run())


def test_send_prompt_without_pending_question_still_starts_a_turn():
    """Guard the guard: with nothing pending the ordinary turn path is untouched."""
    async def run():
        d = _driver("sqn")
        await d.start()
        await d.send_prompt("hello")
        assert d._pump is not None
        await asyncio.wait_for(d._pump, timeout=2.0)
        assert d.queued() == []
        await d.aclose()

    asyncio.run(run())
