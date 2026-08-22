"""Event-loop lag watchdog.

Sleeps for a fixed interval and measures how much longer than that it actually
took. The overshoot is time the loop spent unable to schedule us — i.e. blocked
in synchronous work. RC1 (blocking sqlite3 on the loop) was invisible until an
external py-spy dump; this makes it self-reporting.
"""
import asyncio
import logging
import time

_default_log = logging.getLogger("server.obs.loop_lag")


class LoopLagWatchdog:
    def __init__(self, interval: float = 1.0, warn_after: float = 0.5, _log=None) -> None:
        self.interval = interval
        self.warn_after = warn_after
        self.peak_lag_s = 0.0
        self._log = _log if _log is not None else _default_log

    async def run(self) -> None:
        while True:
            started = time.monotonic()
            await asyncio.sleep(self.interval)
            lag = (time.monotonic() - started) - self.interval
            if lag > self.peak_lag_s:
                self.peak_lag_s = lag
            if lag >= self.warn_after:
                self._log.warning(
                    "loop_lag: event loop blocked for %.2fs (interval=%.2fs)",
                    lag, self.interval,
                )
