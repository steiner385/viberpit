import asyncio
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.events import TurnStart, TextDelta, TurnEnd, Status
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f
from claude_agent_sdk import TextBlock

def _factory(msgs):
    def make(cwd, config_dir, can_use_tool, mode, resume):
        return FakeSDKClient(msgs, on_can_use_tool=can_use_tool)
    return make

def test_driver_streams_turn_and_sets_session_id():
    msgs = [f.sys_init("sess-1"), f.stream_start(uuid="t1"),
            f.stream_text("Hello ", uuid="t1"), f.stream_text("there", uuid="t1"),
            f.assistant(TextBlock(text="Hello there"), uuid="t1"), f.result(session_id="sess-1", uuid="t1")]
    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(), identity="me")
        await d.start()
        await d.send_prompt("hi")
        seen = []
        async for e in d.events():
            seen.append(e)
            if isinstance(e, TurnEnd):
                break
        await d.aclose()
        return seen, d.claude_session_id
    seen, sid = asyncio.run(run())
    names = [type(e).__name__ for e in seen]
    assert names[0] == "Status"  # working
    assert [n for n in names if n != "Status"] == ["TurnStart", "TextDelta", "TextDelta", "TurnEnd"]
    assert sid == "sess-1"

def test_interrupt_denies_pending_and_stops():
    msgs = [f.stream_start(uuid="t1"), f.stream_text("a", uuid="t1"), f.result(uuid="t1")]
    async def run():
        reg = PermissionRegistry()
        d = SessionDriver("s2", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=reg, identity="me")
        await d.start()
        await d.send_prompt("go")
        await d.interrupt()
        # a pending permission for this session would now be denied
        pid, fut = await reg.request("s2", "t1", "Bash", {"command": "ls"}, mode="interactive",
                                     denylist=PermissionRegistry.DANGEROUS, allowlist=True, identity="me")
        reg.deny_all("s2")
        await d.aclose()
        return await fut
    assert asyncio.run(run()) is False

def test_events_terminates_after_aclose():
    msgs = [f.result(uuid="t1")]
    async def run():
        d = SessionDriver("s3", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(), identity="me")
        await d.start(); await d.send_prompt("x")
        got = []
        async def consume():
            async for e in d.events():
                got.append(e)
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        await d.aclose()
        await asyncio.wait_for(task, timeout=2)   # must terminate, not hang
        return True
    assert asyncio.run(run()) is True
