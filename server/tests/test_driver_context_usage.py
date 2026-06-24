"""The persistent ctx% readout must reflect REAL current context occupancy, not
the per-turn usage aggregate (which sums cache reads across a turn's API calls and
pins the readout at 0%). The driver pulls the SDK's get_context_usage() — the same
data the /context command shows — once a turn settles and forwards it as a
ContextUsage event."""
from __future__ import annotations
import asyncio
from claude_agent_sdk import TextBlock
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions import events as ev
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f


def _factory(msgs, context_usage):
    def make(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return FakeSDKClient(msgs, on_can_use_tool=can_use_tool, context_usage=context_usage)
    return make


def _run_turn(msgs, context_usage):
    async def run():
        d = SessionDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs, context_usage),
                          registry=PermissionRegistry(), identity="me")
        await d.start()
        await d.send_prompt("hi")
        seen = []
        agen = d.events()
        # Drain until ContextUsage arrives or the stream goes quiet (idle timeout)
        # — the negative cases never emit one, so we can't block on it.
        while True:
            try:
                e = await asyncio.wait_for(agen.__anext__(), timeout=0.5)
            except (asyncio.TimeoutError, StopAsyncIteration):
                break
            seen.append(e)
            if isinstance(e, ev.ContextUsage):
                break
        await d.aclose()
        return seen
    return asyncio.run(run())


_MSGS = [f.sys_init("sess-1"), f.stream_start(uuid="t1"), f.stream_text("hi", uuid="t1"),
         f.assistant(TextBlock(text="hi"), uuid="t1"), f.result(session_id="sess-1", uuid="t1")]


def test_driver_emits_exact_context_usage_after_turn():
    cu = {"totalTokens": 65729, "maxTokens": 200000, "percentage": 32.9,
          "model": "claude-opus-4-8"}
    seen = _run_turn(_MSGS, cu)
    cue = next((e for e in seen if isinstance(e, ev.ContextUsage)), None)
    assert cue is not None, "driver did not emit a ContextUsage event after the turn"
    assert cue.used_tokens == 65729
    assert cue.max_tokens == 200000
    # Emitted only after the turn closes — never before TurnEnd.
    types = [type(e).__name__ for e in seen]
    assert types.index("TurnEnd") < types.index("ContextUsage")


def test_driver_skips_context_usage_when_sdk_returns_empty():
    # No live context data (e.g. fake/empty client) → no bogus 0-token event.
    seen = _run_turn(_MSGS, {})
    assert not any(isinstance(e, ev.ContextUsage) for e in seen)


def test_driver_skips_context_usage_when_max_tokens_missing():
    seen = _run_turn(_MSGS, {"totalTokens": 100})
    assert not any(isinstance(e, ev.ContextUsage) for e in seen)
