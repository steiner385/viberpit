"""Edge-side client for the session daemon over a Unix socket."""
import asyncio
import json
import logging
from typing import Any

import httpx
import websockets

from server.ipc import protocol as proto

log = logging.getLogger("server.ipc.client")


class DaemonUnavailable(Exception):
    """The daemon socket could not be reached."""


class DaemonError(Exception):
    """The daemon returned an error envelope for an RPC call."""

    def __init__(self, err_type: str, message: str):
        super().__init__(f"{err_type}: {message}")
        self.err_type = err_type
        self.message = message


class DaemonClient:
    def __init__(self, socket_path: str):
        self._sock = socket_path
        self._http = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            base_url="http://daemon",
            timeout=30.0,
        )
        self._ws: Any = None
        self._reader_task: asyncio.Task | None = None
        self._queues: dict[str, asyncio.Queue] = {}      # stream -> event queue
        self._ready: dict[str, asyncio.Event] = {}        # stream -> subscribed-on-daemon
        self._desired: set[str] = set()                   # streams to (re)subscribe on connect
        self._last_seq: dict[str, int] = {}               # stream -> highest delivered seq
        self._baseline: dict[str, int | None] = {}        # stream -> initial `since` for first subscribe
        self._started = False

    async def call(self, method: str, **params) -> dict:
        try:
            resp = await self._http.post(f"/rpc/{method}", json=params)
        except httpx.ConnectError as exc:
            raise DaemonUnavailable(str(exc)) from exc
        body = resp.json()
        if "error" in body:
            err = body["error"]
            raise DaemonError(err.get("type", "Error"), err.get("message", ""))
        return body["result"]

    def subscribe(self, stream: str, since: int | None = None, maxsize: int = 1000):
        self._queues[stream] = asyncio.Queue(maxsize=maxsize)
        self._ready[stream] = asyncio.Event()
        self._desired.add(stream)
        self._baseline[stream] = since
        if not self._started:
            self._started = True
            self._reader_task = asyncio.create_task(self._reader())
        elif self._ws is not None:
            # Reader already connected — register this new stream now. (On the first
            # subscribe, or after a reconnect, the reader subscribes all _desired itself.)
            asyncio.create_task(self._send_subscribe(stream))
        return self._iter(stream)

    def _since_for(self, stream: str) -> int | None:
        # Reconnect → resume after the last delivered seq; first subscribe → the baseline.
        if stream in self._last_seq:
            return self._last_seq[stream]
        return self._baseline.get(stream)

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

    async def _iter(self, stream: str):
        q = self._queues[stream]
        while True:
            yield await q.get()

    def _deliver(self, stream: str, event: dict, seq: int | None = None) -> None:
        if seq is not None:
            self._last_seq[stream] = seq
        q = self._queues.get(stream)
        if q is None:
            return
        if q.full():
            try:
                q.get_nowait()  # drop oldest
            except asyncio.QueueEmpty:
                pass
            log.warning("ipc: subscriber queue for %s full — dropped oldest", stream)
        q.put_nowait(event)

    async def _reader(self) -> None:
        uri = "ws://daemon/events"
        backoff = 0.05
        while True:
            try:
                async with websockets.unix_connect(self._sock, uri) as ws:
                    self._ws = ws
                    backoff = 0.05
                    for stream in list(self._desired):
                        await ws.send(json.dumps(proto.subscribe_frame(stream, since=self._since_for(stream))))
                        self._ready[stream].set()
                    async for raw in ws:
                        frame = json.loads(raw)
                        if frame.get("type") == proto.EVENT:
                            self._deliver(frame["stream"], frame["event"], frame.get("seq"))
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
