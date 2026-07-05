"""Edge-side client for the session daemon over a Unix socket or mTLS TCP."""
import asyncio
import json
import logging
from typing import Any

import httpx
import websockets

from server.ipc import protocol as proto
from server.ipc.paths import Endpoint, parse_endpoint

log = logging.getLogger("server.ipc.client")


def protocol_compatible(daemon_protocol: int) -> bool:
    """True when the daemon's wire protocol matches this build's."""
    from server.ipc import protocol as _proto
    return daemon_protocol == _proto.PROTOCOL_VERSION


class DaemonUnavailable(Exception):
    """The daemon socket could not be reached."""


class DaemonError(Exception):
    """The daemon returned an error envelope for an RPC call."""

    def __init__(self, err_type: str, message: str):
        super().__init__(f"{err_type}: {message}")
        self.err_type = err_type
        self.message = message


def daemon_ws_connect(endpoint: Endpoint, path: str, ssl_ctx=None):
    """Return a websockets connect context manager for a daemon WS path.

    Single source of truth for opening edge→daemon WebSockets (events + vnc).
    `path` is like "/events" or "/vnc?session=abc".
    """
    if endpoint.is_unix:
        return websockets.unix_connect(endpoint.path, f"ws://daemon{path}")
    return websockets.connect(f"wss://{endpoint.host}:{endpoint.port}{path}", ssl=ssl_ctx)


class DaemonClient:
    def __init__(self, endpoint, *, client_tls=None):
        if isinstance(endpoint, str):
            endpoint = parse_endpoint(endpoint if "://" in endpoint else f"unix://{endpoint}")
        self.endpoint = endpoint
        if endpoint.is_unix:
            self._ssl = None
            self._http = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=endpoint.path),
                base_url="http://daemon",
                timeout=30.0,
            )
        else:
            from server.ipc.tls import client_ssl_context, client_tls_files
            self._ssl = client_ssl_context(client_tls or client_tls_files())
            self._http = httpx.AsyncClient(
                base_url=f"https://{endpoint.host}:{endpoint.port}",
                verify=self._ssl,
                timeout=30.0,
            )
        self._ws: Any = None
        self._reader_task: asyncio.Task | None = None
        # stream -> set of subscriber queues. A stream may have MORE THAN ONE local
        # subscriber (e.g. the same session open in two tabs/devices, both served by
        # this one shared edge client). Each gets its own queue; _deliver fans out to
        # all of them. Previously this was a single queue per stream that subscribe()
        # overwrote, so a second subscriber silently starved the first — the live
        # transcript went dead until a detach/reattach forced a fresh disk replay.
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        # queue -> events dropped since its last emitted GAP. A full queue drops its
        # oldest event; the drop is otherwise invisible (the daemon watermark already
        # advanced, so a reconnect never re-sends it). _iter drains this into ONE GAP
        # per overflow burst so the consumer resyncs from disk. Keyed by identity;
        # popped when the subscriber is discarded (single loop, no locking).
        self._dropped: dict[asyncio.Queue, int] = {}
        self._ready: dict[str, asyncio.Event] = {}        # stream -> subscribed-on-daemon
        self._desired: set[str] = set()                   # streams to (re)subscribe on connect
        self._last_seq: dict[str, int] = {}               # stream -> highest delivered seq
        self._baseline: dict[str, int | None] = {}        # stream -> initial `since` for first subscribe
        self._epoch: str | None = None                    # daemon PROC_EPOCH; change ⇒ restart
        self._started = False

    async def call(self, method: str, *, timeout: float | None = None, **params) -> dict:
        """Invoke an RPC. ``timeout`` overrides the client default for this call.

        A wedged/slow daemon surfaces as a typed ``DaemonUnavailable`` — NOT a raw
        ``httpx.ReadTimeout`` bubbling to an unhandled ASGI 500 (the GET /api/accounts
        wedge, incident 2026-07-02). Callers on the control path (usage, counts) pass a
        short timeout so one wedged daemon can't tie up the edge for the full 30s.
        """
        post_kwargs = {} if timeout is None else {"timeout": timeout}
        try:
            resp = await self._http.post(f"/rpc/{method}", json=params, **post_kwargs)
        except httpx.ConnectError as exc:
            raise DaemonUnavailable(str(exc)) from exc
        except httpx.TimeoutException as exc:
            raise DaemonUnavailable(f"{method} timed out: {exc}") from exc
        body = resp.json()
        if "error" in body:
            err = body["error"]
            raise DaemonError(err.get("type", "Error"), err.get("message", ""))
        return body["result"]

    async def info(self) -> dict:
        try:
            resp = await self._http.get("/info")
        except httpx.ConnectError as exc:
            raise DaemonUnavailable(str(exc)) from exc
        return resp.json()

    def _add_subscriber(self, stream: str, maxsize: int = 1000) -> asyncio.Queue:
        """Register a new local subscriber queue for *stream* and return it."""
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers.setdefault(stream, set()).add(q)
        self._dropped[q] = 0
        return q

    def subscribe(self, stream: str, since: int | None = None, maxsize: int = 1000):
        q = self._add_subscriber(stream, maxsize)
        self._ready.setdefault(stream, asyncio.Event())
        # The daemon-side subscription is per stream, not per local subscriber. Only the
        # FIRST subscriber establishes (and seeds the resume baseline for) the stream;
        # additional subscribers just join the fan-out. Re-sending subscribe for an
        # already-desired stream would re-trigger the daemon's replay-on-subscribe and
        # duplicate events into every queue.
        newly_desired = stream not in self._desired
        if newly_desired:
            self._desired.add(stream)
            self._baseline[stream] = since
        if not self._started:
            self._started = True
            self._reader_task = asyncio.create_task(self._reader())
        elif self._ws is not None and newly_desired:
            # Reader already connected — register this new stream now. (On the first
            # subscribe, or after a reconnect, the reader subscribes all _desired itself.)
            asyncio.create_task(self._send_subscribe(stream))
        return self._iter(stream, q)

    def _since_for(self, stream: str) -> int | None:
        # Reconnect → resume after the last delivered seq; first subscribe → the baseline.
        if stream in self._last_seq:
            return self._last_seq[stream]
        return self._baseline.get(stream)

    async def _reconcile_epoch(self) -> None:
        """Detect a daemon restart and drop stale per-stream watermarks.

        The daemon's seq space is per-process: a restart resets seq→0. Carrying the
        old high `since` into the new daemon makes its `seq > since` replay filter drop
        every restart-window event. On a changed epoch, clear `_last_seq` and reset each
        baseline to None so the next subscribe is `since=None` (full-ring replay).
        Best-effort: if `/info` is unreachable, keep prior watermarks and retry next loop.
        """
        try:
            epoch = (await self.info()).get("epoch")
        except Exception:  # noqa: BLE001 — /info momentarily unavailable mid-restart
            return
        if epoch is None:
            return
        if self._epoch is not None and epoch != self._epoch:
            self._last_seq.clear()
            for stream in self._baseline:
                self._baseline[stream] = None
        self._epoch = epoch

    async def _send_subscribe(self, stream: str) -> None:
        ws = self._ws
        if ws is None:
            return  # a reconnect will re-subscribe all _desired
        try:
            await ws.send(json.dumps(proto.subscribe_frame(stream, since=self._since_for(stream))))
            self._ready[stream].set()
        except Exception:  # noqa: BLE001 — reader re-subscribes on its next connect
            pass

    async def events_ready(self, stream: str) -> None:
        """Await until the daemon has acknowledged the subscription (test sync point)."""
        await asyncio.wait_for(self._ready[stream].wait(), 5)

    async def _iter(self, stream: str, q: asyncio.Queue):
        try:
            while True:
                lost = self._dropped.get(q, 0)
                if lost:
                    # This queue overflowed since we last drained it: emit ONE GAP for
                    # the whole burst (not one per dropped event), then reset. Same wire
                    # shape as the daemon ring-eviction GAP so ws._forward_live's existing
                    # branch forces a from-disk resync. oldest=None: a local drop has no
                    # daemon seq anchor. seq is untouched — a gap is not a real event.
                    self._dropped[q] = 0
                    yield {"type": proto.GAP, "stream": stream, "lost": lost, "oldest": None}
                yield await q.get()
        finally:
            # Drop just THIS subscriber's queue; other subscribers on the stream keep
            # receiving. (GeneratorExit/cancel of a consuming task lands here.)
            subs = self._subscribers.get(stream)
            if subs is not None:
                subs.discard(q)
            self._dropped.pop(q, None)

    def _deliver(self, stream: str, event: dict, seq: int | None = None) -> None:
        if seq is not None:
            self._last_seq[stream] = seq
        subs = self._subscribers.get(stream)
        if not subs:
            return
        for q in subs:
            if q.full():
                try:
                    q.get_nowait()  # drop oldest
                except asyncio.QueueEmpty:
                    pass
                # Count the drop; _iter turns a non-zero count into one GAP for the
                # consumer so the lost span is visible instead of silently vanishing.
                self._dropped[q] = self._dropped.get(q, 0) + 1
                log.warning("ipc: subscriber queue for %s full — dropped oldest (%d lost, GAP pending)",
                            stream, self._dropped[q])
            q.put_nowait(event)

    async def _reader(self) -> None:
        backoff = 0.05
        while True:
            try:
                async with daemon_ws_connect(self.endpoint, "/events", self._ssl) as ws:
                    self._ws = ws
                    backoff = 0.05
                    # A reconnect may be a brand-new daemon (deploy/crash) whose seq
                    # space reset to 0. Reconcile the epoch BEFORE resubscribing so a
                    # stale `since` watermark can't make the new daemon skip every
                    # restart-window event (the silent transcript-tail gap).
                    await self._reconcile_epoch()
                    for stream in list(self._desired):
                        await ws.send(json.dumps(proto.subscribe_frame(stream, since=self._since_for(stream))))
                        self._ready[stream].set()
                    async for raw in ws:
                        frame = json.loads(raw)
                        ftype = frame.get("type")
                        if ftype == proto.EVENT:
                            self._deliver(frame["stream"], frame["event"], frame.get("seq"))
                        elif ftype == proto.GAP:
                            # The daemon's in-memory replay couldn't cover our resume anchor
                            # (issue #185): `lost` events were evicted before we reconnected.
                            # Surface it loudly — the in-session view for this stream is now
                            # behind durable storage and should be resynced from the transcript.
                            stream = frame.get("stream")
                            log.warning(
                                "ipc: replay gap on %s — %s event(s) lost before seq %s; "
                                "in-memory view is incomplete (resync from disk)",
                                stream, frame.get("lost"), frame.get("oldest"))
                            # Propagate the gap to local subscribers as a control event so
                            # the consumer (RemoteDriver → ws._forward_live) can trigger a
                            # from-disk resync. Logging alone left the edge view silently
                            # behind durable storage. seq=None: a gap is not a real event, so
                            # it must NOT advance the resume watermark.
                            self._deliver(stream, {
                                "type": proto.GAP, "stream": stream,
                                "lost": frame.get("lost"), "oldest": frame.get("oldest"),
                            })
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — drop/connect failures → retry
                log.warning("ipc: events WS error (%s) — reconnecting in %.2fs", exc, backoff)
            self._ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 2.0)

    async def aclose(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._http.aclose()
