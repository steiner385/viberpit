import asyncio

from claude_agent_sdk import TextBlock

from server.sessions.driver import SessionDriver
from server.sessions.events import TurnEnd
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f


def _factory(msgs):
    def make(cwd, config_dir, can_use_tool, mode, resume):
        return FakeSDKClient(msgs, on_can_use_tool=can_use_tool)
    return make


def test_status_progresses_in_progress_then_done():
    msgs = [f.sys_init("sess-1"), f.stream_start(uuid="t1"),
            f.stream_text("hi", uuid="t1"),
            f.assistant(TextBlock(text="hi"), uuid="t1"),
            f.result(session_id="sess-1", uuid="t1")]
    captured: list[str] = []

    async def on_status(sid, status):
        captured.append(status)

    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(),
                          identity="me", on_status=on_status)
        await d.start()
        await d.send_prompt("hello")
        async for e in d.events():
            if isinstance(e, TurnEnd):
                break
        # Allow the drain's finally block (which emits the terminal status) to run.
        if d._pump:
            await d._pump
        await d.aclose()
        return captured

    seen = asyncio.run(run())
    assert "in_progress" in seen
    assert seen[-1] == "done"
