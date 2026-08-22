"""WebSocket <-> x11vnc RFB byte relay, gated by Tailscale identity."""
from __future__ import annotations
import asyncio
import logging

from fastapi import APIRouter, Depends, WebSocket
from server.auth.middleware import require_ws_identity
from server.ipc.client import daemon_ws_connect

log = logging.getLogger("server.vnc")

CLOSE_NOT_ENABLED = 4401
CLOSE_NO_DISPLAY = 4404


async def _open_rfb_connection(port: int, *, timeout: float = 2.0, interval: float = 0.05):
    """Open a loopback TCP connection to x11vnc's RFB port, retrying on
    ConnectionRefused for up to ``timeout`` seconds before giving up.

    x11vnc is spawned lazily on the first attach and binds its RFB port
    asynchronously (~1s later). X11Manager.ensure_vnc already waits for that
    bind, but the wait is best-effort — under load or a slow x11vnc start it can
    return an unbound port, and a single connect would then race the bind and
    fail with ConnectionRefused, losing the first relay (issue #135). A short
    bounded retry recovers in-process so the user never sees noVNC's
    "Reconnecting…". Mirrors X11Manager._await_ready's poll-with-budget style.
    """
    waited = 0.0
    while True:
        try:
            return await asyncio.open_connection("127.0.0.1", port)
        except ConnectionRefusedError:
            if waited >= timeout:
                raise
            await asyncio.sleep(interval)
            waited += interval


async def _relay(ws, tcp, idle: float = 30.0) -> None:
    """Flow-controlled bidirectional pump. Each direction awaits its write
    before reading more, bounding in-process buffering."""

    async def ws_to_tcp():
        while True:
            try:
                data = await ws.receive_bytes()
            except (StopAsyncIteration, Exception):
                return
            tcp.write(data)
            await tcp.drain()

    async def tcp_to_ws():
        while True:
            data = await tcp.read(65536)
            if not data:
                return
            await ws.send_bytes(data)

    t1 = asyncio.create_task(ws_to_tcp())
    t2 = asyncio.create_task(tcp_to_ws())
    try:
        _done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    finally:
        try:
            await tcp.close()
        except Exception:  # noqa: BLE001 — cleanup is best-effort
            pass


class _StreamAdapter:
    """Adapts asyncio (reader, writer) to the .read/.write/.drain shape _relay uses."""

    def __init__(self, reader, writer):
        self._r = reader
        self._w = writer

    async def read(self, n):
        return await self._r.read(n)

    def write(self, b):
        self._w.write(b)

    async def drain(self):
        await self._w.drain()

    async def close(self):
        try:
            self._w.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass


class _WSAdapter:
    """Adapts a `websockets` client connection to the .read/.write/.drain shape _relay
    uses, so the SAME byte-pump drives both a local TCP RFB stream (in-process mode)
    and a WS-over-IPC tunnel to the daemon (remote mode). EOF (peer close) → read() == b"".
    """

    def __init__(self, ws):
        self._ws = ws
        self._pending: bytes | None = None

    async def read(self, n):
        from websockets.exceptions import ConnectionClosed
        try:
            data = await self._ws.recv()
        except ConnectionClosed:
            return b""
        return data.encode() if isinstance(data, str) else data

    def write(self, b):
        self._pending = b

    async def drain(self):
        if self._pending is not None:
            await self._ws.send(self._pending)
            self._pending = None

    async def close(self):
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass


def build_vnc_ws_router(
    get_x11,
    get_daemon_endpoint=lambda: None,
    get_daemon_ssl=lambda: None,
    get_local_x11=lambda session_id: None,
) -> APIRouter:
    """Return an APIRouter with /ws/vnc.

    Remote mode (get_daemon_endpoint() truthy): proxy the browser WS to the daemon's
    /vnc via daemon_ws_connect (unix or wss), propagating a 4xxx daemon close code.
    In-process mode (default): relay straight to the local x11vnc.

    ``get_local_x11(session_id)`` lets the caller pin specific sessions to a display
    on *this* host even in remote mode, by returning an X11Manager for them (None
    means "not local, route normally"). Reauth uses it: its virtual display runs on
    the master because the master owns the credentials, so proxying its RFB stream
    to the worker would reach a host that never spawned the display."""
    router = APIRouter()

    @router.websocket("/ws/vnc")
    async def vnc_ws(
        websocket: WebSocket,
        identity: str = Depends(require_ws_identity),
    ) -> None:
        session_id = websocket.query_params.get("session", "")
        # A session pinned to a local display never proxies, daemon or not.
        local_x11 = get_local_x11(session_id)
        endpoint = None if local_x11 is not None else get_daemon_endpoint()
        if endpoint:
            from websockets.exceptions import ConnectionClosed
            await websocket.accept()
            close_code = 1000
            try:
                path = f"/vnc?session={session_id}"
                async with daemon_ws_connect(endpoint, path, get_daemon_ssl()) as dws:
                    await _relay(websocket, _WSAdapter(dws))
                    # 4404 (no display) and other daemon close codes reach us here via the
                    # closed daemon connection; _WSAdapter.read swallowed the ConnectionClosed.
                    close_code = dws.close_code or 1000
            except ConnectionClosed as exc:
                close_code = exc.code or 1000
            except Exception:
                log.exception("vnc: remote proxy failed session=%s", session_id)
            if 4000 <= close_code < 5000:
                try:
                    await websocket.close(close_code)
                except Exception:
                    pass
            return

        x11 = local_x11 if local_x11 is not None else get_x11()
        if x11 is None:
            await websocket.accept()
            await websocket.close(CLOSE_NOT_ENABLED, "xvfb disabled")
            return
        if x11.display_for(session_id) is None:
            await websocket.accept()
            await websocket.close(CLOSE_NO_DISPLAY, "no display")
            return
        await websocket.accept()
        port = await x11.attach(session_id)
        try:
            reader, writer = await _open_rfb_connection(port)
            await _relay(websocket, _StreamAdapter(reader, writer))
        except Exception:
            log.exception("vnc: relay failed session=%s", session_id)
        finally:
            x11.detach(session_id)

    return router
