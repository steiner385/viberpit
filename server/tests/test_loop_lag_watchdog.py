"""The watchdog must notice when the event loop stops being scheduled."""
import asyncio
import contextlib
import logging
import time

import pytest

from server.obs.loop_lag import LoopLagWatchdog


class _CapturingLog:
    """Stands in for a Logger, recording the structured events it is asked to emit."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, level, msg, *args, extra=None, **kwargs) -> None:
        extra = extra or {}
        self.events.append((extra.get("event", msg), dict(extra.get("fields") or {})))

    @property
    def fields(self) -> list[dict]:
        return [f for _event, f in self.events]


@pytest.mark.asyncio
async def test_reports_nothing_when_the_loop_is_healthy():
    log = _CapturingLog()
    wd = LoopLagWatchdog(interval=0.05, warn_after=0.5, _log=log)
    task = asyncio.create_task(wd.run())
    await asyncio.sleep(0.3)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert log.events == []
    assert wd.peak_lag_s < 0.5


@pytest.mark.asyncio
@pytest.mark.slow
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

    assert log.events, "a 600ms block must be reported"
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

    assert len(log.events) == 1, f"expected one line, got {len(log.events)}"
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

    assert len(log.events) == 2
    event, fields = log.events[1]
    assert event == "loop_lag.stall"
    assert fields["suppressed"] == 3, f"suppressed count missing from {fields!r}"
    assert fields["suppressed_peak_s"] == pytest.approx(2.5), (
        f"suppressed peak missing from {fields!r}")
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
    assert log.events == []

    wd._note_lag(0.9)
    assert len(log.events) == 1
    assert log.fields[0]["suppressed"] == 0


@pytest.mark.asyncio
@pytest.mark.slow
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

    assert len(log.events) == 1, f"expected one line, got {log.events!r}"
    assert wd.peak_lag_s >= 0.2


def test_stall_records_are_one_condition_to_the_error_sink():
    """Eight measured lags, one filed condition (#1848 and its seven siblings).

    The watchdog's throttle only paces how often it reports; what the error sink
    counts as *one* condition is decided by the record's shape. When the lag was
    interpolated into the message it reached the fingerprint, so a week of one
    stall opened eight auto-error issues. Asserted through the real logging path
    because the bug lived in the handoff, not in either side alone.
    """
    from server.obs.error_signature import signature_of

    captured: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    log = logging.getLogger("server.obs.loop_lag")
    handler = _Sink()
    log.addHandler(handler)
    try:
        for lag in (0.78, 4.22, 1.12, 5.03, 5.21, 3.04, 3.63, 3.13):
            LoopLagWatchdog(interval=1.0, warn_after=0.5,
                            _clock=_FakeClock())._note_lag(lag)
    finally:
        log.removeHandler(handler)

    assert len(captured) == 8, f"expected one record per stall, got {len(captured)}"
    sigs = [signature_of(r, source="daemon", host="rocky") for r in captured]
    assert sigs[0].event == "loop_lag.stall"
    assert len({s.fingerprint for s in sigs}) == 1, "one stall split into many issues"
    # The measurement is still reported — it moved from the identity to the title.
    assert "lag_s" in sigs[0].sample_message
