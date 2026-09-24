"""The watchdog can *measure* a stall after it ends, but only a stack dump taken
*during* the block names the culprit. The tracer's own thread runs outside the
loop, so it fires even while the loop is blocked; the watchdog drives it as a
deadman switch — re-armed on every healthy tick, it expires only when the loop
stops scheduling coroutines.

See issue #762: the daemon blocks its loop for up to 130s and logs nothing about
where, because the async watchdog is itself unschedulable during the block. The
tracer was reimplemented in #792 on a plain ``threading.Thread`` +
``sys._current_frames()`` after the original ``faulthandler`` dumper segfaulted
the daemon by walking frames from a GIL-free C thread.
"""
import asyncio
import contextlib
import tempfile
import time

import pytest

from server.obs.loop_lag import LoopLagWatchdog, ThreadStallTracer


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


@pytest.mark.slow
def test_thread_tracer_dumps_a_stack_while_the_thread_is_blocked():
    """The real tracer must capture a stack *during* a synchronous block.

    ``time.sleep`` releases the GIL, so the tracer thread is schedulable and
    snapshots the main thread's frame — exactly the sync-sqlite/blocking-socket
    case the daemon actually hits.
    """
    with tempfile.NamedTemporaryFile("w+", suffix=".txt") as f:
        tracer = ThreadStallTracer(file=f)
        tracer.arm(0.1)
        # Block this thread longer than the timeout — exactly a sync sqlite read.
        time.sleep(0.4)
        tracer.cancel()
        f.seek(0)
        dumped = f.read()

    assert "loop_lag stall dump" in dumped, (
        f"the tracer should have written a stall dump: {dumped!r}"
    )
    assert "test_thread_tracer_dumps_a_stack" in dumped, (
        "the dump must name the blocked frame so the culprit is identifiable"
    )


@pytest.mark.slow
def test_thread_tracer_fires_only_once_until_rearmed():
    """``repeat=False`` semantics: a single stall yields exactly one dump."""
    with tempfile.NamedTemporaryFile("w+", suffix=".txt") as f:
        tracer = ThreadStallTracer(file=f)
        tracer.arm(0.1)
        time.sleep(0.5)  # well past the deadline, but no re-arm
        tracer.cancel()
        f.seek(0)
        dumped = f.read()

    assert dumped.count("loop_lag stall dump") == 1, (
        f"one stall must produce one dump, not repeat: {dumped!r}"
    )


def test_thread_tracer_rearm_and_idle_cancel_are_safe():
    """Re-arming resets the deadline; cancelling when idle is a harmless no-op."""
    with tempfile.NamedTemporaryFile("w+", suffix=".txt") as f:
        tracer = ThreadStallTracer(file=f)
        tracer.cancel()          # never armed — must not raise
        tracer.arm(10.0)
        tracer.arm(10.0)         # reset before it can fire
        tracer.cancel()          # disarm (and stop the thread) before the 10s elapses
        f.seek(0)
        assert f.read() == "", "no dump should be written when the deadline never passes"


@pytest.mark.slow
def test_thread_tracer_can_be_rearmed_after_cancel():
    """cancel() stops the thread; a later arm() must transparently restart it."""
    with tempfile.NamedTemporaryFile("w+", suffix=".txt") as f:
        tracer = ThreadStallTracer(file=f)
        tracer.arm(10.0)
        tracer.cancel()          # stops the worker thread
        tracer.arm(0.1)          # must spin a fresh thread and fire
        time.sleep(0.4)
        tracer.cancel()
        f.seek(0)
        assert "loop_lag stall dump" in f.read(), (
            "arm() after cancel() must revive the tracer"
        )


# --- production wiring: the dump is on by default (#1913) --------------------
# Opt-in meant it never ran: dobby logged 38s stalls for weeks with no stack to
# name the culprit, because nobody had set the knob. Both processes build the
# watchdog through one factory so the default cannot drift between them.

def test_watchdog_from_env_arms_the_stall_dump_by_default(monkeypatch):
    from server.obs.loop_lag import DEFAULT_STALL_DUMP_S, watchdog_from_env

    monkeypatch.delenv("VIBERPIT_LOOP_STALL_DUMP_S", raising=False)
    monkeypatch.delenv("VIBERPIT_LOOP_STALL_DUMP_PATH", raising=False)
    wd = watchdog_from_env(_tracer=_RecordingTracer())
    assert DEFAULT_STALL_DUMP_S > 0
    assert wd.dump_after == DEFAULT_STALL_DUMP_S


def test_watchdog_from_env_zero_is_the_off_switch(monkeypatch):
    from server.obs.loop_lag import watchdog_from_env

    monkeypatch.setenv("VIBERPIT_LOOP_STALL_DUMP_S", "0")
    wd = watchdog_from_env(_tracer=_RecordingTracer())
    assert wd.dump_after is None
    assert wd._tracer is None


def test_watchdog_from_env_honours_an_explicit_threshold_and_path(monkeypatch, tmp_path):
    from server.obs.loop_lag import watchdog_from_env

    dump = tmp_path / "stalls.txt"
    monkeypatch.setenv("VIBERPIT_LOOP_STALL_DUMP_S", "12.5")
    monkeypatch.setenv("VIBERPIT_LOOP_STALL_DUMP_PATH", str(dump))
    wd = watchdog_from_env()
    try:
        assert wd.dump_after == 12.5
        assert isinstance(wd._tracer, ThreadStallTracer)
        assert wd._tracer._file.name == str(dump)
    finally:
        wd._tracer._file.close()


def test_watchdog_from_env_warns_only_past_the_dump_threshold_by_default(monkeypatch):
    """A filed stall warning has a stack dump beside it (#1514)."""
    from server.obs.loop_lag import DEFAULT_STALL_DUMP_S, watchdog_from_env

    monkeypatch.delenv("VIBERPIT_LOOP_LAG_WARN_S", raising=False)
    monkeypatch.delenv("VIBERPIT_LOOP_STALL_DUMP_S", raising=False)
    wd = watchdog_from_env(_tracer=_RecordingTracer())
    assert wd.alert_after == DEFAULT_STALL_DUMP_S

    monkeypatch.setenv("VIBERPIT_LOOP_LAG_WARN_S", "2")
    assert watchdog_from_env(_tracer=_RecordingTracer()).alert_after == 2.0
