"""Unit tests for the scriptable SDK stream generator primitives themselves
(``server/tests/fakes/scripted_sdk.py``): gates, deterministic delays, per-turn
segmentation of ``receive_response()``, and the composable scenario builders. The
driver-level seams that USE these live in ``test_driver_stream_seams.py``.
"""
from __future__ import annotations

import asyncio

from claude_agent_sdk import (AssistantMessage, ResultMessage, StreamEvent, SystemMessage,
                              ToolUseBlock, UserMessage)

from server.tests.fakes import fake_sdk as f
from server.tests.fakes.scripted_sdk import (
    Delay, Gate, ScriptedSDKClient, say, subagent_thread, turn, use_tool,
)


def test_gate_pauses_stream_until_released():
    gate = Gate("mid")
    client = ScriptedSDKClient([f.stream_text("a", uuid="t1"), gate,
                                f.stream_text("b", uuid="t1"), f.result(uuid="t1")])

    async def run():
        got = []

        # A continuous consumer (as the driver's drain does) advances the pull-based
        # generator; the gate parks it after the first event.
        async def consume():
            async for m in client.receive_response():
                got.append(m)

        task = asyncio.create_task(consume())
        await gate.wait_reached()                   # consumer parked at the gate
        assert gate.is_reached is True
        assert [type(m).__name__ for m in got] == ["StreamEvent"]   # only the pre-gate event
        gate.release()
        await asyncio.wait_for(task, 1)
        assert [type(m).__name__ for m in got] == ["StreamEvent", "StreamEvent", "ResultMessage"]

    asyncio.run(run())


def test_delay_yields_to_loop_without_wall_clock():
    order = []
    client = ScriptedSDKClient([f.stream_text("x", uuid="t1"), Delay(ticks=3),
                                f.stream_text("y", uuid="t1"), f.result(uuid="t1")])

    async def consume():
        async for m in client.receive_response():
            if isinstance(m, StreamEvent):
                order.append(m.event["delta"]["text"])

    async def other():
        # Runs entirely within the Delay's loop-yields — no real time elapses.
        for _ in range(2):
            order.append("other")
            await asyncio.sleep(0)

    async def run():
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        await asyncio.gather(consume(), other())
        assert loop.time() - t0 < 0.05          # deterministic, not wall-clock bound
        # "other" interleaved between the two text events, proving the yield points.
        assert order[0] == "x" and order[-1] == "y" and "other" in order

    asyncio.run(run())


def test_receive_response_segments_one_turn_per_call():
    """The cursor persists across calls: each ``receive_response()`` yields exactly one
    turn (up to and including its ResultMessage), like the real SDK."""
    scenario = ([f.sys_init("sess-1")]
                + turn("t1", say("t1", "one", chunks=1))
                + turn("t2", say("t2", "two", chunks=1)))
    client = ScriptedSDKClient(scenario)

    async def drain_once():
        return [m async for m in client.receive_response()]

    async def run():
        first = await drain_once()
        second = await drain_once()
        third = await drain_once()
        # First segment carries the one-time init + turn 1, ending at its result.
        assert isinstance(first[0], SystemMessage)
        assert isinstance(first[-1], ResultMessage) and first[-1].session_id == "sess-1"
        assert sum(isinstance(m, ResultMessage) for m in first) == 1
        # Second segment is turn 2 only (no re-emitted init), ending at its result.
        assert not any(isinstance(m, SystemMessage) for m in second)
        assert isinstance(second[-1], ResultMessage)
        assert sum(isinstance(m, ResultMessage) for m in second) == 1
        # Cursor exhausted → an empty segment.
        assert third == []

    asyncio.run(run())


def test_builders_compose_expected_message_shapes():
    # A turn wraps fragments with a stream_start head and a result tail.
    steps = turn("t1",
                 say("t1", "hi", chunks=1),
                 use_tool("t1", "tc1", "Read", {"file_path": "a"}, "data"))
    kinds = [type(m).__name__ for m in steps]
    assert kinds[0] == "StreamEvent"                     # stream_start head
    assert isinstance(steps[-1], ResultMessage)          # result tail
    assert any(isinstance(m, AssistantMessage)
               and any(isinstance(b, ToolUseBlock) for b in m.content) for m in steps)
    assert any(isinstance(m, UserMessage) for m in steps)   # the tool_result

    # A sub-agent thread tags its own output with parent_tool_use_id=task_id.
    sub = subagent_thread("t1", "task1", "sa1", "Explore", "found",
                          tools=[("g1", "Grep", {"pattern": "p"}, "m")])
    task_call = next(m for m in sub if isinstance(m, AssistantMessage)
                     and any(getattr(b, "name", None) == "Task" for b in m.content))
    assert task_call.parent_tool_use_id is None          # the Task call is main-thread
    sub_texts = [m for m in sub if isinstance(m, StreamEvent)
                 and m.parent_tool_use_id == "task1"]
    assert sub_texts, "sub-agent stream events must carry parent_tool_use_id"
