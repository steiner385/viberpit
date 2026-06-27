"""The session-daemon ASGI app: unary RPC registry + (Task 3) multiplexed event WS.

Served by uvicorn bound to a Unix socket. No TCP. No app-level auth — the socket's
filesystem permissions are the trust boundary.
"""
import json
import logging
import os
from collections import deque
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from server.ipc import protocol as proto

log = logging.getLogger("server.ipc.daemon")


def create_daemon_app() -> FastAPI:
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
        frame = proto.event_frame(stream, event, seq)
        for ws in list(app.state.subscribers.get(stream, ())):
            try:
                await ws.send_text(_dumps(frame))
            except Exception:  # noqa: BLE001 — drop a dead/slow connection, keep publishing
                app.state.subscribers.get(stream, set()).discard(ws)

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
                            try:
                                await ws.send_text(_dumps(
                                    proto.gap_frame(stream, lost=lost, oldest=oldest_seq)))
                            except Exception:  # noqa: BLE001 — peer gone before replay
                                continue
                        for seq, event in list(buf):
                            if since is None or seq > since:
                                try:
                                    await ws.send_text(_dumps(proto.event_frame(stream, event, seq)))
                                except Exception:  # noqa: BLE001 — peer gone mid-replay
                                    break
                elif frame.get("type") == proto.UNSUBSCRIBE and stream:
                    app.state.subscribers.get(stream, set()).discard(ws)
                    mine.discard(stream)
        except WebSocketDisconnect:
            pass
        finally:
            for stream in mine:
                app.state.subscribers.get(stream, set()).discard(ws)

    @app.get("/info")
    async def info() -> dict:
        from server.version import running_git_sha
        return {"sha": running_git_sha(), "protocol": proto.PROTOCOL_VERSION}

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
