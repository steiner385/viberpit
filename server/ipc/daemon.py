"""The session-daemon ASGI app: unary RPC registry + (Task 3) multiplexed event WS.

Served by uvicorn bound to a Unix socket. No TCP. No app-level auth — the socket's
filesystem permissions are the trust boundary.
"""
import asyncio
import contextlib
import json
import logging
import os
import time
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
    #
    # Default 8192 (audit W2.1 mitigation, raised from 2048): the wider ring is what makes
    # the wave-3 cursor catch-up (attach.sinceSrcSeq → resumeOk, server/bridge/ws.py) hit
    # instead of falling back to a full disk replay across a longer reconnect gap. Memory
    # trade-off: the ring is PER LIVE STREAM and holds at most _maxlen (seq, event) tuples;
    # at ~1–2 KB per normalized event that is roughly 8–16 MB per busy session at the cap,
    # and far less typically (idle streams hold only their turn tail). Bounded and evicted
    # oldest-first, so it never grows without limit. Override with CCSWITCH_IPC_REPLAY_BUFFER.
    _maxlen = int(os.environ.get("CCSWITCH_IPC_REPLAY_BUFFER", "8192"))
    app.state.buffers = {}   # stream -> deque[(seq, event)]
    app.state.seqs = {}      # stream -> last assigned seq (int)
    # Turn-boundary tracking for in-progress-turn replay: the seq of the current
    # turn's user message, and whether a turn is open. A reconnecting edge resumes
    # from turn_start_seq so the whole in-progress turn replays from the ring.
    app.state.turn_start_seqs = {}   # stream -> seq of the last `user` event
    app.state.turn_open = {}         # stream -> bool
    # Wall-clock start of the open turn (epoch seconds, recorded when its `user`
    # event was published). The edge threads it through ensure_driver → RemoteDriver
    # → replay_window so the incomplete-turn drop only trims the turn that actually
    # IS in progress: the CLI's disk write lags the live stream (MCP fleet boot on
    # resume), so the disk's last user entry can belong to a PREVIOUS, completed
    # turn — dropping that amputates history nothing re-supplies.
    app.state.turn_start_ts = {}     # stream -> float epoch seconds
    # A turn closes on end-of-turn EVENTS, not on a status guess. `turnEnd` (SDK
    # ResultMessage → ev.TurnEnd) is the canonical completion; `turnFailure`
    # (ev.TurnFailure — the recoverable rate_limit/auth_401/turn_dropped settle) is
    # the failure completion. `_TURN_END_EVENTS` names them.
    _TURN_END_EVENTS = {"turnEnd", "turnFailure"}
    # Fallback ONLY: the drain's exception path settles to a terminal status WITHOUT
    # emitting a turnEnd (TurnEnd is produced solely from the SDK ResultMessage during
    # a normal receive), so a `status` in this set is the only close signal there.
    # `idle` is deliberately EXCLUDED: derive_status returns it only as the never-ran
    # sentinel (turns_completed == 0), and recovery/reset paths (interrupt of the first
    # turn, a rehydrated/rebuilt driver) emit it mid-flight — closing on it would drop
    # the resume anchor and null turn_started_at, re-enabling the #482 amputation.
    _TERMINAL_STATUSES = {"done", "complete", "needs_attention"}
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

    def turn_open_for(stream: str) -> bool:
        return bool(app.state.turn_open.get(stream))

    app.state.turn_open_for = turn_open_for

    def turn_started_at(stream: str) -> float | None:
        # Meaningful only while the turn is open. turn_open is flipped False by an
        # end-of-turn EVENT (turnEnd / turnFailure) AND by a terminal status arriving
        # without one (drain exception path) — gate on it rather than on the raw ts so
        # a settled turn can't leak its stale start time, and a transient mid-turn
        # `idle` (no longer a close condition) can't null a still-open turn's start.
        if app.state.turn_open.get(stream):
            return app.state.turn_start_ts.get(stream)
        return None

    app.state.turn_started_at = turn_started_at

    def resume_since(stream: str) -> int | None:
        # While a turn is open, resume from just before its user message so the whole
        # in-progress turn replays from the ring; otherwise from-now (disk has the rest).
        if app.state.turn_open.get(stream):
            return app.state.turn_start_seqs.get(stream, 0) - 1
        return head_seq(stream)

    app.state.resume_since = resume_since

    def ring_covers(stream: str, since: int | None) -> bool:
        """True when a live subscribe at ``since`` would replay the whole tail from the
        ring WITHOUT a GAP — i.e. the daemon can resume this stream from ``since`` with no
        data loss. This is the coverage probe the edge runs BEFORE committing to the
        wave-3 cursor catch-up (server/bridge/ws.py): deciding first means a not-covered
        cursor never leaves a half-sent ``resumeOk`` on the wire (the airtight-fallback
        requirement).

        Covered iff the anchor sits inside the retained window:
          * ``since + 1 >= oldest`` — mirrors the EXACT subscribe-time GAP test
            (``since + 1 < oldest`` ⇒ evicted ⇒ GAP), so the probe and the real subscribe
            can never disagree; and
          * ``since <= head`` — the anchor is not AHEAD of what this daemon has produced.
            A browser srcSeq cursor can only exceed head after a daemon restart (the seq
            space is per-process and resets to 0); its stale high cursor must never be
            treated as covered by the fresh low ring. (The browser also flips its
            srcContiguous flag on the post-restart srcSeq jump — this is the server-side
            belt to that suspenders.)

        A fresh anchor (``since`` None/<0) is never a catch-up cursor, and an empty/absent
        ring can guarantee nothing → both return False (full disk replay)."""
        buf = app.state.buffers.get(stream)
        if not buf or since is None or since < 0:
            return False
        oldest_seq = buf[0][0]
        head = app.state.seqs.get(stream, -1)
        return oldest_seq - 1 <= since <= head

    app.state.ring_covers = ring_covers

    def ring_tail(stream: str, since: int | None) -> dict:
        """One-shot snapshot of the retained ring events AFTER ``since`` (seq in
        ``(since, head]``), for the wave-3 cursor catch-up.

        Why a snapshot RPC and not just a re-subscribe at ``since``: the edge's shared
        DaemonClient keeps ONE daemon subscription PER STREAM (server/ipc/client.py) — a
        re-subscribe by a healing/reconnecting viewer joins the existing fan-out and its
        ``since`` is IGNORED, so it would never replay the missed tail (and a naive
        since-override would leave the client falsely told it caught up). Instead the edge
        replays this snapshot then continues on the shared live fan-out, deduping the
        overlap by seq (RemoteDriver.events()). ``head`` lets that dedup know where the
        snapshot ends. Not covered ⇒ empty snapshot; the edge falls back to a disk replay."""
        head = app.state.seqs.get(stream, -1)
        if not ring_covers(stream, since):
            return {"covered": False, "events": [], "head": head}
        buf = app.state.buffers.get(stream) or ()
        events = [{"seq": s, "event": e} for (s, e) in buf if since is None or s > since]
        return {"covered": True, "events": events, "head": head}

    app.state.ring_tail = ring_tail

    def evict(stream: str) -> None:
        app.state.buffers.pop(stream, None)
        app.state.seqs.pop(stream, None)
        app.state.stats.pop(stream, None)
        app.state.turn_start_seqs.pop(stream, None)
        app.state.turn_open.pop(stream, None)
        app.state.turn_start_ts.pop(stream, None)

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
        etype = event.get("type")
        if etype == "user":
            app.state.turn_start_seqs[stream] = seq
            app.state.turn_open[stream] = True
            app.state.turn_start_ts[stream] = time.time()
        elif etype in _TURN_END_EVENTS:
            app.state.turn_open[stream] = False
        elif etype == "status" and event.get("state") in _TERMINAL_STATUSES:
            app.state.turn_open[stream] = False
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
