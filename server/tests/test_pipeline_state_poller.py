"""PipelineStatePoller: shared background population of the dashboard frame."""
from __future__ import annotations

import asyncio

import pytest

from server.pipeline.state_poller import PipelineStatePoller


@pytest.mark.asyncio
async def test_poll_once_publishes_frame_and_bumps_generation():
    frames = [{"n": 1}, {"n": 2}]
    calls = 0

    async def builder():
        nonlocal calls
        calls += 1
        return frames[calls - 1]

    poller = PipelineStatePoller(builder)
    assert poller.snapshot() is None
    assert poller.generation == 0

    await poller.poll_once()
    assert poller.snapshot() == {"n": 1}
    assert poller.generation == 1

    await poller.poll_once()
    assert poller.snapshot() == {"n": 2}
    assert poller.generation == 2
    assert calls == 2


@pytest.mark.asyncio
async def test_poll_once_swallows_builder_error_and_keeps_last_frame():
    state = {"ok": True}
    raise_next = False

    async def builder():
        if raise_next:
            raise RuntimeError("boom")
        return state

    poller = PipelineStatePoller(builder)
    await poller.poll_once()
    assert poller.snapshot() == {"ok": True}
    assert poller.generation == 1

    raise_next = True
    await poller.poll_once()  # must not raise
    # Carry-forward: last good frame stays, generation does not advance.
    assert poller.snapshot() == {"ok": True}
    assert poller.generation == 1


@pytest.mark.asyncio
async def test_build_now_populates_cold_cache_and_returns_frame():
    async def builder():
        return {"warm": True}

    poller = PipelineStatePoller(builder)
    result = await poller.build_now()
    assert result == {"warm": True}
    assert poller.snapshot() == {"warm": True}
    assert poller.generation == 1


@pytest.mark.asyncio
async def test_wait_for_frame_returns_when_newer_frame_published():
    ready = asyncio.Event()

    async def builder():
        await ready.wait()
        return {"frame": "late"}

    poller = PipelineStatePoller(builder)

    async def publisher():
        ready.set()
        await poller.poll_once()

    waiter = asyncio.create_task(poller.wait_for_frame(0, timeout=5.0))
    pub = asyncio.create_task(publisher())
    state, gen = await asyncio.wait_for(waiter, timeout=5.0)
    await pub
    assert state == {"frame": "late"}
    assert gen == 1


@pytest.mark.asyncio
async def test_wait_for_frame_times_out_to_current_frame():
    async def builder():
        return {"frame": 1}

    poller = PipelineStatePoller(builder)
    await poller.poll_once()  # generation == 1

    # Ask for something newer than gen 1; nothing publishes → timeout returns
    # the current frame so the caller can heartbeat.
    state, gen = await poller.wait_for_frame(1, timeout=0.05)
    assert state == {"frame": 1}
    assert gen == 1


@pytest.mark.asyncio
async def test_start_runs_loop_and_stop_cancels_cleanly():
    calls = 0

    async def builder():
        nonlocal calls
        calls += 1
        return {"tick": calls}

    poller = PipelineStatePoller(builder, interval_s=0.01)
    await poller.start()
    await poller.start()  # idempotent — no second task
    try:
        for _ in range(200):
            if poller.generation >= 2:
                break
            await asyncio.sleep(0.005)
        assert poller.generation >= 2
    finally:
        await poller.stop()
    gen_at_stop = poller.generation
    await asyncio.sleep(0.05)
    assert poller.generation == gen_at_stop  # loop truly stopped


@pytest.mark.asyncio
async def test_daemon_unavailable_is_a_throttled_info_not_an_error(caplog):
    """A daemon restart/deploy window makes every 10 s build hit DaemonUnavailable.
    That is an expected transient, not a pipeline bug: #1656 was 92+ ERROR records
    (with a full httpx traceback each) for it, and demoting only the *repeats* to
    DEBUG (#2310) still left the first occurrence of every window at WARNING — the
    filing decision for server.obs.errsink, so every restart re-filed #2361. A
    short window must log at INFO, once per throttle window, then demote repeats
    to DEBUG — and still carry forward."""
    import logging

    import server.pipeline.state_poller as mod
    from server.ipc.client import DaemonUnavailable

    mod._DAEMON_UNAVAILABLE_LOG.clear()
    fail = False

    async def builder():
        if fail:
            raise DaemonUnavailable("All connection attempts failed")
        return {"ok": True}

    poller = PipelineStatePoller(builder)
    await poller.poll_once()
    fail = True
    with caplog.at_level(logging.DEBUG, logger="server.pipeline.state_poller"):
        await poller.poll_once()
        await poller.poll_once()

    records = [r for r in caplog.records if r.name == "server.pipeline.state_poller"]
    assert [r.levelno for r in records] == [logging.INFO, logging.DEBUG]
    assert all(r.exc_info is None for r in records)  # no traceback spam
    assert poller.snapshot() == {"ok": True}
    assert poller.generation == 1


@pytest.mark.asyncio
async def test_daemon_unavailable_escalates_to_one_warning_after_sustained_outage(
    caplog,
):
    """A restart/deploy window is a few ticks and stays at INFO (previous test).
    An outage that outlasts _DAEMON_UNAVAILABLE_ESCALATE_AFTER consecutive polls is
    a genuine signal and must still surface as a WARNING — but exactly once per
    outage, not once per tick past the threshold, or the flood #1656 fixed comes
    back in a new shape."""
    import logging

    import server.pipeline.state_poller as mod
    from server.ipc.client import DaemonUnavailable

    mod._DAEMON_UNAVAILABLE_LOG.clear()

    async def builder():
        raise DaemonUnavailable("All connection attempts failed")

    poller = PipelineStatePoller(builder)
    with caplog.at_level(logging.DEBUG, logger="server.pipeline.state_poller"):
        for _ in range(mod._DAEMON_UNAVAILABLE_ESCALATE_AFTER + 5):
            await poller.poll_once()

    records = [r for r in caplog.records if r.name == "server.pipeline.state_poller"]
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "consecutive polls" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_daemon_unavailable_escalation_resets_on_recovery(caplog):
    """A successful build (the daemon comes back) must reset the streak, so a
    second later outage escalates again instead of the counter staying latched."""
    import logging

    import server.pipeline.state_poller as mod
    from server.ipc.client import DaemonUnavailable

    mod._DAEMON_UNAVAILABLE_LOG.clear()
    fail = True

    async def builder():
        if fail:
            raise DaemonUnavailable("All connection attempts failed")
        return {"ok": True}

    poller = PipelineStatePoller(builder)
    with caplog.at_level(logging.DEBUG, logger="server.pipeline.state_poller"):
        for _ in range(mod._DAEMON_UNAVAILABLE_ESCALATE_AFTER):
            await poller.poll_once()
        fail = False
        await poller.poll_once()  # recovers: resets the streak
        fail = True
        for _ in range(mod._DAEMON_UNAVAILABLE_ESCALATE_AFTER):
            await poller.poll_once()

    records = [r for r in caplog.records if r.name == "server.pipeline.state_poller"]
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 2  # one per outage, not a single latched escalation


@pytest.mark.asyncio
async def test_daemon_connect_timeout_is_a_throttled_info_not_an_error(caplog):
    """#1726: a TLS-connect timeout to the daemon (httpx.ConnectTimeout) reaches the
    poller as ``DaemonTimeout`` — a *subclass* of DaemonUnavailable — and filed 48
    ERROR records with full httpx tracebacks. It is the same expected transient as
    an outright connect failure and must take the same throttled-INFO path."""
    import logging

    import httpx

    import server.pipeline.state_poller as mod
    from server.ipc.client import DaemonTimeout

    mod._DAEMON_UNAVAILABLE_LOG.clear()

    async def builder():
        try:
            raise httpx.ConnectTimeout("")
        except httpx.ConnectTimeout as exc:
            raise DaemonTimeout(f"sessions.list timed out: {exc}") from exc

    poller = PipelineStatePoller(builder)
    with caplog.at_level(logging.DEBUG, logger="server.pipeline.state_poller"):
        await poller.poll_once()
        await poller.poll_once()

    records = [r for r in caplog.records if r.name == "server.pipeline.state_poller"]
    assert [r.levelno for r in records] == [logging.INFO, logging.DEBUG]
    assert all(r.exc_info is None for r in records)
    assert poller.snapshot() is None  # nothing published; loop survives


@pytest.mark.asyncio
async def test_unexpected_builder_error_still_logs_at_error(caplog):
    """Only the daemon-unreachable transient is demoted; a real bug keeps its
    ERROR + traceback so it is still filed."""
    import logging

    async def builder():
        raise RuntimeError("boom")

    with caplog.at_level(logging.DEBUG, logger="server.pipeline.state_poller"):
        await PipelineStatePoller(builder).poll_once()

    (rec,) = [r for r in caplog.records if r.name == "server.pipeline.state_poller"]
    assert rec.levelno == logging.ERROR
    assert rec.exc_info is not None
