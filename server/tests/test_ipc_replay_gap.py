"""IPC replay-buffer observability + reconnect-gap detection (issue #185).

The replay buffer is a bounded ring: 'dropping oldest' on every event past the cap
is normal for live delivery and must NOT flood the logs. The genuine defect is the
SILENT reconnect gap — a subscriber resuming from a `since` anchor older than the
oldest event still retained loses the in-between events with no signal. These tests
lock the counters + the explicit gap control frame that makes that visible.
"""
import asyncio
import json
import logging as _logging

import pytest
import websockets

from server.ipc import protocol as proto
from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app
from server.tests.ipc_harness import serve_daemon_uds


async def _collect(ws, n, timeout=2):
    out = []
    for _ in range(n):
        out.append(json.loads(await asyncio.wait_for(ws.recv(), timeout)))
    return out


@pytest.mark.asyncio
async def test_publish_tracks_buffer_stats(monkeypatch):
    monkeypatch.setenv("CCSWITCH_IPC_REPLAY_BUFFER", "2")
    app = create_daemon_app()
    async with serve_daemon_uds(app):
        for i in range(5):  # seq 0..4; buffer holds 2 → 3 oldest dropped
            await app.state.publish("s1", {"type": str(i)})
    stats = app.state.buffer_stats()["s1"]
    assert stats["published"] == 5
    assert stats["dropped"] == 3
    assert stats["high_water"] == 2


@pytest.mark.asyncio
async def test_ring_drop_does_not_warn_per_event(monkeypatch, caplog):
    """Benign ring eviction must not emit a per-event WARNING (log-flood guard)."""
    monkeypatch.setenv("CCSWITCH_IPC_REPLAY_BUFFER", "2")
    app = create_daemon_app()
    async with serve_daemon_uds(app):
        with caplog.at_level(_logging.WARNING):
            for i in range(20):
                await app.state.publish("s1", {"type": str(i)})
    drop_warnings = [r for r in caplog.records if "dropping oldest" in r.message.lower()]
    assert drop_warnings == []  # demoted to a counter; no flood


@pytest.mark.asyncio
async def test_subscribe_emits_gap_frame_when_anchor_evicted(monkeypatch):
    monkeypatch.setenv("CCSWITCH_IPC_REPLAY_BUFFER", "2")
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        for i in range(5):  # seq 0..4; retained = seq 3,4
            await app.state.publish("s1", {"type": str(i)})
        async with websockets.unix_connect(sock, "ws://daemon/events") as ws:
            # Resume from seq 0 — events seq 1,2 were evicted (oldest retained = 3).
            await ws.send(json.dumps(proto.subscribe_frame("s1", since=0)))
            frames = await _collect(ws, 3)  # gap frame + the 2 retained events
    gap = frames[0]
    assert gap["type"] == proto.GAP
    assert gap["stream"] == "s1"
    assert gap["oldest"] == 3
    assert gap["lost"] == 2          # seq 1 and 2
    # The retained events still follow the gap signal.
    assert [f.get("seq") for f in frames[1:]] == [3, 4]


@pytest.mark.asyncio
async def test_client_delivers_gap_sentinel_to_subscribers(monkeypatch):
    """The edge client must PROPAGATE a daemon gap frame to its local subscribers
    (issue #185), not merely log it — so the consumer (RemoteDriver → ws._forward_live)
    can resync the desynced view from disk. Previously the gap was log-only, leaving the
    in-session transcript silently behind durable storage after a ring eviction."""
    monkeypatch.setenv("CCSWITCH_IPC_REPLAY_BUFFER", "2")
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        for i in range(5):  # seq 0..4; retained = seq 3,4 (oldest = 3)
            await app.state.publish("s1", {"type": str(i)})
        client = DaemonClient(sock)
        try:
            stream = client.subscribe("s1", since=0)  # anchor predates oldest → gap
            got = []
            for _ in range(3):  # gap sentinel, then the 2 retained events
                got.append(await asyncio.wait_for(stream.__anext__(), 2))
        finally:
            await client.aclose()
    assert got[0] == {"type": proto.GAP, "stream": "s1", "lost": 2, "oldest": 3}
    assert [e["type"] for e in got[1:]] == ["3", "4"]  # retained events still delivered


@pytest.mark.asyncio
async def test_no_gap_frame_when_anchor_within_buffer(monkeypatch):
    monkeypatch.setenv("CCSWITCH_IPC_REPLAY_BUFFER", "4")
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        for i in range(3):  # seq 0,1,2 all retained
            await app.state.publish("s1", {"type": str(i)})
        async with websockets.unix_connect(sock, "ws://daemon/events") as ws:
            await ws.send(json.dumps(proto.subscribe_frame("s1", since=0)))
            frames = await _collect(ws, 2)  # only seq 1,2 (>since), no gap frame
    assert all(f["type"] != proto.GAP for f in frames)
    assert [f["seq"] for f in frames] == [1, 2]


# ---------------------------------------------------------------------------
# ring_covers — the wave-3 cursor-catchup coverage probe (audit W2.3). It must
# agree EXACTLY with the subscribe-time GAP test: covered iff a subscribe(since=)
# would replay the whole tail with no GAP. Plus the epoch guard: a cursor AHEAD of
# head (a stale srcSeq from a previous daemon process whose seq space reset) is
# never covered by the fresh low ring.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ring_covers_true_when_anchor_within_window(monkeypatch):
    monkeypatch.setenv("CCSWITCH_IPC_REPLAY_BUFFER", "8")
    app = create_daemon_app()
    async with serve_daemon_uds(app):
        for i in range(5):  # seq 0..4 all retained (oldest=0, head=4)
            await app.state.publish("s1", {"type": str(i)})
    rc = app.state.ring_covers
    assert rc("s1", 4) is True   # fully caught up (== head): subscribe replays nothing, no gap
    assert rc("s1", 2) is True   # tail 3,4 replays, no gap
    assert rc("s1", 0) is True   # since+1 == oldest+1 > oldest, covered


@pytest.mark.asyncio
async def test_ring_covers_false_when_anchor_evicted(monkeypatch):
    monkeypatch.setenv("CCSWITCH_IPC_REPLAY_BUFFER", "2")
    app = create_daemon_app()
    async with serve_daemon_uds(app):
        for i in range(5):  # seq 0..4; retained = 3,4 (oldest=3)
            await app.state.publish("s1", {"type": str(i)})
    rc = app.state.ring_covers
    # since+1 < oldest → the in-between events were evicted → NOT covered (== the exact
    # subscribe-time GAP condition; probe and real subscribe can never disagree).
    assert rc("s1", 0) is False
    assert rc("s1", 1) is False
    assert rc("s1", 2) is True   # since+1 == oldest(3) → boundary, still covered


@pytest.mark.asyncio
async def test_ring_covers_false_when_anchor_ahead_of_head(monkeypatch):
    """Epoch-reset guard: a browser srcSeq cursor can only exceed head after a daemon
    restart (per-process seq space resets to 0). Its stale high cursor must NOT be treated
    as covered by the fresh low ring — else a catch-up would resume past every real event."""
    monkeypatch.setenv("CCSWITCH_IPC_REPLAY_BUFFER", "8")
    app = create_daemon_app()
    async with serve_daemon_uds(app):
        for i in range(3):  # seq 0..2 (head=2)
            await app.state.publish("s1", {"type": str(i)})
    rc = app.state.ring_covers
    assert rc("s1", 2) is True    # == head
    assert rc("s1", 3) is False   # ahead of head → stale cursor → full replay
    assert rc("s1", 5000) is False


@pytest.mark.asyncio
async def test_ring_covers_false_for_fresh_or_empty(monkeypatch):
    monkeypatch.setenv("CCSWITCH_IPC_REPLAY_BUFFER", "8")
    app = create_daemon_app()
    async with serve_daemon_uds(app):
        rc = app.state.ring_covers
        assert rc("never-seen", 3) is False   # no ring at all → can't guarantee coverage
        await app.state.publish("s1", {"type": "0"})
        assert rc("s1", None) is False         # fresh anchor is never a catch-up cursor
        assert rc("s1", -1) is False


@pytest.mark.asyncio
async def test_no_gap_frame_on_fresh_subscribe(monkeypatch):
    """since=-1 (or None) is a fresh attach, never a gap even if events were dropped."""
    monkeypatch.setenv("CCSWITCH_IPC_REPLAY_BUFFER", "2")
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        for i in range(5):
            await app.state.publish("s1", {"type": str(i)})
        async with websockets.unix_connect(sock, "ws://daemon/events") as ws:
            await ws.send(json.dumps(proto.subscribe_frame("s1", since=-1)))
            frames = await _collect(ws, 2)
    assert all(f["type"] != proto.GAP for f in frames)
    assert [f["seq"] for f in frames] == [3, 4]
