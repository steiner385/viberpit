from __future__ import annotations
import asyncio
import base64
import logging
import time
import uuid
from typing import Callable
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from . import events as ev
from .failure import classify
from .translator import Translator
from .permissions import PermissionRegistry
from .questions import QuestionRegistry
from .status import StatusSignals, derive_status

log = logging.getLogger(__name__)

_STREAM_DONE = object()


async def _aiter_one(message: dict):
    yield message


class SessionDriver:
    """Drives one Claude session via an SDK client, exposing normalized events.

    The orchestrator composes the per-account config dir and passes it as `config_dir`;
    `sdk_factory(cwd, config_dir, can_use_tool, mode, resume) -> SDKClientProtocol` builds
    the client (production = real ClaudeSDKClient; tests = FakeSDKClient).
    """

    def __init__(self, session_id, account, cwd, config_dir, *, mode, sdk_factory, registry,
                 identity, denylist=PermissionRegistry.DANGEROUS, resume=None,
                 on_session_id=None, on_sync_back: Callable[[], None] | None = None,
                 on_status=None, on_recoverable_failure=None, clock=None, suggester=None):
        self.session_id = session_id
        self.account = account
        self.cwd = cwd
        self.config_dir = config_dir
        self.mode = mode
        self.identity = identity
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
        # async callback ``on_recoverable_failure(session_id, kind, retry_after)`` fired
        # when a turn fails with a recoverable kind (rate_limit / auth_401 / process_crash). The
        # orchestrator drives the actual recovery (same-account retry/recompose, then
        # rotation). ``clock`` is injected only for deterministic retry-after-from-reset
        # tests; production uses wall-clock time.
        self._on_recoverable_failure = on_recoverable_failure
        self._clock = clock or time.time
        self._last_progress_at: float = self._clock()
        self._last_rate_reset: int | None = None  # resets_at (epoch s) from a RateLimitEvent
        self._factory = sdk_factory
        self._reg = registry
        self._qreg = QuestionRegistry()
        self._tr = Translator()
        self._client = None
        self._out: asyncio.Queue = asyncio.Queue()
        self._pump: asyncio.Task | None = None
        self._titled: bool = False
        self._queue: list[tuple[str, list[dict]]] = []
        self._turn_active: bool = False
        self._turns_completed: int = 0
        self._last_turn_outcome: str = "success"
        self._suggester = suggester
        self._suggest_task: asyncio.Task | None = None
        self._last_user_text: str = ""
        self._last_assistant_parts: list[str] = []
        self._last_turn_id: str = ""
        self._idle_seeded: bool = False

    def _turn(self):
        return getattr(self._tr, "_turn_id", None) or "turn"

    @property
    def last_progress_at(self) -> float:
        """Timestamp of the last meaningful driver event (set at driver construction)."""
        return self._last_progress_at

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

    def _note_progress(self, e) -> None:
        """Advance _last_progress_at only when e carries real content.

        Meaningful: TextDelta/ThinkingDelta with non-empty text, or any
        ToolCall/ToolResult/TurnEnd/Question/PermissionRequest.
        Empty-text deltas are NOT meaningful and must not advance the clock.
        """
        if isinstance(e, (ev.TextDelta, ev.ThinkingDelta)):
            if e.text:
                self._last_progress_at = self._clock()
        elif isinstance(e, (ev.ToolCall, ev.ToolResult, ev.TurnEnd, ev.Question, ev.PermissionRequest)):
            self._last_progress_at = self._clock()

    def current_status(self) -> str:
        """The rich derived status right now (no side effects). Used on WS attach."""
        return derive_status(self._dash_signals())

    async def _emit_status(self, count: int | None = None) -> None:
        """Single status emitter — rich vocabulary to BOTH consumers.

        Emits a live Status event (for the in-session StatusBar) AND notifies the
        orchestrator (which persists it for the session LIST). `count` is the queue
        depth shown in the busy label; defaults to the live queue length.
        Best-effort on the callback: a failure must never break the session loop.
        """
        status = derive_status(self._dash_signals())
        n = len(self._queue) if count is None else count
        await self._out.put(ev.Status(state=status, count=n))
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
            await self._out.put(ev.Question(
                question_id=qid,
                turn_id=turn_id,
                header=header,
                question=question_text,
                options=options,
                multi_select=multi_select,
            ))
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
        )

    async def _can_use_tool(self, tool_name, tool_input, ctx=None):
        turn_id = self._turn()
        if tool_name == "AskUserQuestion":
            return await self._handle_ask_user_question(tool_input, turn_id)
        perm_id, fut = await self._reg.request(
            self.session_id, turn_id, tool_name, tool_input,
            mode=self.mode, denylist=self.denylist, allowlist=True, identity=self.identity)
        if perm_id is not None:
            await self._out.put(ev.PermissionRequest(
                perm_id, turn_id, tool_name, ev.tool_kind(tool_name),
                tool_input, plan=(tool_name == "ExitPlanMode")))
            # A prompt is now pending for the user → waiting_input / ready_review.
            await self._emit_status()
        allowed = await fut
        # Decision made; the perm is no longer pending → back to in_progress.
        await self._emit_status()
        return PermissionResultAllow() if allowed else PermissionResultDeny(message="denied by user")

    async def start(self):
        self._client = self._factory(self.cwd, self.config_dir, self._can_use_tool, self.mode, self.resume)
        await self._client.__aenter__()

    async def send_prompt(self, text: str, images: list[dict] | None = None):
        if self._client is None:
            raise RuntimeError("SessionDriver.send_prompt called before start()")
        images = images or []
        # C2 — FIFO queue: if a pump is live, enqueue (text, images) instead of rejecting.
        if self._pump is not None and not self._pump.done():
            self._queue.append((text, images))
            log.debug("driver.send_prompt: queued prompt for session=%s, queue_depth=%d",
                      self.session_id, len(self._queue))
            await self._emit_status()
            await self._out.put(ev.QueueUpdate(self.queue_wire()))
            return
        # C3 — auto-title from the first prompt (first line, max 50 chars).
        if not self._titled:
            self._titled = True
            first_line = text.splitlines()[0] if text else (text or "image")
            await self._out.put(ev.Title(title=first_line[:50], source="prompt"))
        await self._start_turn(text, images)

    async def _start_turn(self, text: str, images: list[dict] | None = None):
        """Echo the user prompt, emit working status, query the SDK, drain."""
        images = images or []
        self._cancel_suggest()
        self._last_user_text = text
        self._last_assistant_parts = []
        self._turn_active = True
        self._last_turn_outcome = "success"
        await self._emit_status()
        # Build the live UserMessage image refs (url/mime/name only).
        wire_images = [{"url": im["url"], "mime": im["mime"], "name": im.get("name", "")}
                       for im in images]
        user_turn = uuid.uuid4().hex
        await self._out.put(ev.UserMessage(
            turn_id=user_turn, text=text, src_uuid=user_turn,
            images=wire_images or None))
        assert self._client is not None  # _start_turn only runs once the client is connected
        if images:
            content: list[dict] = []
            if text:
                content.append({"type": "text", "text": text})
            for im in images:
                with open(im["path"], "rb") as fh:
                    data = base64.b64encode(fh.read()).decode("ascii")
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": im["mime"], "data": data}})
            message = {"type": "user",
                       "message": {"role": "user", "content": content},
                       "parent_tool_use_id": None, "session_id": "default"}
            await self._client.query(_aiter_one(message))
        else:
            await self._client.query(text)
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

    async def _maybe_signal_recovery(self, code: str, message: str) -> None:
        """Classify a turn-failure surface; if recoverable, emit TurnFailure and notify
        the orchestrator. Retry-After comes from the error text, falling back to a
        RateLimitEvent reset observed this turn (resets_at - now)."""
        failure = classify(code, message)
        if not failure.is_recoverable:
            return
        retry_after = failure.retry_after
        if retry_after is None and self._last_rate_reset is not None:
            retry_after = max(0.0, float(self._last_rate_reset) - self._clock())
        await self._out.put(ev.TurnFailure(
            kind=failure.kind, account=self.account, retry_after=retry_after))
        if self._on_recoverable_failure is not None:
            try:
                await self._on_recoverable_failure(self.session_id, failure.kind, retry_after)
            except Exception:
                log.warning("driver.on_recoverable_failure failed session=%s",
                            self.session_id, exc_info=True)

    async def _drain(self):
        try:
            async for msg in self._client.receive_response():
                for e in self._tr.feed(msg):
                    if isinstance(e, ev.RateLimit):
                        # Internal: stash the reset for retry timing; don't forward.
                        if e.resets_at is not None:
                            self._last_rate_reset = e.resets_at
                        continue
                    if isinstance(e, ev.TextDelta):
                        self._last_assistant_parts.append(e.text)
                    elif isinstance(e, ev.TurnEnd):
                        self._last_turn_outcome = e.outcome
                        self._last_turn_id = e.turn_id
                    elif isinstance(e, ev.ErrorEvent):
                        await self._maybe_signal_recovery(e.code, e.message)
                    self._note_progress(e)
                    await self._out.put(e)
                if self.claude_session_id is None and self._tr.session_id:
                    self.claude_session_id = self._tr.session_id
                    if self._on_session_id is not None:
                        try:
                            await self._on_session_id(self.session_id, self.claude_session_id)
                        except Exception:
                            log.exception("driver.on_session_id session=%s", self.session_id)
        except Exception as exc:
            log.exception("driver.drain session=%s", self.session_id)
            # Surface the real failure to the UI, not a generic placeholder. The cause
            # (e.g. a dead token or a closed subprocess) is exactly what the user needs
            # to act on; redaction scrubs any secret-shaped substrings downstream.
            detail = str(exc).strip() or "no detail"
            full = f"{type(exc).__name__}: {detail}"
            await self._out.put(ev.ErrorEvent(
                "driver_error", full[:500], turn_id=self._turn()))
            self._last_turn_outcome = "failed"
            await self._maybe_signal_recovery("driver_error", full)
        finally:
            # Capture any token rotation the SDK performed during this turn.
            await self._call_sync_back()
            # If queued prompts remain, pop the front and start the next turn.
            if self._queue:
                next_text, next_images = self._queue.pop(0)
                log.debug("driver.drain: dequeuing next prompt for session=%s, remaining=%d",
                          self.session_id, len(self._queue))
                await self._out.put(ev.QueueUpdate(self.queue_wire()))
                await self._start_turn(next_text, next_images)
            else:
                self._turn_active = False
                self._turns_completed += 1
                await self._emit_status()
                self._maybe_start_suggestions()

    def queued(self) -> list[str]:
        """Return a snapshot of the queued prompt texts."""
        return [t for t, _ in self._queue]

    def queue_wire(self) -> list[dict]:
        """Structured queue snapshot: text + image refs per queued prompt."""
        return [
            {"text": t,
             "images": [{"url": im["url"], "mime": im["mime"], "name": im.get("name", "")}
                        for im in imgs]}
            for t, imgs in self._queue
        ]

    async def cancel_queued(self, index: int) -> bool:
        """Cancel a queued prompt by index. Returns True if removed, False if index invalid."""
        if index < 0 or index >= len(self._queue):
            return False
        self._queue.pop(index)
        await self._out.put(ev.QueueUpdate(self.queue_wire()))
        await self._emit_status()
        return True

    async def events(self):
        while True:
            item = await self._out.get()
            if item is _STREAM_DONE:
                return
            yield item

    def _cancel_suggest(self) -> None:
        if self._suggest_task is not None and not self._suggest_task.done():
            self._suggest_task.cancel()

    def _maybe_start_suggestions(self) -> None:
        """Fire-and-forget suggestion generation once the session is idle. Best-effort."""
        if self._suggester is None:
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
        items = await self._suggester.generate(self.cwd, self.config_dir, context)
        log.info("suggestions session=%s turn=%s n=%d", self.session_id, turn_id, len(items))
        if items:
            self._last_suggestions = items
            await self._out.put(ev.Suggestions(self.session_id, turn_id, items))

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

    async def interrupt(self):
        self._reg.deny_all(self.session_id)
        self._qreg.deny_all(self.session_id)
        # Clear the queue so a Stop abandons all pending queued prompts.
        self._queue.clear()
        self._cancel_suggest()
        if self._client:
            await self._client.interrupt()
        self._turn_active = False
        await self._out.put(ev.QueueUpdate([]))
        await self._emit_status()

    async def set_permission_mode(self, mode: str):
        self.mode = mode
        if self._client:
            await self._client.set_permission_mode("plan" if mode == "plan" else "default")

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

    async def reconnect(self) -> None:
        """Tear down the SDK client and start a FRESH session (resume=None).

        Implements /clear: a conversation's history IS the resumed session, so
        clearing == dropping the resume handle and reconnecting. Resets per-turn
        and titling state so the new session behaves like a brand-new one.
        """
        if self._pump is not None and not self._pump.done():
            self._pump.cancel()
            try:
                await self._pump
            except asyncio.CancelledError:
                pass
        self._pump = None
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            await self._call_sync_back()
        self.resume = None
        self.claude_session_id = None
        self._titled = False
        self._queue.clear()
        self._turn_active = False
        self._tr = Translator()
        client = self._factory(self.cwd, self.config_dir, self._can_use_tool, self.mode, None)
        self._client = client
        await client.__aenter__()

    def resolve_permission(self, perm_id, decision, identity) -> bool:
        return self._reg.resolve(perm_id, decision, identity)

    async def aclose(self):
        self._cancel_suggest()
        self._reg.deny_all(self.session_id)
        self._qreg.deny_all(self.session_id)
        if self._pump:
            self._pump.cancel()
            try:
                await self._pump
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.__aexit__(None, None, None)
        # Final sync_back: capture any token rotation that occurred during the
        # session (e.g. a long-running session where the SDK refreshed but the
        # drain's finally sync_back already ran before the close was requested).
        await self._call_sync_back()
        await self._out.put(_STREAM_DONE)
