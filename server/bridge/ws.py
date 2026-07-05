"""WebSocket bridge — normalized-event transport.

Single authenticated WebSocket per client.  Ships normalized session events
(``e.to_wire()`` dicts) coalesced into ``batch`` frames; no PTY bytes.

Factory:
    router = build_ws_router(orchestrator)
    app.include_router(router)

Message protocol (JSON):
  client→server: attach, detach, prompt, interrupt, permissionResponse,
                 setPermissionMode, answerQuestion
  server→client: batch (carrying ALL session-scoped events, including the
                 replayReset/replay/replayComplete sequence and attach re-emits
                 like permissionRequest/status), plus top-level error /
                 commands / commandResult / lifecycle frames.

  Every session-scoped frame flows through ONE outbound FIFO (out_q → batcher)
  so replay and live events share a single total order per socket — see
  _run_replay_and_complete for the stale-straddle rationale. Top-level frames
  are reserved for things that are NOT transcript state (error banners, the
  slash-command catalog, session-LIST lifecycle updates).

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
from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from server.audit import record_audit, redact_event
from server.auth.identity import verify_identity
from server.auth.middleware import require_ws_identity
from server.db import db_session
from server.ipc import protocol as proto
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


async def _forward_live(stream, out_q: "asyncio.Queue[dict]", session_id: str) -> None:
    """Forward a driver stream's live events into *out_q* as wire dicts.

    *stream* is a ``DriverStream``/``RemoteDriverStream`` opened by
    ``driver.open_stream()`` at attach time (its ``events()`` replays the
    atomically-captured in-progress-turn snapshot, then goes live).

    Each dict is stamped with the owning ``sessionId`` and a per-session monotonic
    ``seq`` so the client can (a) discard events from a session it is no longer
    viewing and (b) detect a hole and self-heal via re-attach. A single event that
    fails to serialize must not silence the whole stream: skip it but still advance
    ``seq`` so the gap is visible. A crash in the generator becomes an ``error`` frame.
    """
    with bind_session(session_id):
        seq = 0
        try:
            async for e in stream.events():
                try:
                    wire = e.to_wire()
                except Exception:
                    log.exception("ws.to_wire session=%s", session_id)
                    seq += 1  # consume a seq so the client sees a gap and re-attaches
                    continue
                if wire.get("type") == proto.GAP:
                    # A daemon replay-gap sentinel (issue #185): events were evicted from
                    # the ring before the edge could deliver them, so this view is behind
                    # disk. Don't forward the sentinel — consume a seq to leave a hole the
                    # client's liveBatch gap-healing detects → detach+attach → full replay
                    # from the transcript (the durable source of truth).
                    log.warning("ws.live_stream: daemon replay gap session=%s lost=%s — "
                                "forcing client resync from disk", session_id, wire.get("lost"))
                    seq += 1
                    continue
                wire["sessionId"] = session_id
                wire["seq"] = seq
                seq += 1
                out_q.put_nowait(wire)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.exception("ws.live_stream session=%s", session_id)
            detail = str(exc).strip() or "no detail"
            # Session-tagged like every other out_q frame: untagged, a crash frame
            # surviving a detach would be applied to whichever session the client
            # is viewing when the batcher flushes it (same misrouting family as
            # the stale-straddle bug).
            out_q.put_nowait({**ErrorEvent(
                "live_stream_failed",
                f"The live session stream stopped unexpectedly. {type(exc).__name__}: {detail[:500]}",
                turn_id=None,
            ).to_wire(), "sessionId": session_id})


def _redact_buffer(buf: list[dict]) -> list[dict]:
    """Redact each outbound event; drop any event whose redaction raises instead of
    letting one bad event kill the batcher task (which would freeze the socket).
    A dropped event leaves a seq hole, so the client self-heals."""
    out: list[dict] = []
    for e in buf:
        try:
            out.append(redact_event(e))
        except Exception:
            log.exception("ws.redact: dropping unredactable event type=%s", e.get("type"))
    return out


async def _run_replay_and_complete(orchestrator, enqueue: "Callable[[dict], None]",
                                   session_id: str,
                                   drop_incomplete: bool = False,
                                   turn_started_at: float | None = None) -> list:
    """Replay the transcript (trimmed of the in-progress turn when drop_incomplete),
    then replayComplete — every frame IN ORDER through *enqueue* (the socket's
    single outbound FIFO: out_q → batcher). Returns the replayed events.

    WHY the FIFO and not a direct send (the stale-straddle bug, B4): live events
    flow driver → out_q → batcher, and a detach never drains that queue. On a
    detach→attach (navigation, gap-healing), pre-detach frames can still be parked
    in out_q or the batcher's flush buffer; a direct-sent replay would OVERTAKE
    them, so stale old-epoch frames land AFTER the fresh replayReset — same
    sessionId, so no client filter drops them. They duplicate transcript content
    and their stale ``seq`` poisons the client's just-reset gap baseline (the new
    live stream restarts at seq 0 → looks like a gap → detach+attach heal loop).
    Routing the replay through the same queue makes the queue the total order.

    Frames are enqueued as RAW wire dicts: the batcher's ``_redact_buffer``
    redacts everything at flush, so redacting here would be a second, parallel
    redaction path (the exact drift-prone shape constitution VI bans).

    replayComplete rides the queue too (so it cannot overtake the replay), and
    still fires on a replay failure so the client's loading spinner clears.

    turn_started_at (epoch seconds, from the live driver) scopes the trim to the turn
    that actually IS in progress — the CLI's disk write lags the live stream, so the
    file's last user entry can belong to a PREVIOUS, completed turn (see replay.py)."""
    events: list = []
    earliest_turn = 0
    total_turns = 0
    try:
        path = await orchestrator.transcript_path(session_id)
        if path:
            window = await replay_window(path, drop_incomplete_turn=drop_incomplete,
                                         turn_started_at=turn_started_at)
            events = window.events
            earliest_turn = window.earliest_turn
            total_turns = window.total_turns
        # Lead the replay with a reset so a reconnect re-attach rebuilds the
        # transcript cleanly instead of re-appending a duplicate copy (see the
        # reducer's replayReset case). Send it whenever there is history to replay
        # OR a live in-progress turn will follow (drop_incomplete): on a LIVE attach
        # the in-progress turn is trimmed from THIS replay and re-supplied by the
        # live stream, so when the trim leaves the history window empty we must STILL
        # reset — otherwise the client renders the live turn under a stale
        # "Beginning of session" state and double-appends it on the next reconnect
        # (#390 regression: the old `if events:` guard skipped the reset here).
        if events or drop_incomplete:
            # Tag every replay frame with its owning sessionId, exactly as the live
            # stream stamps _forward_live events. Without the tag the client routes
            # replay frames by whichever session is subscribed when they arrive, not by
            # identity — so a stale replay for session A landing just after the user
            # navigates to B applies to B: the untagged replayReset wipes B's transcript
            # and its history never returns (blank-on-navigation bug). Tagging lets the
            # client (handleLiveBatch / useSessionRuntime.onEvent) drop a replay meant
            # for a session it is no longer viewing.
            enqueue({"type": "replayReset", "sessionId": session_id})
            for e in events:
                enqueue({**e.to_wire(), "sessionId": session_id})
    except Exception:
        log.warning("ws.attach: transcript replay failed for %s", session_id, exc_info=True)
    enqueue({"type": "replayComplete", "sessionId": session_id,
             "earliestTurn": earliest_turn, "totalTurns": total_turns})
    return events


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
                    await _send({"type": "batch", "events": _redact_buffer(buf)})
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

        async def _replay_and_complete(session_id: str, drop_incomplete: bool = False,
                                       turn_started_at: float | None = None) -> list:
            """Delegate to the module-level helper, binding this socket's orchestrator
            and out_q (the single session-frame FIFO — see the helper's stale-straddle
            rationale).  A transcript that can't be located or parsed must NOT crash the
            WS — the helper swallows the error and always fires replayComplete so the
            client's loading spinner clears.  Returns the replayed events (for
            idle-suggestion seeding).  Pass drop_incomplete=True (plus the stream's
            turn_started_at) for a LIVE session whose stream reports turn_active so the
            in-progress turn — and ONLY that turn — is owned solely by the stream's
            snapshot replay (no duplication, no amputated history)."""
            return await _run_replay_and_complete(orchestrator, out_q.put_nowait,
                                                  session_id, drop_incomplete,
                                                  turn_started_at)

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
                # The error frame stays a direct top-level send (it's a banner, not
                # transcript state), but replayComplete IS transcript state — route
                # it through out_q so it can never overtake queued session frames.
                out_q.put_nowait({"type": "replayComplete", "sessionId": session_id})
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
                out_q.put_nowait({"type": "replayComplete", "sessionId": session_id})
                return None
            if driver is None:
                await _send({"type": "error", "code": "SESSION_NOT_FOUND",
                             "message": f"Session {session_id!r} not found",
                             "sessionId": session_id})
                out_q.put_nowait({"type": "replayComplete", "sessionId": session_id})
                return None
            return driver

        async def _emit_persisted_context_usage(session_id: str, rec) -> None:
            """Re-emit the record's last exact context usage as a contextUsage frame.

            No-op when there's no record or no persisted reading (a session that has
            never finished a turn). Best-effort: never blocks the attach. Rides the
            out_q FIFO (session-tagged, unredacted — the batcher redacts at flush)
            like every other session-scoped frame, so it can't interleave into the
            middle of a queued replay."""
            used = getattr(rec, "context_used_tokens", None) if rec is not None else None
            mx = getattr(rec, "context_max_tokens", None) if rec is not None else None
            if not isinstance(used, int) or not isinstance(mx, int) or mx <= 0:
                return
            try:
                from server.sessions import events as ev
                out_q.put_nowait({**ev.ContextUsage(
                    used_tokens=used, max_tokens=mx,
                    percentage=round(used / mx * 100, 1),
                    model=getattr(rec, "model", None),
                ).to_wire(), "sessionId": session_id})
            except Exception:
                log.warning("ws.attach: context usage re-emit failed", exc_info=True)

        async def _emit_command_catalog(session_id: str) -> None:
            """Emit the session's slash-command catalog so the composer palette
            populates. For a live session the orchestrator builds it fresh from the
            SDK's get_server_info and caches it; for a DETACHED session (no driver) it
            replays the last-known cached catalog — so the palette works on view without
            forcing a resume. Best-effort: never blocks the attach.

            DELIBERATELY a direct top-level send, not out_q: the client's 'commands'
            handler is TOP-LEVEL-only (client.ts case 'commands' → the dedicated
            'commands' emitter; SESSION_EVENT_TYPES does NOT include it), so a
            batch-wrapped catalog would be dispatched as a sessionEvent that nothing
            consumes — silently breaking the slash-command menu. The catalog is not
            transcript state, so its ordering vs the replay is irrelevant; being
            direct, it is redacted here."""
            try:
                catalog = await orchestrator.command_catalog(session_id)
                if catalog:
                    await _send(redact_event({"type": "commands", "sessionId": session_id, "commands": catalog}))
            except Exception:
                log.warning("ws.attach: command_catalog failed for %s", session_id, exc_info=True)

        async def _emit_live_extras(session_id: str, driver, events: list) -> None:
            """Post-attach live wiring: suggestion seeding, command catalog, pending
            permission/question re-emit, live status, and the deferred initial prompt.
            Only meaningful once a live driver exists. Each step is best-effort.

            Session-scoped frames (permissionRequest, question, status, queueUpdate)
            ride out_q — the replay now flows through that queue, and a direct send
            here would OVERTAKE it (a permissionRequest landing before replayReset
            gets wiped: the reducer stores permission blocks inside turns). They are
            enqueued raw + session-tagged; the batcher redacts at flush. The
            'commands' catalog is the deliberate exception (see below)."""
            # A session resumed into an idle state has no in-memory turn context, so the
            # turn-completion suggestion trigger never fires. Seed one suggestion from
            # the replayed transcript tail (best-effort; driver gates + dedupes).
            try:
                from server.sessions.suggestions import kick_idle_suggestions
                kick_idle_suggestions(driver, events)
            except Exception:
                log.warning("ws.attach: idle suggestion seeding failed for %s", session_id, exc_info=True)

            # Slash-command catalog (built from the SDK's get_server_info) for this
            # session — top-level by design, see _emit_command_catalog's docstring.
            await _emit_command_catalog(session_id)

            # Re-emit any pending permission requests for this session. Build the frame
            # through PermissionRequest.to_wire() — the same wire format the live path
            # uses — so the re-emit can't drift from it (it previously hardcoded
            # plan=False, dropping the plan flag for a pending ExitPlanMode approval).
            for p in await orchestrator.pending_permissions(session_id):
                out_q.put_nowait({**PermissionRequest(
                    p["permId"], p["turn_id"], p["tool"], tool_kind(p["tool"]),
                    p["input"], plan=(p["tool"] == "ExitPlanMode"),
                ).to_wire(), "sessionId": session_id})

            # Re-emit any pending AskUserQuestion events for this session. Mirrors the
            # permission re-emit above so a refresh/reattach while the model is blocked
            # in _handle_ask_user_question never silently hangs.
            from server.sessions import events as ev
            for q in await orchestrator.pending_questions(session_id):
                out_q.put_nowait({**ev.Question(
                    question_id=q["question_id"],
                    turn_id=q["turn_id"],
                    header=q["header"],
                    question=q["question"],
                    options=q["options"],
                    multi_select=q["multi_select"],
                ).to_wire(), "sessionId": session_id})

            # Emit the current live status so a client attaching or reconnecting mid-turn
            # renders the true state immediately. The live status is otherwise only
            # pushed on transitions, so without this a mid-turn attach would show the
            # reducer's initial 'idle' ("Ready"). Best-effort.
            # Riding out_q is also the fix for the status-frame collision (B5): as a
            # top-level frame, {"type":"status","state":...} hit the client's
            # LIFECYCLE 'status' handler (which expects {sessionId,status}) and never
            # reached the transcript reducer. Batched, it is dispatched as a
            # sessionEvent → the reducer's 'status' case — where it always belonged.
            try:
                out_q.put_nowait({**ev.Status(
                    state=driver.current_status(),
                    count=len(driver.queued()),
                ).to_wire(), "sessionId": session_id})
            except Exception:
                log.warning("ws.attach: live status emit failed for %s", session_id, exc_info=True)

            # Re-emit the live queue so a reattach repopulates the queued-prompt
            # chips (with their images). The driver holds the queue across attaches;
            # without this, a navigate-away/back showed an empty strip. Best-effort.
            try:
                if driver.queued():
                    out_q.put_nowait({**ev.QueueUpdate(driver.queue_wire()).to_wire(),
                                      "sessionId": session_id})
            except Exception:
                log.warning("ws.attach: queue re-emit failed for %s", session_id, exc_info=True)

            # Send the deferred initial prompt (if any) now that the client is watching.
            # The prompt was stored in create_session instead of being sent immediately
            # so that its events stream live to this first attach rather than racing the
            # transcript-file write or arriving before any subscriber is registered on
            # the driver. Only fires on the first attach (pop clears the entry);
            # reconnects fall through to live-stream only. Best-effort like every
            # other step here: a raise would propagate out of _handle_attach's
            # try, close the just-opened stream, and kill the whole attach — the
            # client would spin on a dead socket over a deferred nicety.
            try:
                pending_prompt = await orchestrator.pop_pending_initial_prompt(session_id)
                pending_attachment_ids = await orchestrator.pop_pending_initial_attachment_ids(session_id)
                if pending_prompt is not None or pending_attachment_ids:
                    await orchestrator.send_prompt(
                        session_id, pending_prompt or "", pending_attachment_ids)
            except Exception:
                log.warning("ws.attach: deferred initial prompt send failed for %s",
                            session_id, exc_info=True)

        def _start_live_stream(session_id: str, stream) -> None:
            """Forward an opened driver stream's events into the batcher for this
            session. The stream was opened by driver.open_stream() BEFORE the replay
            (see _handle_attach's settle-race note); once this task starts, the
            stream's events() generator owns queue teardown."""
            live_tasks[session_id] = asyncio.create_task(
                _forward_live(stream, out_q, session_id), name=f"ws-live-{session_id}")

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
            await _emit_persisted_context_usage(session_id, rec)

            if rec is not None and getattr(rec, "detached", False):
                await _replay_and_complete(session_id)
                # Replay the last-known slash-command catalog so the composer palette
                # populates on view. A detached session has no live driver (the lazy-view
                # path defers the resume), so without this the catalog frame — otherwise
                # emitted only from _emit_live_extras — never arrives and the autocomplete
                # is silently empty until the user sends a prompt.
                await _emit_command_catalog(session_id)
                return

            # LIVE session (or unknown — let ensure_driver decide): a live record's
            # driver already exists and ensure_driver fast-returns it; an unknown
            # session yields None and surfaces SESSION_NOT_FOUND below.
            driver = await _resume_driver_or_error(session_id)
            if driver is None:
                return
            # Open the driver stream SYNCHRONOUSLY, BEFORE the replay (the
            # settle-race bug, B3): open_stream() atomically snapshots the
            # in-progress turn buffer AND pins turn_active/turn_started_at to that
            # same instant. The replay below awaits (disk IO, and in remote mode
            # 4+ RPCs); if the turn settles during those awaits the driver clears
            # its buffer — a stream opened after the replay would snapshot nothing,
            # and the in-progress turn this replay drops (drop_incomplete=True)
            # would be delivered by NOBODY: the trailing turn silently vanishes and
            # the fresh live seq starts at 0 with no gap, so the client can't
            # self-heal. The pre-opened queue also captures every event emitted
            # while the replay runs.
            stream = driver.open_stream()
            try:
                events = await _replay_and_complete(
                    session_id, drop_incomplete=stream.turn_active,
                    turn_started_at=stream.turn_started_at)
                await _emit_live_extras(session_id, driver, events)
            except BaseException:
                # Nothing has consumed the stream yet (only events()'s finally or
                # close() deregisters it) — discard the queue so a failed attach
                # can't leak a subscriber that buffers every future emit forever.
                stream.close()
                raise
            _start_live_stream(session_id, stream)

        async def _ensure_live(session_id: str) -> bool:
            """Make the session live + streaming so a prompt can flow, resuming a
            detached session lazily. Returns False if the resume failed (an error frame
            was already emitted). Idempotent: a no-op if already streaming."""
            if session_id in live_tasks:
                return True
            driver = await _resume_driver_or_error(session_id)
            if driver is None:
                return False
            # Same uniform interface as _handle_attach (no replay here, so there is
            # no settle race to close — but ONE way of opening a live stream keeps
            # the two paths from drifting). No awaits between open and task start,
            # so no leak window either.
            _start_live_stream(session_id, driver.open_stream())
            # Now that a live driver exists, (re)build + emit the command catalog. This
            # both refreshes the palette and self-populates the persisted cache for a
            # session that had never been live before (e.g. one predating catalog caching,
            # or only ever resumed lazily) — so its NEXT detached view shows the palette.
            await _emit_command_catalog(session_id)
            # Flip the header from the read-only 'Ready' to the live state as the
            # resumed turn begins. Best-effort; rides out_q like every other
            # session-scoped frame (raw + tagged, batcher redacts — and batching is
            # what routes it to the reducer's 'status' case instead of colliding
            # with the client's top-level lifecycle 'status' handler, B5).
            try:
                from server.sessions import events as ev
                out_q.put_nowait({**ev.Status(
                    state=driver.current_status(),
                    count=len(driver.queued()),
                ).to_wire(), "sessionId": session_id})
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
