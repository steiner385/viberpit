"""WebSocket bridge — T035.

Single authenticated WebSocket per client.  Multiplexes terminal I/O and
lifecycle events per the contracts/websocket.md contract.

Factory:
    router = build_ws_router(orchestrator)
    app.include_router(router)

Message protocol (JSON):
  client→server: attach, detach, input, resize, interrupt
  server→client: output, replayComplete, status, usage, rotated, sessionEnded, error

NFR-005: output frames coalesced and flushed no more often than ~16 ms.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from server.auth.middleware import require_ws_identity

if TYPE_CHECKING:
    from server.sessions.orchestrator import SessionOrchestrator

log = logging.getLogger(__name__)

# Output batching cadence (seconds) — ~16ms matches a 60fps display refresh
_BATCH_INTERVAL = 0.016


def build_ws_router(orchestrator: "SessionOrchestrator") -> APIRouter:
    """Return an APIRouter containing the /ws endpoint wired to *orchestrator*."""
    router = APIRouter()

    @router.websocket("/ws")
    async def ws_endpoint(
        websocket: WebSocket,
        identity: str = Depends(require_ws_identity),
    ) -> None:
        await websocket.accept()
        log.debug("ws.connect identity=%s", identity)

        # Per-connection state
        # attached_sessions: session_id → (output_queue, batch_task, mgr_queue)
        attached: dict[str, tuple[asyncio.Queue, asyncio.Task, asyncio.Queue]] = {}

        # Subscribe to orchestrator lifecycle events (rotated, etc.)
        event_q = orchestrator.subscribe_events()

        async def _send(msg: dict) -> None:
            """Send a JSON message; ignore send errors after disconnect."""
            try:
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_text(json.dumps(msg))
            except Exception:
                pass

        async def _send_error(code: str, message: str, session_id: str | None = None) -> None:
            payload: dict = {"type": "error", "code": code, "message": message}
            if session_id is not None:
                payload["sessionId"] = session_id
            await _send(payload)

        # ------------------------------------------------------------------
        # Output batcher per attached session
        # ------------------------------------------------------------------

        async def _batch_output(session_id: str, out_q: asyncio.Queue) -> None:
            """Coalesce output chunks from out_q and flush every _BATCH_INTERVAL."""
            buf: list[bytes] = []
            while True:
                try:
                    # Drain available items without blocking
                    while True:
                        chunk = out_q.get_nowait()
                        if chunk is None:
                            # Sentinel: process exited
                            if buf:
                                await _send({
                                    "type": "output",
                                    "sessionId": session_id,
                                    "data": base64.b64encode(b"".join(buf)).decode(),
                                })
                                buf = []
                            # Determine exit code
                            code = orchestrator.manager.exit_code(session_id)
                            await _send({"type": "sessionEnded", "sessionId": session_id, "code": code})
                            return
                        buf.append(chunk)
                except asyncio.QueueEmpty:
                    pass

                if buf:
                    await _send({
                        "type": "output",
                        "sessionId": session_id,
                        "data": base64.b64encode(b"".join(buf)).decode(),
                    })
                    buf = []

                # Wait one batch interval, or wake early if new data arrives
                try:
                    chunk = await asyncio.wait_for(out_q.get(), timeout=_BATCH_INTERVAL)
                    if chunk is None:
                        code = orchestrator.manager.exit_code(session_id)
                        await _send({"type": "sessionEnded", "sessionId": session_id, "code": code})
                        return
                    buf.append(chunk)
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    return

        # ------------------------------------------------------------------
        # Lifecycle event forwarder (rotated, etc.)
        # ------------------------------------------------------------------

        async def _forward_events() -> None:
            """Forward orchestrator lifecycle events to the WS client."""
            try:
                while True:
                    event = await event_q.get()
                    await _send(event)
            except asyncio.CancelledError:
                pass

        event_task = asyncio.create_task(_forward_events(), name="ws-events")

        # ------------------------------------------------------------------
        # Message handler helpers
        # ------------------------------------------------------------------

        async def _handle_attach(session_id: str) -> None:
            if session_id in attached:
                return  # already attached

            mgr = orchestrator.manager
            try:
                scrollback = mgr.scrollback(session_id)
            except KeyError:
                await _send_error("SESSION_NOT_FOUND", f"Session {session_id!r} not found", session_id)
                return

            # Replay scrollback as output frames
            if scrollback:
                await _send({
                    "type": "output",
                    "sessionId": session_id,
                    "data": base64.b64encode(scrollback).decode(),
                })

            await _send({"type": "replayComplete", "sessionId": session_id})

            # Subscribe to live output
            mgr_q = mgr.subscribe(session_id)
            out_q: asyncio.Queue = asyncio.Queue()

            async def _relay() -> None:
                """Relay from mgr_q → out_q (so the batcher drives output_q)."""
                try:
                    while True:
                        chunk = await mgr_q.get()
                        out_q.put_nowait(chunk)
                        if chunk is None:
                            return
                except asyncio.CancelledError:
                    pass

            relay_task = asyncio.create_task(_relay(), name=f"ws-relay-{session_id}")
            batch_task = asyncio.create_task(
                _batch_output(session_id, out_q), name=f"ws-batch-{session_id}"
            )

            # Store relay + batch tasks; we clean up both on detach
            # We store (out_q, combined_cleanup_task, mgr_q) for cleanup
            # Use a wrapper task that waits for both
            async def _combined() -> None:
                await asyncio.gather(relay_task, batch_task, return_exceptions=True)

            combined = asyncio.create_task(_combined(), name=f"ws-combined-{session_id}")
            attached[session_id] = (out_q, combined, mgr_q)

        async def _handle_detach(session_id: str) -> None:
            if session_id not in attached:
                return
            _out_q, combined, mgr_q = attached.pop(session_id)
            combined.cancel()
            try:
                await asyncio.wait_for(combined, timeout=0.5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            try:
                orchestrator.manager.unsubscribe(session_id, mgr_q)
            except Exception:
                pass

        async def _handle_input(session_id: str, data: str) -> None:
            try:
                raw = base64.b64decode(data)
            except Exception:
                # Accept plain text as fallback
                raw = data.encode()
            try:
                await orchestrator.manager.write_input(session_id, raw)
            except KeyError:
                await _send_error("SESSION_NOT_FOUND", f"Session {session_id!r} not found", session_id)

        async def _handle_resize(session_id: str, cols: int, rows: int) -> None:
            try:
                await orchestrator.manager.resize(session_id, cols, rows)
            except KeyError:
                await _send_error("SESSION_NOT_FOUND", f"Session {session_id!r} not found", session_id)

        async def _handle_interrupt(session_id: str) -> None:
            try:
                await orchestrator.manager.write_input(session_id, b"\x03")
            except KeyError:
                await _send_error("SESSION_NOT_FOUND", f"Session {session_id!r} not found", session_id)

        # ------------------------------------------------------------------
        # Main receive loop
        # ------------------------------------------------------------------

        try:
            while True:
                try:
                    raw = await websocket.receive_text()
                except WebSocketDisconnect:
                    break
                except Exception:
                    break

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await _send_error("INVALID_JSON", "Message must be a JSON object")
                    continue

                msg_type = msg.get("type", "")
                session_id = msg.get("sessionId", "")

                if msg_type == "attach":
                    await _handle_attach(session_id)
                elif msg_type == "detach":
                    await _handle_detach(session_id)
                elif msg_type == "input":
                    await _handle_input(session_id, msg.get("data", ""))
                elif msg_type == "resize":
                    await _handle_resize(session_id, int(msg.get("cols", 80)), int(msg.get("rows", 24)))
                elif msg_type == "interrupt":
                    await _handle_interrupt(session_id)
                else:
                    await _send_error("UNKNOWN_TYPE", f"Unknown message type: {msg_type!r}")

        finally:
            # Clean up all attached sessions
            for sid in list(attached.keys()):
                await _handle_detach(sid)

            # Stop lifecycle event forwarder
            event_task.cancel()
            try:
                await asyncio.wait_for(event_task, timeout=0.5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

            orchestrator.unsubscribe_events(event_q)
            log.debug("ws.disconnect identity=%s", identity)

    return router
