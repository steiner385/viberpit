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
