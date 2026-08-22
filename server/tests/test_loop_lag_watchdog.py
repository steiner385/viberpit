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
