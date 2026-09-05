"""Driver attach-race seam tests, driven by the scriptable SDK stream generator.

These exercise the seams a synchronous message-list fake cannot reach: a live turn
streaming while a second subscriber attaches, a turn that settles in the window between
``open_stream()`` and the first consume, two subscribers attaching at different points of
the same turn, and a full multi-turn/multi-tool/sub-agent/compaction scenario replayed
through the driver. Timing is deterministic — every checkpoint is an awaited
``Gate.wait_reached()`` / ``Gate.release()``; there are no wall-clock ``sleep``s.

See ``server/tests/fakes/scripted_sdk.py`` for the scenario DSL.
"""
from __future__ import annotations

import asyncio

from claude_agent_sdk import TextBlock

from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions import events as ev
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.scripted_sdk import (
    Gate, scripted_factory, turn, say, use_tool, subagent_thread, compaction,
)


def _driver(scenario, session_id="s"):
    return ClaudeAgentDriver(session_id, "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                         sdk_factory=scripted_factory(scenario),
                         registry=PermissionRegistry(), identity="me")


def _is_top_turn_end(e) -> bool:
    return isinstance(e, ev.TurnEnd) and e.parent_tool_use_id is None


async def _collect_until_top_turn_end(gen, *, count=1, timeout=1.0):
    """Drain an events() generator until ``count`` top-level TurnEnds are seen."""
    out = []
    ends = 0
    while ends < count:
        e = await asyncio.wait_for(gen.__anext__(), timeout)
        out.append(e)
        if _is_top_turn_end(e):
            ends += 1
    return out


def _text(events) -> str:
    return "".join(e.text for e in events if isinstance(e, ev.TextDelta))


def _content_shape(events) -> dict:
    """Content-wise fingerprint of a subscriber's stream — granularity-independent, so a
    coalesced snapshot and its live-delta equivalent compare equal."""
    return {
        "text": _text(events),
        "thinking": "".join(e.text for e in events if isinstance(e, ev.ThinkingDelta)),
        "user_msgs": [e.text for e in events if isinstance(e, ev.UserMessage)],
        "tool_calls": [(e.name, e.tool_id, e.parent_tool_use_id)
                       for e in events if isinstance(e, ev.ToolCall)],
        "tool_results": [(e.tool_id, e.ok, e.parent_tool_use_id)
                         for e in events if isinstance(e, ev.ToolResult)],
        "turn_ends": [(e.turn_id, e.parent_tool_use_id) for e in events
                      if isinstance(e, ev.TurnEnd)],
    }


# ---------------------------------------------------------------------------
# 1. Attach WHILE a turn is streaming at a gate → coalesced snapshot + live tail.
# ---------------------------------------------------------------------------
def test_attach_mid_stream_gets_coalesced_snapshot_then_live_tail():
    gate = Gate()
    scenario = [
        f.sys_init("sess-1"),
        f.stream_start(uuid="t1"),
        f.stream_text("Hello ", uuid="t1"),
        f.stream_text("world", uuid="t1"),
        gate,
        f.stream_text(" and more", uuid="t1"),
        f.assistant(TextBlock(text="Hello world and more"), uuid="t1"),
        f.result(session_id="sess-1", uuid="t1"),
    ]

    async def run():
        d = _driver(scenario)
        await d.start()
        # Subscriber A is live from the start — the "other consumer" replaying alongside B.
        a_seen: list = []
        gen_a = d.events()

        async def drive_a():
            async for e in gen_a:
                a_seen.append(e)
                if _is_top_turn_end(e):
                    break

        task_a = asyncio.create_task(drive_a())
        await d.send_prompt("hi")
        await gate.wait_reached()          # deterministically mid-turn
        assert d.turn_active is True

        # B attaches mid-turn. Its snapshot must reconstruct the turn so far, coalesced
        # into a single TextDelta — no per-delta gap, no duplication.
        gen_b = d.events()
        b_first = await asyncio.wait_for(gen_b.__anext__(), 1)   # UserMessage
        b_snapshot = [b_first]
        # Pull the remaining snapshot events (up to the live boundary) — bounded, small.
        b_snapshot.append(await asyncio.wait_for(gen_b.__anext__(), 1))   # TurnStart
        b_snapshot.append(await asyncio.wait_for(gen_b.__anext__(), 1))   # coalesced TextDelta
        assert any(isinstance(e, ev.UserMessage) for e in b_snapshot)
        assert _text(b_snapshot) == "Hello world"   # coalesced, not "Hello "/"world" split

        # Release the tail; B receives the rest live with no re-append of the snapshot.
        gate.release()
        b_rest = await _collect_until_top_turn_end(gen_b)
        b_all = b_snapshot + b_rest
        assert _text(b_all) == "Hello world and more"        # snapshot + tail, each once
        assert sum(_is_top_turn_end(e) for e in b_all) == 1  # exactly one TurnEnd, no dup

        await task_a
        # A (live from the start) and B (snapshot + tail) converge content-wise.
        assert _content_shape(a_seen) == _content_shape(b_all)

        await gen_b.aclose()
        await d.aclose()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. Turn COMPLETES between open_stream() and first consume → delivered once.
#    Regression fence for the W2.2/#482 DriverStream settle-race fix.
# ---------------------------------------------------------------------------
def test_turn_settles_between_open_stream_and_consume_delivered_exactly_once():
    gate = Gate()
    scenario = [
        f.sys_init("sess-1"),
        f.stream_start(uuid="t1"),
        f.stream_text("settled ", uuid="t1"),
        f.stream_text("turn", uuid="t1"),
        gate,
        f.assistant(TextBlock(text="settled turn"), uuid="t1"),
        f.result(session_id="sess-1", uuid="t1"),
    ]

    async def run():
        d = _driver(scenario, session_id="s2")
        await d.start()
        await d.send_prompt("hi")
        await gate.wait_reached()

        # Open the attach handle mid-turn: snapshot + flags pinned to THIS instant.
        stream = d.open_stream()
        assert stream.turn_active is True
        assert stream.turn_started_at is not None

        # Release the turn and let it settle COMPLETELY before anyone consumes the
        # stream. The drain clears _turn_buffer and flips turn_active False — the
        # snapshot pinned by open_stream must be unaffected, and the TurnEnd emitted
        # after open_stream lands in the pre-registered queue.
        gate.release()
        assert d._pump is not None
        await asyncio.wait_for(d._pump, timeout=1)
        assert d.turn_active is False
        assert d._turn_buffer == []

        # Now consume: the settled turn is delivered exactly once — snapshot text plus
        # the queued TurnEnd, no gap, no duplicate.
        gen = stream.events()
        collected = await _collect_until_top_turn_end(gen)
        assert _text(collected) == "settled turn"
        assert sum(_is_top_turn_end(e) for e in collected) == 1
        assert any(isinstance(e, ev.UserMessage) for e in collected)

        await gen.aclose()
        await d.aclose()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. Two subscribers attaching at different gates converge to identical content.
# ---------------------------------------------------------------------------
def test_two_subscribers_at_different_gates_converge():
    g1, g2 = Gate("g1"), Gate("g2")
    scenario = [
        f.sys_init("sess-1"),
        f.stream_start(uuid="t1"),
        f.stream_text("alpha ", uuid="t1"),
        g1,
        f.stream_text("beta ", uuid="t1"),
        f.tool_use("tc1", "Read", {"file_path": "x.py"}, uuid="t1"),
        f.user_tool_result("tc1", "contents"),
        g2,
        f.stream_text("gamma", uuid="t1"),
        f.assistant(TextBlock(text="alpha beta gamma"), uuid="t1"),
        f.result(session_id="sess-1", uuid="t1"),
    ]

    async def run():
        d = _driver(scenario, session_id="s3")
        await d.start()
        await d.send_prompt("hi")

        await g1.wait_reached()
        stream_a = d.open_stream()   # attaches after "alpha "

        g1.release()
        await g2.wait_reached()
        stream_b = d.open_stream()   # attaches after the Read tool round-trip

        g2.release()
        gen_a, gen_b = stream_a.events(), stream_b.events()
        a_all = await _collect_until_top_turn_end(gen_a)
        b_all = await _collect_until_top_turn_end(gen_b)

        shape_a, shape_b = _content_shape(a_all), _content_shape(b_all)
        assert shape_a == shape_b
        # And the content is the WHOLE turn, not a truncated tail.
        assert shape_a["text"] == "alpha beta gamma"
        assert shape_a["tool_calls"] == [("Read", "tc1", None)]
        assert shape_a["tool_results"] == [("tc1", True, None)]

        await gen_a.aclose()
        await gen_b.aclose()
        await d.aclose()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. A full realistic scenario replayed through the driver → ordered wire events.
#    Golden-ish: assert on event types + key fields, not brittle full-dict equality.
# ---------------------------------------------------------------------------
def test_full_scenario_produces_expected_ordered_event_sequence():
    scenario = (
        [f.sys_init("sess-1", model="claude-opus-4-8")]
        + turn("t1", say("t1", "First answer"))
        + turn("t2", say("t2", "Reading", chunks=1),
                     use_tool("t2", "tc1", "Read", {"file_path": "a.py"}, "print(1)\n"),
                     say("t2", "Got it", chunks=1))
        + turn("t3", subagent_thread("t3", "task1", "sa1", "Explore", "searched",
                                     tools=[("g1", "Grep", {"pattern": "foo"}, "match")]),
                     compaction(),
                     say("t3", "Wrapped up", chunks=1))
    )

    async def run():
        d = _driver(scenario, session_id="s4")
        await d.start()
        gen = d.events()
        await d.send_prompt("p1")
        await d.send_prompt("p2")
        await d.send_prompt("p3")
        seen = await _collect_until_top_turn_end(gen, count=3, timeout=1.0)

        # Project to (type, salient-field) tuples — structural transcript events only
        # (control events Status/QueueUpdate are position-noisy and excluded).
        keep = (ev.UserMessage, ev.SessionMeta, ev.TurnStart, ev.TurnModel, ev.TextDelta,
                ev.ThinkingDelta, ev.ToolCall, ev.ToolResult, ev.Condensing, ev.TurnEnd)

        def proj(e):
            if isinstance(e, ev.UserMessage):
                return ("user", e.text)
            if isinstance(e, ev.SessionMeta):
                return ("sessionMeta", e.model)
            if isinstance(e, ev.TurnStart):
                return ("turnStart", e.turn_id, e.parent_tool_use_id)
            if isinstance(e, ev.TurnModel):
                return ("turnModel", e.parent_tool_use_id)
            if isinstance(e, ev.TextDelta):
                return ("text", e.text, e.parent_tool_use_id)
            if isinstance(e, ev.ThinkingDelta):
                return ("thinking", e.text, e.parent_tool_use_id)
            if isinstance(e, ev.ToolCall):
                return ("toolCall", e.name, e.tool_id, e.parent_tool_use_id)
            if isinstance(e, ev.ToolResult):
                return ("toolResult", e.tool_id, e.ok, e.parent_tool_use_id)
            if isinstance(e, ev.Condensing):
                return ("condensing", e.active)
            if isinstance(e, ev.TurnEnd):
                return ("turnEnd", e.turn_id, e.parent_tool_use_id, e.outcome)
            raise AssertionError(e)

        projected = [proj(e) for e in seen if isinstance(e, keep)]
        expected = [
            ("user", "p1"),
            ("sessionMeta", "claude-opus-4-8"),
            ("turnStart", "t1", None),
            ("text", "First ", None), ("text", "answer", None),
            ("turnModel", None),
            ("turnEnd", "t1", None, "success"),

            ("user", "p2"),
            ("turnStart", "t2", None),
            ("text", "Reading", None),
            ("turnModel", None),
            ("toolCall", "Read", "tc1", None),
            ("toolResult", "tc1", True, None),
            ("text", "Got it", None),
            ("turnEnd", "t2", None, "success"),

            ("user", "p3"),
            ("turnStart", "t3", None),
            ("turnModel", None),
            ("toolCall", "Task", "task1", None),
            ("turnStart", "sa1", "task1"),
            ("text", "sear", "task1"), ("text", "ched", "task1"),
            ("turnModel", "task1"),
            ("toolCall", "Grep", "g1", "task1"),
            ("toolResult", "g1", True, "task1"),
            ("toolResult", "task1", True, None),
            ("condensing", False),
            ("text", "Wrapped up", None),
            ("turnEnd", "t3", None, "success"),
        ]
        assert projected == expected

        await gen.aclose()
        await d.aclose()

    asyncio.run(run())
