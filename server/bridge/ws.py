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
from server.ipc.client import DaemonUnavailable
from server.obs.context import bind_session
from server.sessions.events import ErrorEvent, PermissionRequest, tool_kind
from server.sessions.replay import replay_window

if TYPE_CHECKING:
    from server.sessions.facade import InProcessOrchestratorAdapter

log = logging.getLogger(__name__)

# Outbound batching cadence (seconds) — ~16ms matches a 60fps display refresh
_BATCH_INTERVAL = 0.016
# Maximum inbound frame size (bytes)
_MAX_FRAME = 262144
# Hard ceiling (seconds) on resuming a detached session's SDK subprocess. A
# resume boots the whole MCP-server fleet, which can legitimately take tens of
# seconds cold — but it must never block forever. On a saturated box a wedged
# resume used to hang the attach with no timeout, leaving the client spinning on
# "Loading session…" indefinitely. Bound it and surface SESSION_RESUME_TIMEOUT.
_RESUME_TIMEOUT = 90.0


async def _forward_live(driver, out_q: "asyncio.Queue[dict]", session_id: str) -> None:
    """Forward a driver's live events into *out_q* as wire dicts.

    A crash in the stream must not leave the UI silent: convert the exception into
    an ``error`` frame and enqueue it so the client learns the live stream died
    instead of staring at a frozen transcript. Cancellation is normal (detach).
    """
    with bind_session(session_id):
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


def build_ws_router(orchestrator: "InProcessOrchestratorAdapter") -> APIRouter:
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

        # Subscribe to orchestrator lifecycle events (rotated, etc.). In remote
        # mode an unreachable daemon must close the socket cleanly (1013 = try
        # again later), not crash the handler.
        try:
            event_q = await orchestrator.subscribe_events()
        except DaemonUnavailable:
            log.warning("ws.connect: session daemon unavailable")
            await websocket.close(code=1013)
            return

        # Serialises all websocket writes. The batcher, the attach/replay path, and
        # the lifecycle forwarder all call _send concurrently; interleaved send_text
        # calls on one socket can raise or corrupt frames.
        send_lock = asyncio.Lock()

        async def _send(msg: dict) -> None:
            """Send a JSON message; ignore send errors after disconnect."""
            try:
                if websocket.client_state == WebSocketState.CONNECTED:
                    async with send_lock:
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
        # Lifecycle event forwarder (status, rotated, …) → top-level frames
        # ------------------------------------------------------------------

        async def _forward_events() -> None:
            try:
                while True:
                    event = await event_q.get()
                    # Lifecycle/control frames (status, rotated, …) must reach the
                    # client as TOP-LEVEL frames so their dedicated handlers fire —
                    # e.g. status → _patchSessionStatus, which live-updates the session
                    # LIST. Routing them through out_q would wrap them in a 'batch'
                    # frame, which the client treats as events for the OPEN session only.
                    await _send(redact_event(event))
            except asyncio.CancelledError:
                pass

        event_task = asyncio.create_task(_forward_events(), name="ws-events")

        # ------------------------------------------------------------------
        # Attach / detach
        # ------------------------------------------------------------------

        async def _replay_and_complete(session_id: str) -> list:
            """Replay the transcript (if any) as one batch frame, then replayComplete.

            A transcript that can't be located or parsed must NOT crash the WS (which
            would close the socket → client reconnect-loop on a blank screen) — skip the
            history and continue. replayComplete ALWAYS fires so the client's loading
            spinner clears, and now carries earliestTurn/totalTurns so the client knows
            whether older history exists. Returns the replayed events (for idle-suggestion
            seeding).
            """
            events: list = []
            earliest_turn = 0
            total_turns = 0
            try:
                path = await orchestrator.transcript_path(session_id)
                if path:
                    window = await replay_window(path)
                    events = window.events
                    earliest_turn = window.earliest_turn
                    total_turns = window.total_turns
                if events:
                    # Lead the replay with a reset. attach is re-sent on every WS
                    # reconnect (client.ts onopen), so the same transcript is replayed
                    # repeatedly into a reducer that is never recreated. Streamed
                    # text/thinking deltas are append-only, so without a reset each
                    # reconnect re-appends a full copy of every assistant block — the
                    # "sessions duplicating content" bug. The reset is the first event
                    # of this single batch frame, so the client clears and rebuilds
                    # atomically (no flicker, no duplication).
                    redacted_replay = [{"type": "replayReset"}]
                    redacted_replay += [redact_event(e.to_wire()) for e in events]
                    await _send({"type": "batch", "events": redacted_replay})
            except Exception:
                log.warning("ws.attach: transcript replay failed for %s", session_id, exc_info=True)
            await _send({"type": "replayComplete", "earliestTurn": earliest_turn, "totalTurns": total_turns})
            return events

        async def _resume_driver_or_error(session_id: str):
            """Resume (or fetch) the live driver, bounded by _RESUME_TIMEOUT.

            Threads the connection's verified identity onto the resumed driver so that
            AskUserQuestion / permission answers (registered under driver.identity,
            resolved with this WS identity) actually match. On any failure emits an
            error frame plus a replayComplete (so the client's loading state always
            clears) and returns None.
            """
            try:
                driver = await asyncio.wait_for(
                    orchestrator.ensure_driver(session_id, identity), _RESUME_TIMEOUT)
            except asyncio.TimeoutError:
                # A wedged resume (saturated box / stuck MCP fleet) must not hang the
                # socket forever — surface it so the client can stop spinning and retry.
                log.warning("ws.attach: ensure_driver timed out for %s after %.0fs",
                            session_id, _RESUME_TIMEOUT)
                await _send({"type": "error", "code": "SESSION_RESUME_TIMEOUT",
                             "message": "This session is taking too long to resume — the "
                                        "server may be busy. Try again in a moment.",
                             "sessionId": session_id})
                await _send({"type": "replayComplete"})
                return None
            except DaemonUnavailable:
                log.warning("ws.attach: session daemon unavailable for %s", session_id)
                await websocket.close(code=1013)
                return None
            except Exception:
                # A resumed/rehydrated session can fail to start its SDK subprocess —
                # most commonly because the account's token is dead (needs re-auth) or
                # the stored claude_session_id can no longer be resumed. Letting that
                # propagate would crash the WS handler, closing the socket and making
                # the client reconnect-loop forever on a blank screen. Report it and
                # keep the socket alive (replayComplete clears the loading state).
                log.warning("ws.attach: ensure_driver failed for %s", session_id, exc_info=True)
                rec = await orchestrator.get_session(session_id)
                acct = rec.account if rec else None
                hint = (f" Its account '{acct}' may need re-authentication — check the Accounts tab."
                        if acct else "")
                await _send({"type": "error", "code": "SESSION_START_FAILED",
                             "message": f"Couldn't resume this session.{hint}",
                             "sessionId": session_id})
                await _send({"type": "replayComplete"})
                return None
            if driver is None:
                await _send({"type": "error", "code": "SESSION_NOT_FOUND",
                             "message": f"Session {session_id!r} not found",
                             "sessionId": session_id})
                await _send({"type": "replayComplete"})
                return None
            return driver

        async def _emit_persisted_context_usage(rec) -> None:
            """Re-emit the record's last exact context usage as a contextUsage frame.

            No-op when there's no record or no persisted reading (a session that has
            never finished a turn). Best-effort: never blocks the attach."""
            used = getattr(rec, "context_used_tokens", None) if rec is not None else None
            mx = getattr(rec, "context_max_tokens", None) if rec is not None else None
            if not isinstance(used, int) or not isinstance(mx, int) or mx <= 0:
                return
            try:
                from server.sessions import events as ev
                await _send(redact_event(ev.ContextUsage(
                    used_tokens=used, max_tokens=mx,
                    percentage=round(used / mx * 100, 1),
                    model=getattr(rec, "model", None),
                ).to_wire()))
            except Exception:
                log.warning("ws.attach: context usage re-emit failed", exc_info=True)

        async def _emit_live_extras(session_id: str, driver, events: list) -> None:
            """Post-attach live wiring: suggestion seeding, command catalog, pending
            permission/question re-emit, live status, and the deferred initial prompt.
            Only meaningful once a live driver exists. Each step is best-effort."""
            # A session resumed into an idle state has no in-memory turn context, so the
            # turn-completion suggestion trigger never fires. Seed one suggestion from
            # the replayed transcript tail (best-effort; driver gates + dedupes).
            try:
                from server.sessions.suggestions import kick_idle_suggestions
                kick_idle_suggestions(driver, events)
            except Exception:
                log.warning("ws.attach: idle suggestion seeding failed for %s", session_id, exc_info=True)

            # Slash-command catalog (built from the SDK's get_server_info) for this session.
            try:
                catalog = await orchestrator.command_catalog(session_id)
                await _send(redact_event({"type": "commands", "sessionId": session_id, "commands": catalog}))
            except Exception:
                log.warning("ws.attach: command_catalog failed for %s", session_id, exc_info=True)

            # Re-emit any pending permission requests for this session. Build the frame
            # through PermissionRequest.to_wire() — the same wire format the live path
            # uses — so the re-emit can't drift from it (it previously hardcoded
            # plan=False, dropping the plan flag for a pending ExitPlanMode approval).
            for p in await orchestrator.pending_permissions(session_id):
                perm_frame = redact_event(PermissionRequest(
                    p["permId"], p["turn_id"], p["tool"], tool_kind(p["tool"]),
                    p["input"], plan=(p["tool"] == "ExitPlanMode"),
                ).to_wire())
                await _send(perm_frame)

            # Re-emit any pending AskUserQuestion events for this session. Mirrors the
            # permission re-emit above so a refresh/reattach while the model is blocked
            # in _handle_ask_user_question never silently hangs.
            from server.sessions import events as ev
            for q in await orchestrator.pending_questions(session_id):
                q_wire = redact_event(ev.Question(
                    question_id=q["question_id"],
                    turn_id=q["turn_id"],
                    header=q["header"],
                    question=q["question"],
                    options=q["options"],
                    multi_select=q["multi_select"],
                ).to_wire())
                await _send(q_wire)

            # Emit the current live status so a client attaching or reconnecting mid-turn
            # renders the true state immediately. The live status is otherwise only
            # pushed on transitions, so without this a mid-turn attach would show the
            # reducer's initial 'idle' ("Ready"). Best-effort.
            try:
                await _send(redact_event(ev.Status(
                    state=driver.current_status(),
                    count=len(driver.queued()),
                ).to_wire()))
            except Exception:
                log.warning("ws.attach: live status emit failed for %s", session_id, exc_info=True)

            # Re-emit the live queue so a reattach repopulates the queued-prompt
            # chips (with their images). The driver holds the queue across attaches;
            # without this, a navigate-away/back showed an empty strip. Best-effort.
            try:
                if driver.queued():
                    await _send(redact_event(ev.QueueUpdate(driver.queue_wire()).to_wire()))
            except Exception:
                log.warning("ws.attach: queue re-emit failed for %s", session_id, exc_info=True)

            # Send the deferred initial prompt (if any) now that the client is watching.
            # The prompt was stored in create_session instead of being sent immediately
            # so that its events stream live to this first attach rather than racing the
            # transcript-file write or arriving before any consumer exists in
            # driver._out. Only fires on the first attach (pop clears the entry);
            # reconnects fall through to live-stream only.
            pending_prompt = await orchestrator.pop_pending_initial_prompt(session_id)
            pending_attachment_ids = await orchestrator.pop_pending_initial_attachment_ids(session_id)
            if pending_prompt is not None or pending_attachment_ids:
                await orchestrator.send_prompt(
                    session_id, pending_prompt or "", pending_attachment_ids)

        def _start_live_stream(session_id: str, driver) -> None:
            """Forward the driver's live events into the batcher for this session."""
            live_tasks[session_id] = asyncio.create_task(
                _forward_live(driver, out_q, session_id), name=f"ws-live-{session_id}")

        async def _handle_attach(session_id: str) -> None:
            if session_id in live_tasks:
                return  # already attached

            # READ-ONLY attach for a detached session: replay history only. A detached
            # record has no live driver, and resuming one just to *view* it spawns an
            # SDK subprocess + its whole MCP-server fleet — which, on a busy box, can
            # wedge the attach and leave the client spinning on "Loading session…"
            # forever. So defer the resume until the user actually sends a prompt (see
            # _ensure_live). Viewing a finished/detached session is now instant.
            try:
                rec = await orchestrator.get_session(session_id)
            except DaemonUnavailable:
                # In remote mode get_session is an RPC to the daemon; if it's down,
                # close 1013 (try again later) rather than crashing the handler.
                log.warning("ws.attach: session daemon unavailable for %s", session_id)
                await websocket.close(code=1013)
                return

            # Replay the last EXACT context usage so the ctx% readout is right immediately —
            # the live ContextUsage event is never re-emitted, so without this a reloaded
            # session falls back to the model-string approximation (0% for a 1M opus session
            # whose API-stripped id lacks the `[1m]` marker). Emitted for live AND detached
            # sessions; survives the following replayReset (which doesn't clear contextUsage).
            await _emit_persisted_context_usage(rec)

            if rec is not None and getattr(rec, "detached", False):
                await _replay_and_complete(session_id)
                return

            # LIVE session (or unknown — let ensure_driver decide): a live record's
            # driver already exists and ensure_driver fast-returns it; an unknown
            # session yields None and surfaces SESSION_NOT_FOUND below.
            driver = await _resume_driver_or_error(session_id)
            if driver is None:
                return
            events = await _replay_and_complete(session_id)
            await _emit_live_extras(session_id, driver, events)
            _start_live_stream(session_id, driver)

        async def _ensure_live(session_id: str) -> bool:
            """Make the session live + streaming so a prompt can flow, resuming a
            detached session lazily. Returns False if the resume failed (an error frame
            was already emitted). Idempotent: a no-op if already streaming."""
            if session_id in live_tasks:
                return True
            driver = await _resume_driver_or_error(session_id)
            if driver is None:
                return False
            _start_live_stream(session_id, driver)
            # Flip the header from the read-only 'Ready' to the live state as the
            # resumed turn begins. Best-effort.
            try:
                from server.sessions import events as ev
                await _send(redact_event(ev.Status(
                    state=driver.current_status(),
                    count=len(driver.queued()),
                ).to_wire()))
            except Exception:
                log.warning("ws._ensure_live: live status emit failed for %s", session_id, exc_info=True)
            return True

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
                    # Resume a read-only (detached) session lazily on first input, then
                    # send. send_prompt no-ops without a live driver, so the resume must
                    # happen here — not on attach, where it would block mere viewing.
                    if await _ensure_live(session_id):
                        text = msg.get("text", "")
                        attachment_ids = msg.get("attachmentIds")
                        status = await orchestrator.send_prompt(session_id, text, attachment_ids)
                        if status == "draining":
                            # Deploy drain refused the turn — ack it so the browser
                            # re-queues instead of losing the prompt.
                            await _send({"type": "promptRejected", "sessionId": session_id,
                                         "text": text, "attachmentIds": attachment_ids,
                                         "reason": "draining"})
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
                    await orchestrator.resolve_permission(session_id, perm_id, decision, identity)
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
                    await orchestrator.answer_question(session_id, question_id, selected, identity, mode)
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
                elif msg_type == "command":
                    name = msg.get("name", "")
                    args = msg.get("args", "")
                    try:
                        result = await orchestrator.run_command(session_id, name, args)
                    except KeyError:
                        await _send(redact_event({"type": "commandResult", "sessionId": session_id,
                                     "name": name, "ok": False, "error": "unknown command"}))
                    except Exception:
                        log.warning("ws.command failed session=%s name=%s", session_id, name, exc_info=True)
                        await _send(redact_event({"type": "commandResult", "sessionId": session_id,
                                     "name": name, "ok": False, "error": "command failed"}))
                    else:
                        if result is not None:
                            await _send(redact_event({"type": "commandResult", "sessionId": session_id,
                                         "ok": True, **result}))
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

            await orchestrator.unsubscribe_events(event_q)
            log.debug("ws.disconnect identity=%s", identity)

    return router
