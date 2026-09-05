"""ClaudeAgentDriver.send_prompt(..., interrupt=True) — "interrupt and post".

A prompt sent while a turn is live normally joins the FIFO queue and waits. With
``interrupt=True`` the driver stops the live turn first and runs the new prompt as
the very NEXT turn — ahead of anything already queued, which is kept (a plain Stop
still abandons the queue; this is the "say something now" path, not "forget it all").
"""
from __future__ import annotations

import asyncio

from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.events import QueueUpdate
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient


class _StuckThenQuick(FakeSDKClient):
    """The first query opens a turn that never closes on its own (the CLI mid-work);
    only ``interrupt()`` — which, like the CLI, flushes a synthetic result — ends it.
    Every later query completes at once."""
    async def query(self, prompt: str):
        self.sent.append(prompt)
        if len(self.sent) == 1:
            self._deliver([f.stream_start(uuid="t1")])
        else:
            self._deliver([f.result(uuid=f"r{len(self.sent)}")])


def _make(sdk):
    def factory(cwd, cfg, can_use_tool, mode, resume, *, on_pre_compact=None):
        return sdk
    return ClaudeAgentDriver("ias", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                         sdk_factory=factory, registry=PermissionRegistry(), identity="me")


async def _settle(d: ClaudeAgentDriver, timeout: float = 3.0) -> None:
    """Wait until no prompt turn is running and the queue has drained."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        pump_done = d._pump is None or d._pump.done()
        if pump_done and not d._queue and not d._turn_active:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"driver did not settle: queue={d.queued()} turn_active={d._turn_active}")


def test_interrupt_and_send_stops_live_turn_and_runs_prompt_next():
    """While a turn is live, interrupt=True stops it and the new prompt runs next —
    ahead of a prompt that was already queued, which still runs afterwards."""
    sdk = _StuckThenQuick([])

    async def run():
        d = _make(sdk)
        await d.start()
        await d.send_prompt("first")
        await asyncio.sleep(0.01)               # let the pump pick up the live turn
        await d.send_prompt("later")            # plain send while busy → queued
        assert d.queued() == ["later"]
        await d.send_prompt("now", interrupt=True)
        await _settle(d)
        await d.aclose()

    asyncio.run(run())
    assert sdk.interrupted is True
    assert sdk.sent == ["first", "now", "later"]


def test_interrupt_and_send_while_idle_is_a_plain_send():
    """With no live turn there is nothing to stop: the prompt starts immediately and
    no interrupt control request is sent to the CLI."""
    sdk = FakeSDKClient([f.result(uuid="r1")])

    async def run():
        d = _make(sdk)
        await d.start()
        await d.send_prompt("hello", interrupt=True)
        await _settle(d)
        await d.aclose()

    asyncio.run(run())
    assert sdk.interrupted is False
    assert sdk.sent == ["hello"]


def test_interrupt_and_send_shows_prompt_at_queue_head_while_unwinding():
    """Between the interrupt and the old turn's pump settling, the queue strip must
    show the new prompt FIRST with the surviving queued prompt behind it — never an
    empty queue (which is what a plain Stop emits and would hide the message)."""
    sdk = _StuckThenQuick([])

    async def run():
        d = _make(sdk)
        out_q: asyncio.Queue = asyncio.Queue()
        d._subscribers.add(out_q)
        await d.start()
        await d.send_prompt("first")
        await asyncio.sleep(0.01)               # let the pump pick up the live turn
        await d.send_prompt("later")
        while not out_q.empty():
            out_q.get_nowait()
        await d.send_prompt("now", interrupt=True)
        seen = []
        while not out_q.empty():
            e = out_q.get_nowait()
            if isinstance(e, QueueUpdate):
                seen.append([item["text"] for item in e.items])
        await _settle(d)
        await d.aclose()
        return seen

    updates = asyncio.run(run())
    assert ["now", "later"] in updates, updates
    assert [] not in updates, "a Stop-style empty queue must not be emitted"
