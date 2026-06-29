from __future__ import annotations
import pytest
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions import events as ev
from server.tests.fakes.fake_sdk import FakeSDKClient


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
    await d._on_pre_compact()
    out = []
    while not d._out.empty():
        out.append(d._out.get_nowait())
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
    await d.start()
    d._turn_active = True
    await d._on_pre_compact()
    while not d._out.empty():             # drain the active=True + status events
        d._out.get_nowait()

    await d.interrupt()

    out = []
    while not d._out.empty():
        out.append(d._out.get_nowait())
    cond_off = [e for e in out if isinstance(e, ev.Condensing) and not e.active]
    assert cond_off, "interrupt must emit Condensing(active=False)"
