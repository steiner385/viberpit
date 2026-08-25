"""Event-loop lag watchdog.

Sleeps for a fixed interval and measures how much longer than that it actually
took. The overshoot is time the loop spent unable to schedule us — i.e. blocked
in synchronous work. RC1 (blocking sqlite3 on the loop) was invisible until an
external py-spy dump; this makes it self-reporting.

Warnings are rate-limited (``warn_every``). A saturated loop overshoots on every
tick, so an unthrottled watchdog would emit ~1 line/s (~86k lines/day) — and each
``log.warning`` is itself synchronous work on the already-stalled loop, so the
watchdog would amplify the problem it exists to report. A signal that noisy gets
tuned out. Nothing is lost: suppressed stalls are counted (with their worst lag)
and reported in the next warning that does get through, and ``peak_lag_s`` is
updated on every tick regardless of throttling.

Measuring the lag is only half the job: the coroutine that measures it is itself
unschedulable *during* a block, so by the time it wakes the culprit's frame is
gone (issue #762 — the daemon blocked for up to 130s and logged nothing about
where). ``FaultHandlerStallTracer`` closes that gap: ``faulthandler`` runs its
timer in a separate C thread that fires without the GIL or the loop, so it can
dump the stack of the thread stuck *inside* the blocking call. The watchdog
drives it as a deadman switch — re-armed on every healthy tick, it expires only
when the loop stops scheduling coroutines, which is exactly a stall.
"""
import asyncio
import faulthandler
import logging
import sys
import time

_default_log = logging.getLogger("server.obs.loop_lag")


class FaultHandlerStallTracer:
    """Deadman switch over ``faulthandler.dump_traceback_later``.

    ``arm(timeout)`` schedules a full all-thread stack dump ``timeout`` seconds
    out; the watchdog re-arms it on every healthy tick, so while the loop keeps
    scheduling coroutines the deadline is pushed forward and the dump never
    fires. When the loop blocks synchronously past ``timeout``, faulthandler's
    own watchdog thread — which needs neither the GIL nor the event loop — fires
    and writes the stack of the blocked thread, naming the culprit line that the
    async watchdog can only measure after the fact.

    The dump goes to ``file`` (default ``sys.stderr``, i.e. the journal). A
    second ``arm`` resets the pending timer (documented faulthandler behaviour),
    so re-arming is just calling it again.
    """

    def __init__(self, file=None) -> None:
        self._file = file if file is not None else sys.stderr
        self._armed = False

    def arm(self, timeout: float) -> None:
        faulthandler.dump_traceback_later(timeout, repeat=False, file=self._file)
        self._armed = True

    def cancel(self) -> None:
        if not self._armed:
            return
        faulthandler.cancel_dump_traceback_later()
        self._armed = False


def _build_stall_tracer(dump_path: str | None):
    """Real tracer targeting ``dump_path`` (append) or ``sys.stderr`` (journal).

    The file handle lives for the process lifetime — a daemon-scoped watchdog is
    never torn down before exit, so we deliberately never close it.
    """
    if dump_path:
        # Line-buffered append: faulthandler writes via the raw fd, but text mode
        # gives us a real fileno and keeps the path readable between dumps.
        handle = open(dump_path, "a", buffering=1)
        return FaultHandlerStallTracer(file=handle)
    return FaultHandlerStallTracer(file=sys.stderr)


class LoopLagWatchdog:
    def __init__(
        self,
        interval: float = 1.0,
        warn_after: float = 0.5,
        warn_every: float = 60.0,
        dump_after: float | None = None,
        dump_path: str | None = None,
        _log=None,
        _clock=None,
        _tracer=None,
    ) -> None:
        self.interval = interval
        self.warn_after = warn_after
        self.warn_every = warn_every
        self.peak_lag_s = 0.0
        self._log = _log if _log is not None else _default_log
        self._clock = _clock if _clock is not None else time.monotonic
        # Stack-dump-on-stall (issue #762). Disabled unless a positive threshold
        # is given, so existing call sites and tests are unaffected. A tracer may
        # be injected for tests; otherwise a real faulthandler one is built lazily
        # only when the feature is actually on.
        self.dump_after = dump_after if (dump_after and dump_after > 0) else None
        if self.dump_after is not None:
            self._tracer = _tracer if _tracer is not None else _build_stall_tracer(dump_path)
        else:
            self._tracer = None
        # Throttle state: when we last emitted, and what we swallowed since.
        self._last_warn_at: float | None = None
        self._suppressed = 0
        self._suppressed_peak = 0.0

    def _arm_tracer(self) -> None:
        """(Re)arm the deadman switch for the next interval, if enabled."""
        if self.dump_after is not None and self._tracer is not None:
            self._tracer.arm(self.dump_after)

    def _disarm_tracer(self) -> None:
        if self._tracer is not None:
            try:
                self._tracer.cancel()
            except Exception:  # pragma: no cover - disarm must never mask shutdown
                pass

    def _note_lag(self, lag: float) -> None:
        """Record one measured overshoot and warn about it, rate-limited.

        Split out of ``run`` so the throttle is testable without real sleeps.
        """
        if lag > self.peak_lag_s:
            self.peak_lag_s = lag
        if lag < self.warn_after:
            return

        now = self._clock()
        if self._last_warn_at is not None and (now - self._last_warn_at) < self.warn_every:
            # Inside the quiet window: count it, remember the worst, stay silent.
            self._suppressed += 1
            if lag > self._suppressed_peak:
                self._suppressed_peak = lag
            return

        if self._suppressed:
            self._log.warning(
                "loop_lag: event loop blocked for %.2fs (interval=%.2fs); "
                "%d further stall(s) suppressed in the last %.0fs, worst %.2fs",
                lag, self.interval, self._suppressed,
                now - (self._last_warn_at or now), self._suppressed_peak,
            )
        else:
            self._log.warning(
                "loop_lag: event loop blocked for %.2fs (interval=%.2fs)",
                lag, self.interval,
            )
        self._last_warn_at = now
        self._suppressed = 0
        self._suppressed_peak = 0.0

    async def run(self) -> None:
        self._arm_tracer()
        try:
            while True:
                started = self._clock()
                await asyncio.sleep(self.interval)
                lag = (self._clock() - started) - self.interval
                self._note_lag(lag)
                # Re-arm the deadman switch: this tick proves the loop is alive,
                # so push the dump deadline out. If the *next* sleep is swallowed
                # by a block longer than ``dump_after``, faulthandler fires and
                # captures the culprit's stack — which this coroutine, unschedulable
                # during the block, never could.
                self._arm_tracer()
        finally:
            self._disarm_tracer()
