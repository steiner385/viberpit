"""WebSocket `/ws/stt`: browser PCM stream in, transcript events out.

Protocol (client → server): text `{"type":"start","sampleRate":16000}`, then binary
Int16 little-endian mono frames, then text `{"type":"stop"}`.
Protocol (server → client): `{"type":"ready"}` once the model is loaded,
`{"type":"interim"|"final","text":…}`, `{"type":"error","message":…}`, and
`{"type":"done"}` after the flush that `stop` (or the session deadline) triggers.

Gated by the same Tailscale identity as `/ws` and `/ws/vnc`. Audio lives only in
the DictationSession buffer (≤ max_buffer_s) and is never persisted or logged.
"""
from __future__ import annotations

import asyncio
import contextlib
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
CLOSE_TOO_LARGE = 1009
CLOSE_INTERNAL = 1011

# A connection that never sends `start` must not hold a max_concurrent slot forever.
# Read as a module attribute (not a default arg) at call time so tests can monkeypatch
# `server.stt.ws.HANDSHAKE_TIMEOUT_S` without rebuilding the router.
HANDSHAKE_TIMEOUT_S = 10.0

# Largest binary frame we will buffer. The client streams ~100 ms chunks (3,200 bytes),
# so this is ~20x headroom; anything bigger is a misbehaving client trying to make the
# next tick run VAD+whisper over a huge buffer while holding the process inference lock.
MAX_FRAME_BYTES = 65536


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


def _parse_json_obj(text: str) -> dict:
    """Parse a text frame as JSON, tolerating garbage and non-object JSON alike.

    A bare `data.get(...)` on a JSON value that parses fine but isn't an object
    (`5`, `null`, `"x"`) raises AttributeError, which the outer handler would report
    as an internal (1011) failure with a raw Python message on the wire. Collapsing
    both "invalid JSON" and "valid JSON, wrong shape" to `{}` keeps every caller on
    the existing "missing type" validation path instead.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _handshake(ws: WebSocket, deadline: float) -> bool:
    """Consume the `start` frame; on a protocol violation — including a client that
    never sends one — send `error`, close 1003, return False.

    Bounded by both a fixed per-handshake timeout and whatever remains of the
    session deadline, so an idle connection can never hold a max_concurrent slot
    forever.
    """
    loop = asyncio.get_running_loop()
    remaining = max(deadline - loop.time(), 0.0)
    timeout = min(HANDSHAKE_TIMEOUT_S, remaining)
    try:
        msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
    except asyncio.TimeoutError:
        await _send(ws, {"type": "error", "message": "no start frame"})
        await _close(ws, CLOSE_BAD_PROTOCOL, "handshake timeout")
        return False
    if msg["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(msg.get("code", 1000))
    data = _parse_json_obj(msg["text"]) if msg.get("text") is not None else {}
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
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_session_s          # armed BEFORE the handshake
    if not await _handshake(ws, deadline):
        return
    warm = getattr(engine, "warm", None)
    if warm is not None:
        # First use may download/load the model. This call is deliberately NOT
        # time-boxed: the HF hub client has its own request timeouts, and the app
        # lifespan pre-warms the model at startup so this is normally a no-op. The
        # session deadline is armed before the handshake above but only enforced by
        # the receive loop below, i.e. once warm has returned.
        await asyncio.to_thread(warm)
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
    disconnected = False
    oversized = False
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            # Race the next client frame against the ticker: under normal operation
            # ticker() never completes until `stop` is set below, so tick_task
            # finishing here means it raised. Without this race, a ticker failure
            # would sit unreported until the client happens to send `stop` (or the
            # full session deadline elapses) — ws.receive() alone can't see it.
            recv_task = asyncio.ensure_future(ws.receive())
            done, _pending = await asyncio.wait(
                {recv_task, tick_task}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if recv_task not in done:
                recv_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await recv_task
                break  # either the ticker failed, or the deadline elapsed
            msg = recv_task.result()
            if msg["type"] == "websocket.disconnect":
                disconnected = True
                break
            if (pcm := msg.get("bytes")) is not None:
                if len(pcm) > MAX_FRAME_BYTES:
                    oversized = True
                    break
                # Int16 samples: an odd-length frame would make numpy raise
                # "buffer size must be a multiple of element size" on the next tick.
                pcm = pcm[: len(pcm) & ~1]
                if pcm:
                    session.feed(pcm)
                continue
            text = msg.get("text")
            if text is not None:
                data = _parse_json_obj(text)
                if data.get("type") == "stop":
                    break
    finally:
        stop.set()
        # Wait for an in-flight tick to finish rather than cancelling mid-send.
        (tick_result,) = await asyncio.gather(tick_task, return_exceptions=True)

    # A tick failure (e.g. the engine raising inside speech_segments/transcribe)
    # must not be swallowed: without this, the client would still see a clean
    # final/done/1000 close and nothing would be logged. WebSocketDisconnect and
    # CancelledError are expected shutdown signals, not failures to report.
    tick_exc = tick_result if isinstance(tick_result, BaseException) else None
    if isinstance(tick_exc, (WebSocketDisconnect, asyncio.CancelledError)):
        tick_exc = None
    if tick_exc is not None:
        log.warning("stt: tick loop failed: %s", tick_exc)
        await _send(ws, {"type": "error", "message": str(tick_exc)})
        await _close(ws, CLOSE_INTERNAL)
        return

    if oversized:
        # Protocol violation, not a transcription: drop the buffer without flushing.
        await _send(ws, {"type": "error", "message": "frame too large"})
        await _close(ws, CLOSE_TOO_LARGE, "frame too large")
        return

    if disconnected:
        return
    for ev in await asyncio.to_thread(session.flush):
        await _send(ws, {"type": ev.kind, "text": ev.text})
    await _send(ws, {"type": "done"})
    await _close(ws, 1000)
