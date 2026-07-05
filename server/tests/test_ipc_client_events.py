import asyncio
import pytest
from server.ipc import protocol as proto
from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app
from server.tests.ipc_harness import serve_daemon_uds


@pytest.mark.asyncio
async def test_subscribe_receives_demo_ticks():
    async with serve_daemon_uds(create_daemon_app()) as sock:
        client = DaemonClient(sock)
        try:
            stream = client.subscribe("s1")
            await client.events_ready("s1")          # subscription registered on the daemon
            await client.call("start_demo", stream="s1", n=3)
            got = [await asyncio.wait_for(stream.__anext__(), 2) for _ in range(3)]
        finally:
            await client.aclose()
    assert [e["i"] for e in got] == [0, 1, 2]


@pytest.mark.asyncio
async def test_two_streams_are_demuxed():
    async with serve_daemon_uds(create_daemon_app()) as sock:
        client = DaemonClient(sock)
        try:
            a = client.subscribe("a")
            b = client.subscribe("b")
            await client.events_ready("a")
            await client.events_ready("b")
            await client.call("echo", stream="b", text="forb")
            ev = await asyncio.wait_for(b.__anext__(), 2)
        finally:
            await client.aclose()
    assert ev == {"type": "echo", "text": "forb"}


@pytest.mark.asyncio
async def test_subscribe_after_connection_established():
    """A subscribe() issued AFTER the reader is already connected still registers
    (the dynamic-attach path A2 relies on), not only the streams present at connect."""
    async with serve_daemon_uds(create_daemon_app()) as sock:
        client = DaemonClient(sock)
        try:
            first = client.subscribe("first")
            await client.events_ready("first")        # reader is now connected
            later = client.subscribe("later")         # subscribe after connect
            await client.events_ready("later")
            await client.call("echo", stream="later", text="dynamic")
            ev = await asyncio.wait_for(later.__anext__(), 2)
        finally:
            await client.aclose()
    assert ev == {"type": "echo", "text": "dynamic"}


@pytest.mark.asyncio
async def test_two_subscribers_same_stream_both_receive():
    """Two concurrent subscribers to the SAME stream must BOTH receive each event.

    Regression (transcript goes silent until reattach): subscribe() overwrote the
    stream's single queue, so a second subscriber (e.g. the same session open in a
    second tab/device on the one shared edge client) orphaned the first subscriber's
    queue — it starved permanently until a detach/reattach forced a fresh disk replay.
    """
    async with serve_daemon_uds(create_daemon_app()) as sock:
        client = DaemonClient(sock)
        try:
            a = client.subscribe("dup")
            await client.events_ready("dup")
            b = client.subscribe("dup")          # second subscriber on the SAME stream
            await client.call("echo", stream="dup", text="hi")
            ev_a = await asyncio.wait_for(a.__anext__(), 2)
            ev_b = await asyncio.wait_for(b.__anext__(), 2)
        finally:
            await client.aclose()
    assert ev_a == {"type": "echo", "text": "hi"}
    assert ev_b == {"type": "echo", "text": "hi"}


@pytest.mark.asyncio
async def test_deliver_carries_daemon_src_seq():
    """_deliver threads the daemon's per-stream seq onto the delivered event as a
    dict-compatible _SeqDict.src_seq — additive srcSeq telemetry (audit W2.3) that
    leaves the event's CONTENT (and thus every existing consumer/equality check)
    unchanged."""
    from server.ipc.client import _SeqDict
    client = DaemonClient("/unused.sock")
    q = client._add_subscriber("s")
    client._deliver("s", {"i": 1}, seq=7)
    got = q.get_nowait()
    assert got == {"i": 1}                       # content identical → consumers unaffected
    assert isinstance(got, _SeqDict) and got.src_seq == 7
    # A gap/local control event delivered without a seq carries src_seq None.
    client._deliver("s", {"type": "gap"})
    assert q.get_nowait().src_seq is None
    await client.aclose()


@pytest.mark.asyncio
async def test_deliver_drops_oldest_on_overflow():
    client = DaemonClient("/unused.sock")
    q = client._add_subscriber("s", maxsize=2)
    client._deliver("s", {"i": 1})
    client._deliver("s", {"i": 2})
    client._deliver("s", {"i": 3})           # overflow → drop oldest ({"i":1})
    drained = [q.get_nowait() for _ in range(2)]
    assert drained == [{"i": 2}, {"i": 3}]
    await client.aclose()


# ---------------------------------------------------------------------------
# Local subscriber-queue overflow → synthetic GAP (transcript audit V1)
# ---------------------------------------------------------------------------
# When a local subscriber's bounded queue overflows, _deliver drops the oldest
# queued event. Dropping silently left the edge view permanently behind durable
# storage (the daemon-side watermark had already advanced, so a reconnect never
# re-sent the dropped events). _iter now injects ONE GAP control frame per
# overflow burst — the same wire shape as the daemon ring-eviction GAP — so the
# ws._forward_live consumer forces a from-disk resync.


@pytest.mark.asyncio
async def test_overflow_emits_single_gap_then_events_flow():
    client = DaemonClient("/unused.sock")
    q = client._add_subscriber("s", maxsize=2)
    gen = client._iter("s", q)
    # 5 events into a maxsize-2 queue → 3 oldest dropped; survivors {i:3},{i:4}.
    for i in range(5):
        client._deliver("s", {"i": i})
    first = await asyncio.wait_for(gen.__anext__(), 2)
    assert first == {"type": proto.GAP, "stream": "s", "lost": 3, "oldest": None}
    # Survivors still flow, with NO further GAP.
    survivors = [await asyncio.wait_for(gen.__anext__(), 2) for _ in range(2)]
    assert survivors == [{"i": 3}, {"i": 4}]
    # A fresh event after the burst drains cleanly (no spurious second GAP).
    client._deliver("s", {"i": 5})
    assert await asyncio.wait_for(gen.__anext__(), 2) == {"i": 5}
    await gen.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_second_overflow_burst_yields_second_gap():
    client = DaemonClient("/unused.sock")
    q = client._add_subscriber("s", maxsize=2)
    gen = client._iter("s", q)
    for i in range(5):                       # burst 1: 3 dropped
        client._deliver("s", {"i": i})
    g1 = await asyncio.wait_for(gen.__anext__(), 2)
    assert g1["type"] == proto.GAP and g1["lost"] == 3
    for _ in range(2):                       # drain survivors, queue now empty
        await asyncio.wait_for(gen.__anext__(), 2)
    for i in range(5, 9):                    # burst 2: 4 events → 2 dropped
        client._deliver("s", {"i": i})
    g2 = await asyncio.wait_for(gen.__anext__(), 2)
    assert g2 == {"type": proto.GAP, "stream": "s", "lost": 2, "oldest": None}
    await gen.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_non_overflowing_subscriber_gets_every_event_and_no_gap():
    """Two subscribers on one stream: only the slow one overflows. The fast one
    receives every event and NO GAP — the dropped counter is strictly per-queue."""
    client = DaemonClient("/unused.sock")
    slow = client._add_subscriber("s", maxsize=2)
    fast = client._add_subscriber("s", maxsize=100)
    gslow = client._iter("s", slow)
    gfast = client._iter("s", fast)
    for i in range(5):
        client._deliver("s", {"i": i})
    fast_got = [await asyncio.wait_for(gfast.__anext__(), 2) for _ in range(5)]
    assert fast_got == [{"i": i} for i in range(5)]
    assert all(e.get("type") != proto.GAP for e in fast_got)
    # The overflowing subscriber DOES get a GAP for the same burst.
    g = await asyncio.wait_for(gslow.__anext__(), 2)
    assert g == {"type": proto.GAP, "stream": "s", "lost": 3, "oldest": None}
    await gslow.aclose()
    await gfast.aclose()
    await client.aclose()
