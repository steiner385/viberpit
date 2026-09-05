"""aclose() must not abandon a turn that is wedged mid-tool-call.

If a session is torn down (deploy teardown, session end) while a tool_use is in
flight, hard-cancelling the drain kills the subprocess before the CLI writes its
synthetic ``[Request interrupted by user for tool use]`` result — leaving an
ORPHANED tool_use that dead-ends the next resume (see transcript_repair). aclose()
should interrupt the turn and give the CLI a bounded chance to flush that result.
"""
import asyncio

from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.events import ToolResult
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import PerQueryScript


class _WedgedToolClient(PerQueryScript):
    """Streams a tool_use then blocks until interrupt(), which — like the real CLI —
    flushes the synthetic interrupt tool_result and a terminal result, then ends."""

    def __init__(self, *, flush_on_interrupt=True):
        self._go = asyncio.Event()
        self._flush = flush_on_interrupt
        self.interrupted = False
        self.exited = False
        self.sent: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.exited = True
        return False

    async def query(self, prompt):
        self.sent.append(prompt)

    async def receive_response(self):
        yield f.tool_use("toolu_x", "Agent", {}, uuid="t1")
        await self._go.wait()                      # tool is "running"
        if self._flush:
            yield f.user_tool_result("toolu_x", "[Request interrupted by user for tool use]",
                                     is_error=True, uuid="t1")
            yield f.result(subtype="success", uuid="t1")

    async def interrupt(self):
        self.interrupted = True
        if self._flush:
            self._go.set()                         # let the stream flush + finish

    async def set_permission_mode(self, mode): pass
    async def set_model(self, model=None): pass
    async def get_server_info(self): return None
    async def get_context_usage(self): return {}


def _drive(client):
    async def run():
        d = ClaudeAgentDriver("s1", "primary", "/repo", "/cfg", mode="interactive",
                          sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
                          identity="me")
        d._close_grace = 0.2
        await d.start()
        await d.send_prompt("go")
        # wait until the turn is wedged inside the tool_use (drain blocked)
        for _ in range(100):
            await asyncio.sleep(0.005)
            if d._turn_active and client.sent:
                break
        seen: list = []
        # drain the event queue concurrently so flushed events are observable
        async def collect():
            async for e in d.events():
                seen.append(e)
        task = asyncio.create_task(collect())
        await d.aclose()
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return seen
    return asyncio.run(run())


def test_aclose_interrupts_and_flushes_synthetic_result_for_wedged_tool():
    client = _WedgedToolClient(flush_on_interrupt=True)
    seen = _drive(client)
    assert client.interrupted is True
    assert client.exited is True
    # the synthetic interrupt tool_result was flushed to the stream (orphan resolved)
    res = [e for e in seen if isinstance(e, ToolResult) and e.tool_id == "toolu_x"]
    assert res and res[0].ok is False


def test_aclose_still_closes_when_cli_never_flushes():
    # A wedged tool whose interrupt never produces a result must not hang aclose:
    # after the grace window it cancels the drain and tears down anyway.
    client = _WedgedToolClient(flush_on_interrupt=False)

    async def run():
        d = ClaudeAgentDriver("s1", "primary", "/repo", "/cfg", mode="interactive",
                          sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
                          identity="me")
        d._close_grace = 0.1
        await d.start()
        await d.send_prompt("go")
        for _ in range(100):
            await asyncio.sleep(0.005)
            if d._turn_active and client.sent:
                break
        await asyncio.wait_for(d.aclose(), timeout=2.0)   # must not hang
        return d

    d = asyncio.run(run())
    assert client.interrupted is True
    assert client.exited is True
    assert d._pump.cancelled() or d._pump.done()
