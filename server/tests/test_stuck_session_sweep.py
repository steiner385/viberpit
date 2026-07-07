"""Tests for the periodic stuck-session sweep.

The rotation monitor is event-driven and only fires on account-degradation events.
Sessions paused as needs_attention (all_accounts_busy, crash_cap, etc.) have no
auto-recovery path — this sweep provides it by retrying _rotate_away periodically.
"""
import asyncio
import pytest

from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


def _make_orch():
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o._clock = lambda: 1000.0
    o.calls = []

    async def spy_rotate(record, reason):
        o.calls.append(("rotate", record.id, reason))

    async def spy_pause(record, reason, earliest_reset=None):
        o.calls.append(("pause", record.id, reason))

    o._rotate_away = spy_rotate
    o._pause_session = spy_pause
    return o


def _rec(o, session_id="s1", account="acct", *, status="needs_attention"):
    rec = SessionRecord(id=session_id, name="n", account=account,
                        claude_session_id=None, cols=80, rows=24, cwd="/tmp")
    rec.status = status
    o._records[session_id] = rec
    return rec


# ---------------------------------------------------------------------------
# _sweep_needs_attention: core sweep logic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sweep_rotates_needs_attention_session():
    """A needs_attention session must get a _rotate_away call from the sweep."""
    o = _make_orch()
    _rec(o, status="needs_attention")

    await o._sweep_needs_attention()
    await asyncio.sleep(0)  # let spawned rotate task run

    assert ("rotate", "s1", "stuck_session_sweep") in o.calls


@pytest.mark.asyncio
async def test_sweep_skips_idle_session():
    """Idle (healthy) sessions must not be touched by the sweep."""
    o = _make_orch()
    _rec(o, status="idle")

    await o._sweep_needs_attention()

    assert o.calls == []


@pytest.mark.asyncio
async def test_sweep_skips_ended_session():
    """Ended/archived sessions must not be rotated."""
    o = _make_orch()
    _rec(o, "s-ended", status="ended")
    _rec(o, "s-archived", status="archived")

    await o._sweep_needs_attention()

    assert o.calls == []


@pytest.mark.asyncio
async def test_sweep_handles_multiple_sessions():
    """Every needs_attention session in _records gets its own rotation attempt."""
    o = _make_orch()
    _rec(o, "s1", status="needs_attention")
    _rec(o, "s2", status="idle")
    _rec(o, "s3", status="needs_attention")

    await o._sweep_needs_attention()
    await asyncio.sleep(0)  # let spawned rotate tasks run

    rotated_ids = {c[1] for c in o.calls if c[0] == "rotate"}
    assert rotated_ids == {"s1", "s3"}


@pytest.mark.asyncio
async def test_sweep_loop_skips_work_during_shutdown():
    """_stuck_session_sweep must not call _sweep_needs_attention when shutting down."""
    o = _make_orch()
    _rec(o, status="needs_attention")
    o._shutting_down = True

    swept = []
    original = o._sweep_needs_attention

    async def spy_sweep():
        swept.append(True)
        await original()

    o._sweep_needs_attention = spy_sweep

    # Run the loop but cancel it after the first sleep would complete.
    task = asyncio.create_task(o._stuck_session_sweep())
    await asyncio.sleep(0)   # let the loop enter the if-check
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert swept == [], "sweep must not run during shutdown"


@pytest.mark.asyncio
async def test_sweep_loop_survives_rotate_error():
    """An exception from _sweep_needs_attention must not crash the loop."""
    o = _make_orch()
    o._shutting_down = False
    calls = []

    async def failing_sweep():
        calls.append(True)
        raise RuntimeError("boom")

    o._sweep_needs_attention = failing_sweep

    # Patch sleep so the loop ticks once without waiting.
    sleep_count = 0

    async def fast_sleep(seconds):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 2:
            raise asyncio.CancelledError

    task = asyncio.create_task(o._stuck_session_sweep())
    # Give the loop one iteration to run the failing sweep then sleep.
    await asyncio.sleep(0)   # enter loop body
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The loop ran (calls has one entry) and did not raise despite the error.
    assert calls, "sweep was never called"
