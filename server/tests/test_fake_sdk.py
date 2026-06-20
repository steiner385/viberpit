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
        seen = []
        c = f.FakeSDKClient(msgs)
        assert isinstance(c, SDKClientProtocol)
        async with c:
            await c.query("hi")
            assert c.sent == ["hi"]
            async for m in c.receive_response():
                seen.append(type(m).__name__)
        return seen
    assert asyncio.run(run()) == ["SystemMessage", "StreamEvent", "StreamEvent", "AssistantMessage", "ResultMessage"]

def test_interrupt_stops_replay():
    msgs = [f.stream_text("a"), f.stream_text("b"), f.stream_text("c")]
    async def run():
        c = f.FakeSDKClient(msgs); seen = []
        async with c:
            await c.query("x")
            async for m in c.receive_response():
                seen.append(m); await c.interrupt()   # interrupt after first
        return seen
    assert len(asyncio.run(run())) == 1
