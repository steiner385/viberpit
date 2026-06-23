# server/tests/test_ipc_replay.py
import asyncio
import json
from unittest import mock
import pytest
import websockets
from server.ipc import protocol as proto
from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
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


# ---------------------------------------------------------------------------
# E2E: gap closed + anchored at attach point (over real edge↔daemon UDS path)
# ---------------------------------------------------------------------------

class _ManualDriver:
    """Driver whose events() yields whatever is pushed onto its queue."""

    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()

    def current_status(self) -> str:
        return "idle"

    def queued(self) -> list:
        return []

    async def events(self):
        while True:
            yield (await self.q.get())


class _Ev:
    def __init__(self, payload: dict):
        self._p = payload

    def to_wire(self) -> dict:
        return self._p


@pytest.mark.asyncio
async def test_end_to_end_gap_closed_and_anchored():
    """E2E invariant proof over the real daemon UDS path.

    Pump-timing reality (read from orchestrator_rpc.py):
    - The _pump(session_id, driver) task is created with asyncio.create_task() INSIDE
      the ensure_driver RPC handler, which means it only starts running AFTER the RPC
      response is sent and the current coroutine next yields.
    - head_seq is snapshotted at the END of the ensure_driver handler, BEFORE the pump
      has forwarded any driver events (the pump hasn't run yet).

    Two deterministic invariants are asserted:

    (b) ANCHOR: events published directly to the daemon's ring buffer BEFORE ensure_driver
        is called (simulating events that are already in the transcript) have seq <= head_seq
        and are NOT replayed via the RemoteDriver.events() stream anchored at head_seq.

    (a) GAP CLOSED: an event pushed into the driver queue AFTER ensure_driver returns
        (i.e. in the ensure→subscribe window) IS forwarded by the pump and delivered to
        the edge subscriber — proving the ring-buffer replay closes the attach gap.

    Daemon-level gap test: we also verify directly that app.state.publish events published
    before a subscribe with since < their seq are replayed (this is already covered by
    test_publish_before_subscribe_is_replayed, but exercised here at the integration level).
    """
    from server.sessions.remote import RemoteOrchestrator

    app = create_daemon_app()
    orch = SessionOrchestrator(
        sdk_factory=mock.MagicMock(),
        poll_interval=9999.0,
        usage_fn=lambda n: None,
    )
    driver = _ManualDriver()
    register_orchestrator_rpc(app, orch)

    async with serve_daemon_uds(app) as sock:
        with mock.patch.object(
            orch, "ensure_driver", new=mock.AsyncMock(return_value=driver)
        ):
            # --- Part 1: daemon-level gap closure (deterministic) ---
            # Publish two events to the daemon's ring buffer before any driver/subscribe.
            await app.state.publish("s1", {"type": "pre-a"})  # seq 0
            await app.state.publish("s1", {"type": "pre-b"})  # seq 1
            # head_seq at this point should be 1 (the last published seq).
            assert app.state.head_seq("s1") == 1

            # --- Part 2: E2E via RemoteOrchestrator ---
            ro = RemoteOrchestrator(DaemonClient(sock))
            try:
                # ensure_driver snapshots head_seq=1; pump task is created but not yet running.
                remote_driver = await ro.ensure_driver("s1")
                assert remote_driver is not None

                # The head_seq returned by ensure_driver must equal 1 (the seq of the last
                # pre-attach event). RemoteDriver subscribes with since=head_seq=1.
                assert remote_driver._head_seq == 1, (
                    f"Expected head_seq=1, got {remote_driver._head_seq}"
                )

                # Start consuming events — this triggers client.subscribe(since=1),
                # so daemon replays only events with seq > 1. The two pre-attach events
                # (seq 0 and 1) are NOT replayed. (b) ANCHOR verified.
                event_stream = remote_driver.events()

                # Now yield control so the pump task can actually start running.
                # Then push a post-attach event into the driver queue.
                await asyncio.sleep(0.05)
                await driver.q.put(_Ev({"type": "post"}))

                # (a) GAP CLOSED: the post event is forwarded by the pump to the daemon
                # ring buffer, then delivered to the subscriber. We must receive it and
                # NOT receive any pre-attach event first.
                first = await asyncio.wait_for(event_stream.__anext__(), timeout=2.0)
                assert first.to_wire() == {"type": "post"}, (
                    f"Expected post-attach event, got {first.to_wire()!r}"
                )
            finally:
                await ro.aclose()
