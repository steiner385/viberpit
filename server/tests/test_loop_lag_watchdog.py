"""The watchdog must notice when the event loop stops being scheduled."""
import asyncio
import contextlib
import time

import pytest

from server.obs.loop_lag import LoopLagWatchdog


class _CapturingLog:
    def __init__(self) -> None:
        self.warnings: list[tuple] = []

    def warning(self, msg, *args, **kwargs) -> None:
        self.warnings.append((msg, args))


@pytest.mark.asyncio
async def test_reports_nothing_when_the_loop_is_healthy():
    log = _CapturingLog()
    wd = LoopLagWatchdog(interval=0.05, warn_after=0.5, _log=log)
    task = asyncio.create_task(wd.run())
    await asyncio.sleep(0.3)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert log.warnings == []
    assert wd.peak_lag_s < 0.5


@pytest.mark.asyncio
async def test_warns_when_the_loop_is_blocked():
    log = _CapturingLog()
    wd = LoopLagWatchdog(interval=0.05, warn_after=0.2, _log=log)
    task = asyncio.create_task(wd.run())
    await asyncio.sleep(0.05)

    time.sleep(0.6)          # block the loop exactly as a sync sqlite read would
    await asyncio.sleep(0.1)  # let the watchdog observe the overshoot
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert log.warnings, "a 600ms block must be reported"
    assert wd.peak_lag_s >= 0.2


class _FakeClock:
    """Deterministic monotonic stand-in so throttle windows need no real sleeps."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_repeated_stalls_log_once_per_window():
    """A saturated loop overshoots every tick; it must not emit a line every tick."""
    log = _CapturingLog()
    clock = _FakeClock()
    wd = LoopLagWatchdog(interval=1.0, warn_after=0.5, warn_every=60.0,
                         _log=log, _clock=clock)

    for _ in range(30):
        clock.advance(1.0)
        wd._note_lag(0.9)

    assert len(log.warnings) == 1, f"expected one line, got {len(log.warnings)}"
    # peak_lag_s must stay accurate even for the suppressed ticks.
    assert wd.peak_lag_s == pytest.approx(0.9)


def test_suppressed_stalls_are_reported_in_the_next_warning():
    """Nothing is lost: the next line that gets through carries the swallowed count."""
    log = _CapturingLog()
    clock = _FakeClock()
    wd = LoopLagWatchdog(interval=1.0, warn_after=0.5, warn_every=10.0,
                         _log=log, _clock=clock)

    wd._note_lag(0.6)                # first stall — logs immediately
    for lag in (0.7, 2.5, 0.8):      # inside the quiet window — suppressed
        clock.advance(1.0)
        wd._note_lag(lag)
    clock.advance(30.0)              # window elapsed
    wd._note_lag(0.55)

    assert len(log.warnings) == 2
    msg, args = log.warnings[1]
    assert "suppressed" in msg
    assert 3 in args, f"suppressed count missing from {args!r}"
    assert pytest.approx(2.5) in args, f"suppressed peak missing from {args!r}"
    # The worst lag overall is the suppressed 2.5s, not the logged 0.6s.
    assert wd.peak_lag_s == pytest.approx(2.5)


def test_a_lag_below_the_threshold_never_arms_the_throttle():
    """A quiet loop must not consume the first-warning slot with sub-threshold ticks."""
    log = _CapturingLog()
    clock = _FakeClock()
    wd = LoopLagWatchdog(interval=1.0, warn_after=0.5, warn_every=60.0,
                         _log=log, _clock=clock)

    for _ in range(5):
        clock.advance(1.0)
        wd._note_lag(0.01)
    assert log.warnings == []

    wd._note_lag(0.9)
    assert len(log.warnings) == 1
    assert "suppressed" not in log.warnings[0][0]


@pytest.mark.asyncio
async def test_two_real_stalls_produce_one_line():
    """End-to-end through run(): two blocks inside one window → a single warning."""
    log = _CapturingLog()
    wd = LoopLagWatchdog(interval=0.05, warn_after=0.2, warn_every=60.0, _log=log)
    task = asyncio.create_task(wd.run())
    await asyncio.sleep(0.05)

    for _ in range(2):
        time.sleep(0.4)       # block the loop exactly as a sync sqlite read would
        await asyncio.sleep(0.1)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert len(log.warnings) == 1, f"expected one line, got {log.warnings!r}"
    assert wd.peak_lag_s >= 0.2
