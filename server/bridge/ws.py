"""WebSocket bridge — normalized-event transport.

Single authenticated WebSocket per client.  Ships normalized session events
(``e.to_wire()`` dicts) coalesced into ``batch`` frames; no PTY bytes.

Factory:
    router = build_ws_router(orchestrator)
    app.include_router(router)

Message protocol (JSON):
  client→server: attach, detach, prompt (optional ``interrupt: true`` = stop the
                 in-flight turn and run this prompt next instead of queueing it),
                 interrupt, permissionResponse, setPermissionMode, answerQuestion,
                 activity
  server→client: batch (carrying ALL session-scoped events, including the
                 replayReset/replay/replayComplete sequence and attach re-emits
                 like permissionRequest/status), plus top-level error /
                 commands / commandResult / lifecycle frames.

  Every session-scoped frame flows through ONE outbound FIFO (out_q → batcher)
  so replay and live events share a single total order per socket — see
  _run_replay_and_complete for the stale-straddle rationale. Top-level frames
  are reserved for things that are NOT transcript state (error banners, the
  slash-command catalog, session-LIST lifecycle updates).

Inbound ordering (#1180):
  The receive loop dispatches but does NOT wait. Frames in _CHAINED_FRAMES —
  everything that mutates a session's live stream or its turn order — run on a
  per-session FIFO work chain; the rest (permissionResponse, answerQuestion,
  activity) run inline. Ordering is therefore preserved exactly where it is
  load-bearing (per session) and nowhere else, so a lazy resume that takes tens
  of seconds no longer freezes the whole socket. See the "Per-session work
  chains" section for why a bare task-per-frame would not do.

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
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from server.aio import cancel_and_reap
from server.api.ratelimit import TokenBucketRateLimiter
from server.assistant.context import validate_context
from server.audit import record_audit, redact_event
from server.auth.identity import verify_identity
from server.auth.middleware import require_ws_identity
from server.config import get_settings
from server.db import db_session
from server.ipc import protocol as proto
from server.ipc.client import DaemonTimeout, DaemonUnavailable
from server.obs.context import bind_session, log_event
from server.obs.metrics import assistant_prompts_total
from server.obs.tasks import supervise
from server.sessions.commands.catalog import parse_command_line
from server.sessions.events import ErrorEvent, PermissionRequest, tool_kind
from server.sessions.facade import call_with_deadline
from server.sessions.replay import replay_window
from server.sessions import session_actions
from server.sessions.session_actions import ActionError
from server.sessions.types import is_assistant

if TYPE_CHECKING:
    from server.sessions.facade import InProcessOrchestratorAdapter

log = logging.getLogger(__name__)

# Outbound batching cadence (seconds) — ~16ms matches a 60fps display refresh
_BATCH_INTERVAL = 0.016
# Minimum spacing between lifecycle-triggered auto re-attaches per session. Status
# transitions cluster (idle → connecting → in_progress inside one resume); one replay
# converges the viewer, the rest inside this window are redundant load.
_AUTO_REATTACH_MIN_INTERVAL_S = 2.0
# Maximum inbound frame size (bytes)
_MAX_FRAME = 262144
# Hard ceiling (seconds) on resuming a detached session's SDK subprocess. A
# resume boots the whole MCP-server fleet, which can legitimately take tens of
# seconds cold — but it must never block forever. On a saturated box a wedged
# resume used to hang the attach with no timeout, leaving the client spinning on
# "Loading session…" indefinitely. Bound it and surface SESSION_RESUME_TIMEOUT.
_RESUME_TIMEOUT = 90.0
# Frames that mutate a session's live stream or its turn ordering run on that
# session's ordered work chain instead of inline on the receive loop (#1180).
# Everything NOT listed here is handled inline — deliberately, because those are
# exactly the frames that ANSWER a blocked live turn (permissionResponse,
# answerQuestion) or are pure signals (activity): they have no ordering
# relationship with a resume and are the ones a stalled loop hurt most.
_CHAINED_FRAMES = frozenset({
    "attach", "detach", "prompt", "interrupt", "setPermissionMode",
    "cancelQueued", "command",
})
# Depth cap on one session's work chain. The blocking receive loop this replaces
# had implicit backpressure — a client could not outrun a handler it was waiting
# on, because TCP stopped reading. Deferring removes that, so the queue needs an
# explicit bound or a fast client could pile unbounded 256KB frames on a session
# stuck in a 90 s resume. Over the cap the frame is refused, never silently dropped.
_MAX_SESSION_WORK = 64
# Only log a chain wait that a human would notice; an idle chain starts work in
# well under a millisecond and logging every frame would drown the journal.
_WORK_WAIT_LOG_MS = 250.0
# Ceiling on letting in-flight chain work finish when the socket closes. A
# disconnect DURING a lazy resume must still deliver the user's prompt: the
# blocking loop never even noticed the disconnect until _ensure_live returned, so
# the resume+send always completed. Each item is internally bounded by
# _RESUME_TIMEOUT, so this reproduces that without an unbounded teardown.
_WORK_DRAIN_TIMEOUT = _RESUME_TIMEOUT + 15.0


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

    ADDITIVE ``srcSeq`` (audit W2.3): in remote mode each event also carries the
    daemon's per-stream *source* seq (``e.src_seq``) — the only identity that survives
    re-attach (the per-attach ``seq`` restarts at 0). It is stamped as ``wire["srcSeq"]``
    ONLY when present (local/in-process mode has no daemon seq → the field is omitted,
    never faked). It is pure telemetry: it NEVER affects ordering, dedup, heal, or
    replay. When consecutive source seqs are non-contiguous we log ONE structured
    warning per attach so production loss between daemon and edge becomes visible; the
    GAP-consumption skip already forces a from-disk resync, so it re-baselines the
    source-seq tracker to avoid double-counting that known, expected jump.
    """
    with bind_session(session_id):
        seq = 0
        prev_src: int | None = None      # last daemon source seq seen on THIS attach
        src_gap_logged = False           # rate-limit the srcSeq-gap warning to once/attach
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
                    # The srcSeq jump ACROSS a GAP is expected (events really were lost and
                    # a resync is already forced) — re-baseline so it isn't also flagged.
                    prev_src = None
                    continue
                wire["sessionId"] = session_id
                wire["seq"] = seq
                src = getattr(e, "src_seq", None)
                if src is not None:
                    wire["srcSeq"] = src
                    if prev_src is not None and src != prev_src + 1 and not src_gap_logged:
                        log_event(log, logging.WARNING, "transcript.srcseq.gap",
                                  session_id=session_id, prev=prev_src, got=src)
                        src_gap_logged = True
                    prev_src = src
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


async def _orphaned_question_frames(orchestrator, session_id: str) -> list[dict]:
    """Wire frames re-offering a question whose waiter died with the daemon.

    Empty unless the session has NO live pending question (that path already
    re-emits, and a second copy would duplicate the card) and its pre-restart
    status was waiting_question. The ids come from replay, so the client's reducer
    upgrades the card already on screen rather than appending a duplicate.
    """
    from server.sessions.facade import _maybe_await

    if await orchestrator.pending_questions(session_id):
        return []
    if await _maybe_await(orchestrator.orphaned_interaction(session_id)) != "question":
        return []
    path = await _maybe_await(orchestrator.transcript_path(session_id))
    if not path:
        return []
    from server.sessions.replay import last_question_events
    # Off the event loop: last_question_events reads and json-parses the ENTIRE
    # transcript, and this runs on every attach. A multi-MB transcript would stall
    # the whole daemon loop. Same treatment as replay_window / replay_events.
    qs = await asyncio.to_thread(last_question_events, path)
    return [q.to_wire() for q in qs]


async def _run_replay_and_complete(orchestrator, enqueue: "Callable[[dict], None]",
                                   session_id: str,
                                   drop_incomplete: bool = False,
                                   turn_started_at: float | None = None,
                                   epoch: str | None = None) -> list:
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
            # Same ceiling as orchestrator.transcript_window (imported, never
            # re-declared) — this is THE session-open path, so an unbounded replay
            # here hangs the attach exactly like it hung the transcript RPC. We
            # can't just call orch.transcript_window: only replay_window takes
            # drop_incomplete_turn / turn_started_at, which the live attach needs
            # to hand the in-progress turn off to the live stream. So the wait_for
            # treatment is applied here identically, and degrades identically —
            # no history replayed, replayComplete still sent below so the client's
            # spinner clears and the live stream takes over.
            #
            # Read through the module (not `from … import`) so a test patching the
            # orchestrator's constant bounds this path too.
            from server.sessions import orchestrator as _orch_mod
            window = await asyncio.wait_for(
                replay_window(path, drop_incomplete_turn=drop_incomplete,
                              turn_started_at=turn_started_at),
                timeout=_orch_mod._TRANSCRIPT_WINDOW_CEILING_S,
            )
            events = window.events
            earliest_turn = window.earliest_turn
            total_turns = window.total_turns
        else:
            # No transcript FILE. That is either a Claude session that has not written
            # one yet (the orchestrator answers None and nothing changes) or an engine
            # that has none at all — a Codex thread's history lives in the app-server
            # and is read back over `thread/read`. The Claude branch above stays byte
            # identical on purpose: in remote mode the edge reads the shared transcript
            # file directly, and turning that into an RPC would change both its latency
            # and the ring/gap behaviour. Everything AFTER this — replayReset, the
            # per-frame sessionId tagging, replayComplete — is shared and unchanged.
            #
            # Failure here is CONTAINED rather than left to the outer handler, and
            # that is the whole point of the inner try. This branch also runs on the
            # most ordinary path there is — a brand-new Claude session whose
            # transcript does not exist yet — and the call can legitimately fail on a
            # mixed-version fleet, where a newer edge attaches to a session owned by
            # an older daemon that never registered this RPC. Letting it reach the
            # outer `except` would skip the `replayReset` below, and an attach with no
            # reset is exactly the #390 regression: the reducer keeps its stale
            # transcript and double-appends the live turn on every reconnect. Losing
            # history is recoverable; losing the reset corrupts what the user sees.
            try:
                window = await orchestrator.engine_replay_window(
                    session_id, drop_incomplete_turn=drop_incomplete,
                    turn_started_at=turn_started_at)
            except Exception:
                log.warning("ws.attach: engine replay failed for %s — attaching with "
                            "no history", session_id, exc_info=True)
                window = None
            if window is not None:
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
                # The local disk path yields real ev.* objects; the engine-replay
                # fallback yields those in-process and already-serialized wire dicts
                # over the daemon RPC (the same `hasattr` shape api/sessions.py and the
                # transcript_window RPC handler use). Normalizing here keeps ONE
                # enqueue loop rather than a second, drift-prone copy per source.
                wire = e.to_wire() if hasattr(e, "to_wire") else e
                enqueue({**wire, "sessionId": session_id})
    except asyncio.TimeoutError:
        log.warning("ws.attach: transcript replay for %s exceeded the replay ceiling — "
                    "attaching with no history", session_id)
    except Exception:
        log.warning("ws.attach: transcript replay failed for %s", session_id, exc_info=True)
    # Stamp the daemon epoch (#497) so the browser remembers which epoch minted the cursor
    # it establishes off this replayComplete; a later re-attach echoes it as sinceEpoch and
    # the edge rejects it after a daemon restart. Omitted when None (local/in-process mode
    # has no daemon epoch — mirrors how srcSeq is omitted there).
    complete: dict = {"type": "replayComplete", "sessionId": session_id,
                      "earliestTurn": earliest_turn, "totalTurns": total_turns}
    if epoch is not None:
        complete["epoch"] = epoch
    enqueue(complete)
    return events


def build_ws_router(default_orchestrator: "InProcessOrchestratorAdapter") -> APIRouter:
    """Return an APIRouter whose /ws endpoint resolves the LIVE app.state.sessions.

    *default_orchestrator* is only a fallback: in remote mode the lifespan promotes
    ``app.state.sessions`` from the provisional ``RemoteOrchestrator`` to a
    ``FleetOrchestrator`` *after* this factory runs, so a captured build-time
    reference would pin every attach (ring_covers/ring_tail/subscribe/ensure_driver)
    to the primary host and bypass fleet dispatch. The endpoint therefore reads the
    current orchestrator from the socket's app at connect time.
    """
    router = APIRouter()

    # Per-identity assistant prompt cap (spec §4.6): a burst of max/6 (~10 min of
    # steady use) refilling continuously to the configured hourly rate. Built once
    # per router — i.e. once per app/process, not per connection — so the bucket is
    # shared across every socket and assistant thread the identity has open.
    _max_prompts_per_hour = max(1, get_settings().assistant_max_prompts_per_hour)
    _assistant_prompt_limiter = TokenBucketRateLimiter(
        _max_prompts_per_hour / 6, _max_prompts_per_hour / 3600)

    @router.websocket("/ws")
    async def ws_endpoint(
        websocket: WebSocket,
        identity: str = Depends(require_ws_identity),
    ) -> None:
        await websocket.accept()
        log.debug("ws.connect identity=%s", identity)

        # Set by require_ws_identity when this socket authenticated with a `ccas2:`
        # external token (Task 9.7, `ccswitch ask`) — None for the ordinary
        # human-in-the-browser Tailscale-identity connection. Threaded into the
        # permission/question audit rows below so `via="cli"` distinguishes an
        # answer typed at a shell from one clicked in the drawer.
        via: str | None = getattr(websocket.state, "via", None)

        # Live orchestrator (see build_ws_router docstring). Falls back to the
        # build-time reference only if app.state has no sessions bound yet.
        orchestrator = getattr(websocket.app.state, "sessions", None) or default_orchestrator

        # Single outbound queue of already-wire-format dicts, drained by the batcher.
        out_q: asyncio.Queue[dict] = asyncio.Queue()
        # session_id → live-stream task forwarding driver events into out_q
        live_tasks: dict[str, asyncio.Task] = {}

        # Sessions this socket is VIEWING (attach seen, no detach yet) — independent
        # of live_tasks, which only holds sessions with a live forwarding task. The
        # difference is exactly the "snapshot viewer" set: a detached-session attach
        # replays history and starts no task, and a forwarding task ends on its own
        # when completion releases the driver (_clear_when_finished). Both leave the
        # client rendering a frozen transcript with no way to learn it went stale —
        # the nav-away-and-back bug. _forward_events watches lifecycle status events
        # against this set and re-runs the attach path to converge the viewer.
        attached: set[str] = set()
        # Set once the handler's `finally` starts. Read by every path that could
        # register a NEW forwarding task, so teardown never has to take a lock to
        # fence them (see the teardown block).
        closing = False
        # PER-SESSION attach locks, created on demand. Serialises attach/detach/
        # ensure-live across their two callers: the per-session work chains and the
        # lifecycle-triggered auto re-attach task. Without it, an auto re-attach can
        # interleave with a user attach/prompt and register two forwarding tasks for
        # one session (duplicate events, leaked subscriber).
        #
        # Keyed per session because the invariant it protects — live_tasks[sid] and
        # sid's membership in `attached` — is per session; there is no cross-session
        # invariant here. A socket-global lock put an unrelated session's user prompt
        # behind a wedged auto re-attach for up to _RESUME_TIMEOUT (90 s), undoing
        # exactly the cross-session independence #1180's per-session work chains
        # introduced — and, unlike a slow user attach, it was triggerable with no
        # user action at all.
        attach_locks: dict[str, asyncio.Lock] = {}
        attach_lock_users: dict[str, int] = {}
        # Auto re-attach bookkeeping: per-session in-flight guard, last-fire time, and
        # the in-flight tasks themselves (so teardown can reap them by name).
        auto_reattach_pending: set[str] = set()
        auto_reattach_last: dict[str, float] = {}
        auto_reattach_tasks: dict[str, asyncio.Task] = {}

        @asynccontextmanager
        async def _attach_lock(session_id: str) -> AsyncIterator[None]:
            """Hold this session's attach lock, creating it on demand.

            The entry is dropped again once the LAST holder/waiter leaves, so the map
            stays bounded by concurrently-attaching sessions rather than by every
            session this socket ever touched. The refcount is what makes that safe:
            deleting an entry while anyone still held or awaited it would hand the
            next caller a DIFFERENT Lock object than the current holder's, silently
            breaking mutual exclusion.
            """
            lock = attach_locks.get(session_id)
            if lock is None:
                lock = attach_locks[session_id] = asyncio.Lock()
            attach_lock_users[session_id] = attach_lock_users.get(session_id, 0) + 1
            try:
                async with lock:
                    yield
            finally:
                remaining = attach_lock_users[session_id] - 1
                if remaining:
                    attach_lock_users[session_id] = remaining
                else:
                    del attach_lock_users[session_id]
                    attach_locks.pop(session_id, None)

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

        async def _close_unexpectedly() -> None:
            try:
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.close(code=1011)
            except Exception:
                pass

        def _on_forward_task_failed(_exc: BaseException) -> None:
            """A dead outbound forwarding task (batcher / lifecycle forwarder / live
            stream) makes this socket useless: no frame will ever flush again, but the
            socket stays open-looking — a silently frozen tail. Close it (1011) so the
            client's reconnect logic takes over (that path replays from disk). We do NOT
            try to restart a task on a socket we can no longer trust. Single source of
            truth for all three forwarding tasks (supervise already logged the crash)."""
            asyncio.create_task(_close_unexpectedly())

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

        batcher_task = supervise(asyncio.create_task(_batcher(), name="ws-batcher"),
                                 name="ws-batcher", on_failure=_on_forward_task_failed)

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
                    # Liveness handshake: a status event for a session this socket is
                    # viewing WITHOUT a live forwarding task means the viewer is a
                    # snapshot (detached-session attach, or the task ended when the
                    # driver was released) and just went stale. Re-run the attach path
                    # to converge it. Spawned, not awaited: _handle_attach does disk
                    # IO / RPCs and this forwarder must keep lifecycle frames flowing.
                    sid = event.get("sessionId")
                    if (event.get("type") == "status" and sid
                            and sid in attached
                            and sid not in live_tasks
                            and sid not in auto_reattach_pending
                            and not closing):
                        # The interval floor spaces REPLAYS, not spawns: _auto_reattach
                        # stamps auto_reattach_last only when it actually re-attaches.
                        # A run that skips (record gone / still detached) costs one
                        # get_session and must not spend the budget — otherwise the
                        # first status event of a resume, handled a hair before the
                        # driver registers, would silence the one that follows it and
                        # the viewer would stay stale for a whole turn.
                        if (time.monotonic() - auto_reattach_last.get(sid, 0.0)
                                >= _AUTO_REATTACH_MIN_INTERVAL_S):
                            auto_reattach_pending.add(sid)
                            auto_reattach_tasks[sid] = supervise(
                                asyncio.create_task(
                                    _auto_reattach(sid),
                                    name=f"ws-auto-reattach-{sid}"),
                                name=f"ws-auto-reattach-{sid}")
                            # No on_failure: a failed auto re-attach leaves the viewer
                            # a snapshot (status quo ante), which the next status event
                            # or the client's own liveness probe retries — killing the
                            # socket over it would be strictly worse.
            except asyncio.CancelledError:
                pass

        event_task = supervise(asyncio.create_task(_forward_events(), name="ws-events"),
                               name="ws-events", on_failure=_on_forward_task_failed)

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
                                                  turn_started_at,
                                                  epoch=orchestrator.daemon_epoch())

        def _error_complete(session_id: str) -> dict:
            """Build an error-path replayComplete frame.

            Stamps epoch (#497) via the same cached sync read that _run_replay_and_complete
            uses — daemon_epoch() is an in-memory field with no DB dependency, so it is safe
            to call even when the DB connection is broken (e.g. aiosqlite pool corruption on
            async-generator teardown). The epoch lets a client that retries the attach know
            which epoch's cursor is valid without a fresh /info round-trip.

            Socket-scoped rather than nested in _resume_driver_or_error because it is the
            ONE way every aborted attach clears the client's spinner — the saturated-chain
            refusal needs it too, and a second copy there would be a parallel
            implementation of an invariant this file states twice.
            """
            frame: dict = {"type": "replayComplete", "sessionId": session_id}
            ep = orchestrator.daemon_epoch()
            if ep is not None:
                frame["epoch"] = ep
            return frame

        async def _attach_failed(session_id: str, code: str, message: str,
                                 auto: bool) -> None:
            """Report an attach failure: an error banner plus the replayComplete the
            client's loading state waits on. The ONE place the three failure branches
            below pay that debt.

            Silent when *auto* — a lifecycle-triggered re-attach is background
            convergence the user never asked for. Nothing set a loading state (the
            replay had not started), so there is no replayComplete debt, and an error
            banner for a session that may not even be in the foreground is noise about
            a failure whose entire consequence is "the viewer keeps the snapshot it
            already had", retried by the next status event.
            """
            if auto:
                return
            # The error frame stays a direct top-level send (it's a banner, not
            # transcript state), but replayComplete IS transcript state — route it
            # through out_q so it can never overtake queued session frames.
            await _send({"type": "error", "code": code, "message": message,
                         "sessionId": session_id})
            out_q.put_nowait(_error_complete(session_id))

        async def _resume_driver_or_error(session_id: str, *, auto: bool = False):
            """Resume (or fetch) the live driver, bounded by _RESUME_TIMEOUT.

            Threads the connection's verified identity onto the resumed driver so that
            AskUserQuestion / permission answers (registered under driver.identity,
            resolved with this WS identity) actually match. On any failure emits an
            error frame plus a replayComplete (so the client's loading state always
            clears) and returns None.

            *auto* marks a re-attach this socket decided on by itself (the
            lifecycle-triggered auto re-attach). It must fail SOFT: no error frames
            (see _attach_failed) and above all no socket close — a transient daemon
            blip would otherwise take a dashboard socket's other five attached
            sessions down with the one that happened to emit a status event.
            """
            try:
                # The deadline goes INTO the RPC (call_with_deadline), never around it:
                # asyncio.wait_for CANCELS an in-flight httpx request, orphaning the
                # socket and stranding its pool slot ACTIVE forever (issue #734). Expiry
                # therefore arrives as DaemonTimeout — a DaemonUnavailable subclass, so
                # it must be caught BEFORE the generic daemon-down branch below or a
                # slow resume would close 1013 instead of surfacing the retry hint.
                driver = await call_with_deadline(
                    orchestrator.ensure_driver, session_id, identity,
                    timeout=_RESUME_TIMEOUT)
            except (asyncio.TimeoutError, DaemonTimeout):
                # A wedged resume (saturated box / stuck MCP fleet) must not hang the
                # socket forever — surface it so the client can stop spinning and retry.
                log.warning("ws.attach: ensure_driver timed out for %s after %.0fs",
                            session_id, _RESUME_TIMEOUT)
                await _attach_failed(
                    session_id, "SESSION_RESUME_TIMEOUT",
                    "This session is taking too long to resume — the server may be "
                    "busy. Try again in a moment.", auto)
                return None
            except DaemonUnavailable:
                log.warning("ws.attach: session daemon unavailable for %s", session_id)
                if not auto:
                    await websocket.close(code=1013)
                return None
            except Exception:
                # A resumed/rehydrated session can fail to start its SDK subprocess —
                # most commonly because the account's token is dead (needs re-auth) or
                # the stored engine_session_id can no longer be resumed. Letting that
                # propagate would crash the WS handler, closing the socket and making
                # the client reconnect-loop forever on a blank screen. Report it and
                # keep the socket alive (replayComplete clears the loading state).
                log.warning("ws.attach: ensure_driver failed for %s", session_id, exc_info=True)
                if not auto:
                    # Only looked up for the banner's account hint, so it is skipped
                    # entirely on the silent auto path (one fewer RPC in remote mode).
                    rec = await orchestrator.get_session(session_id)
                    acct = rec.account if rec else None
                    hint = (f" Its account '{acct}' may need re-authentication — check the Accounts tab."
                            if acct else "")
                    await _attach_failed(session_id, "SESSION_START_FAILED",
                                         f"Couldn't resume this session.{hint}", auto)
                return None
            if driver is None:
                await _attach_failed(session_id, "SESSION_NOT_FOUND",
                                     f"Session {session_id!r} not found", auto)
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

        async def _run_command_and_reply(session_id: str, name: str, args: str) -> bool:
            """Dispatch a slash command and emit its commandResult frame, exactly
            as the interactive `command` frame does. Returns True if the command
            was known (handled — whether it succeeded or errored), False if
            unknown so the caller can decide the fallback (the command frame
            reports "unknown command"; the initial-prompt path re-sends the text
            as a normal prompt). Single source for command dispatch-and-reply so
            the two entry points can't drift."""
            try:
                result = await orchestrator.run_command(session_id, name, args,
                                                        identity=identity)
            except KeyError:
                return False
            except Exception:
                log.warning("ws.command failed session=%s name=%s", session_id, name, exc_info=True)
                await _send(redact_event({"type": "commandResult", "sessionId": session_id,
                             "name": name, "ok": False, "error": "command failed"}))
                return True
            if result is not None:
                await _send(redact_event({"type": "commandResult", "sessionId": session_id,
                             "ok": True, **result}))
            return True

        async def _emit_live_extras(session_id: str, driver, events: list, *,
                                    open_turn_resupplied: bool = False) -> None:
            """Post-attach live wiring: suggestion seeding, command catalog, pending
            permission/question re-emit, live status, and the deferred initial prompt.
            Only meaningful once a live driver exists. Each step is best-effort.

            Session-scoped frames (permissionRequest, question, status, queueUpdate)
            ride out_q — the replay now flows through that queue, and a direct send
            here would OVERTAKE it (a permissionRequest landing before replayReset
            gets wiped: the reducer stores permission blocks inside turns). They are
            enqueued raw + session-tagged; the batcher redacts at flush. The
            'commands' catalog is the deliberate exception (see below).

            ``open_turn_resupplied`` — the in-progress turn was dropped from the disk
            replay and the live stream's snapshot (driver turn buffer / daemon ring)
            re-supplies it whole, asks included (session bce4b626, 2026-09-05 05:53).
            Then the pending permission/question re-emit below is NOT just redundant,
            it is mis-ordered: these frames enter out_q now, but the snapshot is only
            forwarded once the live task starts — so the ask arrived BEFORE the turn's
            own UserMessage/TurnStart, the client fabricated the assistant turn for it
            at the tail, and the re-supplied prompt was appended UNDER that turn. Every
            later delta then grew the turn above the prompt: the "my prompt is stuck at
            the bottom while the answer scrolls past it" bug. The snapshot copy sits at
            the ask's real place in the turn, so it is the only copy sent. The re-emit
            stays for the attaches nothing re-supplies from — a full disk replay after
            ring eviction (the disk card is read-only and carries a different id), or a
            cursor catch-up (the client already holds the turn, dedup by id)."""
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

            from server.sessions import events as ev
            if not open_turn_resupplied:
                # Re-emit any pending permission requests for this session. Build the
                # frame through PermissionRequest.to_wire() — the same wire format the
                # live path uses — so the re-emit can't drift from it (it previously
                # hardcoded plan=False, dropping the plan flag for a pending ExitPlanMode
                # approval).
                for p in await orchestrator.pending_permissions(session_id):
                    out_q.put_nowait({**PermissionRequest(
                        p["permId"], p["turn_id"], p["tool"], tool_kind(p["tool"]),
                        p["input"], plan=(p["tool"] == "ExitPlanMode"),
                    ).to_wire(), "sessionId": session_id})

                # Re-emit any pending AskUserQuestion events for this session. Mirrors
                # the permission re-emit above so a refresh/reattach while the model is
                # blocked in _handle_ask_user_question never silently hangs.
                for q in await orchestrator.pending_questions(session_id):
                    out_q.put_nowait({**ev.Question(
                        question_id=q["question_id"],
                        turn_id=q["turn_id"],
                        header=q["header"],
                        question=q["question"],
                        options=q["options"],
                        multi_select=q["multi_select"],
                    ).to_wire(), "sessionId": session_id})

            # Re-offer a question whose waiter died with the daemon. The loop above
            # only ever finds LIVE questions; after a restart the registry is empty
            # by construction, so without this the replayed card stays read-only and
            # the session sits in an unknown state (see orphaned_asks.py).
            try:
                for frame in await _orphaned_question_frames(orchestrator, session_id):
                    out_q.put_nowait({**frame, "sessionId": session_id})
            except Exception:
                log.warning("ws.attach: orphaned question re-offer failed for %s",
                            session_id, exc_info=True)

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
            # without this, a navigate-away/back showed an empty strip. Emitted even
            # when EMPTY: a client that missed the live QueueUpdate([]) as the queue
            # drained (WS gap mid-dequeue) otherwise renders its stale chip forever —
            # this re-emit is the only thing that ever corrects it. Best-effort.
            try:
                out_q.put_nowait({**ev.QueueUpdate(driver.queue_wire()).to_wire(),
                                  "sessionId": session_id})
            except Exception:
                log.warning("ws.attach: queue re-emit failed for %s", session_id, exc_info=True)

            # Send the deferred initial prompt (if any) now that the client is watching
            # a live stream.
            await _send_deferred_initial_prompt(session_id)
            # Flush any proactive watch notices queued while this drawer was closed.
            await _flush_pending_notices(session_id)

        async def _send_deferred_initial_prompt(session_id: str) -> None:
            """Pop + send the session's deferred initial prompt, if one is staged.

            The prompt is stored at spawn instead of being sent immediately so that
            its events stream live to a watching client rather than racing the
            transcript-file write or arriving before any subscriber is registered on
            the driver. The pop clears the entry, so it fires at most once; a plain
            reconnect finds nothing and falls through to live-stream only.

            Called from BOTH places this socket opens a live stream — _handle_attach's
            live path and the lazy _ensure_live a typed prompt triggers — because the
            "first attach" can happen BEFORE the spawn: a `pending_approval` / `queued`
            session viewed while it waits attaches on the read-only detached path,
            which never pops. When the spawn then lands (approval, a freed slot), only
            a later live-stream open on this same socket can deliver the prompt
            (incident 2026-09-02: a Fable session approved from its open view sat
            idle, the user's "continue" ran instead, and the titler named it after the
            CLI's `/model` bookkeeping). Best-effort like every other attach step: a
            raise would propagate out of the attach, close the just-opened stream and
            kill the whole attach — the client would spin on a dead socket over a
            deferred nicety.
            """
            try:
                pending_prompt = await orchestrator.pop_pending_initial_prompt(session_id)
                pending_attachment_ids = await orchestrator.pop_pending_initial_attachment_ids(session_id)
                # A bare slash command typed as the initial prompt runs through the
                # SAME dispatch as one typed mid-session (/model, /account, /clear,
                # /context, /help, custom commands) — so it executes as a command
                # instead of being sent to the model as literal text. Only when it
                # has no attachments (a command carrying images isn't a thing) and
                # parses as a single-line command; an unknown command falls back to
                # a normal prompt, mirroring the composer's runCommandFromText.
                handled = False
                if pending_prompt and not pending_attachment_ids:
                    parsed = parse_command_line(pending_prompt)
                    if parsed is not None:
                        handled = await _run_command_and_reply(session_id, parsed[0], parsed[1])
                if not handled and (pending_prompt is not None or pending_attachment_ids):
                    log.info("ws.attach: sending deferred initial prompt for %s", session_id)
                    await orchestrator.send_prompt(
                        session_id, pending_prompt or "", pending_attachment_ids)
            except Exception:
                log.warning("ws.attach: deferred initial prompt send failed for %s",
                            session_id, exc_info=True)

        async def _flush_pending_notices(session_id: str) -> None:
            """Deliver any proactive watch notices queued while this session's
            drawer had no live driver (Task 8.4, spec v2 §4.7 Pack N —
            SessionOrchestrator.post_notice queues one there when it fires with no
            driver attached). Called from the SAME two places as
            _send_deferred_initial_prompt, for the same reason: a live stream may
            open before OR after the notice was queued, so only a call at the
            moment a live stream actually opens can guarantee delivery either way.
            Best-effort — a raise here must not abort the attach."""
            from server.sessions import events as ev
            try:
                for notice in await orchestrator.pop_pending_notices(session_id):
                    out_q.put_nowait({**ev.Notice(
                        session_id=session_id, kind=notice.get("kind", ""),
                        title=notice.get("title", ""), body=notice.get("body", ""),
                        url=notice.get("url"), watch_id=notice.get("watchId"),
                    ).to_wire(), "sessionId": session_id})
            except Exception:
                log.warning("ws.attach: pending notice flush failed for %s",
                            session_id, exc_info=True)

        def _start_live_stream(session_id: str, stream) -> None:
            """Forward an opened driver stream's events into the batcher for this
            session. The stream was opened by driver.open_stream() BEFORE the replay
            (see _handle_attach's settle-race note); once this task starts, the
            stream's events() generator owns queue teardown.

            Refuses to register anything once the socket is tearing down: the
            teardown loop has already snapshotted live_tasks, so a task started
            after it would outlive the socket as a leaked driver subscriber
            buffering into an out_q with no batcher. Nothing has consumed the
            stream yet, so closing it is the complete disposal (same reasoning as
            _handle_attach's failure path)."""
            if closing:
                stream.close()
                return
            task = supervise(
                asyncio.create_task(
                    _forward_live(stream, out_q, session_id), name=f"ws-live-{session_id}"),
                name=f"ws-live-{session_id}", on_failure=_on_forward_task_failed)
            live_tasks[session_id] = task

            def _clear_when_finished(finished: asyncio.Task, sid: str = session_id) -> None:
                """Drop the live_tasks entry when THIS forwarding task ends on its own.

                A forwarding task does not only end via _teardown_live_task: the driver's
                stream also ends normally when the driver is released (aclose() pushes
                _STREAM_DONE), so `events()` returns and this task finishes while the
                socket is still open and attached. The entry used to survive that, and
                `_ensure_live` short-circuits on `session_id in live_tasks` — so the next
                prompt skipped the resume, hit send_prompt with no driver, and vanished.
                Identity-checked so a NEWER task registered for the same session (a
                rebind, or a resume right after this one ended) is never clobbered by a
                late callback from the old one."""
                if live_tasks.get(sid) is finished:
                    live_tasks.pop(sid, None)

            task.add_done_callback(_clear_when_finished)

        async def _maybe_catch_up(session_id: str, driver, since_src_seq: int | None,
                                  since_epoch: str | None) -> bool:
            """Wave-3 cursor catch-up (audit W2.3), remote mode only.

            When the client re-attaches (heal / reconnect) with a CONTIGUOUS srcSeq cursor
            AND the daemon ring still covers it, resume the live stream FROM that cursor
            instead of doing a full disk replay: enqueue a lightweight ``resumeOk`` (through
            the same out_q FIFO as every other session frame — never a direct send that
            could overtake queued frames), anchor the live subscription at the cursor, and
            skip replayReset/replay/replayComplete entirely. The client keeps its rendered
            transcript and the ring tail simply continues it.

            EPOCH GATE (#497). The daemon's srcSeq space is per-process: a restart resets it
            to 0. A browser disconnected ACROSS a restart still holds a stale high
            ``since_src_seq`` from the OLD epoch; if the new epoch has already produced events
            past it while the ring still holds it, ring_covers returns True and catch-up would
            SILENTLY SKIP the new epoch's early events. So the cursor is honored only when the
            epoch that minted it (``since_epoch``, echoed back from the resumeOk/replayComplete
            that established it) equals the daemon's CURRENT epoch.

            The gate is TWO-TIER so it is authoritative even when the edge's cached epoch lags
            a restart (FINDING 2 — the stale-epoch RPC race):
              1. CHEAP short-circuit on the cached ``daemon_epoch()`` — None or a mismatch ⇒
                 full replay with NO RPC (an old client with no sinceEpoch, or an obviously
                 stale cursor, costs nothing).
              2. AUTHORITATIVE confirm: when the cached epoch MATCHES, that is exactly the
                 dangerous case — the cache can be stale-but-non-None while the events-reader
                 is still in reconnect backoff after a daemon restart (RPCs go over the
                 separate ``_http`` channel and already reach the NEW daemon). So we compare
                 ``since_epoch`` against the epoch the daemon reports in the SAME ring_covers
                 response; a silent restart is caught here and full-replays. resumeOk is then
                 stamped with that authoritative epoch. Conservative throughout: unknown/None
                 on either side ⇒ mismatch ⇒ never catch up against an unknown epoch.

            Returns True iff it COMMITTED to the catch-up path. The commitment is ATOMIC
            (FINDING-1): the cheap ring_covers probe gates the path, but the actual commit
            is the ring_tail snapshot fetch — done AFTER the live fan-out is joined and only
            once the snapshot is IN HAND is resumeOk enqueued. So if the ring evicts in the
            window between the probe and the fetch, the snapshot comes back not-covered and
            the caller falls back to the full replay with NO resumeOk on the wire — never a
            half-sent resumeOk over a silently-lost tail. Local/in-process mode's ring_covers
            is a constant (False, None), so it always full-replays and ignores the cursor. Any probe
            or fetch error is treated as not-covered (safe fallback)."""
            if since_src_seq is None:
                return False
            # Epoch gate tier 1 (#497): cheap short-circuit on the cached epoch. None or a
            # mismatch ⇒ full replay with NO RPC. (An old client sends no sinceEpoch → None;
            # an obviously stale cross-epoch cursor mismatches here.)
            cached_epoch = orchestrator.daemon_epoch()
            if (since_epoch is None or cached_epoch is None
                    or since_epoch != cached_epoch):
                return False
            try:
                covered, current_epoch = await orchestrator.ring_covers(session_id, since_src_seq)
            except Exception:
                # A wedged/absent daemon or any probe failure → full replay (the safe path;
                # the disk transcript is the durable source of truth). Never half-commit.
                log.warning("ws.attach: ring_covers probe failed for %s — full replay",
                            session_id, exc_info=True)
                return False
            # Epoch gate tier 2 (FINDING 2): the cached epoch matched, but the cache can lag a
            # restart while RPCs already hit the new daemon. Compare against the epoch the
            # daemon reports in THIS ring_covers response — the authoritative-at-gate-time
            # value. A silent restart (current_epoch != since_epoch, or an old daemon that
            # sent no epoch → None) is caught here and full-replays.
            if current_epoch is None or since_epoch != current_epoch:
                return False
            if not covered:
                return False
            # ATOMIC COMMITMENT (FINDING-1). Join the live fan-out FIRST so every event
            # published from this instant is captured (anything the snapshot does not cover,
            # seq > head, is then already queued) — then fetch the ring_tail snapshot, which
            # re-verifies coverage at fetch time. Only a covered snapshot commits: we pin it
            # and enqueue resumeOk. A not-covered snapshot (evicted since the probe) or any
            # fetch error → full replay, resumeOk never sent. The joined fan-out is reused by
            # events() either way (catch-up replay, or the full-replay live forward).
            driver.join_live()
            try:
                snap = await orchestrator.ring_tail(session_id, since_src_seq)
            except Exception:
                log.warning("ws.attach: ring_tail fetch failed for %s — full replay",
                            session_id, exc_info=True)
                return False
            if not snap.get("covered"):
                return False
            driver.pin_catch_up(snap["events"], snap["head"])
            # Committed: tell the client to keep its transcript and continue from the ring
            # tail. resumeOk rides out_q (raw + session-tagged, the batcher redacts at flush)
            # exactly like replayReset. Stamp the AUTHORITATIVE epoch the daemon reported in
            # the ring_covers response (== since_epoch, we passed tier-2) so the browser
            # re-anchors its cursor to the epoch the ring it just resumed actually belongs
            # to (#497), not the edge's possibly-stale cached epoch.
            out_q.put_nowait({"type": "resumeOk", "sessionId": session_id,
                              "epoch": current_epoch})
            return True

        async def _teardown_live_task(session_id: str) -> None:
            """Cancel + await THIS socket's forwarding task for a session and drop it from
            live_tasks. The SINGLE teardown implementation, shared by _handle_detach and the
            re-attach rebind path (below), so there is never a window where two forwarding
            tasks run for one session on one socket and never a second, drift-prone copy of
            this cancel/await dance. Only this socket's subscription is torn down; the driver
            keeps running and buffering. No-op if the session isn't live on this socket."""
            task = live_tasks.pop(session_id, None)
            if task is None:
                return
            # A supervised task that already FAILED has its exception retrieved (and
            # logged) inside cancel_and_reap, so a dead forwarding task can't crash an
            # otherwise clean detach/rebind — and a cancellation aimed at THIS socket
            # handler propagates instead of being mistaken for the task's (#1164).
            await cancel_and_reap(task, phase=f"ws.live:{session_id}", timeout=0.5)

        async def _handle_attach(session_id: str, since_src_seq: int | None = None,
                                 since_epoch: str | None = None, *,
                                 auto: bool = False) -> None:
            attached.add(session_id)
            # Idempotent-with-replay (Hole 2): a re-attach of a session already live on THIS
            # socket is NOT a silent no-op. That silent return dropped the remounted view's
            # full replay whenever the unmount's detach frame was lost on a closed socket
            # (navigation return → tail-only transcript). Tear down this socket's existing
            # forwarding task FIRST (the exact detach teardown, awaited to completion) and
            # then fall through to the normal attach path — full replay, or catch-up per the
            # frame's cursor. Because teardown is awaited before open_stream/_start_live_stream
            # below, there is never a window with two forwarding tasks for one session here.
            rebind = session_id in live_tasks
            if rebind:
                await _teardown_live_task(session_id)

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
                # close 1013 (try again later) rather than crashing the handler —
                # unless this is an auto re-attach, which must never cost the user a
                # socket it shares with every other session it is viewing (see
                # _resume_driver_or_error's `auto`).
                log.warning("ws.attach: session daemon unavailable for %s", session_id)
                if not auto:
                    await websocket.close(code=1013)
                return

            # Replay the last EXACT context usage so the ctx% readout is right immediately —
            # the live ContextUsage event is never re-emitted, so without this a reloaded
            # session falls back to the model-string approximation (0% for a 1M opus session
            # whose API-stripped id lacks the `[1m]` marker). Emitted for live AND detached
            # sessions; survives the following replayReset (which doesn't clear contextUsage).
            await _emit_persisted_context_usage(session_id, rec)

            if rec is not None and getattr(rec, "detached", False):
                log_event(log, logging.INFO, "ws.attach.decision",
                          session_id=session_id, mode="full_replay",
                          rebind=rebind, cursor=since_src_seq, detached=True)
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
            driver = await _resume_driver_or_error(session_id, auto=auto)
            if driver is None:
                return
            # Wave-3 cursor catch-up (remote mode only): if the client's srcSeq cursor is
            # still covered by the daemon ring, skip the whole disk replay and resume the
            # live stream from the cursor. Probed + committed (resumeOk enqueued) BEFORE
            # open_stream so the decision is made once, up front — never a half-sent
            # resumeOk. Not covered / no cursor / local mode → catching_up is False and the
            # full replay below runs exactly as before.
            catching_up = await _maybe_catch_up(session_id, driver, since_src_seq, since_epoch)
            # Epoch-gate visibility (#497): whether the cursor's minting epoch matched the
            # daemon's current epoch, surfaced in the journal so a stale-epoch full-replay
            # (after a daemon restart) is distinguishable from a not-covered one. Only
            # meaningful when a cursor was actually sent.
            daemon_epoch = orchestrator.daemon_epoch()
            epoch_match = (bool(since_epoch is not None and daemon_epoch is not None
                                and since_epoch == daemon_epoch)
                           if since_src_seq is not None else None)
            # Diagnosability (incident 2026-07-05): the catch-up vs full-replay decision was
            # invisible in the journal, so a tail-only nav-return couldn't be told apart from a
            # legitimate resume. Log the mode + whether this was a rebind (already-live re-attach)
            # + the cursor the client sent, at the moment the decision is made.
            log_event(log, logging.INFO, "ws.attach.decision",
                      session_id=session_id,
                      mode=("catch_up" if catching_up else "full_replay"),
                      rebind=rebind, cursor=since_src_seq, detached=False,
                      since_epoch=since_epoch, daemon_epoch=daemon_epoch,
                      epoch_match=epoch_match)
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
                if catching_up:
                    # Catch-up: no disk replay, no replayReset/replayComplete. The client
                    # keeps its transcript and the ring tail (srcSeq cursor+1…) continues
                    # it. Still re-emit pending live state (permissions/questions/status/
                    # queue) AFTER the resumeOk that _maybe_catch_up already enqueued — the
                    # client dedups those by id. No in-progress-turn trim: the ring already
                    # carries every event after the cursor, and the client has everything up
                    # to it, so there is nothing to drop or re-supply.
                    await _emit_live_extras(session_id, driver, [])
                else:
                    drop_incomplete = stream.turn_active
                    turn_started_at = stream.turn_started_at
                    # Remote mode: dropping the open turn from the disk replay is licensed
                    # ONLY by a re-supply that actually happens. The shared DaemonClient
                    # keeps one daemon subscription per stream, so a RE-attach's subscribe
                    # `since` is silently ignored — the ring never replays into this attach
                    # (incident 2026-07-07, session fced5538: every mid-turn reload of an
                    # autonomous session reset the view to post-attach events only). Pin a
                    # one-shot ring_tail snapshot of the open turn instead; if the ring no
                    # longer covers it (evicted in the window), degrade to the #526 safe
                    # shape — FULL disk replay, turn NOT dropped, trim disabled (the pair
                    # must degrade together, see daemon.turn_started_at). Local drivers
                    # have no resupply hook: their open_stream() queue snapshot already
                    # carries the in-progress turn atomically.
                    prepare = getattr(driver, "prepare_open_turn_resupply", None)
                    if drop_incomplete and prepare is not None and not await prepare():
                        drop_incomplete = False
                        turn_started_at = None
                    events = await _replay_and_complete(
                        session_id, drop_incomplete=drop_incomplete,
                        turn_started_at=turn_started_at)
                    # drop_incomplete is exactly "the stream snapshot delivers the open
                    # turn" — the pair was pinned together above — so it also decides
                    # whether the pending-ask re-emit would duplicate (and precede) it.
                    await _emit_live_extras(session_id, driver, events,
                                            open_turn_resupplied=drop_incomplete)
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
            was already emitted). Idempotent: a no-op if already streaming.

            Emits ``ws.session.resume`` so the cost of the lazy resume is a MEASURED
            number rather than an anecdote (#1180): ``resume_ms`` is ensure_driver
            alone (spawning the SDK subprocess and its MCP fleet — the part suspected
            of dominating the 30 s–2 min a user waits), ``total_ms`` adds the
            catalog/deferred-prompt/status wiring that follows it. An already-live
            session short-circuits and logs nothing, so the event counts only real
            resumes.
            """
            if session_id in live_tasks:
                return True
            started = time.monotonic()
            driver = await _resume_driver_or_error(session_id)
            resume_ms = round((time.monotonic() - started) * 1000, 1)
            if driver is None:
                log_event(log, logging.WARNING, "ws.session.resume",
                          session_id=session_id, resume_ms=resume_ms, ok=False)
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
            # A prompt deferred at spawn goes FIRST: this socket may have attached on
            # the detached path before the session was spawned (see the helper), and
            # the user's typed prompt must queue behind the original one, not replace it.
            await _send_deferred_initial_prompt(session_id)
            # Flush any proactive watch notices queued while this drawer was closed.
            await _flush_pending_notices(session_id)
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
            log_event(log, logging.INFO, "ws.session.resume",
                      session_id=session_id, resume_ms=resume_ms,
                      total_ms=round((time.monotonic() - started) * 1000, 1), ok=True)
            return True

        async def _handle_detach(session_id: str) -> None:
            attached.discard(session_id)
            # Drop the auto re-attach rate stamp with the view it throttled: kept, it
            # would suppress the FIRST legitimate re-attach after a detach-then-attach
            # inside the interval, and it would accumulate an entry per session this
            # socket ever touched. The in-flight guard is NOT cleared here — it is
            # owned by the running task's finally, and clearing it early would let a
            # second task spawn alongside the first.
            auto_reattach_last.pop(session_id, None)
            await _teardown_live_task(session_id)

        async def _auto_reattach(session_id: str) -> None:
            """Re-run the attach path because a lifecycle status event showed this
            attached session changed while the socket had no live forwarding task
            for it (snapshot viewer). No cursor is passed — the full-replay path is
            the safe converger, and the client's reducer double-buffers replayReset
            so the view doesn't flash.

            Reads the record FIRST, outside the attach, for two reasons.

            1. Fail soft. Both of _handle_attach's daemon-down paths close the whole
               socket (1013), which is right for a user-initiated attach and wrong
               here: on a dashboard socket viewing six sessions, a 5 s daemon blip
               plus one status event would close the socket for all six. Nothing the
               user did asked for this attach, so nothing it does may cost them the
               connection — hence `auto=True` below and this guard here.

            2. Skip a session that is still DETACHED. Re-attaching one replays the
               exact snapshot the client already has, so there is nothing to converge
               to — and _handle_attach's detached branch registers no live task, so
               the session stays attached-and-not-live and EVERY later status event
               would re-run a full disk replay and push replayReset + the whole
               transcript window at a session that is not even running. Driver-less
               _persist_status writes happen for paused / needs_attention / queued
               sessions, so a user parked on one would watch its transcript reset on
               a loop. The event worth reacting to is the session becoming LIVE,
               which is exactly the autopilot-resume case this feature exists for:
               ensure_driver registers the driver (or its in-flight spawn) before the
               status flip, so the record already reads live here.

            Preconditions are then re-checked under the session's attach lock: the
            user may have detached, or a user attach/prompt may have raced ahead and
            already made the session live on this socket.
            """
            try:
                try:
                    rec = await orchestrator.get_session(session_id)
                except DaemonUnavailable:
                    log.warning("ws.auto_reattach: session daemon unavailable for %s",
                                session_id)
                    return
                if rec is None or getattr(rec, "detached", False):
                    return
                async with _attach_lock(session_id):
                    if (closing or session_id not in attached
                            or session_id in live_tasks):
                        return
                    log_event(log, logging.INFO, "ws.auto_reattach",
                              session_id=session_id)
                    auto_reattach_last[session_id] = time.monotonic()
                    await _handle_attach(session_id, auto=True)
            finally:
                auto_reattach_pending.discard(session_id)
                auto_reattach_tasks.pop(session_id, None)

        # ------------------------------------------------------------------
        # Per-session work chains — the receive loop must never block (#1180)
        # ------------------------------------------------------------------
        # The handler used to process frames strictly sequentially, so while
        # _ensure_live resumed a detached session's `claude` process NOTHING else on
        # this socket was handled: no permission answer, no question answer, no
        # prompt for a different session. A user who gave up and hit Stop during a
        # slow wake-up was queued behind the very thing they wanted to cancel.
        #
        # Spawning a bare task per frame would fix the stall and break ordering
        # (prompt #2 overtaking prompt #1's resume, a detach tearing down the stream
        # an attach is still building). So ordering is preserved exactly where it is
        # load-bearing — PER SESSION — and nowhere else: one FIFO chain per session
        # id, and the receive loop returns to receive_text() immediately.

        # session_id → its FIFO of pending work (None is the drain sentinel)
        work_queues: dict[str, "asyncio.Queue[tuple[str, Callable[[], Awaitable[None]], float] | None]"] = {}
        # session_id → the task draining that FIFO
        work_tasks: dict[str, asyncio.Task] = {}

        async def _work_chain(
            session_id: str,
            queue: "asyncio.Queue[tuple[str, Callable[[], Awaitable[None]], float] | None]",
        ) -> None:
            """Run one session's deferred frames, strictly in order, until drained.

            A failing item must NOT kill the chain — that would silently strand every
            later frame for this session on an otherwise healthy socket. Each item is
            therefore isolated exactly like the inline dispatch was: a handler that
            raised never took the receive loop down either.
            """
            with bind_session(session_id):
                while True:
                    item = await queue.get()
                    if item is None:      # drain sentinel — socket is closing
                        return
                    label, factory, queued_at = item
                    waited_ms = (time.monotonic() - queued_at) * 1000
                    if waited_ms >= _WORK_WAIT_LOG_MS:
                        # The head-of-line delay this change exists to bound. Visible
                        # in the journal so a stall shows up as a number.
                        log_event(log, logging.INFO, "ws.work.queued",
                                  session_id=session_id, op=label,
                                  waited_ms=round(waited_ms, 1), depth=queue.qsize())
                    # ONE event-loop checkpoint per item — do not remove.
                    #
                    # The receive loop this replaces had this property for free:
                    # anyio's stream receive() begins with an unconditional
                    # `await checkpoint()`, so every frame boundary yielded once.
                    # `asyncio.Queue.get()` does NOT — on a non-empty queue it returns
                    # without ever awaiting, so a burst of frames would run back to
                    # back with zero scheduler ticks between them.
                    #
                    # That window is load-bearing. `attach`+`detach` arriving together
                    # (the client's gap-heal does exactly this) would otherwise cancel
                    # the ws-live task `attach` had just created BEFORE it had ever run,
                    # so the driver-stream snapshot it was holding — the in-progress
                    # turn, which the disk transcript lags — was dropped on the floor.
                    # Pinned by test_ws_frame_order's B4 stale-straddle test.
                    await asyncio.sleep(0)
                    try:
                        await factory()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("ws.work failed session=%s op=%s", session_id, label)

        def _defer(session_id: str, label: str,
                   factory: "Callable[[], Awaitable[None]]") -> bool:
            """Queue *factory* on *session_id*'s chain. False = chain saturated.

            Synchronous by design: it is called from the receive loop, and the
            check-then-enqueue must not straddle an await or two frames could both
            pass the depth cap.

            A chain is created on first use and lives until the socket closes — it is
            deliberately NOT torn down on detach. An idle chain is one coroutine parked
            on queue.get(), and the alternative is worse: detach runs ON the chain, so
            it cannot cleanly retire the task it is running inside, and a re-attach
            would just rebuild it. The bound is sessions-visited-per-socket, i.e. per
            browser tab.
            """
            queue = work_queues.get(session_id)
            if queue is None:
                queue = asyncio.Queue()
                work_queues[session_id] = queue
                work_tasks[session_id] = supervise(
                    asyncio.create_task(_work_chain(session_id, queue),
                                        name=f"ws-work-{session_id}"),
                    name=f"ws-work-{session_id}", on_failure=_on_forward_task_failed)
            if queue.qsize() >= _MAX_SESSION_WORK:
                return False
            queue.put_nowait((label, factory, time.monotonic()))
            return True

        async def _drain_work_chains() -> None:
            """Let every chain finish its queued work, then exit (socket teardown).

            NOT a cancel: a disconnect during a lazy resume must still deliver the
            user's prompt. The blocking loop this replaces could not even observe the
            disconnect until _ensure_live returned, so the resume+send always ran to
            completion — cancelling here would lose messages that used to land. Each
            item is bounded by _RESUME_TIMEOUT inside ensure_driver, so the wait is
            bounded too; the caller cancels whatever is still running afterwards.
            """
            if not work_tasks:
                return
            for queue in work_queues.values():
                queue.put_nowait(None)
            _done, pending = await asyncio.wait(set(work_tasks.values()),
                                                timeout=_WORK_DRAIN_TIMEOUT)
            if pending:
                log.warning("ws.work: %d chain(s) unfinished after %.0fs — cancelling",
                            len(pending), _WORK_DRAIN_TIMEOUT)

        async def _refuse_saturated(msg_type: str, session_id: str, msg: dict) -> None:
            """Tell the client a frame was refused because the session's chain is full.

            A prompt gets ``promptRejected`` — the established "this was NOT delivered"
            ack, which the browser re-queues and flushes later — because a refused
            prompt is a user message and a dropped user message must never be silent.
            Anything else gets an error banner.
            """
            log.warning("ws.work: session %s chain saturated — refusing %s",
                        session_id, msg_type)
            if msg_type == "prompt":
                await _send({"type": "promptRejected", "sessionId": session_id,
                             "text": msg.get("text", ""),
                             "attachmentIds": msg.get("attachmentIds"),
                             "reason": "busy"})
                return
            await _send({"type": "error", "code": "SESSION_BUSY",
                         "message": "This session has too many pending requests — "
                                    "give it a moment and try again.",
                         "sessionId": session_id})
            if msg_type == "attach":
                # Every aborted attach owes the client a replayComplete or it spins on
                # "Loading session…" forever — the same debt _resume_driver_or_error
                # pays on all three of its failure paths. Rides out_q, not a direct
                # send, because replayComplete IS transcript state.
                out_q.put_nowait(_error_complete(session_id))

        # ------------------------------------------------------------------
        # Chained frame handlers — run on the session's work chain, never inline
        # ------------------------------------------------------------------

        async def _handle_prompt(msg: dict, session_id: str) -> None:
            """Resume-if-needed, then deliver one prompt.

            The lazy resume lives here rather than in the receive loop (#1180):
            send_prompt no-ops without a live driver, so a detached session must be
            woken first — and that wake is what used to freeze the whole socket. The
            receipt ack is NOT here; it is sent from the loop before this is even
            queued, so it still lands in milliseconds (see the dispatch site).
            """
            started = time.monotonic()
            # Only the mutator is locked: _ensure_live can start a live forwarding
            # task, so it must not interleave with an auto re-attach for the SAME
            # session. The lock is per session, so nothing here can delay another
            # session's prompt or attach.
            #
            # Note what that does and does not exclude: _ensure_live's tail calls
            # _send_deferred_initial_prompt, so a spawn-deferred prompt IS sent under
            # this lock. That is deliberate — it must land before the typed prompt
            # below — and it is bounded to this one session's lock. The typed
            # send_prompt stays outside because it can block on a whole resuming
            # turn, which would keep this session's own auto re-attach out for the
            # duration.
            async with _attach_lock(session_id):
                live_ok = await _ensure_live(session_id)
            if not live_ok:
                return
            ensured = time.monotonic()
            text = msg.get("text", "")
            attachment_ids = msg.get("attachmentIds")
            # "Interrupt and post": stop the in-flight turn and run this
            # prompt next instead of queueing it behind the turn. Only
            # forwarded when set so the plain contract is untouched.
            interrupt = msg.get("interrupt") is True
            extra: dict[str, bool | dict] = {"interrupt": True} if interrupt else {}
            # The context chip (spec §4.5) and the per-identity prompt rate
            # limit (spec §4.6) both apply only to an assistant-kind session,
            # so both need the record's kind up front — a coding session
            # ignores a supplied context outright and is never rate-limited
            # here. get_session is per-session-routed to the owning host in
            # fleet mode (unlike a bare `record()`, which is not in
            # FleetOrchestrator's _PER_SESSION set and would resolve to
            # RemoteOrchestrator, which has no such method at all).
            raw_context = msg.get("context")
            rec = None
            try:
                rec = await orchestrator.get_session(session_id)
            except Exception:
                # Best-effort: in fleet mode this is a real RPC that can
                # raise DaemonTimeout/DaemonUnavailable, and the prompt
                # frame had no such dependency before the context chip. A
                # transient daemon hiccup must degrade to "no context, no
                # rate limit", never cost the user their message.
                log.warning("ws.prompt session lookup failed session=%s "
                            "— sending without context/rate-limit check",
                            session_id, exc_info=True)
            assistant_record = is_assistant(rec)
            if assistant_record and raw_context is not None:
                validated_context = validate_context(raw_context)
                if validated_context is not None:
                    extra["context"] = dict(validated_context)
            if (assistant_record
                    and _assistant_prompt_limiter.check(identity) is not None):
                # Checked BEFORE send_prompt so a rate-limited prompt costs
                # nothing downstream (same principle as the tool-call
                # limiter in server/assistant/client.py).
                log.warning("ws.prompt rate_limited session=%s identity=%s",
                            session_id, identity)
                await _send({"type": "promptRejected", "sessionId": session_id,
                             "text": text, "attachmentIds": attachment_ids,
                             "reason": "RATE_LIMITED"})
                return
            if assistant_record:
                assistant_prompts_total.inc()
            status = await orchestrator.send_prompt(
                session_id, text, attachment_ids, **extra)
            if status == "no_driver":
                # The 2026-08-30 unresponsive-session incident: the daemon
                # dropped the driver AFTER this socket attached, so
                # _ensure_live's live_tasks short-circuit vouched for a
                # stream whose driver no longer existed and the user's
                # message evaporated behind a "Ready" header. live_tasks is
                # EDGE state; only send_prompt's answer reflects the
                # daemon's truth — so on no_driver, drop the stale stream,
                # rebuild through the FULL _ensure_live path (ensure_driver
                # + fresh open_stream), and retry the send exactly once.
                # A failed rebuild falls through to promptRejected below —
                # recovery must never turn into a retry loop.
                log.warning(
                    "ws.prompt: no_driver with a live stream entry — "
                    "rebuilding driver and retrying once session=%s",
                    session_id)
                # Same rule as the first _ensure_live above: the rebuild mutates
                # live_tasks, the retried send does not — so only the rebuild is
                # locked.
                async with _attach_lock(session_id):
                    await _teardown_live_task(session_id)
                    rebuilt = await _ensure_live(session_id)
                if rebuilt:
                    status = await orchestrator.send_prompt(
                        session_id, text, attachment_ids, **extra)
            await orchestrator.note_user_interaction(session_id)
            # The split #1180 asked for, isolated: `ensure_ms` is the wake
            # (ws.session.resume breaks it down further), `send_ms` is everything
            # after the driver exists. Together they answer "does the resume really
            # dominate the wait?" with a measurement instead of an anecdote — the
            # same question the staged acks made visible to the USER, now answerable
            # from the journal.
            log_event(log, logging.INFO, "ws.prompt.deliver",
                      session_id=session_id, status=status,
                      ensure_ms=round((ensured - started) * 1000, 1),
                      send_ms=round((time.monotonic() - ensured) * 1000, 1))
            if status == "sent":
                # The driver has the text: a turn starts next, or it queues
                # behind a live one (the driver emits its own queue/status
                # frames for that). Only "sent" may claim this — the
                # draining/no_driver answers get promptRejected below.
                await _send({"type": "promptAck", "sessionId": session_id,
                             "stage": "delivered"})
            if interrupt and status == "sent":
                # Same trail a Stop leaves, tagged so the audit shows the
                # turn was cut short to post a prompt, not abandoned.
                try:
                    async with db_session() as db:
                        await record_audit(db, "session.interrupt", identity,
                                           {"sessionId": session_id,
                                            "withPrompt": True})
                except Exception:
                    log.warning("ws.audit_failed event=%s session=%s",
                                "session.interrupt", session_id, exc_info=True)
            if status in ("draining", "no_driver"):
                # The turn was refused — ack it so the browser re-queues
                # instead of losing the prompt. "draining": a deploy drain.
                # "no_driver": the session's driver was released between
                # _ensure_live and the send (or _ensure_live believed it was
                # already live). Either way the prompt was NOT delivered, and
                # a dropped user message must never be silent.
                log.warning("ws.prompt rejected session=%s reason=%s",
                            session_id, status)
                await _send({"type": "promptRejected", "sessionId": session_id,
                             "text": text, "attachmentIds": attachment_ids,
                             "reason": status})

        async def _run_session_frame(msg: dict) -> None:
            """Dispatch one _CHAINED_FRAMES frame. Runs on the session's chain.

            Its identity was already re-verified against the handshake on the receive
            loop before it was queued, so a frame can never outlive a revocation by
            sitting in the queue.
            """
            msg_type = msg.get("type", "")
            session_id = msg.get("sessionId", "")
            if msg_type == "attach":
                # Optional wave-3 catch-up cursor: the client's last CONTIGUOUS daemon
                # srcSeq (audit W2.3). Accept only a real non-negative int (reject bool,
                # which is an int subclass, and anything malformed) — otherwise omit it
                # and full-replay. The server, not the client, still owns the ring
                # coverage decision (ring_covers) before honoring it.
                raw_cursor = msg.get("sinceSrcSeq")
                since_src_seq = (raw_cursor if isinstance(raw_cursor, int)
                                 and not isinstance(raw_cursor, bool)
                                 and raw_cursor >= 0 else None)
                # The epoch that minted the cursor (#497): the browser echoes back the
                # `epoch` it saw on the resumeOk/replayComplete that established this
                # cursor. Accept only a real string; anything else → None → the epoch
                # gate treats it as a mismatch and full-replays (conservative default).
                raw_epoch = msg.get("sinceEpoch")
                since_epoch = raw_epoch if isinstance(raw_epoch, str) else None
                # The session's attach lock: this chain and the lifecycle-triggered
                # auto re-attach are the two mutators of live_tasks/attached for it,
                # and they run on independent tasks.
                async with _attach_lock(session_id):
                    await _handle_attach(session_id, since_src_seq, since_epoch)
            elif msg_type == "detach":
                async with _attach_lock(session_id):
                    await _handle_detach(session_id)
            elif msg_type == "prompt":
                await _handle_prompt(msg, session_id)
            elif msg_type == "interrupt":
                async with db_session() as db:
                    await session_actions.interrupt(
                        orchestrator, db, session_id=session_id, identity=identity)
            elif msg_type == "setPermissionMode":
                mode = msg.get("mode", "")
                async with db_session() as db:
                    await session_actions.set_permission_mode(
                        orchestrator, db, session_id=session_id, mode=mode,
                        identity=identity)
            elif msg_type == "cancelQueued":
                index = msg.get("index")
                if isinstance(index, int) and not isinstance(index, bool):
                    ok = await orchestrator.cancel_queued(session_id, index)
                    if not ok:
                        # Same silent-drop shape as answerQuestion /
                        # permissionResponse (issue #917): a cancel the daemon
                        # can't honor (stale index — the queue already
                        # drained/shifted — or no live driver) must surface,
                        # not 200-and-vanish while the chip stays put.
                        log.warning("ws.cancel_queued rejected session=%s index=%s identity=%s",
                                    session_id, index, identity)
                        await _send({"type": "error", "code": "QUEUED_NOT_FOUND",
                                     "message": "That queued prompt is no longer in the queue "
                                                "— it may have already run or been removed. "
                                                "Reload to see its current state.",
                                     "sessionId": session_id})
            elif msg_type == "command":
                name = msg.get("name", "")
                args = msg.get("args", "")
                if not await _run_command_and_reply(session_id, name, args):
                    await _send(redact_event({"type": "commandResult", "sessionId": session_id,
                                 "name": name, "ok": False, "error": "unknown command"}))

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
                # Skipped for a `ccas2:` (via="cli") socket: `verify_identity` only ever
                # reads the Tailscale proxy header, which a bearer-token connection
                # never carries — re-running it every frame would close a valid
                # ccswitch-ask socket on its very first message. The token's validity
                # (signature + DB revocation + expiry) was already fully checked once
                # at the handshake in `require_ws_identity`, and unlike a Tailscale
                # header behind a proxy, the Authorization header cannot drift mid-
                # socket, so there is nothing here for a per-frame re-check to catch.
                if via != "cli":
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

                if msg_type in _CHAINED_FRAMES:
                    if msg_type == "prompt":
                        # ACK RECEIPT FIRST — before the wake-up below, not after it.
                        #
                        # This is the positive counterpart to the promptRejected frame
                        # further down. Everything else this branch does is slow: on a
                        # detached session _ensure_live resumes a `claude` process, and
                        # that resume is where a user's 30 s–2 min of total UI silence
                        # actually went. Until now the first observable consequence of a
                        # prompt was the driver's own user-echo at the far end of all of
                        # it, so "did my message even reach the server?" had no answer
                        # for as long as the answer took to compute.
                        #
                        # `resuming` tells the browser WHICH wait it is in, so the pending
                        # bubble can say "waking session…" rather than a bare spinner.
                        # live_tasks membership is the same short-circuit _ensure_live
                        # itself uses, read before the await so it reflects the decision
                        # _ensure_live is about to make.
                        #
                        # It stays on the RECEIVE LOOP even though the work below is now
                        # deferred (#1180). Queueing the ack with the work would put it
                        # behind whatever else this session has in flight — the one thing
                        # an "I heard you" frame may never wait for.
                        await _send({
                            "type": "promptAck", "sessionId": session_id,
                            "stage": "received" if session_id in live_tasks else "resuming",
                        })
                    # Ordered per session, off the receive loop. The lazy resume a
                    # prompt triggers (_ensure_live) can take tens of seconds, and it
                    # used to run HERE — freezing every other frame on the socket
                    # (#1180). Queued instead, so the loop is back at receive_text()
                    # in microseconds while the wake proceeds.
                    #
                    # partial, not a lambda: the frame is bound NOW, so the next loop
                    # iteration rebinding `msg` can never change what a queued item runs.
                    if not _defer(session_id, msg_type,
                                  partial(_run_session_frame, msg)):
                        await _refuse_saturated(msg_type, session_id, msg)
                elif msg_type == "permissionResponse":
                    decision = msg.get("decision", "")
                    perm_id = msg.get("permId", "")
                    # The helper records its audit event unconditionally (matching the
                    # pre-extraction behaviour: a rejected decision still leaves an
                    # audit trail), so it must run to completion — and therefore
                    # commit — INSIDE this db_session block; only the ActionError is
                    # caught here, to render the WS-specific error frame afterward.
                    action_error: ActionError | None = None
                    async with db_session() as db:
                        try:
                            await session_actions.resolve_permission(
                                orchestrator, db, session_id=session_id, perm_id=perm_id,
                                decision=decision, identity=identity, via=via)
                        except ActionError as exc:
                            action_error = exc
                    if action_error is not None:
                        # Same silent-drop shape as answerQuestion below: an
                        # unresolvable decision must surface, not 200-and-vanish.
                        log.warning("ws.resolve_permission rejected session=%s perm=%s identity=%s",
                                    session_id, perm_id, identity)
                        await _send({"type": "error", "code": action_error.code,
                                     "message": "That permission request is no longer pending "
                                                "— the session may have moved on. Reload to "
                                                "see its current state.",
                                     "sessionId": session_id})
                elif msg_type == "answerQuestion":
                    question_id = msg.get("questionId", "")
                    selected = msg.get("selected", [])
                    mode = msg.get("mode", "option")
                    action_error = None
                    async with db_session() as db:
                        try:
                            await session_actions.answer_question(
                                orchestrator, db, session_id=session_id,
                                question_id=question_id, selected=selected, mode=mode,
                                identity=identity, via=via)
                        except ActionError as exc:
                            action_error = exc
                    if action_error is not None:
                        # Never a silent no-op: an unresolvable answer (no such pending
                        # question, identity mismatch, no driver) used to return 200
                        # and leave the user staring at a card that never reacts.
                        log.warning("ws.answer_question rejected session=%s qid=%s identity=%s",
                                    session_id, question_id, identity)
                        await _send({"type": "error", "code": action_error.code,
                                     "message": "That question is no longer pending — the "
                                                "session may have moved on. Reload to see "
                                                "its current state.",
                                     "sessionId": session_id})
                elif msg_type == "activity":
                    # Lightweight steering signal — user typing / interacting before send. Refreshes
                    # the human-deference window; no response frame.
                    await orchestrator.note_user_interaction(session_id)
                else:
                    await _send({"type": "error", "code": "UNKNOWN_TYPE",
                                 "message": f"Unknown message type: {msg_type!r}"})

        finally:
            # Let queued/in-flight per-session work finish first (a disconnect
            # mid-resume must still deliver the prompt), THEN cancel whatever is
            # left. The drain runs in its own try so a cancellation arriving during
            # it (a SIGTERM drain) still reaches the cancel-and-reap below instead
            # of leaking the chains as orphan tasks.
            try:
                await _drain_work_chains()
            finally:
                for sid, work_task in list(work_tasks.items()):
                    await cancel_and_reap(work_task, phase=f"ws.work:{sid}", timeout=0.5)
                work_tasks.clear()
                work_queues.clear()

                # Fence the auto re-attach path with plain assignments, BEFORE any
                # await. event_task is still running here and can still spawn one,
                # and a re-attach that registered a forwarding task after the loop
                # below took its live_tasks snapshot would leak a driver subscriber
                # buffering forever on a closed socket. `closing` + an empty
                # `attached` stop that at three checks (the spawn site,
                # _auto_reattach's post-lock re-check, _start_live_stream).
                #
                # Deliberately NOT by taking a lock: a teardown must unwind promptly
                # and must never sit on an unbounded await (#1164). A _handle_attach
                # wedged on a cold MCP fleet holds its session's lock for up to
                # _RESUME_TIMEOUT (90 s); a cancellation arriving while we waited for
                # it would skip the reaps and unsubscribe_events below, orphaning the
                # forwarding tasks and leaving a lifecycle queue the orchestrator
                # publishes into forever.
                closing = True
                attached.clear()
                try:
                    # Reap the forwarder first — it is the only spawner of auto
                    # re-attach tasks, so the set below cannot grow while we drain it.
                    await cancel_and_reap(event_task, phase="ws.events", timeout=0.5)
                    for sid, auto_task in list(auto_reattach_tasks.items()):
                        await cancel_and_reap(auto_task, phase=f"ws.auto_reattach:{sid}",
                                              timeout=0.5)
                    auto_reattach_tasks.clear()
                    # Cancel live streams (bounded per session by _teardown_live_task).
                    for sid in list(live_tasks.keys()):
                        await _handle_detach(sid)
                    await cancel_and_reap(batcher_task, phase="ws.batcher", timeout=0.5)
                finally:
                    # Unconditional: an unsubscribed-from queue is one the orchestrator
                    # keeps publishing into for the life of the process, so it must
                    # survive a cancellation or failure anywhere above.
                    await orchestrator.unsubscribe_events(event_q)
                log.debug("ws.disconnect identity=%s", identity)

    return router
