"""Daemon publish must not block on a slow subscriber (issue #185).

Previously `publish` sent inline via `await ws.send_text`, so one slow edge consumer
blocked the publish call → the per-session pump stopped draining the unbounded
`driver._out` → memory growth + replay-buffer floods. The watchdog half is fixed in
#337; this is the IPC-backpressure half.

Fix: each subscriber gets a bounded send queue drained by its own writer task. `publish`
enqueues non-blocking (`put_nowait`) and DROPS a subscriber whose queue overflows — the
edge reconnects and replays from its `since` anchor (the #334 gap frame makes any gap
explicit). Source events / `driver._out` are never dropped; only the slow subscriber is.
"""
import asyncio

import pytest

from server.ipc.daemon import create_daemon_app


@pytest.mark.asyncio
async def test_publish_enqueues_for_healthy_subscriber():
    app = create_daemon_app()
    ws = object()
    app.state.subscribers["s1"] = {ws}
    app.state.sendq[ws] = asyncio.Queue(maxsize=8)
    await app.state.publish("s1", {"i": 0})
    assert app.state.sendq[ws].qsize() == 1
    assert ws in app.state.subscribers["s1"]


@pytest.mark.asyncio
async def test_publish_drops_subscriber_whose_queue_overflows():
    app = create_daemon_app()
    ws = object()
    app.state.subscribers["s1"] = {ws}
    app.state.sendq[ws] = asyncio.Queue(maxsize=2)
    for i in range(3):  # the 3rd event overflows the maxsize-2 queue
        await app.state.publish("s1", {"i": i})
    assert ws not in app.state.subscribers.get("s1", set())
    assert app.state.sendq.get(ws) is None


@pytest.mark.asyncio
async def test_publish_never_blocks_on_a_stuck_subscriber():
    """put_nowait means publish never awaits a send; publishing far past the cap on a
    subscriber that never drains must still complete promptly (guarded by wait_for)."""
    app = create_daemon_app()
    ws = object()
    app.state.subscribers["s1"] = {ws}
    app.state.sendq[ws] = asyncio.Queue(maxsize=4)

    async def run():
        for i in range(100):
            await app.state.publish("s1", {"i": i})

    await asyncio.wait_for(run(), timeout=2)
    assert ws not in app.state.subscribers.get("s1", set())  # dropped once full


@pytest.mark.asyncio
async def test_one_slow_subscriber_does_not_starve_others():
    app = create_daemon_app()
    slow, fast = object(), object()
    app.state.subscribers["s1"] = {slow, fast}
    app.state.sendq[slow] = asyncio.Queue(maxsize=2)
    app.state.sendq[fast] = asyncio.Queue(maxsize=100)
    for i in range(5):
        await app.state.publish("s1", {"i": i})
    assert slow not in app.state.subscribers["s1"]   # overflowed → dropped
    assert fast in app.state.subscribers["s1"]        # kept up → retained
    assert app.state.sendq[fast].qsize() == 5         # received every event
