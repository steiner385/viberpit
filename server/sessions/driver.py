from __future__ import annotations
import asyncio
import base64
import contextlib
import dataclasses
import functools
import hashlib
import logging
import os
import time
import uuid
from typing import Callable
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ResultMessage
from claude_agent_sdk._errors import CLIConnectionError
from ccswitch import copilot as _copilot_mod
from ccswitch.accounts import provider_of as _provider_of
from server.aio import bounded_wait
from server.assistant.context import render_context_header
from . import events as ev
from .failure import classify, TurnFailure
from .translator import Translator
from .permissions import PermissionRegistry, to_sdk_permission_mode
from .questions import QuestionRegistry
from .sdk_contract import is_turn_message
from .status import StatusSignals, derive_status
from .transcript_repair import repair_resumed_transcript

_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

log = logging.getLogger(__name__)

# DIAGNOSTIC (temporary): gate the per-emit status trace. Off by default — set
# CCSWITCH_STATUS_TRACE=1 to capture the raw derived status + its input signals
# when chasing false-"Ready" reports. Remove with the trace itself once settled.
_STATUS_TRACE = os.environ.get("CCSWITCH_STATUS_TRACE", "false").lower() in ("1", "true", "yes", "on")

_STREAM_DONE = object()

# Events that reconstruct a turn's transcript — the only ones retained in the
# in-progress-turn buffer and replayed to a late subscriber. Control/ephemeral
# events (Status, QueueUpdate, ContextUsage, Suggestions, Title, PermissionRequest,
# Question, Condensing, TurnFailure) are re-emitted from authoritative state by the
# WS attach path (_emit_live_extras); buffering them would double-emit.
_TURN_CONTENT = (ev.UserMessage, ev.TurnStart, ev.TurnModel, ev.SessionMeta,
                 ev.TextDelta, ev.ThinkingDelta, ev.ToolCall, ev.ToolResult,
                 ev.TurnEnd, ev.ErrorEvent)

# How long aclose() waits for the CLI to flush its synthetic interrupt result for a
# turn wedged mid-tool-call before giving up and cancelling the drain.
_CLOSE_GRACE_SEC = 5.0

# Budget for the SDK client's __aexit__ during aclose(): the real client runs two
# 5s subprocess-terminate timeouts internally, so 15s covers it with headroom while
# still keeping a wedged exit from hanging teardown (#846).
_CLOSE_AEXIT_TIMEOUT_S = 15.0

# Soft notice text surfaced when a dropped turn is being auto-resent.
_RECOVERING_MSG = "Recovering the dropped turn…"


async def _aiter_one(message: dict):
    yield message


# Sentinel returned by _MessageSource.next() once the SDK stream has ended (the
# subprocess is gone). A value rather than an exception so the two consumers (a
# prompt's drain, the idle listener) branch on it the same way as on a message.
_EOF = object()


class _MessageSource:
    """The ONE cursor over ``client.receive_messages()``, shared by a prompt's drain and
    the idle listener, and safe to hand over between them by cancellation.

    The SDK's stream is a single bounded channel; whoever awaits it must be exactly one
    consumer at a time, and the hand-off (idle listener cancelled → drain takes over)
    must not lose the message that was in flight at that instant. anyio's memory-stream
    ``receive()`` CAN drop an item delivered in the same tick as the cancellation (the
    sender hands it to the parked receiver, whose wake-up then raises CancelledError).
    So the in-flight ``__anext__`` is never cancelled with its awaiter: it runs as its
    own future, the awaiter waits on a shield, and a cancelled awaiter leaves the
    future in place for the next caller to collect.
    """

    def __init__(self, client) -> None:
        self._gen = client.receive_messages()
        self._pending: asyncio.Future | None = None

    async def next(self):
        """The next SDK message, or ``_EOF`` once the stream has ended."""
        if self._pending is None:
            self._pending = asyncio.ensure_future(self._gen.__anext__())
        pending = self._pending
        try:
            msg = await asyncio.shield(pending)
        except asyncio.CancelledError:
            # Only this awaiter was cancelled: `pending` keeps running and stays
            # stored, so its message is delivered to the next next() — never lost.
            raise
        except StopAsyncIteration:
            self._pending = None
            return _EOF
        except BaseException:
            self._pending = None
            raise
        self._pending = None
        return msg

    async def aclose(self, grace: float, *, phase: str) -> None:
        """Retire the cursor. Bounded like every other teardown wait: a cancel-immune
        ``__anext__`` (a stream that swallows the cancel, #846) is abandoned after
        ``grace`` rather than wedging the close."""
        pending, self._pending = self._pending, None
        if pending is not None and not pending.done():
            pending.cancel()
            await bounded_wait(pending, timeout=grace, phase=phase, cancel_grace=grace)
        with contextlib.suppress(BaseException):
            await self._gen.aclose()


class DriverStream:
    """One attach's atomically-opened view of a driver's event stream.

    Built by ``SessionDriver.open_stream()`` with NO awaits between snapshotting
    the in-progress turn buffer, registering the subscriber queue, and capturing
    ``turn_active``/``turn_started_at`` — so all four describe the SAME instant.

    The WS attach path needs that atomicity (the settle-race bug, B3): it awaits
    a multi-step disk replay between reading ``turn_active`` and consuming the
    stream. If the turn settles during those awaits, the driver clears
    ``_turn_buffer`` — a snapshot taken *after* the replay would be empty, and
    the in-progress turn the replay dropped (``drop_incomplete=True``) would be
    delivered by NOBODY. Opening the stream first pins the snapshot and the
    flags to one consistent instant; anything emitted afterwards lands in the
    queue and is delivered live.
    """

    def __init__(self, driver: "SessionDriver", snapshot: list,
                 queue: asyncio.Queue, turn_active: bool,
                 turn_started_at: float | None) -> None:
        self._driver = driver
        self._snapshot = snapshot
        self._queue = queue
        self.turn_active = turn_active
        self.turn_started_at = turn_started_at

    async def events(self):
        """Yield the snapshot (the past), then drain the queue (strictly the
        future) until the driver's close sentinel."""
        try:
            for e in self._snapshot:
                yield e
            while True:
                item = await self._queue.get()
                if item is _STREAM_DONE:
                    return
                yield item
        finally:
            self._driver._subscribers.discard(self._queue)

    def close(self) -> None:
        """Discard the subscriber queue without consuming ``events()``.

        Leak guard for the WS attach error path: if the replay between
        ``open_stream()`` and the live-task creation raises, nothing will ever
        iterate ``events()`` (whose ``finally`` normally deregisters), so the
        queue must be discarded here or it buffers every future emit forever.
        Idempotent — safe alongside the generator's own discard."""
        self._driver._subscribers.discard(self._queue)


class SessionDriver:
    """Drives one Claude session via an SDK client, exposing normalized events.

    The orchestrator composes the per-account config dir and passes it as `config_dir`;
    `sdk_factory(cwd, config_dir, can_use_tool, mode, resume) -> SDKClientProtocol` builds
    the client (production = real ClaudeSDKClient; tests = FakeSDKClient).
    """

    def __init__(self, session_id, account, cwd, config_dir, *, mode, sdk_factory, registry,
                 identity, denylist=PermissionRegistry.DANGEROUS, resume=None,
                 on_session_id=None, on_sync_back: Callable[[], None] | None = None,
                 on_status=None, on_record_condense_ms=None, get_condense_estimate=None,
                 on_recoverable_failure=None, on_context_usage=None, clock=None, suggester=None,
                 assistant_api=None):
        self.session_id = session_id
        self.account = account
        self.cwd = cwd
        self.config_dir = config_dir
        self.mode = mode
        # The permission mode active immediately before plan mode was entered, so
        # exiting plan restores it instead of collapsing to the SDK 'default' engine.
        self._pre_plan_mode: str | None = None
        # Backing field for the ``identity`` property — the registries it cascades
        # to are not built yet at this point, so the ctor writes it directly.
        self._identity = identity
        self.denylist = denylist
        self.resume = resume
        self.claude_session_id: str | None = None
        self._on_session_id = on_session_id
        self._on_sync_back = on_sync_back
        # async callback ``on_status(session_id, derived_status)`` — keeps
        # SessionRecord.status (shown in the session LIST) in sync with the driver's
        # real state, using the RICH derived vocabulary (in_progress / waiting_input /
        # ready_review / done / idle), not just the live working/idle of the StatusBar.
        self._on_status = on_status
        self._on_record_condense_ms = on_record_condense_ms
        self._get_condense_estimate = get_condense_estimate
        self._condense_started_at: float | None = None
        # async callback ``on_recoverable_failure(session_id, kind, retry_after)`` fired
        # when a turn fails with a recoverable kind (rate_limit / auth_401 / process_crash). The
        # orchestrator drives the actual recovery (same-account retry/recompose, then
        # rotation). ``clock`` is injected only for deterministic retry-after-from-reset
        # tests; production uses wall-clock time.
        self._on_recoverable_failure = on_recoverable_failure
        # async callback ``on_context_usage(session_id, used_tokens, max_tokens)`` fired
        # after each turn settles. Lets the orchestrator persist the exact reading on the
        # SessionRecord so the WS bridge can replay it on attach (the live ContextUsage event
        # is otherwise never re-emitted, leaving reloaded sessions on the 0%-pinning fallback).
        self._on_context_usage = on_context_usage
        self._clock = clock or time.time
        self._last_progress_at: float = self._clock()
        self._last_rate_reset: int | None = None  # resets_at (epoch s) from a RateLimitEvent
        self._factory = sdk_factory
        # The loopback HTTP client an assistant-kind session's MCP tools call
        # ccswitch's own API through (server/assistant/client.py). None for every
        # coding session. Owned here (not the orchestrator) so it is closed exactly
        # once, alongside the SDK client, in aclose() below.
        self._assistant_api = assistant_api
        self._reg = registry
        self._qreg = QuestionRegistry()
        self._tr = Translator()
        self._client = None
        # Broadcast fan-out: each events() subscriber gets its own queue. A single
        # asyncio.Queue is a WORK queue (each item to one getter) — fatal when
        # "attach" happens more than once per session (reconnect, 2nd viewer).
        self._subscribers: set[asyncio.Queue] = set()
        # Coalesced transcript events of the IN-PROGRESS turn, replayed to a late
        # subscriber so a mid-stream (re)attach shows the whole message, not the tail.
        self._turn_buffer: list = []
        self._pump: asyncio.Task | None = None
        self._titled: bool = False
        self._queue: list[tuple[str, list[dict], dict | None]] = []
        # The last prompt-context dict (spec §4.5) actually sent to the SDK — never
        # set for a coding session (it never passes context). Lets send_prompt/
        # _start_turn skip re-prepending the `<ccswitch-context>` header on
        # consecutive prompts from the same page.
        self._last_context: dict | None = None
        self._turn_active: bool = False
        # Epoch seconds of the in-progress turn's start; None whenever no turn is
        # active. Consumed by the WS attach path (→ replay_window.turn_started_at)
        # to scope the incomplete-turn drop to the turn that actually is in progress.
        self._turn_started_at: float | None = None
        self._retrying: bool = False
        self._condensing: bool = False
        self._turns_completed: int = 0
        self._last_turn_outcome: str = "success"
        self._last_pr_resolution: str | None = None
        self._last_model_request: dict | None = None
        self._suggester = suggester
        self._suggest_task: asyncio.Task | None = None
        self._last_user_text: str = ""
        self._turn_produced_output: bool = False
        self._last_assistant_parts: list[str] = []
        self._last_turn_id: str = ""
        self._last_suggestions: list[str] = []
        # Signature (sha1 of the last generated context) guarding _run_suggestions
        # against regenerating byte-identical suggestions across turn boundaries.
        self._last_suggest_sig: str | None = None
        self._idle_seeded: bool = False
        # The single cursor over the SDK stream (see _MessageSource) and the task that
        # drains it BETWEEN prompts. The CLI emits whole turns with no query() behind
        # them (a background task's <task-notification> it injects as a user message),
        # and the SDK channel is bounded — an unread stream blocks the CLI on stdout and
        # replays the stale turn into the next prompt (incident 2026-09-03: a stale
        # `complete` result released the driver mid-answer). So the stream is consumed
        # at ALL times: by the prompt's pump while a prompt turn runs, else by the
        # listener, which tracks a CLI-initiated turn exactly like a prompt's.
        self._source: _MessageSource | None = None
        self._listener: asyncio.Task | None = None
        self._closing: bool = False

    def _turn(self):
        return getattr(self._tr, "_turn_id", None) or "turn"

    @property
    def last_progress_at(self) -> float:
        """Timestamp of the last meaningful driver event (set at driver construction)."""
        return self._last_progress_at

    @property
    def drain_alive(self) -> bool:
        """True while the current turn's drain task is still running.

        The stall watchdog reads this to tell a *resolved* interrupt from one the SDK
        ignored. ``interrupt()`` flips ``_turn_active`` False immediately, but the
        subprocess only really stops when the drain unwinds and completes. If the drain
        is STILL alive a tick later with progress frozen, the interrupt didn't land (a
        wedged compaction subprocess) — the watchdog escalates that to a recompose
        instead of abandoning the zombie turn.
        """
        return self._pump is not None and not self._pump.done()

    def _turn_live(self) -> bool:
        """A turn is in flight: a prompt's pump is draining, or the idle listener is
        tracking a CLI-initiated turn (`_turn_active`). The one test that decides
        whether a new prompt starts now or waits."""
        return self.drain_alive or self._turn_active

    @property
    def awaiting_user_input(self) -> bool:
        """True when the turn is blocked inside ``_can_use_tool`` waiting on the human
        (a pending permission, ExitPlanMode plan approval, or AskUserQuestion).

        Such a turn is ``_turn_active`` with a frozen ``last_progress_at`` only because
        the user hasn't answered yet — it is NOT wedged, so the stall watchdog must skip
        it. Without this, a slow human reply trips a spurious ``[Request interrupted by
        user]``.
        """
        return bool(self._reg.pending(self.session_id)) or bool(
            self._qreg.pending(self.session_id))

    @property
    def in_api_backoff(self) -> bool:
        """True when the current turn is waiting on API rate-limit/backoff rather than
        genuinely wedged. The SDK can go silent during a 429/529 backoff without
        emitting a per-wait ApiRetry, so its progress clock freezes even though the turn
        is alive. The stall watchdog reads this to extend its timeout (issue #185).

        Two signals: the retrying flag (an ApiRetry was seen and no real event has
        resumed the turn yet) and an unexpired rate-limit reset stashed from a
        RateLimit event.
        """
        if self._retrying:
            return True
        if self._last_rate_reset is not None and self._clock() < self._last_rate_reset:
            return True
        return False

    def _note_progress(self, e) -> None:
        """Advance _last_progress_at only when e carries real content.

        Meaningful: TextDelta/ThinkingDelta with non-empty text, or any
        ToolCall/ToolResult/TurnEnd/Question/PermissionRequest.
        Empty-text deltas are NOT meaningful and must not advance the clock.
        """
        if isinstance(e, (ev.TextDelta, ev.ThinkingDelta)):
            if e.text:
                self._last_progress_at = self._clock()
        elif isinstance(e, (ev.ToolCall, ev.ToolResult, ev.TurnEnd, ev.Question, ev.PermissionRequest, ev.ApiRetry)):
            self._last_progress_at = self._clock()

    def current_status(self) -> str:
        """The rich derived status right now (no side effects). Used on WS attach."""
        return derive_status(self._dash_signals())

    def pending_foreground_prompt(self) -> str | None:
        """The active foreground prompt iff a turn is live and has produced no durable
        output yet — safe to re-send after a deploy/crash kill without duplicating
        work. Returns None once the turn has emitted text or run a tool."""
        if self._turn_active and not self._turn_produced_output:
            return self._last_user_text
        return None

    async def _on_pre_compact(self, input=None, tool_use_id=None, ctx=None) -> dict:
        """SDK PreCompact hook: compaction is starting. Stamp the start, advertise the
        learned expected duration to the UI, flip the status. Never blocks compaction."""
        self._condense_started_at = self._clock()
        self._condensing = True
        est: float | None = None
        if self._get_condense_estimate is not None:
            try:
                est = await self._get_condense_estimate()
            except Exception:
                log.warning("driver.get_condense_estimate failed session=%s", self.session_id, exc_info=True)
        self._emit(ev.Condensing(active=True, expected_ms=est))
        await self._emit_status()
        return {}

    async def _consume_condensing(self, e) -> None:
        """Toggle the flag, record a real boundary's duration, forward to the UI."""
        self._condensing = e.active
        if not e.active and self._condense_started_at is not None:
            ms = (self._clock() - self._condense_started_at) * 1000
            self._condense_started_at = None
            if self._on_record_condense_ms is not None:
                try:
                    await self._on_record_condense_ms(ms)
                except Exception:
                    log.warning("driver.record_condense_ms failed session=%s", self.session_id, exc_info=True)
        await self._emit_status()
        self._emit(e)   # forward so the UI clears the bar

    async def _emit_status(self, count: int | None = None) -> None:
        """Single status emitter — rich vocabulary to BOTH consumers.

        Emits a live Status event (for the in-session StatusBar) AND notifies the
        orchestrator (which persists it for the session LIST). `count` is the queue
        depth shown in the busy label; defaults to the live queue length.
        Best-effort on the callback: a failure must never break the session loop.
        """
        sig = self._dash_signals()
        status = derive_status(sig)
        n = len(self._queue) if count is None else count
        # DIAGNOSTIC (temporary): trace the raw status + the signals that produced
        # it, to tell a genuine idle/done from a "Ready" the UI fell back to. Logs
        # the inputs to derive_status plus the staleness clock the stall watchdog
        # uses, so a session that looks idle while monitoring is identifiable.
        # Gated by CCSWITCH_STATUS_TRACE (off by default — fires on every emit).
        # Remove once the false-"Ready" question is settled.
        if _STATUS_TRACE:
            log.info(
                "status.derive session=%s status=%s turn_active=%s turns_completed=%s "
                "last_outcome=%s pending_perm=%s pending_q=%s pending_plan=%s "
                "awaiting_user=%s idle_age=%.1f queue=%d",
                self.session_id, status, sig.turn_active, sig.turns_completed,
                sig.last_turn_outcome, sig.pending_permission, sig.pending_question,
                sig.pending_plan, self.awaiting_user_input,
                self._clock() - self._last_progress_at, n,
            )
        self._emit(ev.Status(state=status, count=n))
        if self._on_status is not None:
            try:
                await self._on_status(self.session_id, status)
            except Exception:
                log.warning("driver.on_status failed session=%s", self.session_id, exc_info=True)

    @staticmethod
    def _normalize_option(opt) -> dict:
        """Normalize a question option to a dict with at least a 'label' key."""
        if isinstance(opt, dict):
            return opt
        return {"label": str(opt)}

    @staticmethod
    def _format_answer_message(q_text: str, selected: list[str], mode: str) -> str:
        """Single-question deny-message body, framed by answer mode."""
        answer_str = ", ".join(selected)
        if mode == "chat":
            return f"[User wants to discuss rather than pick an option] {answer_str}"
        if mode == "freeform":
            return f"[User answered in their own words] {q_text} → {answer_str}"
        return f"[User answered via the question UI] {q_text} → {answer_str}"

    @staticmethod
    def _format_answer_line(q_text: str, selected: list[str], mode: str) -> str:
        """Per-question line for the multi-question deny message, framed by mode."""
        answer_str = ", ".join(selected)
        if mode == "chat":
            return f"- {q_text} → (discussing) {answer_str}"
        if mode == "freeform":
            return f"- {q_text} → (in their own words) {answer_str}"
        return f"- {q_text} → {answer_str}"

    async def _handle_ask_user_question(self, tool_input: dict, turn_id: str) -> PermissionResultDeny:
        """Block the SDK callback, emit ev.Question events, await user answers.

        The SDK offers no channel to return an answer via PermissionResultAllow; the
        only way to feed the answer back to the model is through PermissionResultDeny
        whose `message` the model receives as the tool result text.
        """
        raw_questions = tool_input.get("questions") or []
        if not raw_questions:
            # Malformed call — deny immediately with a note.
            return PermissionResultDeny(message="[User answered via the question UI] (no questions provided)")

        # For each question in the tool call, register a pending entry and emit the event.
        pending: list[tuple[dict, asyncio.Future]] = []
        for q in raw_questions:
            header = q.get("header") or ""
            question_text = q.get("question") or ""
            raw_opts = q.get("options") or []
            options = [self._normalize_option(o) for o in raw_opts]
            multi_select = bool(q.get("multiSelect", False))

            qid, fut = self._qreg.request(
                self.session_id, turn_id, header, question_text, options, multi_select,
                self.identity,
            )
            self._emit(ev.Question(
                question_id=qid,
                turn_id=turn_id,
                header=header,
                question=question_text,
                options=options,
                multi_select=multi_select,
            ))
            # Emit QuestionResolved the moment THIS future settles (answered or
            # dismissed) — per-question, not after the whole gather, so a reattach
            # between the answers of a multi-question call only resurrects the
            # genuinely-unanswered cards. The resolution must be an event on the
            # same stream as the Question frame: the daemon ring replays that frame
            # on a mid-turn reattach, and without a QuestionResolved after it an
            # already-answered question resurrects as pending (and other live tabs
            # never learn it was answered).
            fut.add_done_callback(
                functools.partial(self._emit_question_resolved, qid, turn_id))
            pending.append((q, fut))

        # A question is now pending for the user → waiting_input.
        await self._emit_status()

        # Await all answers concurrently (gather preserves order).
        # Each future resolves to (selected, mode) or None (dismissed).
        futs = [fut for _, fut in pending]
        answers: list[tuple[list[str], str] | None] = await asyncio.gather(*futs)

        # Question(s) resolved → back to in_progress.
        await self._emit_status()

        # Build the deny message.
        questions_list = [q for q, _ in pending]
        if len(questions_list) == 1:
            q_text = questions_list[0].get("question") or ""
            answer = answers[0]
            if answer is None:
                msg = f"[User dismissed the question UI] {q_text} → (dismissed — user did not answer)"
            else:
                selected, mode = answer
                msg = self._format_answer_message(q_text, selected, mode)
        else:
            all_dismissed = all(a is None for a in answers)
            prefix = "[User dismissed the question UI]" if all_dismissed else "[User answered via the question UI]"
            lines = [prefix]
            for q_obj, answer in zip(questions_list, answers):
                q_text = q_obj.get("question") or ""
                if answer is None:
                    lines.append(f"- {q_text} → (dismissed — user did not answer)")
                else:
                    selected, mode = answer
                    lines.append(self._format_answer_line(q_text, selected, mode))
            msg = "\n".join(lines)

        return PermissionResultDeny(message=msg)

    def _emit_question_resolved(self, question_id: str, turn_id: str,
                                fut: asyncio.Future) -> None:
        """Done-callback for a question future: broadcast the resolution.

        The future resolves to (selected, mode) or None (dismissed via deny_all).
        A cancelled future (driver torn down mid-question) emits nothing — there
        is no resolution to report. Best-effort: a broadcast failure must never
        propagate into asyncio's callback machinery."""
        if fut.cancelled():
            return
        try:
            res = fut.result()
            selected, mode = res if res is not None else (None, "dismissed")
            self._emit(ev.QuestionResolved(
                question_id=question_id, turn_id=turn_id,
                selected=selected, mode=mode))
        except Exception:
            log.warning("driver: QuestionResolved emit failed session=%s qid=%s",
                        self.session_id, question_id, exc_info=True)

    @property
    def identity(self) -> str:
        """The identity AskUserQuestion / permission asks are registered under and
        resolved against (see QuestionRegistry.resolve / PermissionRegistry.resolve)."""
        return self._identity

    @identity.setter
    def identity(self, value: str) -> None:
        """Re-thread the driver AND its already-pending asks to *value*.

        The registries snapshot ``self.identity`` at request time, so upgrading only
        the attribute (what orchestrator._rethread_identity does when a real user
        attaches to a driver an internal path built under "system") stranded every
        question or permission raised before that attach: still listed as pending,
        yet every answer rejected as an identity mismatch. Cascading here keeps the
        attribute and the registries a single source of truth — whoever the driver
        now belongs to owns everything it is waiting on.
        """
        old = self._identity
        self._identity = value
        if old == value:
            return
        self._qreg.rethread_identity(self.session_id, old, value)
        self._reg.rethread_identity(self.session_id, old, value)

    def answer_question(self, question_id: str, selected: list[str], identity: str,
                        mode: str = "option") -> bool:
        """Resolve a pending question with the user's selection and mode. Mirrors resolve_permission.

        Passes session_id so the registry can reconcile a replay-derived question id
        (from a transcript-rebuilt card on reattach) onto the live pending waiter.
        """
        return self._qreg.resolve(question_id, selected, identity, mode,
                                  session_id=self.session_id)

    def pending_questions(self) -> list[dict]:
        """Return still-pending question payloads for this session (parallel to pending permissions).

        Each entry has: question_id, turn_id, header, question, options, multi_select.
        Used by the WS bridge to re-emit question events on client reattach.
        """
        return self._qreg.pending(self.session_id)

    def _reset_turn_flags(self) -> None:
        """Clear every turn-scoped flag so none outlives the turn that set it.

        Called from BOTH turn-teardown paths — ``_drain``'s ``finally`` (the normal
        end) and ``interrupt()`` (a Stop / stall-watchdog kill). Keeping the reset in
        one place is what stops the two sites drifting: ``interrupt()`` used to skip
        ``_condensing``, and because ``derive_status`` ranks ``condensing`` ABOVE
        ``turn_active``, an interrupt during compaction stranded the dashboard status on
        'condensing' forever (the card never left "Condensing…" until a fresh prompt
        rebuilt the turn). ``_retrying`` is masked by the ``turn_active`` check so its
        omission was harmless, but it belongs to the same turn-scoped class — reset it
        here too rather than rely on the masking.
        """
        self._retrying = False
        self._condensing = False
        self._condense_started_at = None

    def _dash_signals(self) -> StatusSignals:
        """Snapshot the live driver state into the inputs of derive_status."""
        perms = self._reg.pending(self.session_id)
        return StatusSignals(
            turn_active=self._turn_active,
            pending_permission=bool(perms),
            pending_question=bool(self._qreg.pending(self.session_id)),
            pending_plan=any(p.get("tool") == "ExitPlanMode" for p in perms),
            turns_completed=self._turns_completed,
            last_turn_outcome=self._last_turn_outcome,
            retrying=self._retrying,
            condensing=self._condensing,
        )

    async def _can_use_tool(self, tool_name, tool_input, ctx=None):
        turn_id = self._turn()
        if tool_name == "AskUserQuestion":
            return await self._handle_ask_user_question(tool_input, turn_id)
        perm_id, fut = await self._reg.request(
            self.session_id, turn_id, tool_name, tool_input,
            mode=self.mode, denylist=self.denylist, allowlist=True, identity=self.identity)
        if perm_id is not None:
            self._emit(ev.PermissionRequest(
                perm_id, turn_id, tool_name, ev.tool_kind(tool_name),
                tool_input, plan=(tool_name == "ExitPlanMode")))
            # A prompt is now pending for the user → waiting_input / ready_review.
            await self._emit_status()
        allowed = await fut
        # Broadcast the resolution so it rides the same stream as the request
        # frame — the daemon ring replays that frame on a mid-turn reattach, and
        # without a PermissionResolved after it an already-decided permission
        # resurrects in every reattached client's pinned panel (and other live
        # tabs never learn it was decided). Mirrors QuestionResolved. Gated on
        # perm_id: an auto-approved tool never emitted a request, so there is no
        # card to clear.
        if perm_id is not None:
            self._emit(ev.PermissionResolved(perm_id, turn_id, allowed))
        # Decision made; the perm is no longer pending → back to in_progress.
        await self._emit_status()
        return PermissionResultAllow() if allowed else PermissionResultDeny(message="denied by user")

    async def start(self):
        # A turn interrupted mid-tool-call (manual Stop, stall-watchdog, or a deploy
        # drain that cancels the drain before the CLI writes its synthetic
        # "[Request interrupted by user for tool use]" result) leaves an ORPHANED
        # tool_use as the transcript tail. On resume the CLI rejects that state with
        # error_during_execution / [ede_diagnostic] and every turn dead-ends. Heal it
        # before handing the session back to the SDK. Best-effort — never block start.
        if self.resume:
            try:
                await asyncio.to_thread(
                    repair_resumed_transcript, self.config_dir, self.resume)
            except Exception:
                log.warning("driver.start: transcript repair failed session=%s",
                            self.session_id, exc_info=True)
        self._client = self._factory(self.cwd, self.config_dir, self._can_use_tool, self.mode,
                                     self.resume, on_pre_compact=self._on_pre_compact)
        await self._client.__aenter__()
        self._source = _MessageSource(self._client)
        self._closing = False
        self._start_listener()

    async def send_prompt(self, text: str, images: list[dict] | None = None, *,
                          interrupt: bool = False, context: dict | None = None):
        """Send a prompt. While a turn is live it joins the FIFO queue — unless
        ``interrupt`` is set, in which case the live turn is stopped and this prompt
        runs NEXT ("interrupt and post"), ahead of anything already queued.

        ``context`` is the "context chip" (spec §4.5) — the page the user was on —
        forwarded by the orchestrator only for an assistant-kind session. It never
        touches the ``UserMessage`` text; ``_start_turn`` prepends a rendered header
        to the copy of the text sent to the SDK only, and only when it differs from
        ``self._last_context``."""
        if self._client is None:
            raise RuntimeError("SessionDriver.send_prompt called before start()")
        images = images or []
        if interrupt and self._turn_live():
            # Queue-head insert BEFORE the interrupt: the SDK interrupt awaits the
            # CLI's control response, and the interrupted turn's ResultMessage can
            # reach the pump during that await — its _end_turn pops the head, so
            # the head must already be this prompt or a surviving queued one runs
            # first. Never _start_turn directly here: a pump started before that
            # result is consumed reads it as its own and closes the fresh turn.
            # The interrupt also denies any parked permission/question, so the text
            # is a fresh prompt — the answer short-circuit below must not see it.
            self._queue.insert(0, (text, images, context))
            log.info("driver.send_prompt: interrupt-and-post session=%s queue_depth=%d",
                     self.session_id, len(self._queue))
            await self.interrupt(keep_queue=True)
            return
        # A pending AskUserQuestion means the turn is parked inside _can_use_tool
        # waiting for the user — a message typed now IS the answer, and the driver's
        # own state (not the client's choice of frame type) decides that. It must be
        # delivered as one: the C2 queue below only drains when the turn ENDS, which
        # needs this very answer, so enqueueing here deadlocked the session (reply
        # stuck in the queue strip, model blocked, forever — the frozen "Answer
        # needed" failure mode). Every still-pending question of the call gets the
        # text (a multi-question call is answered as a group) in chat framing, so the
        # model reads it as the user's own words rather than a picked option. The
        # question channel is text-only: images follow as an attachment-only prompt
        # once the turn ends, never dropped.
        if text.strip():
            answered = self._qreg.resolve_all(self.session_id, [text], "chat")
            if answered:
                log.info("driver.send_prompt: delivered prompt as the answer to %d pending "
                         "question(s) session=%s", answered, self.session_id)
                if images:
                    self._queue.append(("", images, None))
                    self._emit(ev.QueueUpdate(self.queue_wire()))
                return
        # C2 — FIFO queue: if a turn is live — a prompt's pump, or a CLI-initiated turn
        # the idle listener is tracking (`_turn_active`) — enqueue (text, images,
        # context) instead of rejecting.
        if self._turn_live():
            self._queue.append((text, images, context))
            log.debug("driver.send_prompt: queued prompt for session=%s, queue_depth=%d",
                      self.session_id, len(self._queue))
            await self._emit_status()
            self._emit(ev.QueueUpdate(self.queue_wire()))
            return
        # C3 — auto-title from the first prompt (first line, max 50 chars).
        if not self._titled:
            self._titled = True
            first_line = text.splitlines()[0] if text else (text or "attachment")
            self._emit(ev.Title(title=first_line[:50], source="prompt"))
        await self._start_turn(text, images, context)

    async def _start_turn(self, text: str, images: list[dict] | None = None,
                          context: dict | None = None):
        """Echo the user prompt, emit working status, query the SDK, drain."""
        images = images or []
        # The prompt's pump takes over the SDK cursor: park the idle listener first
        # (a no-op when this runs INSIDE the listener, dequeuing at a turn's end).
        await self._stop_listener()
        self._cancel_suggest()
        self._last_user_text = text
        self._last_assistant_parts = []
        self._turn_buffer = []
        self._turn_produced_output = False
        self._turn_active = True
        # Wall-clock start of this turn. The CLI's transcript write LAGS the live
        # stream (MCP fleet boot on resume), so a live attach can see a disk JSONL
        # whose last user entry is the PREVIOUS turn's. replay_window compares the
        # entry's timestamp against this to drop only the turn that actually is in
        # progress — same-host clocks, and the CLI stamps its user entry only AFTER
        # this point (it processes the prompt we are about to send).
        self._turn_started_at = time.time()
        self._last_turn_outcome = "success"
        await self._emit_status()
        # Build the live UserMessage image refs (url/mime/name only).
        wire_images = [{"url": im["url"], "mime": im["mime"], "name": im.get("name", "")}
                       for im in images]
        user_turn = uuid.uuid4().hex
        self._emit(ev.UserMessage(
            turn_id=user_turn, text=text, src_uuid=user_turn,
            images=wire_images or None, context=context))
        assert self._client is not None  # _start_turn only runs once the client is connected
        # The context chip (spec §4.5): only ever set by an assistant-kind session's
        # prompt. Prepend a rendered `<ccswitch-context>` header to the copy of the
        # text handed to the SDK — never to the UserMessage above, which is what the
        # transcript bubble renders — and only when the page actually changed since
        # the last prompt, so every follow-up in the same view doesn't repeat it.
        sdk_text = text
        if context is not None and context != self._last_context:
            # Import here, not at module scope: orchestrator.py imports SessionDriver
            # from this module, so a top-level import back would be circular. By the
            # time a prompt is actually sent, orchestrator has long finished importing.
            from server.sessions.orchestrator import local_host_stamp
            sdk_text = render_context_header(context, host=local_host_stamp()) + "\n" + text
            self._last_context = context
        if images:
            content: list[dict] = []
            if sdk_text:
                content.append({"type": "text", "text": sdk_text})
            for im in images:
                with open(im["path"], "rb") as fh:
                    raw = fh.read()
                mime = im["mime"]
                if mime in _IMAGE_MIMES:
                    data = base64.b64encode(raw).decode("ascii")
                    content.append({"type": "image", "source": {
                        "type": "base64", "media_type": mime, "data": data}})
                elif mime == "application/pdf":
                    data = base64.b64encode(raw).decode("ascii")
                    content.append({"type": "document", "source": {
                        "type": "base64", "media_type": "application/pdf", "data": data}})
                elif mime == "application/zip":
                    content.append({"type": "text", "text":
                        f"[Attached: {im.get('name', 'file.zip')} (zip archive, {len(raw)} bytes)]"})
                else:  # text/plain, text/csv, application/json
                    content.append({"type": "text", "text": raw.decode("utf-8", errors="replace")})
            message = {"type": "user",
                       "message": {"role": "user", "content": content},
                       "parent_tool_use_id": None, "session_id": "default"}
            await self._client.query(_aiter_one(message))
        else:
            await self._client.query(sdk_text)
        self._pump = asyncio.create_task(self._drain())

    async def _call_sync_back(self) -> None:
        """Run on_sync_back off the event loop; swallow any exception so the
        session/stream is never broken by a sync_back failure."""
        if self._on_sync_back is None:
            return
        try:
            await asyncio.to_thread(self._on_sync_back)
        except Exception:
            log.warning("driver.sync_back failed (non-fatal) session=%s", self.session_id,
                        exc_info=True)

    async def _maybe_signal_recovery(self, code: str, message: str) -> "TurnFailure":
        """Classify a turn-failure surface; if recoverable, emit TurnFailure and notify
        the orchestrator. Returns the classification so the caller can adapt the UI
        surface (e.g. soften a dropped turn). Retry-After comes from the error text,
        falling back to a RateLimitEvent reset observed this turn (resets_at - now)."""
        failure = classify(code, message)
        if not failure.is_recoverable:
            return failure
        retry_after = failure.retry_after
        if retry_after is None and self._last_rate_reset is not None:
            retry_after = max(0.0, float(self._last_rate_reset) - self._clock())
        self._emit(ev.TurnFailure(
            kind=failure.kind, account=self.account, retry_after=retry_after))
        if self._on_recoverable_failure is not None:
            try:
                await self._on_recoverable_failure(self.session_id, failure.kind, retry_after)
            except Exception:
                log.warning("driver.on_recoverable_failure failed session=%s",
                            self.session_id, exc_info=True)
        return failure

    async def emit_error(self, code: str, message: str) -> None:
        """Surface a red error card anchored to the current turn. Used by the
        orchestrator to explain an exhausted auto-recovery (the give-up surface).

        Anchors to _last_turn_id (set on TurnEnd, not cleared by _reset_turn_flags)
        so the give-up card lands in the same transcript row as the Recovering cards,
        even though the translator's _turn_id has already been reset by drain time."""
        self._emit(ev.ErrorEvent(code, message, turn_id=self._last_turn_id or self._turn()))

    async def _drain(self):
        """A prompt turn's pump: consume the SDK cursor up to and including this turn's
        ResultMessage (the CLI closes every turn with one), then settle the turn."""
        assert self._source is not None  # only started after start() built the cursor
        try:
            while True:
                msg = await self._source.next()
                if msg is _EOF:
                    break
                await self._handle_msg(msg)
                if isinstance(msg, ResultMessage):
                    break
        except Exception as exc:
            log.exception("driver.drain session=%s", self.session_id)
            await self._surface_drain_error(exc)
        finally:
            await self._end_turn()

    async def _handle_msg(self, msg) -> None:
        """Route ONE SDK message through the translator and the per-event side effects.
        Shared by the prompt pump and the idle listener — one turn semantics."""
        for e in self._tr.feed(msg):
            if isinstance(e, ev.RateLimit):
                # Internal: stash the reset for retry timing; don't forward.
                if e.resets_at is not None:
                    self._last_rate_reset = e.resets_at
                continue
            if isinstance(e, ev.ApiRetry):
                # Internal: count as progress so the stall watchdog leaves a
                # 529-backoff turn alone; flip the retrying flag on transition.
                self._note_progress(e)
                if not self._retrying:
                    self._retrying = True
                    await self._emit_status()
                continue   # internal — do not forward to the client
            # Any real (non-retry) event resumes the turn; clear the flag.
            if self._retrying:
                self._retrying = False
                await self._emit_status()
            if isinstance(e, ev.Condensing):
                await self._consume_condensing(e)
                continue
            # Genuine assistant output proves compaction is over. The clean
            # signal is the SDK's compact_boundary (a Condensing(active=False),
            # handled above), but it does not always arrive before the
            # post-compaction reply streams. Since derive_status ranks
            # 'condensing' ABOVE 'turn_active', a lingering flag pins the status
            # to "Condensing…" while real text streams in. Clear it on the first
            # content event — exactly as _retrying is cleared by any real event.
            if self._condensing and isinstance(e, (ev.TextDelta, ev.ThinkingDelta, ev.ToolCall, ev.ToolResult)):
                await self._consume_condensing(ev.Condensing(active=False))
            if isinstance(e, ev.TextDelta):
                self._last_assistant_parts.append(e.text)
            elif isinstance(e, ev.TurnEnd):
                self._last_turn_outcome = e.outcome
                self._last_turn_id = e.turn_id
                if e.pr_resolution is not None:
                    # Intentionally sticky: set-only; first-write-wins is enforced
                    # downstream by the rec.pr_resolution is None guard in _persist_status.
                    self._last_pr_resolution = e.pr_resolution
                self._latch_model_request(e)
            elif isinstance(e, ev.ErrorEvent):
                failure = await self._maybe_signal_recovery(
                    e.code, e.raw_message or e.message)
                if failure.kind == "turn_dropped":
                    # Self-healing auto-resend is in flight (signalled above).
                    # Show a soft "recovering" notice instead of the alarming
                    # red error; the orchestrator pauses with a red surface only
                    # if the resend cap is hit.
                    self._note_progress(e)
                    self._emit(ev.Recovering(
                        turn_id=e.turn_id, message=_RECOVERING_MSG))
                    continue
            if isinstance(e, (ev.TextDelta, ev.ToolCall, ev.ToolResult)):
                self._turn_produced_output = True
            self._note_progress(e)
            self._emit(e)
        if self.claude_session_id is None and self._tr.session_id:
            self.claude_session_id = self._tr.session_id
            if self._on_session_id is not None:
                try:
                    await self._on_session_id(self.session_id, self.claude_session_id)
                except Exception:
                    log.exception("driver.on_session_id session=%s", self.session_id)

    async def _surface_drain_error(self, exc: BaseException) -> None:
        """Surface a stream failure to the UI as the real cause, not a generic
        placeholder (e.g. a dead token or a closed subprocess is exactly what the user
        needs to act on; redaction scrubs any secret-shaped substrings downstream), mark
        the turn failed and signal recovery."""
        detail = str(exc).strip() or "no detail"
        full = f"{type(exc).__name__}: {detail}"
        self._emit(ev.ErrorEvent(
            "driver_error", full[:500], turn_id=self._turn()))
        self._last_turn_outcome = "failed"
        await self._maybe_signal_recovery("driver_error", full)

    async def _end_turn(self) -> None:
        """Settle a turn that just closed (prompt or CLI-initiated alike): sync back,
        clear turn flags, then either dequeue the next prompt or go idle — status
        refresh, suggestions, and the listener back on the cursor."""
        # Capture any token rotation the SDK performed during this turn.
        await self._call_sync_back()
        # Clear every turn-scoped flag so none outlives the turn that set it.
        self._reset_turn_flags()
        # If queued prompts remain, pop the front and start the next turn.
        if self._queue:
            next_text, next_images, next_context = self._queue.pop(0)
            log.debug("driver.drain: dequeuing next prompt for session=%s, remaining=%d",
                      self.session_id, len(self._queue))
            self._emit(ev.QueueUpdate(self.queue_wire()))
            await self._start_turn(next_text, next_images, next_context)
        else:
            self._turn_active = False
            self._turn_started_at = None
            self._turn_buffer = []
            self._turns_completed += 1
            # Turn settled — refresh the exact context-window readout.
            await self._emit_context_usage()
            await self._emit_status()
            self._maybe_start_suggestions()
            self._start_listener()

    # ---- idle listener: the SDK cursor is never left unread -----------------------
    def _start_listener(self) -> None:
        if self._closing or self._source is None:
            return
        if self._listener is not None and not self._listener.done():
            return
        pump = self._pump
        if pump is not None and not pump.done() and pump is not asyncio.current_task():
            return   # a prompt's pump owns the cursor (a pump settling ITS turn is done with it)
        self._listener = asyncio.create_task(
            self._idle_listen(), name=f"driver-idle-listen:{self.session_id}")

    async def _stop_listener(self) -> None:
        """Park the idle listener so a prompt's pump can take the cursor. Cancel-safe:
        a message in flight at the cancel stays in the shared cursor (_MessageSource).
        From INSIDE the listener (a turn's end dequeuing a prompt) just unregister it —
        its loop exits once the current step returns."""
        task, self._listener = self._listener, None
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _open_autonomous_turn(self) -> None:
        """The CLI started a turn on its own — a background task's <task-notification>
        it injected as a user message, the model continuing. Track it like a prompt turn
        (status, stall watchdog, attach snapshot) so the dashboard shows the work and
        the turn's own ResultMessage — not the next prompt's — closes it."""
        self._cancel_suggest()
        self._last_user_text = ""      # nothing of ours to resend if it drops
        self._last_assistant_parts = []
        self._turn_buffer = []
        self._turn_produced_output = False
        self._turn_active = True
        self._turn_started_at = time.time()
        self._last_turn_outcome = "success"
        log.info("driver.autonomous_turn: CLI-initiated turn session=%s", self.session_id)
        await self._emit_status()

    async def _idle_listen(self) -> None:
        assert self._source is not None
        me = asyncio.current_task()
        try:
            while self._listener is me:
                msg = await self._source.next()
                if msg is _EOF:
                    log.warning("driver.idle_listen: SDK stream ended while idle session=%s",
                                self.session_id)
                    return
                # Only a turn's own frames open a turn. Bookkeeping frames (SessionStart
                # hook lifecycle right after connect, task notices) are fed through
                # without one — a turn opened on those never gets its ResultMessage and
                # every prompt queues behind it (see sdk_contract.TURN_MESSAGES).
                if not self._turn_active and is_turn_message(msg):
                    await self._open_autonomous_turn()
                await self._handle_msg(msg)
                if isinstance(msg, ResultMessage):
                    await self._end_turn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("driver.idle_listen session=%s", self.session_id)
            if self._turn_active:
                await self._surface_drain_error(exc)
                await self._end_turn()
        finally:
            if self._listener is me:
                self._listener = None

    def queued(self) -> list[str]:
        """Return a snapshot of the queued prompt texts."""
        return [t for t, _, _ in self._queue]

    def queue_wire(self) -> list[dict]:
        """Structured queue snapshot: text + image refs per queued prompt."""
        return [
            {"text": t,
             "images": [{"url": im["url"], "mime": im["mime"], "name": im.get("name", "")}
                        for im in imgs]}
            for t, imgs, _ in self._queue
        ]

    async def cancel_queued(self, index: int) -> bool:
        """Cancel a queued prompt by index. Returns True if removed, False if index invalid."""
        if index < 0 or index >= len(self._queue):
            return False
        self._queue.pop(index)
        self._emit(ev.QueueUpdate(self.queue_wire()))
        await self._emit_status()
        return True

    def _buffer_event(self, e) -> None:
        """Append e to the in-progress-turn buffer, coalescing consecutive same-block
        text/thinking deltas into one accumulated event (bounded by content, not count)."""
        if isinstance(e, (ev.TextDelta, ev.ThinkingDelta)) and self._turn_buffer:
            last = self._turn_buffer[-1]
            if (type(last) is type(e) and last.turn_id == e.turn_id
                    and last.src_uuid == e.src_uuid
                    and last.parent_tool_use_id == e.parent_tool_use_id):
                self._turn_buffer[-1] = dataclasses.replace(last, text=last.text + e.text)
                return
        self._turn_buffer.append(e)

    def _emit(self, e) -> None:
        """Fan an event out to every subscriber and (if transcript content) the buffer.
        Replaces the old single-queue put; put_nowait on unbounded subscriber queues
        never blocks, so this is synchronous."""
        if isinstance(e, _TURN_CONTENT):
            self._buffer_event(e)
        for q in self._subscribers:
            q.put_nowait(e)

    def emit(self, e) -> None:
        """Public out-of-band emit for the orchestrator (e.g. a user rename → Title).
        Delegates to the broadcast path so external callers never touch internals."""
        self._emit(e)

    @property
    def turn_active(self) -> bool:
        return self._turn_active

    @property
    def turn_started_at(self) -> float | None:
        """Epoch seconds of the in-progress turn's start, or None when idle."""
        return self._turn_started_at

    def _latch_model_request(self, e) -> None:
        """Latch a self-directed model request from a completed turn.

        Stamped with the turn id so the orchestrator can consume each request exactly
        once. Set-only: a turn carrying no request leaves the previous value alone,
        matching _last_pr_resolution's stickiness.
        """
        if e.model_request is not None:
            self._last_model_request = {"turn_id": e.turn_id, **e.model_request}

    @property
    def last_model_request(self) -> dict | None:
        """The most recent ``<!-- ccswitch:model=… -->`` request, or None.
        ``{"turn_id", "model", "reason"}``."""
        return self._last_model_request

    @property
    def last_pr_resolution(self) -> str | None:
        """Resolution token from the most recent ``<!-- ccswitch:no-pr -->`` marker,
        or ``None`` if no completed turn has carried one yet."""
        return self._last_pr_resolution

    def open_stream(self) -> DriverStream:
        """Open an attach stream ATOMICALLY — no await anywhere in this method,
        so no event (and no turn settle) can slip between snapshotting the
        in-progress turn, registering the live queue, and capturing the turn
        flags (single asyncio thread). Snapshot = the past; the queue = strictly
        the future; turn_active/turn_started_at = the same instant as both."""
        snapshot = list(self._turn_buffer)
        # Re-emit the latest suggestions to every new subscriber. Suggestions are
        # current-state (like Status), not buffered turn content, and are produced by a
        # fire-and-forget task that can fire before OR after a subscriber attaches — so a
        # late/reconnecting viewer (or an idle-attach seed that raced the subscribe) would
        # otherwise never see them. Delivering from the cached snapshot is idempotent: the
        # producing _emit either already ran (cache set, no live copy for THIS subscriber)
        # or runs later (live copy) — never both for one subscriber.
        if self._last_suggestions:
            snapshot.append(ev.Suggestions(
                self.session_id, self._last_turn_id, list(self._last_suggestions)))
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return DriverStream(self, snapshot, q, self._turn_active, self._turn_started_at)

    async def events(self):
        """Single-shot subscribe: thin delegate over open_stream() — ONE
        implementation of the snapshot+queue semantics (used by the daemon-side
        RPC pump; the WS bridge calls open_stream() directly so it can pin the
        turn flags to the same instant as the snapshot).

        The explicit close() matters: ``async for`` does NOT aclose its inner
        generator when THIS generator is closed, so without it the subscriber
        queue would linger in _subscribers until GC finalizes the inner
        generator — a deferred leak. close() is idempotent alongside the inner
        finally's own discard."""
        stream = self.open_stream()
        try:
            async for e in stream.events():
                yield e
        finally:
            stream.close()

    def _cancel_suggest(self) -> None:
        if self._suggest_task is not None and not self._suggest_task.done():
            self._suggest_task.cancel()

    def _maybe_start_suggestions(self) -> None:
        """Fire-and-forget suggestion generation once the session is idle. Best-effort.

        Gated so no tokens are spent generating suggestions nobody will read:
        - no attached viewer → skip (headless/automation sessions never render them);
        - Copilot sessions → skip (third-party model; Haiku suggestions are meaningless,
          mirroring the detached-path guard in orchestrator.latest_suggestions).
        The on-demand sparkle (generate_suggestions_now) bypasses both — an explicit
        human request always generates.
        """
        if not self._subscribers:
            return
        if self._suggester is None:
            return
        if _provider_of(self.account) == _copilot_mod.PROVIDER:
            return
        sig = self._dash_signals()
        if sig.pending_permission or sig.pending_question:
            return
        user = self._last_user_text[:500]
        assistant = "".join(self._last_assistant_parts)[-1500:]
        context = f"User: {user}\n\nAssistant: {assistant}"
        self._cancel_suggest()
        self._suggest_task = asyncio.create_task(self._run_suggestions(context, self._last_turn_id))

    async def _run_suggestions(self, context: str, turn_id: str) -> None:
        # Skip the LLM round-trip when the context is byte-identical to the last
        # generation (e.g. a no-op turn, or a re-attach seeding the same tail): the
        # suggestions would be identical, and _last_suggestions is already replayed to
        # new subscribers by open_stream(). Mirrors the mtime-keyed cache the detached
        # path uses in orchestrator.latest_suggestions.
        sig = hashlib.sha1(context.encode("utf-8", "surrogatepass")).hexdigest()
        if sig == self._last_suggest_sig:
            return
        items = await self._suggester.generate(self.cwd, self.config_dir, context, self.account)
        # Record the signature only once generation actually ran to completion (a
        # cancelled task never reaches here), so a genuinely new context still refreshes.
        self._last_suggest_sig = sig
        log.info("suggestions session=%s turn=%s n=%d", self.session_id, turn_id, len(items))
        if items:
            self._last_suggestions = items
            self._emit(ev.Suggestions(self.session_id, turn_id, items))

    async def generate_suggestions_now(self) -> list[str]:
        """Generate next-prompt suggestions on demand (synchronously awaited).

        Unlike _maybe_start_suggestions (fire-and-forget at turn boundary), this is
        the sparkle's on-demand path: it ALWAYS asks the LLM fresh from the current
        in-memory turn context and returns the items to the caller. Best-effort —
        returns [] when no suggester is configured or generation yields nothing.
        Caches the result into _last_suggestions and broadcasts a Suggestions event
        so any attached viewers' dropdowns refresh too.
        """
        if self._suggester is None:
            return []
        user = self._last_user_text[:500]
        assistant = "".join(self._last_assistant_parts)[-1500:]
        context = f"User: {user}\n\nAssistant: {assistant}"
        items = await self._suggester.generate(self.cwd, self.config_dir, context, self.account)
        if items:
            self._last_suggestions = items
            self._emit(
                ev.Suggestions(self.session_id, self._last_turn_id or "on-demand", items)
            )
        return list(items or [])

    def seed_idle_suggestions(self, user_text: str, assistant_text: str) -> None:
        """Generate suggestions for a session resumed into an idle state.

        Unlike _maybe_start_suggestions (fired at turn completion from in-memory
        context), this seeds context from the replayed transcript. Runs at most once
        per driver and only while no live turn has produced context, so repeated WS
        attaches don't regenerate; a later real turn still refreshes via drain().
        Best-effort and gated (no permission/question pending) inside
        _maybe_start_suggestions.
        """
        if self._suggester is None or self._idle_seeded:
            return
        if self._turn_active or self._last_assistant_parts:
            return
        if not (user_text.strip() or assistant_text.strip()):
            return
        self._idle_seeded = True
        self._last_user_text = user_text
        self._last_assistant_parts = [assistant_text]
        if not self._last_turn_id:
            self._last_turn_id = "resumed"
        self._maybe_start_suggestions()

    async def interrupt(self, *, keep_queue: bool = False):
        """Stop the live turn. A Stop abandons every queued prompt; ``keep_queue``
        (interrupt-and-post) leaves the queue for the settling turn to drain."""
        self._reg.deny_all(self.session_id)
        self._qreg.deny_all(self.session_id)
        if not keep_queue:
            self._queue.clear()
        self._cancel_suggest()
        if self._client:
            try:
                await self._client.interrupt()
            except CLIConnectionError:
                # Client disconnected (e.g. after daemon restart mid-turn). The CLI is
                # already dead, so interrupt is unnecessary — proceed to recovery.
                pass
        self._turn_active = False
        self._turn_started_at = None
        # Clear turn-scoped flags BEFORE deriving the post-interrupt status. Without
        # this, _condensing stays True and (outranking turn_active in derive_status)
        # re-persists 'condensing' — the interrupt would visibly do nothing to the pill.
        was_condensing = self._condensing
        self._reset_turn_flags()
        if was_condensing:
            # Forward an inactive boundary so an open session view clears its progress
            # overlay (mirrors _consume_condensing); the normal boundary never arrives.
            self._emit(ev.Condensing(active=False))
        self._emit(ev.QueueUpdate(self.queue_wire()))
        await self._emit_status()

    async def set_permission_mode(self, mode: str):
        # Plan mode is a temporary overlay. The web plan toggle turns it OFF by
        # sending the 'default' sentinel; treat that as "return to the mode the
        # session had before plan" rather than the SDK 'default' engine, which
        # PermissionRegistry.request reads as "prompt on every dangerous tool" —
        # silently dropping a full-auto session into constant approvals.
        if mode == "plan" and self.mode != "plan":
            self._pre_plan_mode = self.mode
        elif mode == "default" and self.mode == "plan":
            mode = self._pre_plan_mode or "default"
            self._pre_plan_mode = None
        self.mode = mode
        if self._client:
            await self._client.set_permission_mode(to_sdk_permission_mode(mode))

    async def set_model(self, model: str | None) -> None:
        if self._client:
            await self._client.set_model(model)

    async def server_info(self) -> dict | None:
        if self._client is None:
            return None
        return await self._client.get_server_info()

    async def context_usage(self) -> dict:
        if self._client is None:
            return {}
        return await self._client.get_context_usage()

    async def _emit_context_usage(self) -> None:
        """Forward exact context-window occupancy (same source as `/context`) so the
        persistent ctx% readout reflects REAL remaining context — not the per-turn
        ``turnEnd.usage`` aggregate, which sums cache reads across a turn's API calls
        and pins the readout at 0%. Best-effort: a dead client or missing data emits
        nothing (the frontend keeps its per-call approximation as a fallback)."""
        try:
            cu = await self.context_usage()
        except Exception:
            log.debug("context_usage pull failed session=%s", self.session_id, exc_info=True)
            return
        if not isinstance(cu, dict):
            return
        used, mx = cu.get("totalTokens"), cu.get("maxTokens")
        if not isinstance(used, int) or not isinstance(mx, int) or mx <= 0:
            return
        pct, model = cu.get("percentage"), cu.get("model")
        self._emit(ev.ContextUsage(
            used_tokens=used, max_tokens=mx,
            percentage=float(pct) if isinstance(pct, (int, float)) else round(used / mx * 100, 1),
            model=model if isinstance(model, str) else None))
        # Persist the exact reading so a fresh attach can replay it (live-only otherwise).
        if self._on_context_usage is not None:
            try:
                await self._on_context_usage(self.session_id, used, mx)
            except Exception:
                log.debug("on_context_usage persist failed session=%s", self.session_id, exc_info=True)

    async def reconnect(self) -> None:
        """Tear down the SDK client and start a FRESH session (resume=None).

        Implements /clear: a conversation's history IS the resumed session, so
        clearing == dropping the resume handle and reconnecting. Resets per-turn
        and titling state so the new session behaves like a brand-new one.
        """
        grace = getattr(self, "_close_grace", _CLOSE_GRACE_SEC)
        self._closing = True
        await self._stop_listener()
        if self._pump is not None and not self._pump.done():
            self._pump.cancel()
            # Same bounded reap as aclose(): a cancel-immune pump must wedge only
            # this /clear, not the driver forever (#846).
            await bounded_wait(self._pump, timeout=grace,
                               phase=f"driver.reconnect-pump:{self.session_id}",
                               cancel_grace=grace)
        self._pump = None
        if self._source is not None:
            await self._source.aclose(grace, phase=f"driver.reconnect-source:{self.session_id}")
            self._source = None
        if self._client is not None:
            await bounded_wait(self._client.__aexit__(None, None, None),
                               timeout=_CLOSE_AEXIT_TIMEOUT_S,
                               phase=f"driver.reconnect-aexit:{self.session_id}")
            await self._call_sync_back()
        self.resume = None
        self.claude_session_id = None
        self._titled = False
        self._queue.clear()
        self._turn_active = False
        self._turn_started_at = None
        self._tr = Translator()
        client = self._factory(self.cwd, self.config_dir, self._can_use_tool, self.mode, None,
                               on_pre_compact=self._on_pre_compact)
        self._client = client
        await client.__aenter__()
        self._source = _MessageSource(client)
        self._closing = False
        self._start_listener()

    def resolve_permission(self, perm_id, decision, identity) -> bool:
        return self._reg.resolve(perm_id, decision, identity)

    async def aclose(self):
        # No more listening: an idle driver's listener is parked on the cursor and
        # must not be counted as a live turn to flush (the grace below is for those).
        self._closing = True
        await self._stop_listener()
        self._cancel_suggest()
        self._reg.deny_all(self.session_id)
        self._qreg.deny_all(self.session_id)
        grace = getattr(self, "_close_grace", _CLOSE_GRACE_SEC)
        if self._pump is not None and not self._pump.done():
            # A turn wedged mid-tool-call must not be abandoned: hard-cancelling the
            # drain kills the subprocess before the CLI writes its synthetic
            # "[Request interrupted by user for tool use]" result, orphaning the
            # tool_use and dead-ending the next resume (see transcript_repair).
            # Interrupt and give the CLI a bounded chance to flush; only cancel if it
            # doesn't finish in time. Drop queued prompts first so the drain's finally
            # can't launch a fresh turn while we're closing.
            self._queue.clear()
            if self._turn_active and self._client is not None:
                # interrupt() awaits a control-channel reply from the CLI — a wedged
                # subprocess left it blocked forever, hanging every close (#846).
                await bounded_wait(self._client.interrupt(), timeout=grace,
                                   phase=f"driver.interrupt:{self.session_id}",
                                   cancel_grace=grace)
            try:
                await asyncio.wait_for(asyncio.shield(self._pump), timeout=grace)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            if not self._pump.done():
                self._pump.cancel()
                # The reap must be bounded too: a pump wedged in an uncancellable
                # region (mid-to_thread, or a stream that swallows the cancel)
                # otherwise blocks close forever (#846).
                await bounded_wait(self._pump, timeout=grace,
                                   phase=f"driver.pump:{self.session_id}",
                                   cancel_grace=grace)
        if self._source is not None:
            await self._source.aclose(grace, phase=f"driver.source:{self.session_id}")
        if self._client:
            await bounded_wait(self._client.__aexit__(None, None, None),
                               timeout=_CLOSE_AEXIT_TIMEOUT_S,
                               phase=f"driver.aexit:{self.session_id}")
        # Final sync_back: capture any token rotation that occurred during the
        # session (e.g. a long-running session where the SDK refreshed but the
        # drain's finally sync_back already ran before the close was requested).
        await self._call_sync_back()
        if self._assistant_api is not None:
            await self._assistant_api.aclose()
        for q in self._subscribers:
            q.put_nowait(_STREAM_DONE)
