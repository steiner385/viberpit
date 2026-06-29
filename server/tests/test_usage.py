"""Tests for server.usage.poller (T019).

- get() returns the (mocked) usage for an account.
- A second immediate get() does NOT trigger a second underlying fetch (dedup).
- A subscriber receives an event when usage changes between polls.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from ccswitch.oauth import Usage
from server.usage.poller import UsagePoller


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_usage(session_pct: float = 10.0, weekly_pct: float = 20.0) -> Usage:
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return Usage(session_pct, reset, weekly_pct, reset, False)


# ---------------------------------------------------------------------------
# T019-a: get() returns the usage returned by usage_fn
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_returns_mocked_usage():
    u = _make_usage(session_pct=42.0)
    calls: list[str] = []

    def fake_usage_fn(account: str) -> Usage:
        calls.append(account)
        return u

    poller = UsagePoller(usage_fn=fake_usage_fn)
    result = await poller.get("alpha")

    assert result is u
    assert calls == ["alpha"]


# ---------------------------------------------------------------------------
# T019-b: second concurrent get() is served without a duplicate underlying fetch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_deduplicates_concurrent_fetches():
    """Two concurrent get() calls for the same account should result in only
    one call to the underlying usage_fn (the lock serialises them and the
    second caller reads the cache written by the first).

    We ensure true concurrency by making the usage_fn block until an asyncio
    Event is set, so both coroutines are alive at the same time and the second
    one definitely sees the lock as held.
    """
    u = _make_usage(session_pct=55.0)
    call_count = 0
    # Gate: the usage_fn waits for this threading Event to be set
    import threading
    gate = threading.Event()
    arrived = threading.Event()

    def slow_usage_fn(account: str) -> Usage:
        nonlocal call_count
        call_count += 1
        arrived.set()       # tell the test thread we've started
        gate.wait(timeout=5)  # block until the test unblocks us
        return u

    poller = UsagePoller(usage_fn=slow_usage_fn)

    # Start first get() — it will block inside the executor
    t1 = asyncio.create_task(poller.get("alpha"))

    # Wait until slow_usage_fn has actually started running in the executor
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, arrived.wait)

    # Now start the second get() — it should see the lock as held and wait
    t2 = asyncio.create_task(poller.get("alpha"))

    # Give t2 a moment to reach the lock check
    await asyncio.sleep(0.05)

    # Release the gate so slow_usage_fn can return
    gate.set()

    r1 = await t1
    r2 = await t2

    assert r1 is u
    assert r2 is u
    assert call_count == 1, f"expected 1 call, got {call_count}"


# ---------------------------------------------------------------------------
# T019-c: sequential get() calls each fetch (no persistent caching across calls
# — the poller's cache is per-run; a second call after the first completes
# WILL call the fn again because the lock is no longer held)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_call_count_sequential():
    """Sequential (non-overlapping) calls each invoke the usage_fn."""
    call_count = 0

    def counting_fn(account: str) -> Usage:
        nonlocal call_count
        call_count += 1
        return _make_usage()

    poller = UsagePoller(usage_fn=counting_fn)
    await poller.get("alpha")
    await poller.get("alpha")

    # Sequential calls are NOT deduplicated — each is a fresh fetch
    assert call_count == 2


# ---------------------------------------------------------------------------
# T019-d: subscriber receives an event when usage changes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscriber_receives_event_on_change():
    u1 = _make_usage(session_pct=10.0)
    u2 = _make_usage(session_pct=80.0)
    values = [u1, u2]
    call_count = 0

    def changing_fn(account: str) -> Usage:
        nonlocal call_count
        result = values[min(call_count, len(values) - 1)]
        call_count += 1
        return result

    poller = UsagePoller(usage_fn=changing_fn)
    q = poller.subscribe()

    # First fetch — publishes (u1, change from None→u1)
    await poller.get("alpha")
    # Second fetch — publishes (u2, change from u1→u2)
    await poller.get("alpha")

    events = []
    while not q.empty():
        events.append(q.get_nowait())

    assert len(events) == 2, f"expected 2 events, got {len(events)}: {events}"
    assert events[0]["account"] == "alpha"
    assert events[0]["usage"] is u1
    assert events[1]["usage"] is u2


# ---------------------------------------------------------------------------
# T019-e: unsubscribe removes the queue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unsubscribe_removes_queue():
    poller = UsagePoller(usage_fn=lambda _: _make_usage())
    q = poller.subscribe()
    poller.unsubscribe(q)

    await poller.get("alpha")
    assert q.empty(), "unsubscribed queue should not receive events"


# ---------------------------------------------------------------------------
# T019-f: start/stop lifecycle — background loop runs and can be stopped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_stop_lifecycle():
    """start() launches a background task; stop() cancels it cleanly."""
    call_count = 0

    def counting_fn(account: str) -> Usage:
        nonlocal call_count
        call_count += 1
        return _make_usage()

    # Patch list_accounts to return one account so all() can run
    with patch("server.usage.poller.list_accounts", return_value=[{"name": "alpha"}]):
        poller = UsagePoller(poll_interval=0.05, usage_fn=counting_fn)
        await poller.start()
        # Let it run at least one sweep
        await asyncio.sleep(0.15)
        await poller.stop()

    assert call_count >= 1, "background loop should have polled at least once"


@pytest.mark.asyncio
async def test_start_idempotent():
    """Calling start() twice does not create duplicate background tasks."""
    poller = UsagePoller(poll_interval=60.0, usage_fn=lambda _: _make_usage())
    await poller.start()
    task_before = poller._task
    await poller.start()  # second call — should be a no-op
    assert poller._task is task_before
    await poller.stop()


# ---------------------------------------------------------------------------
# Blast-radius guard: one account's usage_fn raising must NOT fail the whole
# sweep.  Incident 2026-06-29: a single account with a malformed credential blob
# raised out of usage_fn → all() propagated the exception → /rpc/usage 500 →
# remote edge mapped DaemonError to a blanket 409 on EVERY route. One bad
# account must degrade to None for itself, never poison the others' usage.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_isolates_a_single_account_failure():
    good = _make_usage(session_pct=42.0)

    def usage_fn(account: str) -> Usage:
        if account == "bad":
            raise KeyError("refreshToken")  # the exact incident exception
        return good

    poller = UsagePoller(usage_fn=usage_fn)
    with patch("server.usage.poller.list_accounts",
               return_value=[{"name": "good"}, {"name": "bad"}]):
        result = await poller.all()  # must not raise

    assert result == {"good": good, "bad": None}


@pytest.mark.asyncio
async def test_get_returns_none_when_usage_fn_raises():
    def boom(account: str) -> Usage:
        raise RuntimeError("unexpected")

    poller = UsagePoller(usage_fn=boom)
    assert await poller.get("alpha") is None
