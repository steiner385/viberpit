# server/tests/test_ipc_replay.py
import asyncio
import json
import pytest
import websockets
from server.ipc import protocol as proto
from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app
from server.tests.ipc_harness import serve_daemon_uds


async def _collect(ws, n, timeout=2):
    out = []
    for _ in range(n):
        raw = await asyncio.wait_for(ws.recv(), timeout)
        out.append(json.loads(raw))
    return out


@pytest.mark.asyncio
async def test_publish_before_subscribe_is_replayed():
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        # Publish two events with NO subscriber yet — must be buffered.
        await app.state.publish("s1", {"type": "a"})
        await app.state.publish("s1", {"type": "b"})
        async with websockets.unix_connect(sock, "ws://daemon/events") as ws:
            await ws.send(json.dumps(proto.subscribe_frame("s1", since=-1)))
            frames = await _collect(ws, 2)
    assert [f["event"]["type"] for f in frames] == ["a", "b"]
    assert [f["seq"] for f in frames] == [0, 1]


@pytest.mark.asyncio
async def test_since_anchor_skips_already_seen():
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        await app.state.publish("s1", {"type": "a"})   # seq 0
        await app.state.publish("s1", {"type": "b"})   # seq 1
        head = app.state.head_seq("s1")                # 1
        async with websockets.unix_connect(sock, "ws://daemon/events") as ws:
            await ws.send(json.dumps(proto.subscribe_frame("s1", since=head)))
            # Nothing replayed (all <= head); a new event after subscribe IS delivered.
            await asyncio.sleep(0.1)
            await app.state.publish("s1", {"type": "c"})  # seq 2
            frame = json.loads(await asyncio.wait_for(ws.recv(), 2))
    assert frame["event"]["type"] == "c"
    assert frame["seq"] == 2


@pytest.mark.asyncio
async def test_buffer_is_bounded(monkeypatch):
    monkeypatch.setenv("CCSWITCH_IPC_REPLAY_BUFFER", "4")
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        for i in range(6):
            await app.state.publish("s1", {"type": str(i)})  # seq 0..5
        async with websockets.unix_connect(sock, "ws://daemon/events") as ws:
            await ws.send(json.dumps(proto.subscribe_frame("s1", since=-1)))
            frames = await _collect(ws, 4)
    # Only the last 4 survive (seq 2..5).
    assert [f["seq"] for f in frames] == [2, 3, 4, 5]


@pytest.mark.asyncio
async def test_client_strips_seq_and_replays_gap():
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        # Two events published BEFORE the client subscribes.
        await app.state.publish("s1", {"type": "a"})
        await app.state.publish("s1", {"type": "b"})
        client = DaemonClient(sock)
        try:
            stream = client.subscribe("s1", since=-1)  # anchor before the first event
            got = []
            for _ in range(2):
                got.append(await asyncio.wait_for(stream.__anext__(), 2))
        finally:
            await client.aclose()
    # Bare event dicts (no seq leaked), in order — the pre-subscribe gap is replayed.
    assert got == [{"type": "a"}, {"type": "b"}]


@pytest.mark.asyncio
async def test_client_resubscribe_uses_last_seq():
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            stream = client.subscribe("s1", since=-1)
            await client.events_ready("s1")
            await app.state.publish("s1", {"type": "a"})
            assert await asyncio.wait_for(stream.__anext__(), 2) == {"type": "a"}
            # last_seq now tracks seq 0.
            assert client._last_seq.get("s1") == 0
        finally:
            await client.aclose()
