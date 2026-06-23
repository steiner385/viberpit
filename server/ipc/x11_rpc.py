"""Daemon-side x11 surface: a /vnc WebSocket relaying to the daemon-local x11vnc,
plus x11 RPCs. Mirrors orchestrator_rpc.py — x11 knowledge stays out of the generic
transport (daemon.py). The edge reaches all of this over the IPC transport, so the
VNC path never assumes the edge and daemon share 127.0.0.1.
"""
import asyncio
import logging

from fastapi import WebSocket

from server.bridge.vnc_ws import _relay, _StreamAdapter
from server.ipc.daemon import register_rpc

log = logging.getLogger("server.ipc.x11")

CLOSE_NO_DISPLAY = 4404


async def _connect_rfb(port: int, attempts: int = 20, interval: float = 0.05):
    """Connect to the daemon-local x11vnc, tolerating the spawn→bind race: ensure_vnc
    spawns x11vnc and returns immediately, so the RFB port may not be bound yet."""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return await asyncio.open_connection("127.0.0.1", port)
        except OSError as exc:
            last = exc
            await asyncio.sleep(interval)
    assert last is not None
    raise last


def register_x11(app, x11) -> None:
    """Register the daemon /vnc WS endpoint and x11 RPCs against `x11` (may be None)."""

    @app.websocket("/vnc")
    async def vnc(ws: WebSocket) -> None:
        session_id = ws.query_params.get("session", "")
        if x11 is None or x11.display_for(session_id) is None:
            await ws.accept()
            await ws.close(CLOSE_NO_DISPLAY, "no display")
            return
        await ws.accept()
        port = await x11.attach(session_id)
        try:
            reader, writer = await _connect_rfb(port)
            await _relay(ws, _StreamAdapter(reader, writer))
        except Exception:
            log.exception("daemon vnc: relay failed session=%s", session_id)
        finally:
            x11.detach(session_id)

    async def x11_metrics(params: dict) -> dict:
        return x11.metrics() if x11 is not None else {"enabled": False}

    async def x11_open_browser(params: dict) -> dict:
        session_id = params["session_id"]
        if x11 is None or x11.display_for(session_id) is None:
            return {"ok": False, "reason": "no_display"}
        ok = await x11.launch_browser(session_id, params.get("url"))
        return {"ok": bool(ok), "reason": None if ok else "cap"}

    register_rpc(app, "x11_metrics", x11_metrics)
    register_rpc(app, "x11_open_browser", x11_open_browser)
