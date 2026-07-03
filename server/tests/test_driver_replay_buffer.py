import asyncio
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.events import TextDelta, UserMessage, TurnEnd
from claude_agent_sdk import TextBlock
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient


class _GatedSDK:
    """Streams two text deltas, then blocks on a gate before emitting the result,
    so a test can attach a second subscriber while the turn is still in progress."""
    def __init__(self, gate: asyncio.Event):
        self._gate = gate
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    async def query(self, prompt): pass
    async def interrupt(self): pass
    async def set_permission_mode(self, mode): pass
    async def receive_response(self):
        yield f.sys_init("sess-x")
        yield f.stream_start(uuid="t1")
        yield f.stream_text("Hello ", uuid="t1")
        yield f.stream_text("world", uuid="t1")
        await self._gate.wait()
        yield f.assistant(TextBlock(text="Hello world"), uuid="t1")
        yield f.result(session_id="sess-x", uuid="t1")


def test_late_subscriber_replays_in_progress_turn_then_goes_live():
    gate = asyncio.Event()

    def factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return _GatedSDK(gate)

    async def run():
        d = SessionDriver("s", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=factory, registry=PermissionRegistry(), identity="me")
        await d.start()
        # Subscriber A drives the turn until 2 text deltas have streamed.
        a_seen = []
        gen_a = d.events()
        async def drive_a():
            async for e in gen_a:
                a_seen.append(e)
                if isinstance(e, TurnEnd):
                    break
        task_a = asyncio.create_task(drive_a())
        await d.send_prompt("hi")
        for _ in range(200):
            if sum(isinstance(e, TextDelta) for e in a_seen) >= 2:
                break
            await asyncio.sleep(0.01)
        assert d.turn_active is True

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
        gate.set()
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


def test_buffer_cleared_at_settle_idle_subscriber_replays_nothing():
    msgs = [f.sys_init("s2"), f.stream_start(uuid="t1"),
            f.stream_text("done", uuid="t1"),
            f.assistant(TextBlock(text="done"), uuid="t1"), f.result(session_id="s2", uuid="t1")]

    def factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return FakeSDKClient(msgs)

    async def run():
        d = SessionDriver("s2", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
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
