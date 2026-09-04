import asyncio
from server.tests.fakes import fake_sdk as f
from server.sessions.sdk_contract import SDKClientProtocol
from claude_agent_sdk import SystemMessage, StreamEvent, AssistantMessage, ResultMessage, TextBlock

def test_builders_make_real_sdk_objects():
    assert isinstance(f.sys_init("s1"), SystemMessage)
    assert isinstance(f.stream_text("hi"), StreamEvent)
    assert isinstance(f.assistant(TextBlock(text="hi")), AssistantMessage)
    assert isinstance(f.result(), ResultMessage)
    # stream event carries the raw anthropic event dict
    assert f.stream_text("hi").event["delta"]["text"] == "hi"

def test_fake_client_satisfies_protocol_and_replays_in_order():
    """Like the real CLI, connect is NOT silent: the SessionStart hook frames land on the
    stream before the first query's turn (see ``session_start_hook_frames``)."""
    msgs = [f.sys_init("s1"), f.stream_start(), f.stream_text("Hi"),
            f.assistant(TextBlock(text="Hi")), f.result(session_id="s1")]
    async def run():
        c = f.FakeSDKClient(msgs)
        assert isinstance(c, SDKClientProtocol)
        async with c:
            stream = c.receive_messages()
            await c.query("hi")
            assert c.sent == ["hi"]
            seen = [type(m).__name__ for m in await f.drain_turn(stream)]
        return seen
    assert asyncio.run(run()) == [
        "HookEventMessage", "HookEventMessage", "SystemMessage",          # connect
        "SystemMessage", "StreamEvent", "StreamEvent", "AssistantMessage", "ResultMessage"]

def test_connect_frames_default_to_session_start_hooks_and_can_be_silenced():
    """``startup_frames`` defaults to the captured SessionStart hook frames (no
    ResultMessage among them — they are bookkeeping, not a turn); ``[]`` is a silent
    connect for tests that exercise query/inject/interrupt semantics alone."""
    async def run():
        c = f.FakeSDKClient([])
        async with c:
            stream = c.receive_messages()
            default = [await stream.__anext__() for _ in range(3)]
        silent = f.FakeSDKClient([], startup_frames=[])
        async with silent:
            await silent.query("x")
            only_turn = await f.drain_turn(silent.receive_messages())
        return default, only_turn
    default, only_turn = asyncio.run(run())
    assert [m.subtype for m in default] == ["hook_started", "hook_response", "hook_progress"]
    assert not any(isinstance(m, ResultMessage) for m in default)
    assert [type(m).__name__ for m in only_turn] == ["ResultMessage"]

def test_stream_is_persistent_and_replays_the_script_per_query():
    """One stream for the client's life: each query() delivers the script again, and
    inject() delivers a turn with NO query behind it (a CLI-initiated continuation)."""
    msgs = [f.stream_text("a"), f.result(uuid="r")]
    async def run():
        c = f.FakeSDKClient(msgs, startup_frames=[])
        async with c:
            stream = c.receive_messages()
            await c.query("one")
            first = await f.drain_turn(stream)
            c.inject([f.stream_text("bg"), f.result(uuid="bg")])
            auto = await f.drain_turn(stream)
            await c.query("two")
            second = await f.drain_turn(stream)
        tail = await f.drain_turn(stream)   # __aexit__ ended the stream
        return first, auto, second, tail
    first, auto, second, tail = asyncio.run(run())
    assert [type(m).__name__ for m in first] == ["StreamEvent", "ResultMessage"]
    assert auto[0].event["delta"]["text"] == "bg" and isinstance(auto[-1], ResultMessage)
    assert [type(m).__name__ for m in second] == ["StreamEvent", "ResultMessage"]
    assert tail == []

def test_interrupt_drops_the_rest_and_flushes_a_result():
    """Like the CLI: an interrupt abandons the in-flight turn's remaining messages and
    still closes the turn with a (synthetic) ResultMessage."""
    msgs = [f.stream_text("a"), f.stream_text("b"), f.stream_text("c"), f.result(uuid="r")]
    async def run():
        c = f.FakeSDKClient(msgs, startup_frames=[]); seen = []
        async with c:
            stream = c.receive_messages()
            await c.query("x")
            async for m in stream:
                seen.append(m)
                if len(seen) == 1:
                    await c.interrupt()   # interrupt after first
                if isinstance(m, ResultMessage):
                    break
        return seen
    seen = asyncio.run(run())
    assert [type(m).__name__ for m in seen] == ["StreamEvent", "ResultMessage"]
    assert seen[-1].uuid == "interrupted"
