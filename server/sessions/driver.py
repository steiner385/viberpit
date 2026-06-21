from __future__ import annotations
import asyncio
import logging
import uuid
from typing import Callable
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from . import events as ev
from .translator import Translator
from .permissions import PermissionRegistry
from .questions import QuestionRegistry
from .status import StatusSignals, derive_status

log = logging.getLogger(__name__)

_STREAM_DONE = object()


class SessionDriver:
    """Drives one Claude session via an SDK client, exposing normalized events.

    The orchestrator composes the per-account config dir and passes it as `config_dir`;
    `sdk_factory(cwd, config_dir, can_use_tool, mode, resume) -> SDKClientProtocol` builds
    the client (production = real ClaudeSDKClient; tests = FakeSDKClient).
    """

    def __init__(self, session_id, account, cwd, config_dir, *, mode, sdk_factory, registry,
                 identity, denylist=PermissionRegistry.DANGEROUS, resume=None,
                 on_session_id=None, on_sync_back: Callable[[], None] | None = None,
                 on_status=None):
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
        self._factory = sdk_factory
        self._reg = registry
        self._qreg = QuestionRegistry()
        self._tr = Translator()
        self._client = None
        self._out: asyncio.Queue = asyncio.Queue()
        self._pump: asyncio.Task | None = None
        self._titled: bool = False
        self._queue: list[str] = []
        self._turn_active: bool = False
        self._turns_completed: int = 0

    def _turn(self):
        return getattr(self._tr, "_turn_id", None) or "turn"

    async def _put_status(self, state: str, count: int) -> None:
        """Emit a live Status event (for the in-session StatusBar) AND refresh the
        derived dashboard status so the session LIST reflects the driver's real state."""
        await self._out.put(ev.Status(state=state, count=count))
        await self._emit_dashboard_status()

    @staticmethod
    def _normalize_option(opt) -> dict:
        """Normalize a question option to a dict with at least a 'label' key."""
        if isinstance(opt, dict):
            return opt
        return {"label": str(opt)}

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
        await self._emit_dashboard_status()

        # Await all answers concurrently (gather preserves order).
        futs = [fut for _, fut in pending]
        answers: list[list[str] | None] = await asyncio.gather(*futs)

        # Question(s) resolved → back to in_progress.
        await self._emit_dashboard_status()

        # Build the deny message.
        questions_list = [q for q, _ in pending]
        all_dismissed = all(a is None for a in answers)
        if len(questions_list) == 1:
            q_text = questions_list[0].get("question") or ""
            selected = answers[0]
            if selected is None:
                prefix = "[User dismissed the question UI]"
                msg = f"{prefix} {q_text} → (dismissed — user did not answer)"
            else:
                answer_str = ", ".join(selected)
                msg = f"[User answered via the question UI] {q_text} → {answer_str}"
        else:
            prefix = "[User dismissed the question UI]" if all_dismissed else "[User answered via the question UI]"
            lines = [prefix]
            for q_obj, selected in zip(questions_list, answers):
                q_text = q_obj.get("question") or ""
                if selected is None:
                    answer_str = "(dismissed — user did not answer)"
                else:
                    answer_str = ", ".join(selected)
                lines.append(f"- {q_text} → {answer_str}")
            msg = "\n".join(lines)

        return PermissionResultDeny(message=msg)

    def answer_question(self, question_id: str, selected: list[str], identity: str) -> bool:
        """Resolve a pending question with the user's selection. Mirrors resolve_permission."""
        return self._qreg.resolve(question_id, selected, identity)

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
        )

    async def _emit_dashboard_status(self) -> None:
        """Derive the dashboard status from current signals and notify the orchestrator.

        Best-effort: a failing callback must never break the session loop.
        """
        if self._on_status is None:
            return
        try:
            await self._on_status(self.session_id, derive_status(self._dash_signals()))
        except Exception:
            log.warning("driver.on_status failed session=%s", self.session_id, exc_info=True)

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
            await self._emit_dashboard_status()
        allowed = await fut
        # Decision made; the perm is no longer pending → back to in_progress.
        await self._emit_dashboard_status()
        return PermissionResultAllow() if allowed else PermissionResultDeny(message="denied by user")

    async def start(self):
        self._client = self._factory(self.cwd, self.config_dir, self._can_use_tool, self.mode, self.resume)
        await self._client.__aenter__()

    async def send_prompt(self, text: str):
        if self._client is None:
            raise RuntimeError("SessionDriver.send_prompt called before start()")
        # C2 — FIFO queue: if a pump is live, enqueue the prompt instead of rejecting.
        if self._pump is not None and not self._pump.done():
            self._queue.append(text)
            log.debug("driver.send_prompt: queued prompt for session=%s, queue_depth=%d",
                      self.session_id, len(self._queue))
            await self._put_status("working", len(self._queue))
            await self._out.put(ev.QueueUpdate(list(self._queue)))
            return
        # C3 — auto-title: emit a Title event from the first prompt (first line, max 50 chars).
        if not self._titled:
            self._titled = True
            first_line = text.splitlines()[0] if text else text
            title_text = first_line[:50]
            await self._out.put(ev.Title(title=title_text, source="prompt"))
        await self._start_turn(text)

    async def _start_turn(self, text: str):
        """Start a new turn: echo the user prompt, emit working status, query SDK, drain."""
        self._turn_active = True
        await self._put_status("working", len(self._queue))
        # Echo the user's prompt as a session event. The SDK never sends the human's
        # prompt text back through receive_response (only transcript-replay reconstructs
        # it on re-attach), so without this the live stream shows only responses. The
        # user message is its own turn, keyed by a fresh id distinct from the assistant
        # turn the Translator assigns from the SDK's message_start uuid.
        user_turn = uuid.uuid4().hex
        await self._out.put(ev.UserMessage(turn_id=user_turn, text=text, src_uuid=user_turn))
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

    async def _drain(self):
        try:
            async for msg in self._client.receive_response():
                for e in self._tr.feed(msg):
                    await self._out.put(e)
                if self.claude_session_id is None and self._tr.session_id:
                    self.claude_session_id = self._tr.session_id
                    if self._on_session_id is not None:
                        try:
                            await self._on_session_id(self.session_id, self.claude_session_id)
                        except Exception:
                            log.exception("driver.on_session_id session=%s", self.session_id)
        except Exception:
            log.exception("driver.drain session=%s", self.session_id)
            await self._out.put(ev.ErrorEvent("driver_error", "session stream failed"))
        finally:
            # Capture any token rotation the SDK performed during this turn.
            await self._call_sync_back()
            # If queued prompts remain, pop the front and start the next turn.
            if self._queue:
                next_text = self._queue.pop(0)
                log.debug("driver.drain: dequeuing next prompt for session=%s, remaining=%d",
                          self.session_id, len(self._queue))
                await self._out.put(ev.QueueUpdate(list(self._queue)))
                await self._start_turn(next_text)
            else:
                self._turn_active = False
                self._turns_completed += 1
                await self._put_status("idle", 0)

    def queued(self) -> list[str]:
        """Return a snapshot of the queued prompts."""
        return list(self._queue)

    async def cancel_queued(self, index: int) -> bool:
        """Cancel a queued prompt by index. Returns True if removed, False if index invalid."""
        if index < 0 or index >= len(self._queue):
            return False
        self._queue.pop(index)
        await self._out.put(ev.QueueUpdate(list(self._queue)))
        await self._put_status("working", len(self._queue))
        return True

    async def events(self):
        while True:
            item = await self._out.get()
            if item is _STREAM_DONE:
                return
            yield item

    async def interrupt(self):
        self._reg.deny_all(self.session_id)
        self._qreg.deny_all(self.session_id)
        # Clear the queue so a Stop abandons all pending queued prompts.
        self._queue.clear()
        if self._client:
            await self._client.interrupt()
        self._turn_active = False
        await self._out.put(ev.QueueUpdate([]))
        await self._put_status("idle", 0)

    async def set_permission_mode(self, mode: str):
        self.mode = mode
        if self._client:
            await self._client.set_permission_mode("plan" if mode == "plan" else "default")

    def resolve_permission(self, perm_id, decision, identity) -> bool:
        return self._reg.resolve(perm_id, decision, identity)

    async def aclose(self):
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
