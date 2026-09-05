import asyncio
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.events import Suggestions, TextDelta, UserMessage, TurnEnd
from claude_agent_sdk import TextBlock
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes.scripted_sdk import Gate, scripted_factory


def _gated_stream(gate: Gate):
    """The former ``_GatedSDK``, expressed as a scenario: two text deltas, then a
    gate that blocks before the settle (assistant + result), so a test can attach a
    second subscriber while the turn is still in progress. Generalized onto
    ``ScriptedSDKClient`` — the single scriptable stream primitive (constitution VI)."""
    return [
        f.sys_init("sess-x"),
        f.stream_start(uuid="t1"),
        f.stream_text("Hello ", uuid="t1"),
        f.stream_text("world", uuid="t1"),
        gate,
        f.assistant(TextBlock(text="Hello world"), uuid="t1"),
        f.result(session_id="sess-x", uuid="t1"),
    ]


def test_late_subscriber_replays_in_progress_turn_then_goes_live():
    gate = Gate()
    factory = scripted_factory(_gated_stream(gate))

    async def run():
        d = ClaudeAgentDriver("s", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start()
        # Subscriber A is a concurrent LIVE consumer while B replays the snapshot.
        a_seen = []
        gen_a = d.events()
        async def drive_a():
            async for e in gen_a:
                a_seen.append(e)
                if isinstance(e, TurnEnd):
                    break
        task_a = asyncio.create_task(drive_a())
        await d.send_prompt("hi")
        # Deterministic: the gate is reached only once both text deltas have been fed
        # to the translator and emitted (the stream is suspended at the gate).
        await gate.wait_reached()
        assert d.turn_active is True
        # Mid-turn the driver must expose WHEN the turn started (epoch seconds) —
        # ws.py hands this to replay_window so the incomplete-turn drop can tell the
        # in-progress turn's disk user entry apart from a PREVIOUS turn's.
        assert isinstance(d.turn_started_at, float) and d.turn_started_at > 0

        # Subscriber B attaches mid-turn: its FIRST events must reconstruct the turn.
        gen_b = d.events()
        b_first = []
        for _ in range(6):
            b_first.append(await asyncio.wait_for(gen_b.__anext__(), timeout=1))
            if any(isinstance(e, TextDelta) and e.text == "Hello world" for e in b_first):
                break
        assert any(isinstance(e, UserMessage) for e in b_first)
        assert any(isinstance(e, TextDelta) and e.text == "Hello world" for e in b_first)

        # Now release the turn; B must receive the TurnEnd live (no duplicate text).
        gate.release()
        b_rest = []
        for _ in range(10):
            e = await asyncio.wait_for(gen_b.__anext__(), timeout=1)
            b_rest.append(e)
            if isinstance(e, TurnEnd):
                break
        b_text = "".join(e.text for e in (b_first + b_rest) if isinstance(e, TextDelta))
        assert b_text == "Hello world"   # coalesced snapshot + no live re-append
        await task_a
        await gen_b.aclose()
        await d.aclose()

    asyncio.run(run())


def test_interrupt_clears_turn_started_at():
    """interrupt() flips _turn_active False without running the drain's settle path —
    turn_started_at must be cleared there too, or a later attach would hand a stale
    start time to the replay drop rule."""
    gate = Gate()
    factory = scripted_factory(_gated_stream(gate))

    async def run():
        d = ClaudeAgentDriver("s3", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start()
        seen = []
        gen = d.events()
        async def drive():
            async for e in gen:
                seen.append(e)
                if isinstance(e, TurnEnd):
                    break
        task = asyncio.create_task(drive())
        await d.send_prompt("hi")
        await gate.wait_reached()   # mid-turn, deterministically (both deltas emitted)
        assert d.turn_started_at is not None
        await d.interrupt()
        assert d.turn_active is False
        assert d.turn_started_at is None
        gate.release()   # unblock the drain so it can settle
        # An interrupted turn stops the stream without emitting a TurnEnd (the scripted
        # client honours interrupt, like the real SDK — the old no-op-interrupt fake
        # kept streaming the result). Await the pump directly; the consumer would block
        # forever waiting for a TurnEnd that never comes.
        if d._pump is not None:
            await asyncio.wait_for(d._pump, timeout=1)
        assert d.turn_active is False   # settle path leaves it cleared
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await d.aclose()

    asyncio.run(run())


def test_buffer_cleared_at_settle_idle_subscriber_replays_nothing():
    msgs = [f.sys_init("s2"), f.stream_start(uuid="t1"),
            f.stream_text("done", uuid="t1"),
            f.assistant(TextBlock(text="done"), uuid="t1"), f.result(session_id="s2", uuid="t1")]

    def factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return FakeSDKClient(msgs)

    async def run():
        d = ClaudeAgentDriver("s2", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start()
        await d.send_prompt("hi")
        async for e in d.events():
            if isinstance(e, TurnEnd):
                break
        # Wait for the drain's finally (settle) to run before asserting.
        for _ in range(200):
            if d._pump is not None and d._pump.done():
                break
            await asyncio.sleep(0.01)
        assert d.turn_active is False
        assert d.turn_started_at is None   # settled turn: start time must not leak
        assert d._turn_buffer == []
        # A subscriber opened while idle replays nothing (empty snapshot).
        gen = d.events()
        try:
            await asyncio.wait_for(gen.__anext__(), timeout=0.2)
            raise AssertionError("idle subscriber unexpectedly replayed an event")
        except asyncio.TimeoutError:
            pass
        finally:
            await gen.aclose()
        await d.aclose()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# open_stream(): the atomic attach handle (B3 settle-race fix)
# ---------------------------------------------------------------------------

def _bare_driver(session_id="so1") -> ClaudeAgentDriver:
    """A driver that is never start()ed — open_stream touches no SDK state."""
    return ClaudeAgentDriver(session_id, "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                         sdk_factory=lambda *a, **k: None,
                         registry=PermissionRegistry(), identity="me")


def test_open_stream_snapshot_and_flags_survive_a_later_settle():
    """open_stream() must capture the in-progress-turn snapshot AND the
    turn_active/turn_started_at flags at the SAME instant. A settle that runs
    after open_stream (the drain's finally clearing the buffer) must not affect
    what the stream delivers — that is exactly the WS attach settle-race."""
    async def run():
        d = _bare_driver()
        d._turn_active = True
        d._turn_started_at = 123.5
        d._turn_buffer = [TextDelta(turn_id="t1", text="tail")]
        d._last_suggestions = ["next?"]

        stream = d.open_stream()
        assert stream.turn_active is True
        assert stream.turn_started_at == 123.5
        assert len(d._subscribers) == 1

        # Settle AFTER open_stream: the captured snapshot must be unaffected.
        d._turn_buffer = []
        d._turn_active = False
        d._turn_started_at = None

        gen = stream.events()
        first = await asyncio.wait_for(gen.__anext__(), 1)
        assert isinstance(first, TextDelta) and first.text == "tail"
        # The suggestions re-emit rides the snapshot, same as events() always did.
        second = await asyncio.wait_for(gen.__anext__(), 1)
        assert isinstance(second, Suggestions) and second.items == ["next?"]

        # Live events emitted after open_stream land in the pre-registered queue.
        d._emit(TextDelta(turn_id="t2", text="live"))
        third = await asyncio.wait_for(gen.__anext__(), 1)
        assert isinstance(third, TextDelta) and third.text == "live"

        await gen.aclose()
        assert d._subscribers == set()   # generator teardown discards the queue

    asyncio.run(run())


def test_open_stream_close_discards_queue_without_consuming_events():
    """close() is the leak guard for the WS attach error path: the replay between
    open_stream() and the live-task creation can raise, and nothing will ever
    iterate events() — the subscriber queue must still be discarded."""
    async def run():
        d = _bare_driver("so2")
        stream = d.open_stream()
        assert len(d._subscribers) == 1
        stream.close()
        assert d._subscribers == set()
        # close() is idempotent.
        stream.close()
        assert d._subscribers == set()

    asyncio.run(run())


def test_events_delegates_to_open_stream_single_implementation():
    """events() must be a thin delegate over open_stream() (one implementation,
    constitution VI): a snapshot seeded before subscribing arrives first, then
    live events, and teardown still deregisters the queue."""
    async def run():
        d = _bare_driver("so3")
        d._turn_buffer = [TextDelta(turn_id="t1", text="snap")]
        gen = d.events()
        first = await asyncio.wait_for(gen.__anext__(), 1)
        assert isinstance(first, TextDelta) and first.text == "snap"
        assert len(d._subscribers) == 1
        await gen.aclose()
        assert d._subscribers == set()

    asyncio.run(run())


def test_turn_buffer_retains_pending_asks_and_their_resolutions():
    """A mid-turn attach snapshot must carry the turn's asks: the WS bridge no longer
    re-emits pending permissions/questions ahead of the open-turn resupply (that
    early copy fabricated the assistant turn above the re-supplied prompt), so the
    snapshot is the ONLY thing that re-delivers an ask to a reattaching viewer."""
    from server.sessions import events as ev
    d = _bare_driver()
    d._turn_active = True
    asks = [
        ev.PermissionRequest("p1", "t1", "Bash", ev.ToolKind.BASH, {"command": "ls"}),
        ev.PermissionResolved("p1", "t1", True),
        ev.Question(question_id="q1", turn_id="t1", header="h", question="?",
                    options=[], multi_select=False),
        ev.QuestionResolved(question_id="q1", turn_id="t1", selected=["a"]),
    ]
    for e in asks:
        d._emit(e)
    assert d._turn_buffer == asks
