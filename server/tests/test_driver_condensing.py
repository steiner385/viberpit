from __future__ import annotations
import asyncio
import pytest
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions import events as ev
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes.fake_sdk import PerQueryScript


def _driver(**kw):
    def factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return FakeSDKClient([], on_can_use_tool=can_use_tool)
    clock = {"t": 100.0}
    d = SessionDriver("sid", "alpha", "/tmp", "/tmp/cfg", mode="default",
                      sdk_factory=factory, registry=PermissionRegistry(),
                      identity="wstest@example.com", clock=lambda: clock["t"], **kw)
    return d, clock


@pytest.mark.asyncio
async def test_pending_foreground_prompt_only_before_output():
    d, _ = _driver()
    await d.start()
    assert d.pending_foreground_prompt() is None      # no turn yet
    await d._start_turn("do the thing")
    assert d.pending_foreground_prompt() == "do the thing"   # live turn, no output yet
    d._turn_produced_output = True
    assert d.pending_foreground_prompt() is None       # produced output → unsafe to resend


@pytest.mark.asyncio
async def test_pre_compact_emits_estimate():
    async def est():
        return 8000.0
    d, _ = _driver(get_condense_estimate=est)
    out_q: asyncio.Queue = asyncio.Queue()
    d._subscribers.add(out_q)
    await d._on_pre_compact()
    out = []
    while not out_q.empty():
        out.append(out_q.get_nowait())
    cond = [e for e in out if isinstance(e, ev.Condensing) and e.active]
    assert cond and cond[0].expected_ms == 8000.0


@pytest.mark.asyncio
async def test_boundary_records_clamped_elapsed():
    recorded = []
    async def rec(ms): recorded.append(ms)
    d, clock = _driver(on_record_condense_ms=rec)
    await d._on_pre_compact()           # stamps started_at at t=100
    clock["t"] = 105.0                  # 5s later
    # simulate the drain consuming the translator's boundary event:
    await d._consume_condensing(ev.Condensing(active=False))
    assert recorded == [5000.0]


@pytest.mark.asyncio
async def test_backstop_without_boundary_records_nothing():
    recorded = []
    async def rec(ms): recorded.append(ms)
    d, clock = _driver(on_record_condense_ms=rec)
    await d._on_pre_compact()
    clock["t"] = 110.0
    d._condensing = False
    d._condense_started_at = None       # backstop clears without recording
    assert recorded == []


@pytest.mark.asyncio
async def test_assistant_output_clears_condensing_when_boundary_missing():
    """Compaction that produces no ``compact_boundary`` must still be cleared by the
    post-compaction assistant output. ``derive_status`` ranks ``condensing`` ABOVE
    ``turn_active``, so if real text deltas stream in while ``_condensing`` is still
    True, the status stays 'condensing' and the UI shows the Condensing… bar while the
    reply visibly streams. This mirrors the ``_retrying`` flag, which any real event
    already clears in the drain loop."""
    class _PreCompactClient(PerQueryScript, FakeSDKClient):
        """Fires the SDK PreCompact hook, then streams a reply with NO compact_boundary."""
        def __init__(self, msgs, on_pre_compact, **kw):
            super().__init__(msgs, **kw)
            self._pc = on_pre_compact
        async def receive_response(self):
            await self._pc()                       # compaction begins mid-turn
            for m in self._messages:
                yield m

    msgs = [f.stream_start(uuid="t1"), f.stream_text("post-compaction reply", uuid="t1"),
            f.result(uuid="t1")]

    def factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return _PreCompactClient(msgs, on_pre_compact, on_can_use_tool=can_use_tool)

    d = SessionDriver("sid", "alpha", "/tmp", "/tmp/cfg", mode="default",
                      sdk_factory=factory, registry=PermissionRegistry(), identity="x")
    await d.start()
    await d.send_prompt("hi")
    seen = []
    async for e in d.events():
        seen.append(e)
        if isinstance(e, ev.TurnEnd):
            break
    await d.aclose()

    i_active = next(i for i, x in enumerate(seen) if isinstance(x, ev.Condensing) and x.active)
    i_text = next(i for i, x in enumerate(seen) if isinstance(x, ev.TextDelta))
    i_end = next(i for i, x in enumerate(seen) if isinstance(x, ev.TurnEnd))
    # The post-compaction reply (no compact_boundary) must itself clear condensing:
    # a Condensing(active=False) lands once content starts, before the turn ends.
    cleared_at = [i for i, x in enumerate(seen)
                  if isinstance(x, ev.Condensing) and not x.active and i_active < i < i_end]
    assert cleared_at, "post-compaction output must clear condensing before the turn ends"
    assert cleared_at[0] <= i_text, "condensing must clear no later than the first content delta"


@pytest.mark.asyncio
async def test_interrupt_clears_condensing_flag():
    """A stall-watchdog interrupt while a turn is wedged inside compaction must clear
    the condensing flag. derive_status ranks ``condensing`` ABOVE ``turn_active``, so
    leaving the flag set pins the persisted status to 'condensing' forever even though
    interrupt() set turn_active=False — the bug that strands a card on "Condensing…"."""
    statuses: list[str] = []
    async def on_status(_sid, status): statuses.append(status)
    d, _ = _driver(on_status=on_status)
    await d.start()
    d._turn_active = True
    await d._on_pre_compact()             # compaction starts: condensing=True
    assert d._condensing is True
    statuses.clear()

    await d.interrupt()

    assert d._condensing is False
    assert d._condense_started_at is None
    assert "condensing" not in statuses   # interrupt must not re-persist condensing


@pytest.mark.asyncio
async def test_interrupt_emits_condensing_inactive_to_clear_overlay():
    """interrupt() during compaction must also forward a Condensing(active=False) event
    so an open session view clears its progress overlay (mirrors _consume_condensing)."""
    d, _ = _driver()
    out_q: asyncio.Queue = asyncio.Queue()
    d._subscribers.add(out_q)
    await d.start()
    d._turn_active = True
    await d._on_pre_compact()
    while not out_q.empty():             # drain the active=True + status events
        out_q.get_nowait()

    await d.interrupt()

    out = []
    while not out_q.empty():
        out.append(out_q.get_nowait())
    cond_off = [e for e in out if isinstance(e, ev.Condensing) and not e.active]
    assert cond_off, "interrupt must emit Condensing(active=False)"
