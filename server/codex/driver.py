"""One ccswitch session driven as one Codex thread.

This is the Codex arm of the provider-neutral :class:`~server.sessions.engine.AgentDriver`
contract, and the Codex sibling of ``server/sessions/claude_driver.py``. It owns
exactly one thread on the account's shared `codex app-server` and nothing else:
the process, its restarts and its login belong to
:class:`~server.codex.manager.CodexRunnerManager`, the wire vocabulary to
``schema.py``/``protocol.py``, and the notification→event mapping to
``translator.py``. What is left here — and what this module is therefore about —
is the *session* layer: turn lifecycle, the prompt queue, approvals, permission
modes, and the fan-out every attached client reads.

Four rules shape almost every decision below.

**A crash never becomes a success.** Every turn is fenced with
``begin_turn``/``finish_turn``; a :data:`~server.codex.manager.TurnOutcome.INDETERMINATE`
verdict rewrites the turn's outcome to ``interrupted`` and marks the session
needs-attention. The driver never asserts a turn completed — it asks the
supervisor, which knows whether the process that was running it survived.

**No decision outlives its authority.** An approval is answered only while the
thread id, the native item id and the runner *generation* it was raised under
still hold. A human who takes two minutes to click "Allow" on a request whose
app-server has since been replaced gets a decline, because the process that
asked is gone and the new one never asked.

**Never widen silently.** ``set_permission_mode`` receives unvalidated strings
(the web plan toggle sends ``plan``/``default`` straight through), and the Codex
sandbox is fixed for the life of a thread. An unmapped mode is refused and the
narrower profile kept; a sandbox change is applied by re-resuming the thread at
a turn boundary, and a failure to do so refuses the switch rather than running
the next turn under a sandbox nobody chose.

**One status vocabulary.** Status is always derived through
``server/sessions/status.py``. The translator's `thread/status/changed` mapping
is consumed as a *signal* and never forwarded: it speaks working/idle, while
ccswitch's dashboard speaks in_progress/waiting_input/needs_attention/…, and two
sources writing one pill is how a session ends up "Ready" mid-turn.

Ordering. Everything the app-server says — notifications and supervision notices
alike — is funnelled through one inbox and one pump task, so the turn state
machine has a single writer and a notice can never interleave halfway through a
turn's event batch.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import logging
import os
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from ccswitch import brand
from ccswitch import paths
from server.aio import cancel_and_reap
from server.assistant.context import PromptContext
from server.assistant.options_codex import (ASSISTANT_CODEX_RESUME_UNSUPPORTED,
                                            CodexAssistantOverlay,
                                            assistant_approval_decision, tool_error)
from server.codex import background, schema
from server.codex.capabilities import CODEX_CAPABILITIES
from server.codex.manager import (
    RECOVER_VIA_THREAD_READ,
    CodexRunnerManager,
    RunnerNotice,
    RunnerNoticeKind,
    TurnOutcome,
    TurnToken,
)
from server.codex.permissions import (
    ASSISTANT_PROFILE,
    CodexPermissionProfile,
    UnsupportedCodexPermissionMode,
    profile_for_mode,
)
from server.codex.protocol import CodexAppServerClient
from server.codex.redaction import scrub_exception
from server.codex.translator import TRANSLATED_NOTIFICATIONS, CodexEventTranslator
from server.obs.context import log_event
from server.sessions import events as ev
from server.sessions.background_tasks import OpenBgTask
from server.sessions.completion import marker_instructions
from server.sessions.driver_stream import _STREAM_DONE, DriverStream
from server.sessions.engine import (
    AgentDriverConfig,
    AgentDriverFactory,
    AgentEngine,
)
from server.sessions.permissions import PermissionRegistry
from server.sessions.questions import QuestionRegistry
from server.sessions.status import SEEDABLE_STATUSES, StatusSignals, derive_status

if TYPE_CHECKING:  # imported for typing only — `server.sessions.tools` reaches back
    # into the orchestrator's record accessor, and this module is imported by it.
    from server.sessions.tools.codex import CodexSessionTools

logger = logging.getLogger(__name__)

#: Attachment mime types Codex sends as a native `localImage` input.
_IMAGE_MIMES: Final[frozenset[str]] = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)

#: Text-family mime types with no reliable binary representation on the Codex
#: wire (`turn/start` accepts only `text` and `localImage` — see schema.py's
#: `UserInput` union). These are decoded and folded into a `TextUserInput`
#: instead, mirroring `claude_driver._start_turn`'s inline-text fallback so the
#: two engines answer about the same file the same way.
_TEXT_INLINE_MIMES: Final[frozenset[str]] = frozenset(
    {"text/plain", "text/csv", "application/json"}
)

#: A zip has no text extraction story either engine implements; both describe
#: it rather than reading its contents (see claude_driver._start_turn).
_ZIP_MIME: Final = "application/zip"

#: Content-addressed staging dir created INSIDE a session's cwd so an
#: attachment that lives in the global store (outside cwd) can still satisfy
#: Codex's own realpath(cwd) containment for `localImage`. Dot-prefixed and
#: registered in `.git/info/exclude` (best-effort, see `_ensure_staging_ignored`)
#: so it never appears in `git status` for the user's repo. Name is shared
#: with `worktrees.remove_worktree`'s pre-removal safety net — see
#: `paths.codex_attachment_staging_dirname`.
_STAGING_DIRNAME: Final = paths.codex_attachment_staging_dirname()

#: Events that reconstruct a turn's transcript — the only ones retained in the
#: in-progress-turn buffer and replayed to a late subscriber. Mirrors
#: ``claude_driver._TURN_CONTENT``: control/ephemeral events are re-emitted from
#: authoritative state by the WS attach path, so buffering them double-emits.
_TURN_CONTENT: Final = (
    ev.UserMessage, ev.TurnStart, ev.TurnModel, ev.SessionMeta,
    ev.TextDelta, ev.ThinkingDelta, ev.ToolCall, ev.ToolResult,
    ev.TurnEnd, ev.ErrorEvent,
)

#: `codexErrorInfo` categories the orchestrator can actually recover from, and
#: the ``server/sessions/failure.py`` kind each maps to. Anything else surfaces
#: as a plain error: inventing a recovery kind for an unknown category would
#: send the orchestrator rotating accounts over a bad request.
_RECOVERABLE_ERROR_KINDS: Final[dict[str, str]] = {
    "unauthorized": "auth_401",
    "rateLimitExceeded": "rate_limit",
    "usageLimitExceeded": "rate_limit",
    "sessionBudgetExceeded": "rate_limit",
}

#: Fixed operator-facing text for an `error` notification. The native message is
#: `Redacted` (Phase 2) and stays that way: it can carry a prompt, a path or
#: model output, and this string reaches every attached browser.
_ERROR_MESSAGE: Final = "The Codex app-server reported an error for this session."
_ERROR_CODE: Final = "codex_error"

_CRASH_CODE: Final = "codex_runner_crashed"
_CRASH_MESSAGE: Final = (
    "The Codex app-server for this account stopped unexpectedly; the turn that "
    "was running was interrupted."
)
_UNRECOVERABLE_CODE: Final = "codex_runner_unrecoverable"
_UNRECOVERABLE_MESSAGE: Final = (
    "The Codex app-server for this account could not be restarted. This session "
    "cannot continue until it is started again."
)
_RESYNC_CODE: Final = "codex_thread_lost"
_RESYNC_MESSAGE: Final = (
    "The Codex thread could not be re-read after the app-server restarted."
)
_MODE_REFUSED_CODE: Final = "codex_permission_mode_refused"
_SANDBOX_REFUSED_CODE: Final = "codex_sandbox_change_refused"
_SANDBOX_REFUSED_MESSAGE: Final = (
    "The Codex sandbox for this thread could not be changed, so the permission "
    "mode was left as it was. The session keeps running under the sandbox it "
    "was started with."
)
_TURN_START_FAILED_CODE: Final = "codex_turn_start_failed"
_TURN_START_FAILED_MESSAGE: Final = "The Codex turn could not be started."

#: How long a teardown waits for the inbox pump to unwind before abandoning it.
_CLOSE_GRACE_S: Final = 5.0


class CodexDriverError(RuntimeError):
    """Base class for failures raised out of the Codex driver's own surface."""


class CodexRunnerUnavailable(CodexDriverError):
    """The account's app-server was gone when this driver needed it.

    Typed rather than a bare ``RuntimeError`` because the orchestrator ensures
    the runner immediately before building the driver: seeing this means it died
    in the window between, which is a supervision fact, not a programming error.
    """


class CodexThreadNotStarted(CodexDriverError):
    """A turn operation was attempted before :meth:`CodexAgentDriver.start`."""


class CodexAttachmentRejected(CodexDriverError, ValueError):
    """An attachment could not be sent to Codex, so the prompt was refused.

    Refusing is deliberate: silently dropping the attachment would send a prompt
    that reads as though the file were attached, and the model would answer
    about a file it never received.
    """


@dataclass(frozen=True, slots=True)
class _Notification:
    """One app-server notification, queued for the pump."""

    method: str
    params: schema.ServerNotificationParams


@dataclass(frozen=True, slots=True)
class _Notice:
    """One supervision notice, queued on the SAME inbox as notifications.

    Sharing the queue is the point: a `CRASHED` notice that overtook the events
    of the turn it interrupted would close a turn whose output was still in
    flight.
    """

    notice: RunnerNotice


_InboxItem = _Notification | _Notice


@dataclass(frozen=True, slots=True)
class _PendingApproval:
    """The triple an approval must still satisfy when the human answers.

    ``item_id`` is the app-server's own id for the action being approved, and
    ``generation`` the runner generation it was raised on. Both are checked
    again at reply time — a decision is only valid for the process that asked.
    """

    method: str
    thread_id: str
    item_id: str
    generation: int
    turn_id: str


@dataclass(frozen=True, slots=True)
class _PendingUserInput:
    """The triple a `request_user_input` reply must still satisfy when the
    human answers. Same fencing rationale as :class:`_PendingApproval`: a
    decision is only valid for the process (and thread) that asked."""

    thread_id: str
    item_id: str
    generation: int
    turn_id: str


@dataclass
class _Counters:
    """Observability only — no method- or content-derived keys (Phase 2 rule)."""

    untranslated_notifications: int = 0
    dropped_user_echoes: int = 0
    stale_approvals: int = 0
    stale_user_input: int = 0
    stale_dynamic_tools: int = 0
    assistant_builtins_declined: int = 0
    steer_fallbacks: int = 0
    pump_errors: int = 0


@dataclass(slots=True)
class _QueuedPrompt:
    text: str
    images: list[dict] = field(default_factory=list)
    context: PromptContext | None = None


class CodexAgentDriver:
    """Drives one Codex thread, exposing the normalized ccswitch event stream."""

    engine = AgentEngine.CODEX
    #: Bound, not restated. ``server/codex/capabilities.py`` already declares this
    #: engine's control surface for the availability report; a second literal here
    #: is how the two drifted (it advertised ``questions=True`` while this driver
    #: could only ever answer "no longer pending").
    capabilities = CODEX_CAPABILITIES

    def __init__(
        self,
        session_id: str,
        account: str,
        cwd: str,
        config_dir: str,
        *,
        mode: str,
        manager: CodexRunnerManager,
        registry: PermissionRegistry,
        identity: str,
        resume: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        on_engine_session_id: Any = None,
        on_status: Any = None,
        on_recoverable_failure: Any = None,
        on_context_usage: Any = None,
        clock: Callable[[], float] | None = None,
        assistant_overlay: CodexAssistantOverlay | None = None,
        session_tools: CodexSessionTools | None = None,
    ) -> None:
        self.session_id = session_id
        self.account = account
        self.cwd = cwd
        # Codex isolates per account through CODEX_HOME on the app-server process
        # (server/codex/process.py), not through a per-session directory. The
        # attribute stays on the contract because orchestration reads it for
        # every engine; it is informational here.
        self.config_dir = config_dir
        self.mode = mode
        self.resume = resume
        self._manager = manager
        self._reg = registry
        self._identity = identity
        self._model = _normalize_model(model)
        self._effort = reasoning_effort
        self._on_engine_session_id = on_engine_session_id
        self._on_status = on_status
        self._on_recoverable_failure = on_recoverable_failure
        self._on_context_usage = on_context_usage
        self._clock = clock or time.time
        #: Set only for an assistant-kind session (server/assistant/options_codex.py).
        #: Its presence changes four things about this driver, all in the
        #: fail-closed direction: the thread is started with a host-executed tool
        #: surface and a neutral cwd, it is never resumed, its permission mode is
        #: pinned, and every built-in approval is declined without a card.
        self._assistant = assistant_overlay
        #: Set for a CODING session when the session-tools flag is on
        #: (server/sessions/tools/codex.py). Unlike ``_assistant`` it changes exactly
        #: ONE thing about this driver: a fresh `thread/start` declares the ccswitch
        #: tools. The profile, the resume path and every approval card stay whatever
        #: the session's own mode says — a coding session that gained two tools is
        #: still a coding session.
        self._session_tools = session_tools

        # An assistant thread's authority comes from its overlay, not from the
        # session's permission mode: the mode is a coding-session concept and the
        # drawer has no coding surface for it to describe. Pinned at construction
        # so `turn/start` — which sends `self._profile.approval_policy` on EVERY
        # turn — cannot quietly downgrade the thread from `untrusted` after a
        # `thread/start` that pinned it.
        self._profile: CodexPermissionProfile = (
            ASSISTANT_PROFILE if assistant_overlay is not None
            else profile_for_mode(mode))
        #: A profile whose sandbox differs from the live one, waiting for the
        #: next turn boundary (the sandbox is immutable mid-thread).
        self._pending_profile: tuple[str, CodexPermissionProfile] | None = None
        #: The mode in force before the plan overlay, so the toggle's "default"
        #: sentinel restores it instead of collapsing to the ask-everything
        #: profile. Same contract as ``ClaudeAgentDriver.set_permission_mode``.
        self._pre_plan_mode: str | None = None

        self._thread_id: str | None = None
        self._generation: int | None = None
        self._translator = CodexEventTranslator()
        self._inbox: asyncio.Queue[_InboxItem] = asyncio.Queue()
        self._pump: asyncio.Task[None] | None = None

        self._subscribers: set[asyncio.Queue] = set()
        self._turn_buffer: list[object] = []
        self._queue: list[_QueuedPrompt] = []

        self._turn_id: str | None = None
        self._turn_active = False
        self._turn_started_at: float | None = None
        self._turn_token: TurnToken | None = None
        self._turns_completed = 0
        self._last_turn_outcome = "success"
        self._condensing = False
        # (base, effective) pair from the last on_status overlay (or seed_status):
        # current_status() reports `effective` while the raw derivation still equals
        # `base`, keeping the attach snapshot in step with the session list. Same
        # contract as ClaudeAgentDriver's overlay.
        self._status_overlay: tuple[str, str] | None = None
        self._titled = False
        self._last_progress_at: float = self._clock()
        self._context_usage: dict = {}
        self._pending_approvals: dict[str, _PendingApproval] = {}
        self._qreg = QuestionRegistry()
        self._pending_user_input: dict[str, _PendingUserInput] = {}
        self._closing = False
        self.counters = _Counters()
        #: Set once `_ensure_staging_ignored` has run for this driver's cwd —
        #: the git subprocess it shells out to is a one-time cost, not a
        #: per-attachment one.
        self._staging_ignored = False
        #: Skill name -> absolute path, from the last `skills/list`. `turn/start`
        #: identifies a skill by BOTH, but the command catalog carries only the
        #: name, so the path is resolved HERE — the driver that published the
        #: entry is the only thing that can answer for it, and a dispatch that
        #: guessed a path would run whatever happened to be there.
        self._skill_paths: dict[str, str] = {}

    def __repr__(self) -> str:
        return (
            f"<CodexAgentDriver session={self.session_id} account={self.account} "
            f"turn_active={self._turn_active}>"
        )

    # -- identity / introspection -----------------------------------------

    @property
    def engine_session_id(self) -> str | None:
        """The Codex thread id — the handle a resume needs, exactly as the
        Claude driver's SDK session id is."""
        return self._thread_id

    @property
    def identity(self) -> str:
        return self._identity

    @identity.setter
    def identity(self, value: str) -> None:
        """Re-thread the driver AND its pending approvals/questions to *value*.

        Same failure this closes for the Claude driver: ``request_external``
        snapshots the identity, and ``resolve`` gates on it, so a card raised
        while the driver still carried the "system" sentinel would be listed as
        pending and refuse every answer once a real user attached.
        """
        old = self._identity
        self._identity = value
        if old != value:
            self._reg.rethread_identity(self.session_id, old, value)
            self._qreg.rethread_identity(self.session_id, old, value)

    @property
    def turn_active(self) -> bool:
        return self._turn_active

    @property
    def turn_started_at(self) -> float | None:
        return self._turn_started_at

    @property
    def drain_alive(self) -> bool:
        """No separate drain exists: the app-server owns the turn and tells us
        when it ends, so "a turn is in flight" is the whole truth. The stall
        watchdog reads this to tell a resolved interrupt from an ignored one —
        for Codex an acknowledged `turn/interrupt` IS the resolution."""
        return self._turn_active

    @property
    def in_api_backoff(self) -> bool:
        """Codex surfaces retry state only as a terminal `codexErrorInfo`
        category, never as a "backing off now" signal, so there is nothing to
        report. Reporting True on an unrelated signal would suppress the stall
        watchdog for a genuinely wedged turn."""
        return False

    @property
    def awaiting_user_input(self) -> bool:
        return bool(self._reg.pending(self.session_id)) or bool(
            self._qreg.pending(self.session_id)
        )

    @property
    def last_progress_at(self) -> float:
        return self._last_progress_at

    # -- lifecycle ---------------------------------------------------------

    def _client(self) -> CodexAppServerClient:
        """The account's CURRENT app-server client.

        Always re-read, never cached: a supervised restart replaces the client
        object, and a cached one would keep writing into a closed transport.
        """
        client = self._manager.client_for(self.account)
        if client is None:
            raise CodexRunnerUnavailable(
                f"the codex app-server for {self.account} is not running"
            )
        return client

    async def start(self) -> None:
        """Open (or resume) this session's thread and subscribe to its streams."""
        client = self._client()
        profile = self._profile
        overlay = self._assistant
        response: schema.ThreadResumeResponse | schema.ThreadStartResponse
        if overlay is not None:
            # An assistant thread is ALWAYS started, never resumed, even when the
            # row carries an engine_session_id: `thread/resume` has no
            # `dynamicTools` field on the pinned contract, so resuming would hand
            # the drawer back with the built-ins still declared and every ccswitch
            # tool gone. Losing the model's context is the recoverable half of that
            # trade; the ccswitch transcript is stored by ccswitch, not replayed
            # from the thread.
            if self.resume:
                log_event(
                    logger,
                    logging.INFO,
                    "codex.driver.assistant_thread_restarted",
                    session=self.session_id,
                    reason=ASSISTANT_CODEX_RESUME_UNSUPPORTED,
                )
            response = await client.start_thread(overlay.thread_start_params())
        elif self.resume:
            if self._session_tools is not None:
                # Stated once, per resumed session, because it is a real capability
                # gap rather than a warning: `thread/resume` has no `dynamicTools`
                # field on the pinned contract (and an unknown one is ignored, not
                # refused — see the 2026-09-06 spike), so this thread comes back
                # without the ccswitch tools. A coding session cannot answer that
                # the way the assistant does — by restarting — because its thread
                # carries the work.
                log_event(
                    logger,
                    logging.INFO,
                    "codex.driver.session_tools_unavailable_on_resume",
                    session=self.session_id,
                )
            response = (
                await client.resume_thread(
                    schema.ThreadResumeParams(
                        thread_id=self.resume,
                        cwd=self.cwd,
                        model=self._model,
                        sandbox=profile.sandbox_mode.value,
                        approval_policy=profile.approval_policy.value,
                    )
                )
            )
        else:
            from server.sessions.tools.instruction import append_session_tools

            response = await client.start_thread(
                schema.ThreadStartParams(
                    cwd=self.cwd,
                    model=self._model,
                    sandbox=profile.sandbox_mode.value,
                    approval_policy=profile.approval_policy.value,
                    # Codex cannot act on a model request
                    # (`AgentCapabilities.self_model_switch=False`), so it is
                    # never told about the marker — asking for a signal nothing
                    # honours burns turns on a request that is silently dropped.
                    #
                    # The session-tools paragraph rides the SAME string, through the
                    # same helper the Claude factory's system-prompt append uses, and
                    # ONLY on this branch: declaring the tools without telling the
                    # model when to reach for them leaves a surface it never uses,
                    # and saying they exist on a RESUMED thread (which cannot declare
                    # them) would describe a surface that is not there. Resume needs
                    # no matching guard — `ThreadResumeParams` carries no
                    # `developerInstructions` at all.
                    developer_instructions=append_session_tools(
                        marker_instructions(self_model_switch=False),
                        enabled=self._session_tools is not None,
                    ),
                    # The coding session's ccswitch tool surface, or nothing at
                    # all: an empty tuple emits no `dynamicTools` field, so a
                    # session with the flag off looks exactly as it did before.
                    dynamic_tools=(
                        ()
                        if self._session_tools is None
                        else self._session_tools.dynamic_tools
                    ),
                )
            )
        thread_id = response.thread.id
        self._thread_id = thread_id
        self._generation = self._manager.generation_for(self.account)
        # A resumed thread never re-sends `thread/started`, so the model from the
        # response is the only one the translator will ever see.
        self._translator = CodexEventTranslator(
            thread_id=thread_id, model=response.model
        )
        # Announce the engine session from the AUTHORITATIVE response rather than
        # from `thread/started`: that notification is emitted before this driver
        # has a thread id to register under (so the manager has nowhere to route
        # it), and a resumed thread never sends it at all. `session_id` is the
        # thread id — the engine-native resume handle, the same role the Claude
        # SDK session id plays — so the web's model chip works unchanged.
        self.emit(ev.SessionMeta(model=response.model, session_id=thread_id))
        self._manager.register_thread(
            self.account,
            thread_id,
            self._on_notice,
            events=self._on_event,
            approvals=self._on_approval,
            user_input=self._on_user_input,
            # Registered when EITHER surface is MOUNTED — which is not the same
            # as "declared on this thread": a resumed coding session carries a
            # `_session_tools` whose `dynamicTools` the pinned `thread/resume`
            # cannot send (see server/sessions/tools/codex.py), so the callback
            # is live with nothing declared behind it. That is deliberate and
            # still fail-closed one layer in: `_on_dynamic_tool` refuses any name
            # the mounted surface does not `declare()` before the permission
            # gate, so an `item/tool/call` on such a thread is rejected by us
            # rather than by the protocol. Left unregistered only when NEITHER
            # surface is mounted, where the protocol's own `-32601` is the answer.
            dynamic_tools=(
                None
                if (overlay is None and self._session_tools is None)
                else self._on_dynamic_tool
            ),
        )
        self._closing = False
        self._pump = asyncio.create_task(
            self._pump_loop(), name=f"codex-driver-{self.session_id}"
        )
        log_event(
            logger,
            logging.INFO,
            "codex.driver.started",
            session=self.session_id,
            account=self.account,
            resumed=bool(self.resume),
        )
        if self._on_engine_session_id is not None:
            await self._on_engine_session_id(self.session_id, thread_id)

    async def aclose(self) -> None:
        """Release the thread. The manager owns the process — nothing is killed."""
        self._closing = True
        thread_id = self._thread_id
        if thread_id is not None:
            # Unregister FIRST: an approval arriving during teardown then finds
            # no handler and takes the protocol's fail-closed `-32601` path
            # instead of reaching a driver that is already closing.
            self._manager.unregister_thread(self.account, thread_id)
        self._dismiss_pending_prompts()
        self._finish_turn()
        pump, self._pump = self._pump, None
        await cancel_and_reap(
            pump, phase=f"codex.driver.pump:{self.session_id}", timeout=_CLOSE_GRACE_S
        )
        for q in self._subscribers:
            q.put_nowait(_STREAM_DONE)
        log_event(
            logger, logging.INFO, "codex.driver.closed", session=self.session_id
        )

    async def archive(self) -> None:
        """Archive the thread upstream, then close.

        Separate from :meth:`aclose` because archiving is destructive to
        resumability: closing a session must leave its thread resumable, while
        deleting one must not leave an archivable thread behind.
        """
        thread_id = self._thread_id
        self._dismiss_pending_prompts()
        if thread_id is not None:
            with contextlib.suppress(Exception):
                await self._client().archive_thread(
                    schema.ThreadArchiveParams(thread_id=thread_id)
                )
        await self.aclose()

    async def reconnect(self) -> None:
        """Drop this thread and start a fresh one (the `/clear` semantics).

        A conversation's history IS its thread, so clearing means abandoning the
        thread handle — exactly as the Claude driver drops its resume handle.
        """
        old = self._thread_id
        if old is not None:
            self._manager.unregister_thread(self.account, old)
        # The inbox pump belongs to the thread subscription being replaced. If it
        # survives, start() creates a second consumer for the same queue and live
        # notifications are split nondeterministically between them. Retire it and
        # discard any already-queued frames from the abandoned thread before the
        # fresh thread registers.
        pump, self._pump = self._pump, None
        await cancel_and_reap(
            pump,
            phase=f"codex.driver.reconnect-pump:{self.session_id}",
            timeout=_CLOSE_GRACE_S,
        )
        self._inbox = asyncio.Queue()
        self._dismiss_pending_prompts()
        self._finish_turn()
        self._queue.clear()
        self._turn_active = False
        self._turn_started_at = None
        self._turn_id = None
        self._turn_buffer = []
        self._titled = False
        self.resume = None
        self._thread_id = None
        await self.start()

    # -- turns -------------------------------------------------------------

    async def send_prompt(
        self,
        text: str,
        images: list[dict] | None = None,
        *,
        interrupt: bool = False,
        context: PromptContext | None = None,
    ) -> None:
        """Start, steer, or queue one prompt.

        With no turn in flight the prompt starts one. With a turn in flight it
        is *steered* into that turn — Codex's own mechanism for adding to live
        work, fenced by ``expectedTurnId`` so a turn that ended in the meantime
        cannot silently swallow it. ``interrupt`` stops the live turn and runs
        this prompt next, ahead of anything queued.
        """
        if self._thread_id is None:
            raise CodexThreadNotStarted(
                "CodexAgentDriver.send_prompt called before start()"
            )
        images = images or []
        # Validate attachments BEFORE any state change: a rejected prompt must
        # not interrupt a turn or leave an entry in the queue.
        inputs = self._build_inputs(text, images)
        if not inputs:
            return
        entry = _QueuedPrompt(text=text, images=images, context=context)
        if interrupt and self._turn_active:
            # Queue-head insert BEFORE the interrupt, so the drain that follows
            # runs THIS prompt rather than one that was already waiting.
            self._queue.insert(0, entry)
            await self.interrupt(keep_queue=True)
            return
        if not self._titled and text:
            self._titled = True
            self._emit(ev.Title(title=text.splitlines()[0][:50], source="prompt"))
        if self._turn_active and self._turn_id is not None:
            await self._steer(entry, inputs)
            return
        await self._start_turn(entry, inputs)

    async def _steer(self, entry: _QueuedPrompt, inputs: tuple[schema.UserInput, ...]) -> None:
        """Add a prompt to the live turn, or queue it if that turn just ended."""
        turn_id = self._turn_id
        assert turn_id is not None
        try:
            await self._client().steer_turn(
                schema.TurnSteerParams(
                    thread_id=self._require_thread(),
                    expected_turn_id=turn_id,
                    input=inputs,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- any steer failure means "queue it"
            # The turn ended (or refused steering) between our check and the
            # call. The prompt is NEVER lost: it waits and the turn-end drain
            # starts it. `expectedTurnId` exists so this race is detectable at
            # all rather than silently appending to the wrong turn.
            self.counters.steer_fallbacks += 1
            log_event(
                logger,
                logging.INFO,
                "codex.driver.steer_fallback",
                session=self.session_id,
                error=scrub_exception(exc),
            )
            self._queue.append(entry)
            self._emit(ev.QueueUpdate(self.queue_wire()))
            await self._emit_status()
            if not self._turn_active:
                await self._drain_queue()
            return
        self._emit_user_message(entry, turn_id)

    async def _start_turn(
        self, entry: _QueuedPrompt, inputs: tuple[schema.UserInput, ...]
    ) -> None:
        """Open a new turn for *entry*, fenced to the current runner generation."""
        await self._apply_pending_profile()
        thread_id = self._require_thread()
        self._turn_active = True
        self._turn_started_at = self._clock()
        # A new turn replaces the previous turn's replay buffer, so a late
        # subscriber sees the turn in progress rather than the last two. Cleared
        # HERE (not on the `turn/started` event) because the user echo below is
        # emitted before the app-server has announced the turn.
        self._turn_buffer = []
        self._turn_token = self._manager.begin_turn(self.account, thread_id)
        # The echo carries the attachments and the route chip, which the native
        # `userMessage` item cannot express — see `_consume`.
        self._emit_user_message(entry, self._turn_id or uuid.uuid4().hex)
        await self._emit_status()
        try:
            response = await self._client().start_turn(
                schema.TurnStartParams(
                    thread_id=thread_id,
                    input=inputs,
                    model=self._model,
                    effort=self._effort,
                    cwd=self.cwd,
                    approval_policy=self._profile.approval_policy.value,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- one typed surface for any failure
            self._turn_active = False
            self._turn_started_at = None
            self._finish_turn()
            self._last_turn_outcome = "failed"
            log_event(
                logger,
                logging.ERROR,
                "codex.driver.turn_start_failed",
                session=self.session_id,
                error=scrub_exception(exc),
            )
            await self.emit_error(_TURN_START_FAILED_CODE, _TURN_START_FAILED_MESSAGE)
            return
        # The `turn/started` notification may already have set this; both name
        # the same turn, so the assignment is idempotent either way.
        self._turn_id = response.turn.id

    async def interrupt(self, *, keep_queue: bool = False) -> None:
        """Stop the live turn. A Stop abandons the queue; ``keep_queue`` (the
        interrupt-and-post path) keeps it and drains it immediately."""
        self._dismiss_pending_prompts()
        if not keep_queue:
            self._queue.clear()
        turn_id, thread_id = self._turn_id, self._thread_id
        if turn_id is not None and thread_id is not None:
            try:
                await self._client().interrupt_turn(
                    schema.TurnInterruptParams(thread_id=thread_id, turn_id=turn_id)
                )
            except Exception as exc:  # noqa: BLE001 -- a dead runner is already stopped
                log_event(
                    logger,
                    logging.INFO,
                    "codex.driver.interrupt_failed",
                    session=self.session_id,
                    error=scrub_exception(exc),
                )
        # The app-server has acknowledged; a `turn/completed` for this turn may
        # still arrive and is forwarded, but the turn is over for the client.
        self._turn_active = False
        self._turn_started_at = None
        self._turn_id = None
        self._condensing = False
        self._finish_turn()
        self._emit(ev.QueueUpdate(self.queue_wire()))
        await self._emit_status()
        await self._drain_queue()

    async def _drain_queue(self) -> None:
        """Start the next queued prompt, if any, once nothing is in flight."""
        if self._closing or self._turn_active or not self._queue:
            return
        while self._queue:
            entry = self._queue.pop(0)
            self._emit(ev.QueueUpdate(self.queue_wire()))
            try:
                inputs = self._build_inputs(entry.text, entry.images)
            except CodexAttachmentRejected as exc:
                # The file moved (or was replaced by a symlink out of the workspace)
                # while the prompt waited. Surface it instead of raising into a
                # turn-end handler that has no caller to report to, then keep
                # draining: one invalid attachment must not strand every valid
                # prompt behind it with no active turn left to trigger another drain.
                await self.emit_error("codex_attachment_rejected", str(exc))
                continue
            if not inputs:
                continue
            await self._start_turn(entry, inputs)
            return

    def _finish_turn(self) -> TurnOutcome:
        """Close the supervision fence, if one is open."""
        token, self._turn_token = self._turn_token, None
        if token is None:
            return TurnOutcome.COMPLETED
        return self._manager.finish_turn(token)

    def _emit_user_message(self, entry: _QueuedPrompt, turn_id: str) -> None:
        wire_images = [
            {"url": im.get("url", ""), "mime": im.get("mime", ""),
             "name": im.get("name", "")}
            for im in entry.images
        ]
        src = uuid.uuid4().hex
        self._emit(
            ev.UserMessage(
                turn_id=turn_id,
                text=entry.text,
                src_uuid=src,
                images=wire_images or None,
                context=entry.context,
            )
        )

    def _build_inputs(
        self, text: str, images: list[dict]
    ) -> tuple[schema.UserInput, ...]:
        """Compose `turn/start` input, rejecting attachments Codex must not get.

        Path containment is checked here — before the app-server ever sees any
        value — and against the REAL path, so a symlink planted inside the
        workspace cannot smuggle a file from outside it into the model's context.
        An attachment resolving OUTSIDE the workspace is accepted only when it
        resolves inside the ccswitch-managed content-addressed attachments store
        (the normal upload path — see `server/sessions/attachments.py`); anything
        else outside cwd is refused. Never widen this to "any external path":
        that is exactly the containment bypass this check exists to prevent.
        """
        inputs: list[schema.UserInput] = []
        if text:
            inputs.append(schema.TextUserInput(text=text))
        if not images:
            return tuple(inputs)
        root = os.path.realpath(self.cwd)
        attachments_root = os.path.realpath(str(paths.attachments_dir()))
        for im in images:
            mime = im.get("mime", "")
            name = im.get("name") or "file"
            path = im.get("path") or ""
            resolved = os.path.realpath(path)
            in_workspace = resolved == root or resolved.startswith(root + os.sep)
            in_store = (
                resolved == attachments_root
                or resolved.startswith(attachments_root + os.sep)
            )
            if not (in_workspace or in_store):
                # No path in the message: it reaches logs and every attached
                # browser, and Phase 4 forbids native paths in both.
                raise CodexAttachmentRejected(
                    "the attachment is outside this session's working directory"
                )
            if mime in _IMAGE_MIMES:
                send_path = resolved if in_workspace else self._stage_in_workspace(
                    resolved, root
                )
                inputs.append(schema.LocalImageUserInput(path=send_path))
            elif mime in _TEXT_INLINE_MIMES:
                data = Path(resolved).read_bytes()
                inputs.append(
                    schema.TextUserInput(text=data.decode("utf-8", errors="replace"))
                )
            elif mime == _ZIP_MIME:
                data = Path(resolved).read_bytes()
                inputs.append(schema.TextUserInput(
                    text=f"[Attached: {name} (zip archive, {len(data)} bytes)]"
                ))
            else:
                raise CodexAttachmentRejected(
                    "Codex sessions accept images, text/CSV/JSON files and zip "
                    "archives only; this file was not sent."
                )
        return tuple(inputs)

    def _stage_in_workspace(self, source: str, root: str) -> str:
        """Copy an attachment from the global store into a workspace-local
        staging dir so it satisfies Codex's own realpath(cwd) containment.

        Content-addressed by sha256: resending the same image across turns (or
        across a queued retry) reuses one on-disk file rather than growing the
        staging dir unbounded.
        """
        data = Path(source).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        ext = Path(source).suffix
        staging_dir = Path(root) / _STAGING_DIRNAME
        staging_dir.mkdir(parents=True, exist_ok=True)
        dest = staging_dir / f"{digest}{ext}"
        if not dest.exists():
            tmp = dest.with_name(f"{dest.name}.{os.getpid()}.tmp")
            tmp.write_bytes(data)
            tmp.replace(dest)
        if not self._staging_ignored:
            _ensure_staging_ignored(root)
            self._staging_ignored = True
        return str(dest)

    # -- model / effort / permission mode ----------------------------------

    async def set_model(self, model: str | None) -> None:
        """Switch models. Mid-turn this retargets the live turn; otherwise it is
        stored for the next one. ``None``/``"default"`` means "account default"
        and is stored as ``None`` so the app-server picks."""
        self._model = _normalize_model(model)
        if not self._turn_active or self._turn_id is None or self._model is None:
            return
        with contextlib.suppress(Exception):
            # A `targetUnavailable` answer (or a failure) is not an error: the
            # stored model still applies from the next turn.
            await self._client().update_turn_settings(
                schema.TurnSettingsUpdateParams(
                    thread_id=self._require_thread(),
                    turn_id=self._turn_id,
                    model=self._model,
                    effort=self._effort,
                )
            )

    async def set_permission_mode(self, mode: str) -> None:
        """Apply a ccswitch permission mode, or refuse it.

        This entry point is unvalidated end to end — the web plan toggle sends
        ``plan``/``default`` directly, and the REST route does not check the
        value — so an arbitrary string genuinely arrives here. An unmapped one
        is refused and surfaced, keeping the profile the session already has,
        because the alternative (a default) is how a session ends up with more
        authority than its mode name promises.
        """
        if self._assistant is not None:
            # The assistant overlay pins `read-only` + `untrusted` for the life of
            # the thread. Honouring a mode change would mean either widening the
            # sandbox (the boundary the built-ins are held by) or re-resuming to
            # apply it — and a resume drops `dynamicTools`. Both roads end at a
            # drawer with more authority and fewer tools, so this is a no-op.
            log_event(
                logger,
                logging.INFO,
                "codex.driver.assistant_mode_pinned",
                session=self.session_id,
            )
            return
        resolved = mode
        if mode == "plan" and self.mode != "plan":
            self._pre_plan_mode = self.mode
        elif mode == "default" and self.mode == "plan":
            resolved = self._pre_plan_mode or "default"
            self._pre_plan_mode = None
        try:
            profile = profile_for_mode(resolved)
        except UnsupportedCodexPermissionMode as exc:
            log_event(
                logger,
                logging.WARNING,
                "codex.driver.permission_mode_refused",
                session=self.session_id,
            )
            await self.emit_error(
                _MODE_REFUSED_CODE,
                f"Codex has no sandbox profile for permission mode {exc.mode!r}; "
                f"the session kept its current mode ({self.mode}).",
            )
            return
        if profile.sandbox_mode == self._profile.sandbox_mode:
            # Only the approval policy moved, and that rides the next
            # `turn/start` — no thread-level change is needed.
            self.mode = resolved
            self._profile = profile
            await self._emit_status()
            return
        # The sandbox is fixed for the life of a thread (see the `TurnStartParams`
        # docstring in schema.py), so a sandbox change means re-resuming. Mid-turn
        # that would move the ground under work already running: park it for the
        # next boundary. The mode is adopted only once the sandbox really changed,
        # so the approval policy can never take effect under the old sandbox.
        if self._turn_active:
            self._pending_profile = (resolved, profile)
            return
        await self._apply_profile(resolved, profile)

    async def _apply_pending_profile(self) -> None:
        pending, self._pending_profile = self._pending_profile, None
        if pending is not None:
            await self._apply_profile(*pending)

    async def _apply_profile(self, mode: str, profile: CodexPermissionProfile) -> bool:
        """Re-resume the thread under *profile*'s sandbox; refuse on failure."""
        thread_id = self._thread_id
        if thread_id is None:
            return False
        try:
            await self._client().resume_thread(
                schema.ThreadResumeParams(
                    thread_id=thread_id,
                    cwd=self.cwd,
                    model=self._model,
                    sandbox=profile.sandbox_mode.value,
                    approval_policy=profile.approval_policy.value,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- refuse rather than widen
            log_event(
                logger,
                logging.ERROR,
                "codex.driver.sandbox_change_refused",
                session=self.session_id,
                error=scrub_exception(exc),
            )
            await self.emit_error(_SANDBOX_REFUSED_CODE, _SANDBOX_REFUSED_MESSAGE)
            return False
        self.mode = mode
        self._profile = profile
        log_event(
            logger,
            logging.INFO,
            "codex.driver.sandbox_changed",
            session=self.session_id,
            sandbox=profile.sandbox_mode.value,
        )
        await self._emit_status()
        return True

    # -- approvals ---------------------------------------------------------

    async def _on_approval(
        self, method: str, params: schema.ApprovalParams
    ) -> schema.ApprovalResponse:
        """Turn one app-server approval request into a ccswitch permission card.

        Every refusal path answers ``decline``: this coroutine is the app-server's
        JSON-RPC reply, and the one outcome that must be unreachable by accident
        is an accept.
        """
        if self._assistant is not None:
            # An assistant thread declares no built-in it may use, so every
            # approval it raises is the model reaching outside its surface — a
            # prompt-injection surface, not a user decision. Declined here without
            # ever touching the permission registry, mirroring
            # `ClaudeAgentDriver._can_use_tool`'s hard deny for a
            # non-`mcp__ccswitch__` tool. A card would be worse than useless: it
            # would invite an operator to grant an authority the drawer's whole
            # design says it does not have.
            self.counters.assistant_builtins_declined += 1
            log_event(
                logger,
                logging.WARNING,
                "codex.driver.assistant_builtin_declined",
                session=self.session_id,
                method=method,
            )
            return assistant_approval_decision(method)
        thread_id = self._thread_id
        generation = self._manager.generation_for(self.account)
        if (
            self._closing
            or thread_id is None
            or params.thread_id != thread_id
            or generation is None
            or generation != self._generation
        ):
            self.counters.stale_approvals += 1
            log_event(
                logger,
                logging.WARNING,
                "codex.driver.approval_refused",
                session=self.session_id,
                reason="stale_request",
            )
            return _decline(method)
        tool, tool_input = _approval_shape(params)
        perm_id, future = await self._reg.request_external(
            self.session_id,
            params.turn_id,
            tool,
            tool_input,
            identity=self.identity,
        )
        self._pending_approvals[perm_id] = _PendingApproval(
            method=method,
            thread_id=thread_id,
            item_id=params.item_id,
            generation=generation,
            turn_id=params.turn_id,
        )
        self._emit(
            ev.PermissionRequest(
                perm_id,
                params.turn_id,
                tool,
                ev.tool_kind(tool),
                tool_input,
            )
        )
        await self._emit_status()
        try:
            decision = await future
        finally:
            pending = self._pending_approvals.pop(perm_id, None)
        allowed = isinstance(decision, str) and decision in ("allow", "always")
        # Re-check the triple: a human can take minutes, and the app-server that
        # asked may have been replaced in the meantime. Answering the NEW process
        # with a decision made about the OLD one's request is exactly the
        # confused-deputy shape the generation fence exists to prevent.
        if allowed and not self._approval_still_valid(pending):
            self.counters.stale_approvals += 1
            allowed = False
            decision = "deny"
            log_event(
                logger,
                logging.WARNING,
                "codex.driver.approval_refused",
                session=self.session_id,
                reason="generation_changed",
            )
        self._emit(ev.PermissionResolved(perm_id, params.turn_id, allowed))
        await self._emit_status()
        if decision == "always":
            # The engine's OWN session-scoped accept — never a ccswitch grant
            # (``request_external`` writes none) and never a persisted global
            # exception.
            return _response(method, "acceptForSession")
        return _response(method, "accept") if allowed else _decline(method)

    def _approval_still_valid(self, pending: _PendingApproval | None) -> bool:
        if pending is None or self._closing:
            return False
        if pending.thread_id != self._thread_id:
            return False
        return self._manager.generation_for(self.account) == pending.generation

    def resolve_permission(self, perm_id: str, decision: str, identity: str) -> bool:
        return self._reg.resolve(perm_id, decision, identity)

    # -- client-declared tools (assistant threads + coding-session tools) ----

    async def _on_dynamic_tool(
        self, params: schema.DynamicToolCallParams
    ) -> schema.DynamicToolCallResponse:
        """Run one ccswitch tool this thread declared as a `dynamicTool`.

        Serves BOTH surfaces — the assistant drawer's tools and a coding session's
        (``server/sessions/tools/codex.py``) — with no branch between them: they are
        never mounted together, and each answers the same three questions
        (``declares``, ``mcp_name``, ``run_tool``). One gate, two surfaces, rather
        than a second copy of this policy per surface.

        This is the Codex half of that tool surface, and it is
        deliberately the same *decision* as the Claude half rather than a
        parallel one: the call is gated through
        ``PermissionRegistry.request`` under the canonical
        ``mcp__viberpit__<tool>`` name, so a read auto-allows, a mutation offers a
        card that can be remembered, and a destructive tool always prompts and
        never offers "always allow" — one policy, two engines.

        Nothing here can execute a built-in: this handler only ever sees a tool
        the overlay declared, and `run_tool` refuses a name that is not in its
        table.
        """
        overlay: CodexAssistantOverlay | CodexSessionTools | None = (
            self._assistant or self._session_tools
        )
        thread_id = self._thread_id
        generation = self._manager.generation_for(self.account)
        if (
            overlay is None
            or self._closing
            or thread_id is None
            or params.thread_id != thread_id
            or generation is None
            or generation != self._generation
        ):
            self.counters.stale_dynamic_tools += 1
            log_event(
                logger,
                logging.WARNING,
                "codex.driver.dynamic_tool_refused",
                session=self.session_id,
                reason="stale_request",
            )
            return tool_error(
                f"{brand.NAME} tool error: this session is no longer accepting tool calls"
            )
        if not overlay.declares(params.tool):
            # Refused BEFORE the permission gate, not after. An undeclared name
            # can never execute, and the registry's `mcp__ccswitch__` branch
            # treats an unannotated tool conservatively as mutation-like — so
            # letting it through would raise a confirmation card for a tool that
            # does not exist, and park the turn until a human answered it.
            log_event(
                logger,
                logging.WARNING,
                "codex.driver.dynamic_tool_refused",
                session=self.session_id,
                reason="undeclared_tool",
            )
            return await overlay.run_tool(params.tool, params.arguments)
        tool = overlay.mcp_name(params.tool)
        arguments = (
            dict(params.arguments) if isinstance(params.arguments, Mapping) else {}
        )
        perm_id, future, always_allowed = await self._reg.request(
            self.session_id,
            params.turn_id,
            tool,
            arguments,
            mode=self.mode,
            denylist=(),
            allowlist=True,
            identity=self.identity,
        )
        if perm_id is not None:
            self._emit(
                ev.PermissionRequest(
                    perm_id,
                    params.turn_id,
                    tool,
                    ev.tool_kind(tool),
                    arguments,
                    always_allowed=always_allowed,
                )
            )
            await self._emit_status()
        allowed = await future
        if perm_id is not None:
            self._emit(ev.PermissionResolved(perm_id, params.turn_id, allowed))
            await self._emit_status()
        if not allowed:
            return tool_error("denied by user")
        # Re-check the fence after the wait, exactly as `_on_approval` does: a
        # human can take minutes, and running a mutation against an app-server
        # that was replaced in the meantime is the same confused-deputy shape.
        if (
            self._closing
            or self._thread_id != thread_id
            or self._manager.generation_for(self.account) != generation
        ):
            self.counters.stale_dynamic_tools += 1
            log_event(
                logger,
                logging.WARNING,
                "codex.driver.dynamic_tool_refused",
                session=self.session_id,
                reason="generation_changed",
            )
            return tool_error(
                "ccswitch tool error: the session was replaced before this tool ran"
            )
        return await overlay.run_tool(params.tool, arguments)

    # -- structured questions ------------------------------------------------

    async def _on_user_input(
        self, params: schema.ToolRequestUserInputParams
    ) -> schema.ToolRequestUserInputResponse:
        """Turn one `item/tool/requestUserInput` call into ccswitch question
        card(s) and await the human's answer(s) — Codex's `AskUserQuestion`
        equivalent, reusing the SAME ``QuestionRegistry``/``ev.Question`` the
        Claude driver uses rather than a parallel question stack.

        The app-server raises one request carrying a *batch* of questions and
        expects one reply carrying a batch of answers, so all of a call's
        questions share one fencing check and resolve into one JSON-RPC
        result — never several partial replies.
        """
        thread_id = self._thread_id
        generation = self._manager.generation_for(self.account)
        if (
            self._closing
            or thread_id is None
            or params.thread_id != thread_id
            or generation is None
            or generation != self._generation
        ):
            self.counters.stale_user_input += 1
            log_event(
                logger,
                logging.WARNING,
                "codex.driver.user_input_refused",
                session=self.session_id,
                reason="stale_request",
            )
            return schema.ToolRequestUserInputResponse(
                answers={
                    question.id: schema.ToolRequestUserInputAnswer(answers=())
                    for question in params.questions
                }
            )
        pending: list[tuple[schema.ToolRequestUserInputQuestion, str, asyncio.Future]] = []
        for question in params.questions:
            options = [
                {"label": option.label, "description": option.description}
                for option in (question.options or [])
            ]
            qid, fut = self._qreg.request(
                self.session_id,
                params.turn_id,
                question.header,
                question.question,
                options,
                False,  # Codex offers no multi-select variant of this question.
                self.identity,
            )
            self._pending_user_input[qid] = _PendingUserInput(
                thread_id=thread_id,
                item_id=params.item_id,
                generation=generation,
                turn_id=params.turn_id,
            )
            self._emit(
                ev.Question(
                    question_id=qid,
                    turn_id=params.turn_id,
                    header=question.header,
                    question=question.question,
                    options=options,
                    multi_select=False,
                )
            )
            pending.append((question, qid, fut))
        # A question is now pending for the user → waiting_question.
        await self._emit_status()
        answers = await asyncio.gather(*(fut for _, _, fut in pending))
        # Question(s) resolved → back to in_progress.
        await self._emit_status()
        result: dict[str, schema.ToolRequestUserInputAnswer] = {}
        for (question, qid, _), answer in zip(pending, answers):
            record = self._pending_user_input.pop(qid, None)
            # Re-check validity BEFORE emitting or replying: a human can take
            # minutes, and the app-server that asked may have been replaced
            # meanwhile (same re-check `_on_approval` makes via
            # `_approval_still_valid`). Downgrade to the dismissed shape here
            # so the emitted event and the wire reply always agree — neither
            # may report a selection the new process never asked for.
            if answer is not None and not self._user_input_still_valid(record):
                self.counters.stale_user_input += 1
                answer = None
            self._emit(
                ev.QuestionResolved(
                    question_id=qid,
                    turn_id=params.turn_id,
                    selected=None if answer is None else answer[0],
                    mode="dismissed" if answer is None else answer[1],
                )
            )
            if answer is None:
                result[question.id] = schema.ToolRequestUserInputAnswer(answers=())
                continue
            selected, _mode = answer
            result[question.id] = schema.ToolRequestUserInputAnswer(
                answers=tuple(selected)
            )
        return schema.ToolRequestUserInputResponse(answers=result)

    def _user_input_still_valid(self, pending: _PendingUserInput | None) -> bool:
        if pending is None or self._closing:
            return False
        if pending.thread_id != self._thread_id:
            return False
        return self._manager.generation_for(self.account) == pending.generation

    def _dismiss_pending_approvals(self) -> None:
        """Settle every pending prompt so no app-server request is stranded.

        Called on every exit path — stop, crash, auth loss, interrupt, archive —
        because each pending prompt is an app-server request blocked on this
        driver's answer, and a JSON-RPC request nobody answers holds the turn
        that raised it open forever.
        """
        pending = self._reg.pending(self.session_id)
        self._reg.deny_all(self.session_id)
        for record in pending:
            # Emitted HERE rather than left to the waiting `_on_approval`: that
            # coroutine resumes on a later tick, and on the teardown paths the
            # stream is closed before then, so the card would never be cleared
            # in an attached client. The waiter emits its own copy when it does
            # resume; a repeated resolution is idempotent, an unresolved card is
            # a permission prompt that never goes away.
            self._emit(
                ev.PermissionResolved(record["permId"], record.get("turn_id") or "", False)
            )
        self._pending_approvals.clear()

    def _dismiss_pending_questions(self) -> None:
        """Settle every pending `request_user_input` question so no app-server
        request is stranded. Same rationale and call sites as
        :meth:`_dismiss_pending_approvals` — see there."""
        pending = self._qreg.pending(self.session_id)
        self._qreg.deny_all(self.session_id)
        for record in pending:
            # Emitted HERE for the same reason `_dismiss_pending_approvals` emits
            # its own copy rather than leaving it to the waiter: on a teardown
            # path the stream closes before the awaiting `_on_user_input` coroutine
            # resumes, and an unresolved card is a question that never goes away.
            self._emit(
                ev.QuestionResolved(
                    question_id=record["question_id"],
                    turn_id=record["turn_id"],
                    selected=None,
                    mode="dismissed",
                )
            )
        self._pending_user_input.clear()

    def _dismiss_pending_prompts(self) -> None:
        """Settle every pending approval AND question — the one call every exit
        path (stop, crash, auth loss, interrupt, archive) makes, so a new prompt
        kind added later has exactly one place to be added to."""
        self._dismiss_pending_approvals()
        self._dismiss_pending_questions()

    # -- inbound streams ---------------------------------------------------

    def _on_event(self, method: str, params: schema.ServerNotificationParams) -> None:
        """Manager-side sink: queue only. Synchronous by contract, and the pump
        is where anything that can await happens."""
        self._inbox.put_nowait(_Notification(method=method, params=params))

    def _on_notice(self, notice: RunnerNotice) -> None:
        self._inbox.put_nowait(_Notice(notice=notice))

    async def _pump_loop(self) -> None:
        """Serialize every inbound frame into the turn state machine.

        A handler failure is counted and dropped rather than allowed to end the
        pump: a session that stops reading its app-server is deaf for the rest
        of its life, which is far worse than one mishandled frame.
        """
        while True:
            item = await self._inbox.get()
            try:
                if isinstance(item, _Notification):
                    await self._handle_notification(item)
                else:
                    await self._handle_notice(item.notice)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- one bad frame, not the session
                self.counters.pump_errors += 1
                log_event(
                    logger,
                    logging.ERROR,
                    "codex.driver.pump_error",
                    session=self.session_id,
                    error=scrub_exception(exc),
                )

    async def _handle_notification(self, item: _Notification) -> None:
        if item.method not in TRANSLATED_NOTIFICATIONS:
            await self._handle_untranslated(item)
            return
        for event in self._translator.translate(item.method, item.params):
            await self._consume(event)

    async def _handle_untranslated(self, item: _Notification) -> None:
        """Notifications the translator does not own: this driver's own business.

        Only `error` is actionable — the account-scoped ones describe the shared
        app-server's login state, which the manager and the authority own.
        """
        self.counters.untranslated_notifications += 1
        params = item.params
        if not isinstance(params, schema.ErrorNotificationParams):
            return
        category = params.codex_error_info
        await self.emit_error(category or _ERROR_CODE, _ERROR_MESSAGE)
        kind = _RECOVERABLE_ERROR_KINDS.get(category or "")
        if kind is None:
            return
        if kind == "auth_401":
            # Credentials are gone: a human's answer can no longer be honoured,
            # so nothing may sit waiting on one.
            self._dismiss_pending_prompts()
        self._emit(ev.TurnFailure(kind=kind, account=self.account))
        await self._signal_recoverable(kind)

    async def _consume(self, event: object) -> None:
        """Apply one translated event to the turn state machine, then fan it out."""
        self._note_progress(event)
        if isinstance(event, ev.TurnStart):
            if self._turn_id is None:
                self._turn_id = event.turn_id
            self._turn_active = True
            if self._turn_started_at is None:
                self._turn_started_at = self._clock()
            self._emit(event)
            await self._emit_status()
            return
        if isinstance(event, ev.TurnEnd):
            await self._end_turn(event)
            return
        if isinstance(event, ev.UserMessage):
            # The driver already echoed this prompt in `_start_turn`/`_steer`,
            # with the attachments and route chip the native `userMessage` item
            # cannot carry. Forwarding the native one too would show every
            # prompt twice.
            self.counters.dropped_user_echoes += 1
            return
        if isinstance(event, ev.Status):
            # `thread/status/changed` is a SIGNAL, not a status: ccswitch's
            # vocabulary is richer (waiting_input / needs_attention / …) and is
            # derived in one place. Re-deriving keeps a single writer for the pill.
            await self._emit_status()
            return
        if isinstance(event, ev.Condensing):
            self._condensing = event.active
            self._emit(event)
            await self._emit_status()
            return
        if isinstance(event, ev.ContextUsage):
            self._context_usage = {
                "totalTokens": event.used_tokens,
                "maxTokens": event.max_tokens,
                "percentage": event.percentage,
                "model": event.model,
            }
            self._emit(event)
            if self._on_context_usage is not None:
                with contextlib.suppress(Exception):
                    await self._on_context_usage(
                        self.session_id, event.used_tokens, event.max_tokens
                    )
            return
        self._emit(event)

    async def _end_turn(self, event: ev.TurnEnd) -> None:
        """Close the turn the app-server says ended, honouring the crash fence."""
        if self._turn_id is None or event.turn_id != self._turn_id:
            # A late completion for a turn already interrupted, or one belonging
            # to a previous connection. Forward it so the client can settle that
            # turn's card, but never touch the live turn: an interrupt clears
            # `_turn_id` and may already have started the NEXT turn, and letting
            # the old turn's completion end that one would kill a running turn.
            self._emit(event)
            return
        outcome = event.outcome
        if self._finish_turn() is TurnOutcome.INDETERMINATE:
            # The process that was running this turn is gone; whether the model
            # finished is unknowable, so it is never reported as a success.
            outcome = "interrupted"
            self._last_turn_outcome = "failed"
        else:
            self._last_turn_outcome = "failed" if outcome == "error" else outcome
        self._turns_completed += 1
        self._turn_active = False
        self._turn_started_at = None
        self._turn_id = None
        self._condensing = False
        self._emit(dataclasses.replace(event, outcome=outcome))
        await self._emit_status()
        await self._drain_queue()

    async def _handle_notice(self, notice: RunnerNotice) -> None:
        """React to one supervision verdict about this thread's app-server."""
        if notice.kind is RunnerNoticeKind.UNRECOVERABLE:
            self._dismiss_pending_prompts()
            await self._abandon_turn(_UNRECOVERABLE_CODE, _UNRECOVERABLE_MESSAGE)
            await self._signal_recoverable("process_crash")
            return
        if notice.kind is RunnerNoticeKind.CRASHED:
            self._dismiss_pending_prompts()
            await self._abandon_turn(_CRASH_CODE, _CRASH_MESSAGE)
            return
        # RESTARTED: a fresh process with a fresh generation is serving this
        # account. Every id the previous connection streamed is history.
        self._generation = notice.generation
        self._translator.reset()
        if notice.recover_via == RECOVER_VIA_THREAD_READ:
            await self._resynchronize()

    async def _abandon_turn(self, code: str, message: str) -> None:
        """End an in-flight turn as interrupted — never as a success."""
        await self.emit_error(code, message)
        turn_id = self._turn_id
        self._finish_turn()
        if turn_id is None or not self._turn_active:
            return
        self._turn_active = False
        self._turn_started_at = None
        self._turn_id = None
        self._condensing = False
        self._turns_completed += 1
        self._last_turn_outcome = "failed"
        self._emit(ev.TurnEnd(turn_id, outcome="interrupted"))
        await self._emit_status()

    async def _resynchronize(self) -> None:
        """Confirm the thread survived the restart, and re-read its state.

        Deliberately NOT a transcript replay: every item of this thread has
        already been delivered to the attached clients, and re-emitting them
        would double the whole conversation. What a restart actually invalidates
        is our knowledge of whether the thread still exists — a thread the new
        app-server cannot load is a dead session, and the operator must be told
        rather than left with a prompt box that silently fails.
        """
        thread_id = self._thread_id
        if thread_id is None:
            return
        try:
            await self._client().read_thread(
                schema.ThreadReadParams(thread_id=thread_id, include_turns=False)
            )
        except Exception as exc:  # noqa: BLE001 -- one typed surface
            log_event(
                logger,
                logging.ERROR,
                "codex.driver.resync_failed",
                session=self.session_id,
                error=scrub_exception(exc),
            )
            await self.emit_error(_RESYNC_CODE, _RESYNC_MESSAGE)
            await self._signal_recoverable("process_crash")
            return
        log_event(
            logger, logging.INFO, "codex.driver.resynchronized",
            session=self.session_id,
        )

    async def _signal_recoverable(self, kind: str) -> None:
        if self._on_recoverable_failure is None:
            return
        try:
            await self._on_recoverable_failure(self.session_id, kind, None)
        except Exception:  # noqa: BLE001 -- recovery signalling is best-effort
            logger.debug("codex: recoverable-failure callback failed", exc_info=True)

    # -- fan-out / status --------------------------------------------------

    def _note_progress(self, event: object) -> None:
        if isinstance(event, (ev.TextDelta, ev.ThinkingDelta)):
            if event.text:
                self._last_progress_at = self._clock()
        elif isinstance(
            event, (ev.ToolCall, ev.ToolResult, ev.TurnEnd, ev.PermissionRequest)
        ):
            self._last_progress_at = self._clock()

    def _buffer_event(self, event: object) -> None:
        """Append to the in-progress-turn buffer, coalescing consecutive deltas
        of the same block so a long answer costs one entry, not thousands."""
        if isinstance(event, (ev.TextDelta, ev.ThinkingDelta)) and self._turn_buffer:
            last = self._turn_buffer[-1]
            if (
                type(last) is type(event)
                and last.turn_id == event.turn_id
                and last.src_uuid == event.src_uuid
                and last.parent_tool_use_id == event.parent_tool_use_id
            ):
                self._turn_buffer[-1] = dataclasses.replace(
                    last, text=last.text + event.text
                )
                return
        self._turn_buffer.append(event)

    def _emit(self, event: object) -> None:
        if isinstance(event, _TURN_CONTENT):
            self._buffer_event(event)
        for q in self._subscribers:
            q.put_nowait(event)

    def emit(self, event: object) -> None:
        """Public out-of-band emit (e.g. an orchestrator-side rename → Title)."""
        self._emit(event)

    async def emit_error(self, code: str, message: str) -> None:
        self._emit(ev.ErrorEvent(code=code, message=message, turn_id=self._turn_id))
        await self._emit_status()

    def _dash_signals(self) -> StatusSignals:
        return StatusSignals(
            turn_active=self._turn_active,
            pending_permission=bool(self._reg.pending(self.session_id)),
            pending_question=bool(self._qreg.pending(self.session_id)),
            # Codex has no plan-approval tool, so this signal is structurally
            # false rather than unimplemented.
            pending_plan=False,
            turns_completed=self._turns_completed,
            last_turn_outcome=self._last_turn_outcome,
            condensing=self._condensing,
        )

    def current_status(self) -> str:
        """The rich derived status right now (no side effects). Used on WS attach.

        Honors the last ``on_status`` overlay / persisted seed while the raw
        derivation still equals the base that produced it — see
        ``ClaudeAgentDriver.current_status`` for the shared rationale."""
        status = derive_status(self._dash_signals())
        if self._status_overlay is not None and self._status_overlay[0] == status:
            return self._status_overlay[1]
        return status

    def seed_status(self, persisted: str) -> None:
        """Adopt the orchestrator's persisted status as this driver's snapshot.

        Mirrors ``ClaudeAgentDriver.seed_status`` — see there for the rationale.
        Only resting hand-back statuses (``SEEDABLE_STATUSES``) are adopted."""
        if persisted not in SEEDABLE_STATUSES:
            return
        base = derive_status(self._dash_signals())
        if base != persisted:
            self._status_overlay = (base, persisted)

    async def _emit_status(self, count: int | None = None) -> None:
        status = derive_status(self._dash_signals())
        final = status
        if self._on_status is not None:
            try:
                ret = await self._on_status(self.session_id, status)
                if isinstance(ret, str) and ret:
                    final = ret
            except Exception:  # noqa: BLE001 -- a status sink must not break the session
                logger.debug("codex: status callback failed", exc_info=True)
        self._status_overlay = (status, final) if final != status else None
        self._emit(
            ev.Status(final, count=len(self._queue) if count is None else count)
        )

    def open_stream(self) -> DriverStream:
        """Open an attach stream ATOMICALLY — no await anywhere in this method,
        so the snapshot, the subscriber queue and the turn flags all describe
        the same instant (see ``DriverStream``)."""
        snapshot = list(self._turn_buffer)
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return DriverStream(self, snapshot, q, self._turn_active, self._turn_started_at)

    async def events(self) -> AsyncIterator[object]:
        """Single-shot subscribe — a thin delegate over :meth:`open_stream`."""
        stream = self.open_stream()
        try:
            async for event in stream.events():
                yield event
        finally:
            stream.close()

    # -- queue -------------------------------------------------------------

    def queued(self) -> list[str]:
        return [entry.text for entry in self._queue]

    def queue_wire(self) -> list[dict]:
        return [
            {
                "text": entry.text,
                "images": [
                    {"url": im.get("url", ""), "mime": im.get("mime", ""),
                     "name": im.get("name", "")}
                    for im in entry.images
                ],
            }
            for entry in self._queue
        ]

    async def cancel_queued(self, index: int) -> bool:
        if index < 0 or index >= len(self._queue):
            return False
        self._queue.pop(index)
        self._emit(ev.QueueUpdate(self.queue_wire()))
        await self._emit_status()
        return True

    def answer_question(self, question_id: str, selected: list[str], identity: str,
                        mode: str = "option") -> bool:
        """Resolve a pending `request_user_input` question. Mirrors
        ``ClaudeAgentDriver.answer_question`` exactly, including the
        session-scoped reconciliation for a replay-derived id on reattach."""
        return self._qreg.resolve(question_id, selected, identity, mode,
                                  session_id=self.session_id)

    def pending_questions(self) -> list[dict]:
        """Still-pending question payloads for this session. Mirrors
        ``ClaudeAgentDriver.pending_questions`` — used by the WS bridge to
        re-emit question events on client reattach."""
        return self._qreg.pending(self.session_id)

    # -- surfaces Codex does not have --------------------------------------

    def seed_idle_suggestions(self, user_text: str, assistant_text: str) -> None:
        """No-op: follow-up suggestions are generated by a Claude sub-session the
        Codex path does not build, so there is nothing to seed."""
        return None

    # -- vendor command catalog --------------------------------------------

    async def server_info(self) -> dict | None:
        """This engine's own catalog of invocable actions, in the shared shape.

        `server_info` is the engine-neutral "what do you publish?" hook that
        ``server/sessions/commands/catalog.py`` normalizes; the Claude driver
        answers it from the CLI's `get_server_info`, and this one answers it from
        `skills/list`. It used to return None, which left a Codex palette holding
        only ccswitch's own commands and no way to tell "this engine publishes
        nothing" from "we never asked" (#1286).

        Nothing here is synthesized: every entry is a skill the app-server just
        reported, at a path it resolved, and each one is dispatchable through the
        `SkillUserInput` variant of `turn/start`. A read failure returns None
        rather than an empty list — the orchestrator falls back to the last-known
        catalog for None, which is the honest answer for "could not ask".
        """
        try:
            response = await self._client().list_skills(
                schema.SkillsListParams(cwds=(self.cwd,) if self.cwd else ())
            )
        except Exception as exc:  # noqa: BLE001 -- any failure means "no answer"
            log_event(
                logger,
                logging.WARNING,
                "codex.driver.skills_list_failed",
                session=self.session_id,
                error=scrub_exception(exc),
            )
            return None
        skills = response.enabled_skills()
        # Scan errors are reported, not rendered: a skill directory that failed
        # to parse is why a name the user expects is missing, and silently
        # serving the shorter list is how that becomes unexplainable.
        errors = sum(len(entry.errors) for entry in response.data)
        if errors:
            log_event(
                logger,
                logging.INFO,
                "codex.driver.skills_list_errors",
                session=self.session_id,
                errors=errors,
                skills=len(skills),
            )
        self._skill_paths = {skill.name: skill.path for skill in skills}
        return {
            "commands": [
                {"name": skill.name, "description": skill.summary}
                for skill in skills
            ]
        }

    async def send_skill(self, name: str, args: str = "") -> None:
        """Run one published skill natively, optionally with extra instructions.

        The skill is sent as a `SkillUserInput`, so the app-server runs the entry
        it published rather than being shown a `/name` token it does not parse.
        ``args`` rides along as an ordinary text input in the SAME turn, which is
        how a skill invocation carries "…and here is what to apply it to".

        Raises ``KeyError`` for a name this driver never published: the catalog
        the caller looked the name up in came from `server_info` above, so a miss
        means the skill was removed between the two calls, and a turn started on
        a guessed path would run the wrong thing.
        """
        path = self._skill_paths.get(name)
        if path is None:
            raise KeyError(name)
        inputs: tuple[schema.UserInput, ...] = (
            schema.SkillUserInput(name=name, path=path),
        )
        if args:
            inputs = (*inputs, schema.TextUserInput(text=args))
        entry = _QueuedPrompt(text=f"/{name}" + (f" {args}" if args else ""))
        if self._thread_id is None:
            raise CodexThreadNotStarted(
                "CodexAgentDriver.send_skill called before start()"
            )
        if self._turn_active and self._turn_id is not None:
            await self._steer(entry, inputs)
            return
        await self._start_turn(entry, inputs)

    async def context_usage(self) -> dict:
        """The last `thread/tokenUsage/updated` reading, in the same shape the
        Claude driver returns, or ``{}`` before the first one arrives."""
        return dict(self._context_usage)

    async def open_background_work(self) -> list[OpenBgTask]:
        """This thread's live background terminals, as engine-neutral open tasks.

        The Codex half of the engine-neutral "is this session parked on work of its
        own?" question (#1284); the Claude half is a transcript scan, which is why
        the orchestrator asks each engine its own way and both answers land in the
        same ``OpenBgTask`` vocabulary.

        Every entry comes back ``replayable=False``. That is not caution, it is the
        contract: these processes die with the app-server and nothing in the pinned
        protocol can start one again (``server/codex/background.py``).

        Raises rather than degrading to ``[]``. "Could not ask the app-server" and
        "nothing is running" are different facts, and collapsing them here would hide
        a broken runner behind a plausible empty list. The orchestrator does choose to
        degrade — but it logs the failure first, which it can only do if it sees one.
        """
        thread_id = self._require_thread()
        terminals = await background.open_background_terminals(
            self._client(), thread_id
        )
        return [background.to_open_task(terminal) for terminal in terminals]

    # -- helpers -----------------------------------------------------------

    def _require_thread(self) -> str:
        if self._thread_id is None:
            raise CodexThreadNotStarted("this codex driver has no thread")
        return self._thread_id


def _ensure_staging_ignored(root: str) -> None:
    """Best-effort: hide `_STAGING_DIRNAME` from `git status` for *root*.

    Written to `.git/info/exclude` (the common git dir, shared by every linked
    worktree of the same repo) rather than the repo's tracked `.gitignore` —
    editing a tracked file would itself show up as a diff in the user's repo.
    Never raises: a session's cwd is not always a git repo (scratch sessions
    have none at all), and a failure here must not block sending the
    attachment. Worst case the staging dir shows up as untracked, which
    `worktrees.remove_worktree`'s dirty guard already treats conservatively
    (kept, not destroyed) rather than unsafely.
    """
    try:
        common = subprocess.run(
            ["git", "-C", root, "rev-parse", "--path-format=absolute",
             "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        )
        if common.returncode != 0:
            return
        exclude_path = Path(common.stdout.strip()) / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        line = f"/{_STAGING_DIRNAME}/"
        existing = exclude_path.read_text() if exclude_path.exists() else ""
        if line in existing.splitlines():
            return
        with exclude_path.open("a") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(line + "\n")
    except (OSError, subprocess.SubprocessError):
        pass


def _normalize_model(model: str | None) -> str | None:
    """``None``/empty/``"default"`` all mean "let the account default win"."""
    if model is None:
        return None
    stripped = model.strip()
    if not stripped or stripped == "default":
        return None
    return stripped


def _approval_shape(params: schema.ApprovalParams) -> tuple[str, dict[str, Any]]:
    """The permission card for one approval request.

    The tool NAMES are the ones ``translator._tool_shape`` already uses, so the
    approval card and the tool card of the same action render identically. The
    input carries only what the request itself states — never the native item id,
    which belongs in the correlation fields.
    """
    if isinstance(params, schema.CommandExecutionApprovalParams):
        shape: dict[str, Any] = {"command": params.command or "", "cwd": params.cwd or ""}
        if params.reason:
            shape["reason"] = params.reason
        return "Bash", shape
    change: dict[str, Any] = {}
    if params.grant_root:
        change["grant_root"] = params.grant_root
    if params.reason:
        change["reason"] = params.reason
    return "Edit", change


def _response(method: str, decision: str) -> schema.ApprovalResponse:
    if method == schema.METHOD_COMMAND_EXECUTION_APPROVAL:
        return schema.CommandExecutionApprovalResponse(decision=decision)
    return schema.FileChangeApprovalResponse(decision=decision)


def _decline(method: str) -> schema.ApprovalResponse:
    return _response(method, "decline")


class CodexAgentDriverFactory(AgentDriverFactory):
    """Build Codex drivers while keeping the runner manager below the engine seam.

    ``capabilities`` is republished from the driver class (never restated) so
    orchestration can ask what this engine supports before a driver exists — see
    ``AgentDriverRegistry.capabilities_for``.

    ``model_for_session`` mirrors ``ClaudeAgentDriverFactory``'s
    ``sdk_factory_for_session``: the launch model is per-session data that
    ``AgentDriverConfig`` does not carry (the Claude path bakes it into its SDK
    factory), so it is resolved by the same kind of callable rather than by
    widening the shared config for one engine.
    """

    capabilities = CodexAgentDriver.capabilities

    def __init__(
        self,
        manager: CodexRunnerManager,
        permission_registry: PermissionRegistry,
        model_for_session: Callable[[str], str | None] | None = None,
        assistant_overlay_for_session: (
            Callable[[str], CodexAssistantOverlay | None] | None
        ) = None,
        session_tools_for_session: (
            Callable[[str], "CodexSessionTools | None"] | None
        ) = None,
    ) -> None:
        self._manager = manager
        self._permission_registry = permission_registry
        self._model_for_session = model_for_session
        self._assistant_overlay_for_session = assistant_overlay_for_session
        self._session_tools_for_session = session_tools_for_session

    def create(self, config: AgentDriverConfig) -> CodexAgentDriver:
        model = (
            None
            if self._model_for_session is None
            else self._model_for_session(config.session_id)
        )
        # Resolved through a callable for the same reason the model is: the
        # overlay holds a live tool table bound to THIS session's loopback
        # `AssistantApi`, so it can neither be persisted on the row nor carried
        # over RPC in `AgentDriverConfig`.
        overlay = (
            None
            if self._assistant_overlay_for_session is None
            else self._assistant_overlay_for_session(config.session_id)
        )
        # Same seam, same reason: the surface closes over a LIVE accessor for this
        # session's record, so it can be neither persisted nor sent over RPC.
        session_tools = (
            None
            if self._session_tools_for_session is None
            else self._session_tools_for_session(config.session_id)
        )
        return CodexAgentDriver(
            config.session_id,
            config.account,
            config.cwd,
            config.config_dir,
            mode=config.mode,
            manager=self._manager,
            registry=self._permission_registry,
            identity=config.identity,
            resume=config.resume,
            model=model,
            reasoning_effort=config.reasoning_effort,
            on_engine_session_id=config.on_engine_session_id,
            on_status=config.on_status,
            on_recoverable_failure=config.on_recoverable_failure,
            on_context_usage=config.on_context_usage,
            clock=config.clock,
            assistant_overlay=overlay,
            session_tools=session_tools,
        )
