"""The session-daemon ASGI app: unary RPC registry + (Task 3) multiplexed event WS.

Served by uvicorn bound to a Unix socket. No TCP. No app-level auth — the socket's
filesystem permissions are the trust boundary.
"""
import asyncio
import contextlib
import json
import logging
import os
from collections import deque
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from server.ipc import protocol as proto

log = logging.getLogger("server.ipc.daemon")


def create_daemon_app() -> FastAPI:
    from server.obs.context import PROC_EPOCH
    import uuid as _uuid
    # Capture a unique epoch at app-creation time. In production this equals
    # PROC_EPOCH (one app per process); the UUID suffix differentiates test
    # instances created within the same process/millisecond. The edge client
    # watches this in /info to detect a daemon restart (seq-reset gap recovery).
    _app_epoch = f"{PROC_EPOCH}-{_uuid.uuid4().hex[:8]}"

    app = FastAPI(title="ccswitch-sessiond")
    app.state.rpc = {}

    # stream_id -> set of connected WebSockets subscribed to it
    app.state.subscribers = {}

    # Per-stream replay ring buffer: (seq, event). Closes the at-attach/reconnect gap —
    # events published before a subscriber connects are kept and replayed on subscribe.
    _maxlen = int(os.environ.get("CCSWITCH_IPC_REPLAY_BUFFER", "256"))
    app.state.buffers = {}   # stream -> deque[(seq, event)]
    app.state.seqs = {}      # stream -> last assigned seq (int)
    # Per-stream observability (issue #185): the ring buffer drops its oldest entry on
    # every event past the cap — that's NORMAL for live delivery, so it must NOT log per
    # event. We count instead. `dropped` only matters as a precursor to a RECONNECT GAP
    # (a subscriber resuming from a seq older than the oldest retained entry), which IS
    # signalled explicitly on subscribe.
    app.state.stats = {}     # stream -> {"published", "dropped", "high_water"}

    def _stat(stream: str) -> dict:
        s = app.state.stats.get(stream)
        if s is None:
            s = app.state.stats[stream] = {"published": 0, "dropped": 0, "high_water": 0}
        return s

    def buffer_stats() -> dict:
        # Snapshot copy so callers can't mutate internal counters.
        return {k: dict(v) for k, v in app.state.stats.items()}

    app.state.buffer_stats = buffer_stats

    def head_seq(stream: str) -> int:
        return app.state.seqs.get(stream, -1)

    app.state.head_seq = head_seq

    def evict(stream: str) -> None:
        app.state.buffers.pop(stream, None)
        app.state.seqs.pop(stream, None)
        app.state.stats.pop(stream, None)

    app.state.evict = evict

    # Per-subscriber send decoupling (issue #185). Sending inline with `await
    # ws.send_text` let one slow edge consumer block `publish` → the per-session pump
    # stopped draining the unbounded driver._out → memory growth + ring-buffer floods.
    # Each connection now has a bounded send queue drained by its own writer task;
    # `publish` enqueues non-blocking and drops a subscriber whose queue overflows (it
    # reconnects and replays from `since`). Source events are never dropped.
    _sendq_max = int(os.environ.get("CCSWITCH_IPC_SEND_QUEUE", "1024"))
    app.state.sendq = {}     # ws -> bounded asyncio.Queue[str] (per-connection buffer)
    app.state.writers = {}   # ws -> writer Task draining sendq to ws.send_text

    def _detach(ws):
        """Remove a connection from every stream's subscriber set and its send queue.
        Idempotent; returns its writer task (if any) without cancelling it."""
        for subs in app.state.subscribers.values():
            subs.discard(ws)
        app.state.sendq.pop(ws, None)
        return app.state.writers.pop(ws, None)

    def drop_subscriber(ws) -> None:
        """Detach a too-slow / dead connection and stop its writer. The writer's
        finally closes the socket; the edge reconnects and replays from `since`."""
        t = _detach(ws)
        if t is not None and not t.done():
            t.cancel()

    app.state.drop_subscriber = drop_subscriber

    def _enqueue(ws, text: str) -> bool:
        """Non-blocking hand-off to a subscriber's writer. Returns False (and drops the
        subscriber) when its bounded queue is full — i.e. it can't keep up."""
        q = app.state.sendq.get(ws)
        if q is None:
            return False
        try:
            q.put_nowait(text)
            return True
        except asyncio.QueueFull:
            drop_subscriber(ws)
            return False

    async def publish(stream: str, event: dict) -> None:
        seq = app.state.seqs.get(stream, -1) + 1
        app.state.seqs[stream] = seq
        buf = app.state.buffers.get(stream)
        if buf is None:
            buf = app.state.buffers[stream] = deque(maxlen=_maxlen)
        st = _stat(stream)
        st["published"] += 1
        if len(buf) == buf.maxlen:
            # Benign ring eviction — count it, don't flood the log (issue #185).
            st["dropped"] += 1
        buf.append((seq, event))
        if len(buf) > st["high_water"]:
            st["high_water"] = len(buf)
        frame_text = _dumps(proto.event_frame(stream, event, seq))
        for ws in list(app.state.subscribers.get(stream, ())):
            if not _enqueue(ws, frame_text):
                # Slow/dead subscriber — count the drop so operators can spot a
                # consumer that can't keep up (issue #185).
                st["dropped_subscribers"] = st.get("dropped_subscribers", 0) + 1

    app.state.publish = publish

    @app.post("/rpc/{method}")
    async def rpc(method: str, request: Request) -> JSONResponse:
        fn = app.state.rpc.get(method)
        if fn is None:
            return JSONResponse(
                {"error": {"type": "UnknownMethod", "message": method}}, status_code=404)
        params = await request.json() if await request.body() else {}
        try:
            result = await fn(params)
            return JSONResponse({"result": result})
        except Exception as exc:  # noqa: BLE001 — surface handler errors as an envelope
            return JSONResponse(
                {"error": {"type": type(exc).__name__, "message": str(exc)}}, status_code=500)

    @app.websocket("/events")
    async def events(ws: WebSocket) -> None:
        await ws.accept()
        mine: set[str] = set()
        # One bounded queue + writer task per connection. The writer is the sole caller
        # of ws.send_text, so replay and live frames never race a concurrent send.
        sendq: asyncio.Queue = asyncio.Queue(maxsize=_sendq_max)
        app.state.sendq[ws] = sendq

        async def _writer() -> None:
            try:
                while True:
                    text = await sendq.get()
                    await ws.send_text(text)
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — peer gone / send failed → tear down
                pass
            finally:
                _detach(ws)
                with contextlib.suppress(Exception):
                    await ws.close()

        app.state.writers[ws] = asyncio.create_task(_writer())
        try:
            while True:
                frame = _loads(await ws.receive_text())
                stream = frame.get("stream")
                if frame.get("type") == proto.SUBSCRIBE and stream:
                    app.state.subscribers.setdefault(stream, set()).add(ws)
                    mine.add(stream)
                    since = frame.get("since")
                    buf = app.state.buffers.get(stream)
                    if buf:
                        # Reconnect-gap detection (issue #185): a resume anchor (since >= 0)
                        # that predates the oldest retained event means the in-between events
                        # were evicted from the ring and CANNOT be replayed from memory. That
                        # was previously silent — the edge would resume mid-stream and look
                        # wedged/desynced. Signal it explicitly so the edge resyncs from disk.
                        oldest_seq = buf[0][0]
                        if since is not None and since >= 0 and since + 1 < oldest_seq:
                            lost = oldest_seq - (since + 1)
                            log.warning(
                                "ipc: replay gap stream=%s since=%d oldest=%d lost=%d "
                                "— edge must resync from disk", stream, since, oldest_seq, lost)
                            _enqueue(ws, _dumps(
                                proto.gap_frame(stream, lost=lost, oldest=oldest_seq)))
                        for seq, event in list(buf):
                            if since is None or seq > since:
                                _enqueue(ws, _dumps(proto.event_frame(stream, event, seq)))
                elif frame.get("type") == proto.UNSUBSCRIBE and stream:
                    app.state.subscribers.get(stream, set()).discard(ws)
                    mine.discard(stream)
        except WebSocketDisconnect:
            pass
        finally:
            for stream in mine:
                app.state.subscribers.get(stream, set()).discard(ws)
            drop_subscriber(ws)

    @app.get("/info")
    async def info() -> dict:
        from server.version import running_git_sha
        return {"sha": running_git_sha(), "protocol": proto.PROTOCOL_VERSION, "epoch": _app_epoch}

    @app.get("/healthz")
    async def healthz() -> dict:
        from server.version import running_git_sha
        # Include per-stream replay-buffer stats (issue #185) so operators can spot the
        # streams that flood the ring (high dropped count) — the population that risks a
        # reconnect gap — without grepping logs.
        return {"status": "ok", "sha": running_git_sha(), "buffers": app.state.buffer_stats()}

    register_rpc(app, "ping", _ping)
    register_rpc(app, "start_demo", _make_start_demo(app))
    register_rpc(app, "echo", _make_echo(app))
    return app


async def _ping(params: dict) -> dict:
    return {"pong": True}


def register_rpc(app: FastAPI, name: str, fn) -> None:
    """Register an `async def fn(params: dict) -> dict` handler under `name`."""
    app.state.rpc[name] = fn


def _dumps(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _loads(raw: str) -> dict:
    return json.loads(raw)


def _make_start_demo(app: FastAPI):
    async def start_demo(params: dict) -> dict:
        stream, n = params["stream"], int(params["n"])
        for i in range(n):
            await app.state.publish(stream, {"type": "tick", "i": i})
        return {"emitted": n}
    return start_demo


def _make_echo(app: FastAPI):
    async def echo(params: dict) -> dict:
        await app.state.publish(params["stream"], {"type": "echo", "text": params["text"]})
        return {"ok": True}
    return echo
