"""The watchdog can *measure* a stall after it ends, but only a stack dump taken
*during* the block names the culprit. faulthandler's timer thread fires even while
the loop is blocked; the watchdog drives it as a deadman switch — re-armed on every
healthy tick, it expires only when the loop stops scheduling coroutines.

See issue #762: the daemon blocks its loop for up to 130s and logs nothing about
where, because the async watchdog is itself unschedulable during the block.
"""
import asyncio
import contextlib
import tempfile
import time

import pytest

from server.obs.loop_lag import FaultHandlerStallTracer, LoopLagWatchdog


class _RecordingTracer:
    """Fake deadman switch: records arm/cancel calls instead of touching faulthandler."""

    def __init__(self) -> None:
        self.arms: list[float] = []
        self.cancels = 0

    def arm(self, timeout: float) -> None:
        self.arms.append(timeout)

    def cancel(self) -> None:
        self.cancels += 1


def test_dump_after_defaults_to_disabled_so_existing_wiring_is_unchanged():
    """Constructing without dump_after must not arm any tracer (back-compat)."""
    wd = LoopLagWatchdog()
    assert wd.dump_after is None
    assert wd._tracer is None


def test_a_non_positive_dump_after_disables_the_tracer():
    wd = LoopLagWatchdog(dump_after=0.0, _tracer=_RecordingTracer())
    assert wd.dump_after is None


@pytest.mark.asyncio
async def test_healthy_ticks_rearm_the_deadman_switch_and_shutdown_disarms_it():
    tracer = _RecordingTracer()
    wd = LoopLagWatchdog(interval=0.02, dump_after=5.0, _tracer=tracer)
    task = asyncio.create_task(wd.run())
    await asyncio.sleep(0.2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # Re-armed once before the first sleep and again after every healthy tick.
    assert len(tracer.arms) >= 3, f"expected repeated re-arms, got {tracer.arms!r}"
    assert set(tracer.arms) == {5.0}, "each re-arm must use the configured dump_after"
    # The deadman switch must be disarmed on shutdown so a stopped daemon never dumps.
    assert tracer.cancels >= 1


@pytest.mark.asyncio
async def test_no_tracer_calls_when_dump_after_is_unset():
    """The measuring path must keep working with the tracer entirely absent."""
    wd = LoopLagWatchdog(interval=0.02)  # dump_after defaults to None
    task = asyncio.create_task(wd.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    # Nothing to assert beyond "it did not raise" — there is no tracer to arm.
    assert wd._tracer is None


def test_faulthandler_tracer_dumps_a_stack_while_the_thread_is_blocked():
    """The real tracer must capture a stack *during* a synchronous block."""
    with tempfile.NamedTemporaryFile("w+", suffix=".txt") as f:
        tracer = FaultHandlerStallTracer(file=f)
        tracer.arm(0.1)
        # Block the (only) thread longer than the timeout — exactly a sync sqlite read.
        time.sleep(0.4)
        tracer.cancel()
        f.seek(0)
        dumped = f.read()

    assert "Timeout" in dumped, f"faulthandler should have written a timeout dump: {dumped!r}"
    assert "test_faulthandler_tracer_dumps_a_stack" in dumped, (
        "the dump must name the blocked frame so the culprit is identifiable"
    )


def test_faulthandler_tracer_rearm_and_idle_cancel_are_safe():
    """Re-arming resets the timer; cancelling when idle is a harmless no-op."""
    with tempfile.NamedTemporaryFile("w+", suffix=".txt") as f:
        tracer = FaultHandlerStallTracer(file=f)
        tracer.cancel()          # never armed — must not raise
        tracer.arm(10.0)
        tracer.arm(10.0)         # reset before it can fire
        tracer.cancel()          # disarm before the 10s elapses
        f.seek(0)
        assert f.read() == "", "no dump should be written when the timer never expires"
