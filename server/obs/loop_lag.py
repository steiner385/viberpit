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
"""
import asyncio
import logging
import time

_default_log = logging.getLogger("server.obs.loop_lag")


class LoopLagWatchdog:
    def __init__(
        self,
        interval: float = 1.0,
        warn_after: float = 0.5,
        warn_every: float = 60.0,
        _log=None,
        _clock=None,
    ) -> None:
        self.interval = interval
        self.warn_after = warn_after
        self.warn_every = warn_every
        self.peak_lag_s = 0.0
        self._log = _log if _log is not None else _default_log
        self._clock = _clock if _clock is not None else time.monotonic
        # Throttle state: when we last emitted, and what we swallowed since.
        self._last_warn_at: float | None = None
        self._suppressed = 0
        self._suppressed_peak = 0.0

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
        while True:
            started = self._clock()
            await asyncio.sleep(self.interval)
            lag = (self._clock() - started) - self.interval
            self._note_lag(lag)
