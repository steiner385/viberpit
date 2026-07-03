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
