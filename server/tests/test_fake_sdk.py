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
    assert asyncio.run(run()) == ["SystemMessage", "StreamEvent", "StreamEvent", "AssistantMessage", "ResultMessage"]

def test_stream_is_persistent_and_replays_the_script_per_query():
    """One stream for the client's life: each query() delivers the script again, and
    inject() delivers a turn with NO query behind it (a CLI-initiated continuation)."""
    msgs = [f.stream_text("a"), f.result(uuid="r")]
    async def run():
        c = f.FakeSDKClient(msgs)
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
        c = f.FakeSDKClient(msgs); seen = []
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
