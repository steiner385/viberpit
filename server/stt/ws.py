"""WebSocket `/ws/stt`: browser PCM stream in, transcript events out.

Protocol (client → server): text `{"type":"start","sampleRate":16000}`, then binary
Int16 little-endian mono frames, then text `{"type":"stop"}`.
Protocol (server → client): `{"type":"ready"}` once the model is loaded,
`{"type":"interim"|"final","text":…}`, `{"type":"error","message":…}`, and
`{"type":"done"}` after the flush that `stop` (or the session deadline) triggers.

Gated by the same Tailscale identity as `/ws` and `/ws/vnc`. Audio lives only in
the DictationSession buffer (≤ max_window_s) and is never persisted or logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from server.auth.middleware import require_ws_identity
from server.stt.engine import SAMPLE_RATE, SttEngine
from server.stt.session import DictationSession

log = logging.getLogger(__name__)

CLOSE_NOT_ENABLED = 4004
CLOSE_BUSY = 4005
CLOSE_BAD_PROTOCOL = 1003
CLOSE_INTERNAL = 1011


def build_stt_ws_router(
    get_engine: Callable[[], SttEngine | None],
    *,
    tick_s: float = 1.0,
    max_concurrent: int = 3,
    max_session_s: float = 600.0,
    session_factory: Callable[[SttEngine], DictationSession] = DictationSession,
) -> APIRouter:
    router = APIRouter()
    active = 0

    @router.websocket("/ws/stt")
    async def stt_ws(websocket: WebSocket, identity: str = Depends(require_ws_identity)) -> None:
        nonlocal active
        engine = get_engine()
        await websocket.accept()
        if engine is None:
            await websocket.close(CLOSE_NOT_ENABLED, "stt disabled")
            return
        if active >= max_concurrent:
            await websocket.close(CLOSE_BUSY, "stt busy")
            return
        active += 1
        try:
            await _serve(websocket, engine, session_factory(engine),
                         tick_s=tick_s, max_session_s=max_session_s)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # engine/model failures surface to the UI, never crash the app
            log.warning("stt: session for %s failed: %s", identity, exc)
            await _send(websocket, {"type": "error", "message": str(exc)})
            await _close(websocket, CLOSE_INTERNAL)
        finally:
            active -= 1

    return router


async def _send(ws: WebSocket, msg: dict) -> None:
    if ws.client_state == WebSocketState.CONNECTED and ws.application_state == WebSocketState.CONNECTED:
        await ws.send_json(msg)


async def _close(ws: WebSocket, code: int, reason: str = "") -> None:
    if ws.application_state == WebSocketState.CONNECTED:
        await ws.close(code, reason)


async def _handshake(ws: WebSocket) -> bool:
    """Consume the `start` frame; on a protocol violation send `error`, close 1003, return False."""
    msg = await ws.receive()
    if msg["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(msg.get("code", 1000))
    try:
        data = json.loads(msg["text"]) if msg.get("text") is not None else {}
    except ValueError:
        data = {}
    if data.get("type") != "start":
        await _send(ws, {"type": "error", "message": "expected a start frame"})
        await _close(ws, CLOSE_BAD_PROTOCOL, "expected start")
        return False
    if data.get("sampleRate") != SAMPLE_RATE:
        await _send(ws, {"type": "error", "message": f"sampleRate must be {SAMPLE_RATE}"})
        await _close(ws, CLOSE_BAD_PROTOCOL, "bad sampleRate")
        return False
    return True


async def _serve(ws: WebSocket, engine: SttEngine, session: DictationSession,
                 *, tick_s: float, max_session_s: float) -> None:
    if not await _handshake(ws):
        return
    warm = getattr(engine, "warm", None)
    if warm is not None:
        await asyncio.to_thread(warm)          # first use may download/load the model
    await _send(ws, {"type": "ready"})

    stop = asyncio.Event()

    async def ticker() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=tick_s)
                return
            except asyncio.TimeoutError:
                pass
            for ev in await asyncio.to_thread(session.tick):
                await _send(ws, {"type": ev.kind, "text": ev.text})

    tick_task = asyncio.create_task(ticker())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_session_s
    disconnected = False
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if msg["type"] == "websocket.disconnect":
                disconnected = True
                break
            if (pcm := msg.get("bytes")) is not None:
                session.feed(pcm)
                continue
            text = msg.get("text")
            if text is not None:
                try:
                    data = json.loads(text)
                except ValueError:
                    data = {}
                if data.get("type") == "stop":
                    break
    finally:
        stop.set()
        # Wait for an in-flight tick to finish rather than cancelling mid-send.
        await asyncio.gather(tick_task, return_exceptions=True)

    if disconnected:
        return
    for ev in await asyncio.to_thread(session.flush):
        await _send(ws, {"type": ev.kind, "text": ev.text})
    await _send(ws, {"type": "done"})
    await _close(ws, 1000)
