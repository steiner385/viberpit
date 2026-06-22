"""WebSocket bridge — normalized-event transport.

Single authenticated WebSocket per client.  Ships normalized session events
(``e.to_wire()`` dicts) coalesced into ``batch`` frames; no PTY bytes.

Factory:
    router = build_ws_router(orchestrator)
    app.include_router(router)

Message protocol (JSON):
  client→server: attach, detach, prompt, interrupt, permissionResponse,
                 setPermissionMode, answerQuestion
  server→client: batch, replayComplete, permissionRequest, error

Security:
  - Handshake identity via the require_ws_identity dependency.
  - Per-frame identity re-verification: a frame whose identity no longer matches
    the handshake identity closes the socket with 1008.
  - 256KB inbound frame cap.

Outbound events are coalesced and flushed no more often than ~16 ms.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from server.audit import record_audit, redact_event
from server.auth.identity import verify_identity
from server.auth.middleware import require_ws_identity
from server.db import db_session
from server.sessions.events import ErrorEvent, tool_kind
from server.sessions.replay import replay_events

if TYPE_CHECKING:
    from server.sessions.orchestrator import SessionOrchestrator

log = logging.getLogger(__name__)

# Outbound batching cadence (seconds) — ~16ms matches a 60fps display refresh
_BATCH_INTERVAL = 0.016
# Maximum inbound frame size (bytes)
_MAX_FRAME = 262144


async def _forward_live(driver, out_q: "asyncio.Queue[dict]", session_id: str) -> None:
    """Forward a driver's live events into *out_q* as wire dicts.

    A crash in the stream must not leave the UI silent: convert the exception into
    an ``error`` frame and enqueue it so the client learns the live stream died
    instead of staring at a frozen transcript. Cancellation is normal (detach).
    """
    try:
        async for e in driver.events():
            out_q.put_nowait(e.to_wire())
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        log.exception("ws.live_stream session=%s", session_id)
        detail = str(exc).strip() or "no detail"
        out_q.put_nowait(ErrorEvent(
            "live_stream_failed",
            f"The live session stream stopped unexpectedly. {type(exc).__name__}: {detail[:500]}",
            turn_id=None,
        ).to_wire())


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

        # Single outbound queue of already-wire-format dicts, drained by the batcher.
        out_q: asyncio.Queue[dict] = asyncio.Queue()
        # session_id → live-stream task forwarding driver events into out_q
        live_tasks: dict[str, asyncio.Task] = {}

        # Subscribe to orchestrator lifecycle events (rotated, etc.)
        event_q = orchestrator.subscribe_events()

        async def _send(msg: dict) -> None:
            """Send a JSON message; ignore send errors after disconnect."""
            try:
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_text(json.dumps(msg))
            except Exception:
                pass

        # ------------------------------------------------------------------
        # Outbound batcher — coalesces wire dicts into one batch frame / interval
        # ------------------------------------------------------------------

        async def _batcher() -> None:
            try:
                while True:
                    # Block for the first item, then drain everything available.
                    first = await out_q.get()
                    buf = [first]
                    # Brief settle window so bursts coalesce into one frame.
                    await asyncio.sleep(_BATCH_INTERVAL)
                    while True:
                        try:
                            buf.append(out_q.get_nowait())
                        except asyncio.QueueEmpty:
                            break
                    # Redact each event dict before transmission.
                    redacted_buf = [redact_event(e) for e in buf]
                    await _send({"type": "batch", "events": redacted_buf})
            except asyncio.CancelledError:
                return

        batcher_task = asyncio.create_task(_batcher(), name="ws-batcher")

        # ------------------------------------------------------------------
        # Lifecycle event forwarder (rotated, etc.) → batcher
        # ------------------------------------------------------------------

        async def _forward_events() -> None:
            try:
                while True:
                    event = await event_q.get()
                    out_q.put_nowait(event)
            except asyncio.CancelledError:
                pass

        event_task = asyncio.create_task(_forward_events(), name="ws-events")

        # ------------------------------------------------------------------
        # Attach / detach
        # ------------------------------------------------------------------

        async def _handle_attach(session_id: str) -> None:
            if session_id in live_tasks:
                return  # already attached

            try:
                # Thread the connection's verified identity onto the resumed driver so
                # that AskUserQuestion / permission answers (registered under
                # driver.identity, resolved with this WS identity) actually match.
                driver = await orchestrator.ensure_driver(session_id, identity)
            except Exception:
                # A resumed/rehydrated session can fail to start its SDK subprocess —
                # most commonly because the account's token is dead (needs re-auth) or
                # the stored claude_session_id can no longer be resumed. Letting that
                # propagate would crash the WS handler, closing the socket and making
                # the client reconnect-loop forever on a blank screen. Report it and
                # keep the socket alive (replayComplete clears the loading state).
                log.warning("ws.attach: ensure_driver failed for %s", session_id, exc_info=True)
                rec = orchestrator.get_session(session_id)
                acct = rec.account if rec else None
                hint = (f" Its account '{acct}' may need re-authentication — check the Accounts tab."
                        if acct else "")
                await _send({"type": "error", "code": "SESSION_START_FAILED",
                             "message": f"Couldn't resume this session.{hint}",
                             "sessionId": session_id})
                await _send({"type": "replayComplete"})
                return
            if driver is None:
                await _send({"type": "error", "code": "SESSION_NOT_FOUND",
                             "message": f"Session {session_id!r} not found",
                             "sessionId": session_id})
                await _send({"type": "replayComplete"})
                return

            # 1) Replay transcript (if any) as a single batch frame. A transcript that
            #    can't be located or parsed must NOT crash the WS (which would close the
            #    socket → client reconnect-loop on a blank screen) — skip the history and
            #    continue; the live session still works.
            try:
                path = orchestrator.transcript_path(session_id)
                events = await replay_events(path) if path else []
                if events:
                    redacted_replay = [redact_event(e.to_wire()) for e in events]
                    await _send({"type": "batch", "events": redacted_replay})
            except Exception:
                log.warning("ws.attach: transcript replay failed for %s", session_id, exc_info=True)
            await _send({"type": "replayComplete"})

            # 2) Re-emit any pending permission requests for this session.
            for p in orchestrator._registry.pending(session_id):
                perm_frame = redact_event({
                    "type": "permissionRequest", "permId": p["permId"],
                    "turnId": p["turn_id"], "tool": p["tool"],
                    "toolKind": tool_kind(p["tool"]).value,
                    "input": p["input"], "plan": False,
                })
                await _send(perm_frame)

            # 2b) Re-emit any pending AskUserQuestion events for this session.
            #     Mirrors the permission re-emit above so a refresh/reattach while the
            #     model is blocked in _handle_ask_user_question never silently hangs.
            from server.sessions import events as ev
            for q in orchestrator.pending_questions(session_id):
                q_wire = redact_event(ev.Question(
                    question_id=q["question_id"],
                    turn_id=q["turn_id"],
                    header=q["header"],
                    question=q["question"],
                    options=q["options"],
                    multi_select=q["multi_select"],
                ).to_wire())
                await _send(q_wire)

            # 2c) Send the deferred initial prompt (if any) now that the client is
            #     watching.  The prompt was stored in create_session instead of being
            #     sent immediately so that its events stream live to this first attach
            #     rather than racing the transcript-file write or arriving before any
            #     consumer exists in driver._out.  Only fires on the first attach
            #     (pop clears the entry); reconnects fall through to live-stream only.
            pending_prompt = orchestrator.pop_pending_initial_prompt(session_id)
            pending_images = orchestrator.pop_pending_initial_images(session_id)
            if pending_prompt is not None or pending_images:
                await orchestrator.send_prompt(
                    session_id, pending_prompt or "", pending_images)

            # 3) Start the live stream: forward driver events into the batcher.
            live_tasks[session_id] = asyncio.create_task(
                _forward_live(driver, out_q, session_id), name=f"ws-live-{session_id}")

        async def _handle_detach(session_id: str) -> None:
            task = live_tasks.pop(session_id, None)
            if task is None:
                return
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=0.5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

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

                # Per-frame identity re-verification (handshake drift / revocation).
                ident = verify_identity(websocket)
                if ident is None or ident != identity:
                    await websocket.close(code=1008)
                    return

                # 256KB frame cap.
                if len(raw) > _MAX_FRAME:
                    await _send({"type": "error", "code": "frame_too_large",
                                 "message": f"Frame exceeds {_MAX_FRAME} bytes"})
                    continue

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await _send({"type": "error", "code": "INVALID_JSON",
                                 "message": "Message must be a JSON object"})
                    continue

                msg_type = msg.get("type", "")
                session_id = msg.get("sessionId", "")

                if msg_type == "attach":
                    await _handle_attach(session_id)
                elif msg_type == "detach":
                    await _handle_detach(session_id)
                elif msg_type == "prompt":
                    await orchestrator.send_prompt(
                        session_id, msg.get("text", ""), msg.get("images"))
                elif msg_type == "interrupt":
                    await orchestrator.interrupt(session_id)
                    try:
                        async with db_session() as db:
                            await record_audit(db, "session.interrupt", identity,
                                               {"sessionId": session_id})
                    except Exception:
                        log.warning("ws.audit_failed event=%s session=%s",
                                    "session.interrupt", session_id, exc_info=True)
                elif msg_type == "permissionResponse":
                    decision = msg.get("decision", "")
                    perm_id = msg.get("permId", "")
                    orchestrator.resolve_permission(session_id, perm_id, decision, identity)
                    try:
                        async with db_session() as db:
                            await record_audit(db, "permission." + decision, identity,
                                               {"sessionId": session_id, "permId": perm_id})
                    except Exception:
                        log.warning("ws.audit_failed event=%s session=%s",
                                    "permission." + decision, session_id, exc_info=True)
                elif msg_type == "setPermissionMode":
                    mode = msg.get("mode", "")
                    await orchestrator.set_permission_mode(session_id, mode)
                    try:
                        async with db_session() as db:
                            await record_audit(db, "permission_mode.change", identity,
                                               {"sessionId": session_id, "mode": mode})
                    except Exception:
                        log.warning("ws.audit_failed event=%s session=%s",
                                    "permission_mode.change", session_id, exc_info=True)
                elif msg_type == "answerQuestion":
                    question_id = msg.get("questionId", "")
                    selected = msg.get("selected", [])
                    mode = msg.get("mode", "option")
                    if mode not in ("option", "freeform", "chat"):
                        mode = "option"
                    orchestrator.answer_question(session_id, question_id, selected, identity, mode)
                    try:
                        async with db_session() as db:
                            await record_audit(db, "question.answer", identity,
                                               {"sessionId": session_id, "questionId": question_id,
                                                "selected": selected, "mode": mode})
                    except Exception:
                        log.warning("ws.audit_failed event=%s session=%s",
                                    "question.answer", session_id, exc_info=True)
                elif msg_type == "cancelQueued":
                    index = msg.get("index")
                    if isinstance(index, int) and not isinstance(index, bool):
                        await orchestrator.cancel_queued(session_id, index)
                else:
                    await _send({"type": "error", "code": "UNKNOWN_TYPE",
                                 "message": f"Unknown message type: {msg_type!r}"})

        finally:
            # Cancel live streams.
            for sid in list(live_tasks.keys()):
                await _handle_detach(sid)

            event_task.cancel()
            try:
                await asyncio.wait_for(event_task, timeout=0.5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

            batcher_task.cancel()
            try:
                await asyncio.wait_for(batcher_task, timeout=0.5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

            orchestrator.unsubscribe_events(event_q)
            log.debug("ws.disconnect identity=%s", identity)

    return router
