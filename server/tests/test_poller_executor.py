"""UsagePoller runs the blocking usage_fn on a dedicated, bounded thread pool.

Isolating usage fetches off the process-wide default executor stops a storm of
slow/hanging upstream usage calls from starving the shared pool the daemon's
control path depends on — the wedge mechanism behind incident 2026-07-02.
"""
import asyncio
import threading

import pytest

from server.usage.poller import UsagePoller


@pytest.mark.asyncio
async def test_usage_fn_runs_on_dedicated_executor_thread():
    seen = {}

    def fake(account):
        seen[account] = threading.current_thread().name
        return None

    p = UsagePoller(usage_fn=fake)
    await p.get("a")
    assert seen["a"].startswith("usage-poller")
    await p.stop()


@pytest.mark.asyncio
async def test_executor_torn_down_on_stop_and_recreated_on_next_use():
    def fake(account):
        return None

    p = UsagePoller(usage_fn=fake)
    await p.get("a")
    ex1 = p._executor
    assert ex1 is not None

    await p.stop()
    assert p._executor is None  # threads released at teardown

    await p.get("a")      # must recreate the pool, not raise on a dead executor
    assert p._executor is not None and p._executor is not ex1
    await p.stop()


@pytest.mark.asyncio
async def test_slow_account_does_not_block_another_account():
    release = threading.Event()

    def fake(account):
        if account == "slow":
            release.wait(2.0)
        return None

    p = UsagePoller(usage_fn=fake)
    try:
        slow_task = asyncio.create_task(p.get("slow"))  # occupies one pool thread
        # A different account must resolve while "slow" is still blocked.
        fast = await asyncio.wait_for(p.get("fast"), timeout=1.0)
        assert fast is None
    finally:
        release.set()
        await slow_task
        await p.stop()
