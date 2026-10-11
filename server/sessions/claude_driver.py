from __future__ import annotations
import asyncio
import base64
import dataclasses
import functools
import hashlib
import inspect
import logging
import os
import time
import uuid
from typing import Callable
from claude_agent_sdk import (AssistantMessage, PermissionResultAllow, PermissionResultDeny,
                              ResultMessage, TaskStartedMessage, TaskUpdatedMessage,
                              TaskNotificationMessage, TERMINAL_TASK_STATUSES)
from claude_agent_sdk._errors import CLIConnectionError
from viberpit import brand
from viberpit import copilot as _copilot_mod
from viberpit.accounts import provider_or_none as _provider_of
from server.aio import bounded_wait, cancel_and_reap, format_task_dump
from server.assistant.context import PromptContext, render_context_header
from server.sessions.prompt_origin import PromptOrigin, render_origin_header
from server.hostname import self_host_name
from server.config import get_settings
from server.mcp_health.status import sanitize_statuses
from server.mcp_spec import is_mcp_tool
from server.obs.metrics import assistant_sessions_live, assistant_ttft_ms, assistant_turn_ms
from server.obs.context import bind_session
from .automation_execution import AutomationExecutionFence
from .background_tasks import _AGENT_TOOLS, _is_background_launch
from . import events as ev
from .driver_stream import _STREAM_DONE, DriverStream
from .failure import classify, TurnFailure
from .engine import (EFFORT_UNCHANGED, AgentCapabilities, AgentDriverConfig,
                     AgentDriverFactory, AgentEngine, _EffortUnchanged)
from .translator import Translator
from .permissions import PermissionRegistry, to_sdk_permission_mode
from .orphaned_asks import (CLOSE_REASON_RESTART, QUESTION_MODE_INTERRUPTED,
                            RESTART_ASK_MESSAGE)
from .questions import QuestionRegistry
from .sdk_contract import is_turn_message
from .status import SEEDABLE_STATUSES, StatusSignals, derive_status
from .transcript_repair import (INTERRUPT_TEXT, RESTART_INTERRUPT_TEXT, find_transcript,
                                repair_resumed_transcript)
from .types import ASSISTANT_KIND, is_assistant_kind

_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

log = logging.getLogger(__name__)

# DIAGNOSTIC (temporary): gate the per-emit status trace. Off by default — set
# VIBERPIT_STATUS_TRACE=1 to capture the raw derived status + its input signals
# when chasing false-"Ready" reports. Remove with the trace itself once settled.
_STATUS_TRACE = os.environ.get("VIBERPIT_STATUS_TRACE", "false").lower() in ("1", "true", "yes", "on")

# Events that reconstruct a turn's transcript — the only ones retained in the
# in-progress-turn buffer and replayed to a late subscriber. Control/ephemeral
# events (Status, QueueUpdate, ContextUsage, Suggestions, Title, PermissionRequest,
# Question, Condensing, TurnFailure) are re-emitted from authoritative state by the
# WS attach path (_emit_live_extras); buffering them would double-emit.
_TURN_CONTENT = (ev.UserMessage, ev.TurnStart, ev.TurnModel, ev.SessionMeta,
                 ev.TextDelta, ev.ThinkingDelta, ev.ToolCall, ev.ToolResult,
                 ev.TurnEnd, ev.ErrorEvent,
                 # The turn's asks and their resolutions ride the attach snapshot too, so
                 # a mid-turn reattach re-delivers a pending permission/question AT ITS
                 # PLACE in the turn — after the prompt and the content that led to it —
                 # exactly as the daemon ring does in remote mode. The WS bridge used to
                 # re-emit pending asks separately, AHEAD of the open-turn resupply; the
                 # client fabricated the assistant turn for that early copy and the
                 # re-supplied prompt then landed under it (the "prompt stuck at the
                 # bottom" bug). The resolutions must travel with them so an already-
                 # decided ask does not resurrect as pending on the reattached view.
                 ev.PermissionRequest, ev.PermissionResolved, ev.Question, ev.QuestionResolved)

# How long aclose() waits for the CLI to flush its synthetic interrupt result for a
# turn wedged mid-tool-call before giving up and cancelling the drain.
_CLOSE_GRACE_SEC = 5.0
# MCP status settle loop: servers are `pending` at connect and settle within ~15 s.
MCP_SETTLE_INTERVAL_S = 5.0
MCP_SETTLE_MAX_S = 30.0

# Budget for the SDK client's __aexit__ during aclose(): the real client runs two
# 5s subprocess-terminate timeouts internally, so 15s covers it with headroom while
# still keeping a wedged exit from hanging teardown (#846).
_CLOSE_AEXIT_TIMEOUT_S = 15.0

# How long aclose() waits for the CLI to PERSIST the turn's last assistant message
# before terminating it (#1012). The CLI writes its transcript lazily, and __aexit__
# goes straight to terminate-then-kill — so a completion teardown (which fires in the
# same tick as the turn's ResultMessage) could kill the subprocess with its final
# reply still in the buffer, exactly as #1009 lost a real answer. Costs nothing in
# the ordinary case: the entry is normally already on disk, so the first scan
# returns and the wait is one stat. Only a genuinely unflushed reply pays, which is
# the case worth paying for.
_TRANSCRIPT_FLUSH_GRACE_S = 5.0
_TRANSCRIPT_FLUSH_POLL_S = 0.1

# What a clean-but-slow aclose() can legitimately take: the sum of its SEQUENTIAL
# stage bounds — interrupt, pump wait, pump reap, source pending + cursor close
# (each `_CLOSE_GRACE_SEC`), the transcript flush, then the SDK __aexit__. Derived,
# never restated, so the orchestrator's wait for a completion release cannot drift
# below the release it waits for again (#2655: a hand-tuned 20 s did, every time).
ACLOSE_BUDGET_S = 5 * _CLOSE_GRACE_SEC + _TRANSCRIPT_FLUSH_GRACE_S + _CLOSE_AEXIT_TIMEOUT_S
# Bytes of transcript tail scanned for the uuid. The entry we are waiting for is the
# last (or near-last) line by construction; an entry bigger than this window simply
# times out into the pre-#1012 behaviour rather than reading a multi-MB file 50x.
_TRANSCRIPT_TAIL_BYTES = 1 << 20


def _scan_transcript_tail(path: str, needle: str, *, last_size: int) -> tuple[bool, int]:
    """``(is *needle* in the file's tail, the file's current size)``.

    Skips the read entirely when the file has not grown since ``last_size`` — a poll
    loop over a transcript that nothing is appending to must not re-read a megabyte
    every tick. Never raises: an unreadable/absent transcript is simply "not yet".
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return False, last_size
    if size == last_size:
        return False, size
    try:
        with open(path, "rb") as fh:
            if size > _TRANSCRIPT_TAIL_BYTES:
                fh.seek(size - _TRANSCRIPT_TAIL_BYTES)
            return needle.encode() in fh.read(), size
    except OSError:
        return False, size

# Soft notice text surfaced when a dropped turn is being auto-resent.
_RECOVERING_MSG = "Recovering the dropped turn…"
# auth_stale: the CLI answered "Not logged in" from a stale in-memory token. The
# orchestrator relaunches it on the current credentials and re-sends the prompt.
_RECOVERING_AUTH_MSG = ("Claude Code lost its login — restarting the session with "
                        "fresh credentials and re-sending your message…")
_SOFT_RECOVERY_MSGS = {"turn_dropped": _RECOVERING_MSG, "auth_stale": _RECOVERING_AUTH_MSG}

# Prefix on the deny message a prompt sends to a permission it unparks (see
# send_prompt). It has to say two things the model would otherwise get wrong: the tool
# did NOT run, and what follows is the user steering rather than a refusal to engage.
# The bare "denied by user" it replaces reads as "stop", which is what made the model
# re-attempt the same blocked writes instead of following the instruction (#1504).
PROMPT_STEER_DENY_PREFIX = (
    "[This tool use was NOT run. The user sent a message instead of answering the "
    "approval prompt — their message follows. Follow it; re-request this tool only if "
    "it is still needed.]"
)


async def _aiter_one(message: dict):
    yield message


# Sentinel returned by _MessageSource.next() once the SDK stream has ended (the
# subprocess is gone). A value rather than an exception so the two consumers (a
# prompt's drain, the idle listener) branch on it the same way as on a message.
_EOF = object()


# Detached reapers for a cursor whose in-flight ``__anext__`` outlived its own
# cancellation. Holding the task (and through it the generator) is the point — see
# ``_close_cursor_when_idle``.
_ORPHAN_CURSOR_CLOSERS: set[asyncio.Task] = set()


def _close_cursor_when_idle(gen, pending: asyncio.Future, phase: str) -> None:
    """Close an abandoned cursor OURSELVES, once its frame is free again.

    ``bounded_wait`` gave up on the in-flight ``__anext__``, so the generator is
    still running: ``aclose()`` now would raise ``aclose(): asynchronous generator
    is already running`` (#1870). Dropping the reference instead is worse — the
    generator then falls to asyncio's asyncgen finalizer hook, which closes it in a
    task NOBODY holds, and a close that raises (the SDK's transport re-raises a
    non-zero CLI exit from its own ``finally``, #1869) surfaces as an unattributed
    "Task exception was never retrieved". So: hold it, wait for the frame, close it,
    and retrieve the outcome here.

    The wait is deliberately unbounded — this task's whole job is to outlive the
    teardown that gave up, and it costs one parked coroutine. Loop shutdown cancels
    it like any other task (nothing here swallows a cancellation, #1164).
    """
    async def _reap() -> None:
        try:
            await asyncio.wait({pending})
            await gen.aclose()
        except Exception:  # noqa: BLE001 - a retired cursor's close cannot affect anything live
            log.debug("cursor close failed after abandonment phase=%s", phase, exc_info=True)

    task = asyncio.ensure_future(_reap())
    _ORPHAN_CURSOR_CLOSERS.add(task)
    task.add_done_callback(_ORPHAN_CURSOR_CLOSERS.discard)


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
        if self._gen is None:
            # A retired cursor IS at EOF: aclose() drops the generator so nothing
            # can revive it, and both consumers already branch on _EOF.
            return _EOF
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
        ``grace`` rather than wedging the close.

        The close itself is OWNED, never dropped and never blanket-suppressed.
        ``suppress(BaseException)`` around an await in a teardown path swallows the
        CALLER'S cancellation as readily as the generator's failure (#1164), and a
        generator left unclosed is finalized by asyncio's GC hook in an unowned task
        whose failure nobody retrieves (#1869, #1870). ``bounded_wait`` does both
        jobs: it absorbs only the child's cancellation and it retrieves the outcome.
        """
        pending, self._pending = self._pending, None
        gen, self._gen = self._gen, None
        if gen is None:
            return
        if pending is not None and not pending.done():
            pending.cancel()
            await bounded_wait(pending, timeout=grace, phase=phase, cancel_grace=grace)
            # `pending.done()`, not bounded_wait's return: it answers False for a
            # clean-but-slow cancellation too, and that generator IS free to close.
            # Only an ABANDONED __anext__ still owns the frame.
            if not pending.done():
                _close_cursor_when_idle(gen, pending, phase)
                return
        closing = asyncio.ensure_future(gen.aclose())
        await bounded_wait(closing, timeout=grace, phase=f"{phase}:cursor-close",
                           cancel_grace=grace)


class ClaudeAgentDriver:
    """Drives one Claude session via an SDK client, exposing normalized events.

    The orchestrator composes the per-account config dir and passes it as `config_dir`;
    `sdk_factory(cwd, config_dir, can_use_tool, mode, resume) -> SDKClientProtocol | Awaitable[SDKClientProtocol]` builds
    the client (production = real ClaudeSDKClient; tests = FakeSDKClient).
    """

    engine = AgentEngine.CLAUDE_CODE
    capabilities = AgentCapabilities(
        models=True,
        interrupt=True,
        approvals=True,
        questions=True,
        adopt=True,
        self_model_switch=True,
        subagents=True,
        attachments=True,
        account_rotation=True,
        # The CLI enumerates its own built-ins, custom commands and skills
        # through `get_server_info()`, and parses `/name` itself when a turn
        # starts — so text passthrough IS this engine's native dispatch and
        # `vendor_skills` stays False.
        vendor_commands=True,
        mcp_status=True,
        # A resume carries the SDK client's full `system_prompt` append (the
        # marker/tool instructions) and `mcp_servers` overlay again — unlike
        # Codex's `thread/resume`, nothing declared at session start is lost.
        durable_tool_surface=True,
        # The Library surface scans/mutates CLAUDE_CONFIG_DIR directly
        # (server/library/scan.py, mutate.py) — Claude's shared config
        # directory, not a per-session concept.
        library_management=True,
    )

    def __init__(self, session_id, account, cwd, config_dir, *, mode, sdk_factory, registry,
                 identity, denylist=PermissionRegistry.DANGEROUS, resume=None,
                 on_session_id=None, on_sync_back: Callable[[], None] | None = None,
                 on_status=None, on_record_condense_ms=None, get_condense_estimate=None,
                 on_recoverable_failure=None, on_context_usage=None, on_mcp_status=None, clock=None, suggester=None,
                 desired_disabled_mcp: list[str] | None = None, assistant_api=None, on_sub_agents_changed=None, on_background_work_changed=None,
                 before_execution=None,
                 automation_execution_id=None, after_execution_close=None,
                 resumed_after_restart: bool = False):
        self.automation_execution_id = automation_execution_id
        self._automation_fence = AutomationExecutionFence(before_execution, after_execution_close)
        self._automation_tasks: set[str] = set()
        self._automation_task_tools: dict[str, str] = {}
        self._automation_background_tools: set[str] = set()
        self._automation_observation_complete = resume is None
        self._automation_terminal_observed = False
        self._automation_close_ok = False
        self._automation_predecessor_unknown = False
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
        # Resuming a turn a daemon restart cut off (#2922): the transcript repair at
        # start() then blames the restart, not the user.
        self._resumed_after_restart = resumed_after_restart
        self._engine_session_id: str | None = None
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
        self._on_mcp_status = on_mcp_status
        self._mcp_last: list = []                     # last sanitized snapshot
        # Servers the orchestrator wrote to the CLI's disabledMcpServers; re-applied once per
        # CLI start because a racing compose() can erase that list before the CLI reads it.
        self._desired_disabled_mcp = desired_disabled_mcp
        self._mcp_enforced = False
        self._mcp_unsupported = False                 # latched after the first control-request failure
        self._mcp_poll_task: asyncio.Task | None = None
        # Fired (synchronously, with the session id) when a Task sub-agent starts or
        # ends, so the orchestrator can refresh the persisted per-session sub-agent
        # summary the board's cards read. Without it the summary only refreshes on a
        # status change, and a card stays "no agents" for a whole turn that spawned
        # four (seen 2026-09-12: header chip said 4 agents, card showed none).
        self._on_sub_agents_changed = on_sub_agents_changed
        self._on_background_work_changed = on_background_work_changed
        self._clock = clock or time.time
        self._last_progress_at: float = self._clock()
        self._last_rate_reset: int | None = None  # resets_at (epoch s) from a RateLimitEvent
        self._last_rate_limit_type: str | None = None  # rate_limit_type from a RateLimitEvent
        self._factory = sdk_factory
        # The loopback HTTP client an assistant-kind session's MCP tools call
        # ViberPit's own API through (server/assistant/client.py). None for every
        # coding session. Owned here (not the orchestrator) so it is closed exactly
        # once, alongside the SDK client, in aclose() below.
        self._assistant_api = assistant_api
        if self._assistant_api is not None:
            assistant_sessions_live.inc()
        self._reg = registry
        self._qreg = QuestionRegistry()
        self._tr = Translator()
        self._client = None
        # The last `<ccswitch-context …/>` header actually SENT to the SDK (assistant
        # sessions only) — the header is re-sent only when it changes, not on every
        # prompt. Deduped on the rendered STRING rather than the context dict so "did
        # the header change" is exact (it also covers the host attribute), and only
        # ever advanced in _start_turn at the moment the header goes out: a prompt
        # that is queued and then abandoned by a Stop never announced its route, so
        # it must not suppress the header for the next prompt on that route.
        self._last_context_header: str | None = None
        # Broadcast fan-out: each events() subscriber gets its own queue. A single
        # asyncio.Queue is a WORK queue (each item to one getter) — fatal when
        # "attach" happens more than once per session (reconnect, 2nd viewer).
        self._subscribers: set[asyncio.Queue] = set()
        # Coalesced transcript events of the IN-PROGRESS turn, replayed to a late
        # subscriber so a mid-stream (re)attach shows the whole message, not the tail.
        self._turn_buffer: list = []
        self._pump: asyncio.Task | None = None
        # True from the moment a `_start_turn` claims the cursor until its pump exists.
        # Distinct from `_turn_active`, which `_end_turn` deliberately leaves set across
        # its own dequeue into `_start_turn` (PR #1480) and so cannot gate that call.
        self._starting_turn: bool = False
        self._titled: bool = False
        # Each entry: (text, images, context). The context chip (assistant sessions
        # only, spec §4.5) rides along so a prompt that waits in the queue still gets
        # its header and its transcript chip when it finally starts — rendering
        # happens at DEQUEUE time in _start_turn, never here.
        self._queue: list[tuple[str, list[dict], PromptContext | None, PromptOrigin | None]] = []
        # Live Task sub-agents: spawning tool-use id -> CLI task id, from the translator's
        # SubAgentStarted/Ended events. Powers stop_subagent (stop_task needs the task id)
        # and permission attribution (a sub-agent's can_use_tool ctx.agent_id IS its task
        # id — verified against claude 2.1.268, spec §2).
        self._subagent_tasks: dict[str, str] = {}
        self._turn_active: bool = False
        # Epoch seconds of the in-progress turn's start; None whenever no turn is
        # active. Consumed by the WS attach path (→ replay_window.turn_started_at)
        # to scope the incomplete-turn drop to the turn that actually is in progress.
        self._turn_started_at: float | None = None
        # Epoch seconds of the in-progress turn's first TextDelta, or None until one
        # arrives (reset per-turn in _start_turn / _open_autonomous_turn). Assistant-kind
        # only — feeds assistant_ttft_ms (spec §4.10).
        self._turn_first_delta_at: float | None = None
        # The SDK cursor hit EOF (or raised) with nobody closing this driver: the CLI
        # exited, or — the 2026-09-16 shape — its stdout reader died on an oversized
        # line while the process lived on. Once set, nothing will ever be READ from
        # this client again (no events, no results, no permission asks), so no
        # control request may be issued against it and no prompt written to it; the
        # orchestrator is recomposing the driver. See _on_stream_ended.
        self._stream_ended: bool = False
        # One recovery per driver for the crash class (process_crash / stream_lost):
        # the drain's classified error and the EOF that always follows it must not
        # each recompose the session.
        self._crash_signalled: bool = False
        self._retrying: bool = False
        self._condensing: bool = False
        self._turns_completed: int = 0
        self._last_turn_outcome: str = "success"
        # (base, effective) pair from the last on_status overlay (or seed_status):
        # current_status() reports `effective` while the raw derivation still equals
        # `base`, keeping the attach snapshot in step with the session list.
        self._status_overlay: tuple[str, str] | None = None
        self._last_pr_resolution: str | None = None
        self._last_pr_resolution_source: str | None = None
        self._last_pr_resolution_note: str | None = None
        self._last_model_request: dict | None = None
        # Per-turn, NOT sticky like _last_pr_resolution: the reason belongs to the turn
        # that handed back, and the next turn clears it so the orchestrator can never
        # audit a stale reason against a later waiting_question.
        self._last_hand_back_reason: str | None = None
        # The ask of a `hand_back` call read MID-TURN (#2647): set from
        # ev.HandBackRequested the moment the Translator sees the tool-use block, read
        # by `_persist_status` beside a busy status, and handed over to
        # `_last_hand_back_reason` by the TurnEnd that latches it. Cleared at turn
        # start and by `_reset_turn_flags` (interrupt) so it never outlives its turn.
        self._pending_hand_back_reason: str | None = None
        self._suggester = suggester
        self._suggest_task: asyncio.Task | None = None
        self._last_user_text: str = ""
        self._turn_produced_output: bool = False
        self._last_assistant_parts: list[str] = []
        self._last_turn_id: str = ""
        # The CLI's own uuid for the last TOP-LEVEL assistant message it streamed —
        # the same uuid it writes into the transcript entry for that message, which
        # is what makes "has the CLI persisted its reply yet?" an observable question
        # at teardown (#1012). Sub-agent messages are excluded: a Task's reply is not
        # the turn's answer, and its sidechain entry is not what a resume reads back.
        # Cleared once observed on disk, so a teardown never waits for a turn whose
        # flush it already witnessed.
        self._last_assistant_uuid: str | None = None
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

    @property
    def engine_session_id(self) -> str | None:
        """The Claude conversation id through the provider-neutral driver contract."""
        return self._engine_session_id

    @property
    def kind(self) -> str | None:
        """"assistant" for an AI-drawer session, None for an ordinary coding
        session. ``self._assistant_api`` (the loopback HTTP client the ViberPit
        MCP tools call through) is the sole, pre-existing signal for this fact —
        every orchestrator call site that builds a driver passes it exactly when
        the session is assistant-kind — so this is a computed property rather
        than a second, parallel ``kind`` field that could drift from it."""
        return ASSISTANT_KIND if self._assistant_api is not None else None

    def _turn(self):
        return getattr(self._tr, "_turn_id", None) or "turn"

    @property
    def last_progress_at(self) -> float:
        """Timestamp of the last meaningful driver event (set at driver construction)."""
        return self._last_progress_at

    def diagnostic_snapshot(self) -> str:
        """Render what this driver's pump task is actually doing right now.

        Observability-only (never called on any recovery/action path): a turn can go
        silent for minutes with no local error and no SDK stream-death signature in the
        journal (2026-09-22, session 808f63fc — a single large Write's underlying model
        generation hung with zero events, only ever visible via the 300s stall watchdog
        after the fact). This gives the orchestrator's stall watchdog something to log
        well before that timeout trips, so the NEXT occurrence has real evidence — the
        live await chain — instead of just "no progress for N seconds".
        """
        turn_id = self._turn()
        bits = [
            f"turn={turn_id} active={self._turn_active} drain_alive={self.drain_alive} "
            f"in_api_backoff={self.in_api_backoff} silent_for={self._clock() - self._last_progress_at:.0f}s"
        ]
        if self._pump is not None:
            bits.append(format_task_dump([self._pump]))
        else:
            bits.append("<no pump task>")
        return "\n".join(bits)

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
        """The rich derived status right now (no side effects). Used on WS attach
        (and, via the daemon RPC snapshot, by remote-mode attaches).

        Honors the last ``on_status`` overlay / persisted seed while the raw
        derivation still equals the base that produced it — so an attach after a
        parked "Answer needed" turn reports waiting_question, not the raw ``done``.
        Any real signal advance (a new turn, condensing, …) changes the raw
        derivation away from the base and supersedes the overlay automatically."""
        status = derive_status(self._dash_signals())
        if self._status_overlay is not None and self._status_overlay[0] == status:
            return self._status_overlay[1]
        return status

    def seed_status(self, persisted: str) -> None:
        """Adopt the orchestrator's persisted status as this driver's snapshot.

        A rebuilt driver (daemon restart, completion release → lazy reopen) starts
        with zeroed signals, so ``current_status`` derives ``idle`` and the attach
        snapshot contradicts the session card (an "Answer needed" card opened to a
        "Ready" session). Only resting hand-back statuses are adopted: mid-turn
        statuses can't be true on a fresh driver, and structured-pending ones
        (waiting_input / ready_review) advertise permission/plan cards that no
        longer exist to render. The seed rides the same (base, effective) overlay
        as ``_emit_status``, so the first real emit supersedes it."""
        if persisted not in SEEDABLE_STATUSES:
            return
        base = derive_status(self._dash_signals())
        if base != persisted:
            self._status_overlay = (base, persisted)

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
        # Compaction may summarize the last <ccswitch-context> header away, so the
        # dedupe can no longer claim the model knows the route — re-announce it on
        # the next prompt. Same invariant as the /clear reset in reconnect(): drop
        # the dedupe whenever the model's memory of the header may be gone. The cost
        # of being wrong here is one repeated header; the cost of not resetting is a
        # model that has silently lost the user's location for the rest of the page
        # visit.
        self._last_context_header = None
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
        """Single status emitter — ONE truth to BOTH consumers.

        Derives the raw dashboard status, lets the orchestrator's ``on_status``
        callback apply its transcript-level overlays (parked ``done`` →
        waiting_external / waiting_question, the terminal ``ended`` guard) by
        RETURNING the final status, then broadcasts that same final value as the
        live Status event (the in-session StatusBar). Emitting the raw status
        BEFORE the callback was the list/session mismatch: the card read
        "Answer needed" (overlaid waiting_question) while the open session read
        "Your turn" (raw ``done``). The overlay is remembered as a
        (base, effective) pair so ``current_status`` reports it too — see there.
        `count` is the queue depth shown in the busy label; defaults to the live
        queue length. Best-effort on the callback: a failure must never break
        the session loop (the raw status is emitted instead).
        """
        sig = self._dash_signals()
        status = derive_status(sig)
        n = len(self._queue) if count is None else count
        # DIAGNOSTIC (temporary): trace the raw status + the signals that produced
        # it, to tell a genuine idle/done from a "Ready" the UI fell back to. Logs
        # the inputs to derive_status plus the staleness clock the stall watchdog
        # uses, so a session that looks idle while monitoring is identifiable.
        # Gated by VIBERPIT_STATUS_TRACE (off by default — fires on every emit).
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
        final = status
        if self._on_status is not None:
            try:
                # isinstance guard: legacy callbacks (and test stubs) return None —
                # anything that isn't a status string means "no overlay".
                ret = await self._on_status(self.session_id, status)
                if isinstance(ret, str) and ret:
                    final = ret
            except Exception:
                log.warning("driver.on_status failed session=%s", self.session_id, exc_info=True)
        self._status_overlay = (status, final) if final != status else None
        self._emit(ev.Status(state=final, count=n))

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

    async def _handle_ask_user_question(self, tool_input: dict, turn_id: str,
                                        parent_tool_use_id: str | None = None) -> PermissionResultDeny:
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
                self.identity, parent_tool_use_id=parent_tool_use_id,
            )
            self._emit(ev.Question(
                question_id=qid,
                turn_id=turn_id,
                header=header,
                question=question_text,
                options=options,
                multi_select=multi_select,
                parent_tool_use_id=parent_tool_use_id,
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
        if any(a is not None and a[1] == QUESTION_MODE_INTERRUPTED for a in answers):
            # A daemon restart closed the driver (#2922): nobody answered OR refused.
            # The card is re-offered after the restart and its answer arrives as a
            # new prompt, so the agent must wait rather than read a dismissal.
            # An answer given before the restart (a multi-question call) still counts.
            lines = [RESTART_ASK_MESSAGE]
            for q_obj, answer in zip(questions_list, answers):
                q_text = q_obj.get("question") or ""
                if answer is None or answer[1] == QUESTION_MODE_INTERRUPTED:
                    lines.append(f"- {q_text} → (not answered yet)")
                else:
                    lines.append(self._format_answer_line(q_text, *answer))
            return PermissionResultDeny(message="\n".join(lines))
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
            if mode == QUESTION_MODE_INTERRUPTED:
                selected = None   # nobody answered — the card is re-offered (#2922)
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
        self._pending_hand_back_reason = None

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
        # A sub-agent's request names its task id as ctx.agent_id (== task_started.task_id,
        # verified 2026-09-11); map it to the spawning Task call so the card can render
        # inside that sub-agent's thread. Best-effort: unmapped → unattributed, as before.
        agent_id = getattr(ctx, "agent_id", None) if ctx is not None else None
        pid = self.subagent_tool_use_id(agent_id) if agent_id is not None else None
        if tool_name == "AskUserQuestion":
            return await self._handle_ask_user_question(tool_input, turn_id, parent_tool_use_id=pid)
        if is_assistant_kind(self.kind) and not is_mcp_tool(tool_name):
            # An assistant-kind session's model is scoped to our own MCP
            # server only (spec §4.4) — anything else (Bash, a third-party MCP
            # server, …) is a hard deny with no card: this is a programming
            # error / prompt-injection surface, not a user decision, so it never
            # touches the registry or emits a PermissionRequest event.
            #
            # `is_mcp_tool` accepts the pre-rebrand prefix too. A resumed assistant
            # thread that was started before the flip can still name a tool the old
            # way, and denying it here would break the drawer for that thread while
            # protecting nothing: the name resolves to the same TOOL_SPECS entry.
            log.warning("driver._can_use_tool: assistant session=%s attempted out-of-surface "
                        "tool=%s", self.session_id, tool_name)
            return PermissionResultDeny(
                message=f"assistant sessions may only use {brand.NAME} tools")
        # Lazy import: server.assistant.tools does not import server.sessions, but
        # keeping this at call time avoids a module-load-order cycle (mirrors
        # PermissionRegistry.request's own lazy import of the same module).
        from server.assistant import tools as assistant_tools
        items = assistant_tools.batch_items(tool_name, tool_input)
        perm_id, fut, always_allowed = await self._reg.request(
            self.session_id, turn_id, tool_name, tool_input,
            mode=self.mode, denylist=self.denylist, identity=self.identity,
            items=items, parent_tool_use_id=pid)
        if perm_id is not None:
            self._emit(ev.PermissionRequest(
                perm_id, turn_id, tool_name, ev.tool_kind(tool_name),
                tool_input, plan=(tool_name == "ExitPlanMode"), always_allowed=always_allowed,
                items=items, parent_tool_use_id=pid,
                rule_suggestions=self._reg.suggestions_for(perm_id)))
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
        if allowed:
            return PermissionResultAllow()
        # A deny raised by `send_prompt` carries the user's own words (see
        # PROMPT_STEER_DENY_PREFIX); a card deny or a Stop carries nothing and keeps
        # the generic string.
        return PermissionResultDeny(
            message=self._reg.take_deny_reason(perm_id) or "denied by user")

    async def start(self):
        if await self._automation_fence.enter():
            # A durable close proof for the previous driver establishes that no
            # managed historical task survived into this resumed execution.
            self._automation_observation_complete = True
        # A turn interrupted mid-tool-call (manual Stop, stall-watchdog, or a deploy
        # drain that cancels the drain before the CLI writes its synthetic
        # "[Request interrupted by user for tool use]" result) leaves an ORPHANED
        # tool_use as the transcript tail. On resume the CLI rejects that state with
        # error_during_execution / [ede_diagnostic] and every turn dead-ends. Heal it
        # before handing the session back to the SDK. Best-effort — never block start.
        if self.resume:
            text = RESTART_INTERRUPT_TEXT if self._resumed_after_restart else INTERRUPT_TEXT
            try:
                await asyncio.to_thread(
                    functools.partial(repair_resumed_transcript, text=text),
                    self.config_dir, self.resume)
            except Exception:
                log.warning("driver.start: transcript repair failed session=%s",
                            self.session_id, exc_info=True)
        client = self._factory(self.cwd, self.config_dir, self._can_use_tool, self.mode,
                               self.resume, on_pre_compact=self._on_pre_compact)
        if inspect.isawaitable(client):
            client = await client
        self._client = client
        await self._client.__aenter__()
        self._source = _MessageSource(self._client)
        self._closing = False
        self._start_listener()
        self._start_mcp_settle()

    async def send_prompt(self, text: str, images: list[dict] | None = None, *,
                          interrupt: bool = False,
                          context: PromptContext | None = None,
                          origin: PromptOrigin | None = None):
        """Send a prompt. While a turn is live it joins the FIFO queue — unless
        ``interrupt`` is set, in which case the live turn is stopped and this prompt
        runs NEXT ("interrupt and post"), ahead of anything already queued.

        ``context`` (assistant sessions only, spec §4.5) is the composer's route chip.
        It rides along in the queue entry even when this prompt does not start
        immediately: a follow-up sent while the assistant is still working is a normal
        interaction, and it must not lose its header or its chip. Rendering and dedupe
        happen in ``_start_turn`` when the turn actually starts, never here."""
        await self._automation_fence.enter()
        if self._client is None:
            raise RuntimeError("ClaudeAgentDriver.send_prompt called before start()")
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
            self._queue.insert(0, (text, images, context, origin))
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
                    self._queue.append(("", images, context, origin))
                    self._emit(ev.QueueUpdate(self.queue_wire()))
                return
        # A pending PERMISSION parks the turn inside _can_use_tool exactly as a question
        # does, and — exactly as with a question — the turn cannot end until it is
        # resolved. Falling through to the C2 queue below therefore deadlocked the
        # session: the prompt waited for a turn end that waited for this permission,
        # while send_prompt's caller was told "sent". Production, 2026-09-09: three
        # writes parked ~1800s (every other call that session finished under 9s), two
        # prompts vanished, and the only exit — interrupt() — denied the parked work
        # generically, so the model retried the same writes and parked again (#1504).
        #
        # Prose can never APPROVE a tool (that is the card's job, and this path never
        # allows anything), but it is unambiguously the user steering, so it resolves
        # every parked prompt as a deny CARRYING the user's words — the same channel
        # _handle_ask_user_question already uses to feed text back to the model. The
        # model reads the instruction mid-turn instead of 30 minutes later.
        # Gated on the park itself, not on the text: an attachment-only message sent at
        # a parked turn is the same deadlock, and "it needs words to count" would leave
        # one instance of the bug standing.
        if self._reg.pending(self.session_id):
            body = text if text.strip() else "(the user sent an attachment)"
            steered = self._reg.deny_all(
                self.session_id, reason=f"{PROMPT_STEER_DENY_PREFIX}\n{body}")
            if steered:
                log.info("driver.send_prompt: steered %d parked permission(s) with the "
                         "user's prompt session=%s", steered, self.session_id)
                # Echo it: the whole failure mode this closes is "I sent a message and
                # nothing happened", so the message must appear in the conversation
                # even though it starts no turn of its own. Images ride the queue and
                # get their own UserMessage when that turn starts, so echoing here
                # would double them.
                if text.strip():
                    echo = uuid.uuid4().hex
                    self._emit(ev.UserMessage(turn_id=echo, text=text, src_uuid=echo,
                                              images=None, context=context,
                                              origin=origin))
                if images:
                    self._queue.append(("", images, context, origin))
                    self._emit(ev.QueueUpdate(self.queue_wire()))
                await self._emit_status()
                return
        # C2 — FIFO queue: if a turn is live — a prompt's pump, or a CLI-initiated turn
        # the idle listener is tracking (`_turn_active`) — enqueue
        # (text, images, context) instead of rejecting.
        if self._turn_live():
            self._queue.append((text, images, context, origin))
            log.debug("driver.send_prompt: queued prompt for session=%s, queue_depth=%d",
                      self.session_id, len(self._queue))
            await self._emit_status()
            self._emit(ev.QueueUpdate(self.queue_wire()))
            return
        # C2b — the stream is dead and a recompose is in flight: hold the prompt for
        # the rebuilt driver (_reassign carries the queue over) rather than write it
        # to a CLI nobody can hear. The orchestrator refuses at its own door first
        # ("recovering"); this is the guard for direct callers.
        if self._stream_ended:
            self._queue.append((text, images, context, origin))
            log.warning("driver.send_prompt: SDK stream dead — holding prompt for the "
                        "rebuilt driver session=%s queue_depth=%d",
                        self.session_id, len(self._queue))
            self._emit(ev.QueueUpdate(self.queue_wire()))
            await self._emit_status()
            return
        # C3 — auto-title from the first prompt (first line, max 50 chars).
        if not self._titled:
            self._titled = True
            first_line = text.splitlines()[0] if text else (text or "attachment")
            self._emit(ev.Title(title=first_line[:50], source="prompt"))
        await self._start_turn(text, images, context=context, origin=origin)

    async def _start_turn(self, text: str, images: list[dict] | None = None, *,
                          context: PromptContext | None = None,
                          origin: PromptOrigin | None = None):
        """Echo the user prompt, emit working status, query the SDK, drain.

        ``context`` and ``origin`` are rendered into single-line headers prepended to
        the text that reaches the SDK — here, at the one moment a turn actually starts,
        so a prompt that came off the queue is treated exactly like one that started
        immediately. The emitted UserMessage always carries the original ``text`` plus
        the raw ``context``/``origin`` dicts, so the transcript stays independent of
        what the model sees.

        Only one call may be starting a turn at a time. ``send_prompt`` reads
        ``_turn_live()`` before calling here, but ``_turn_active`` is not set until after
        the ``await`` on ``_stop_listener()`` below, so two prompts delivered together —
        the unarchive "why you're back" seed and the human's own re-prompt — both saw an
        idle driver, both queried the SDK, and both spawned a pump over the one shared
        cursor: ``RuntimeError: anext(): asynchronous generator is already running``.
        The loser of the race queues instead, exactly as it would have had it arrived a
        tick later; the winner's turn end dequeues it."""
        if self._starting_turn:
            self._queue.append((text, images or [], context, origin))
            log.info("driver.start_turn: another turn is starting — queued prompt "
                     "session=%s queue_depth=%d", self.session_id, len(self._queue))
            await self._emit_status()
            self._emit(ev.QueueUpdate(self.queue_wire()))
            return
        self._starting_turn = True
        try:
            await self._begin_turn(text, images, context=context, origin=origin)
        finally:
            self._starting_turn = False

    async def _begin_turn(self, text: str, images: list[dict] | None = None, *,
                          context: PromptContext | None = None,
                          origin: PromptOrigin | None = None):
        """The body of `_start_turn`, run while it holds the ``_starting_turn`` claim."""
        await self._automation_fence.enter()
        self._automation_terminal_observed = False
        images = images or []
        sdk_text = text
        if context is not None:
            header = render_context_header(context, host=self_host_name())
            # Same header as the last one SENT → omit it: the model already knows,
            # and repeating it would churn the prompt-cache prefix for no gain.
            if header != self._last_context_header:
                sdk_text = f"{header}\n{text}" if text else header
            self._last_context_header = header
        if origin is not None:
            # Deliberately NOT deduped the way the context header is. Origin is a
            # property of THIS prompt, not of the conversation's current state: two
            # consecutive autopilot nudges render an identical marker, and dropping the
            # second would leave that prompt unmarked in the CLI's JSONL — so replay
            # would rebuild it as a human message on the next reload. Prepended outermost
            # (before any context header) to match the strip order in replay.py.
            sdk_text = (f"{render_origin_header(origin)}\n{sdk_text}" if sdk_text
                        else render_origin_header(origin))
        # The prompt's pump takes over the SDK cursor: park the idle listener first
        # (a no-op when this runs INSIDE the listener, dequeuing at a turn's end).
        await self._stop_listener()
        self._cancel_suggest()
        self._last_user_text = text
        self._last_assistant_parts = []
        self._turn_buffer = []
        self._turn_produced_output = False
        self._automation_terminal_observed = False
        # Forget the previous turn's bucket: unlike _last_rate_reset (a timestamp the
        # max(0.0, resets_at - now) clamp neutralises once it's past), a bucket name
        # has no decay — a stale "five_hour" from an old warning would otherwise be
        # replayed onto a LATER turn's 429 that carries no RateLimitEvent of its own.
        self._last_rate_limit_type = None
        self._turn_active = True
        # Idle time belongs to the previous turn, not this turn's stall budget.
        self._last_progress_at = self._clock()
        # Wall-clock start of this turn. The CLI's transcript write LAGS the live
        # stream (MCP fleet boot on resume), so a live attach can see a disk JSONL
        # whose last user entry is the PREVIOUS turn's. replay_window compares the
        # entry's timestamp against this to drop only the turn that actually is in
        # progress — same-host clocks, and the CLI stamps its user entry only AFTER
        # this point (it processes the prompt we are about to send).
        self._turn_started_at = time.time()
        self._turn_first_delta_at = None
        self._last_turn_outcome = "success"
        self._pending_hand_back_reason = None
        await self._emit_status()
        # Build the live UserMessage image refs (url/mime/name only).
        wire_images = [{"url": im["url"], "mime": im["mime"], "name": im.get("name", "")}
                       for im in images]
        user_turn = uuid.uuid4().hex
        self._emit(ev.UserMessage(
            turn_id=user_turn, text=text, src_uuid=user_turn,
            images=wire_images or None, context=context, origin=origin))
        assert self._client is not None  # _start_turn only runs once the client is connected
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
            payload: object = _aiter_one(message)
        else:
            payload = sdk_text
        try:
            await self._client.query(payload)
        except CLIConnectionError:
            # The SDK transport is already dead, but no reader has been scheduled to
            # notice it yet: `_stream_ended` is set asynchronously, by _drain or the
            # idle listener observing the dead cursor, so every guard in send_prompt
            # still said "live" and the prompt arrived here. Uncaught, this escaped
            # the daemon's RPC as an opaque `DaemonError: CLIConnectionError` at the
            # edge (#1997) and left the turn open with no pump to ever close it — the
            # session sat on "Working" until the stall watchdog swept it.
            #
            # It is the same loss _drain reports one scheduling tick later, so take
            # the same exit: mark the stream dead FIRST (so _end_turn issues no
            # control request and dequeues nothing onto the corpse), settle the turn,
            # then signal ONE process_crash — the orchestrator recomposes the driver
            # and replays this prompt. No log of our own: _on_stream_ended owns the
            # record for a stream death, and a second line would file a duplicate
            # auto-error issue for one incident.
            #
            # Same shape as PR #517, which gave interrupt() this guard after a daemon
            # restart left drivers holding disconnected clients; query() never got it.
            self._stream_ended = True
            await self._end_turn()
            await self._on_stream_ended(mid_turn=True)
            return
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
        limit_type = self._last_rate_limit_type if failure.kind == "rate_limit" else None
        await self._signal_recovery(failure, retry_after=retry_after, limit_type=limit_type)
        return failure

    async def _signal_recovery(self, failure: TurnFailure, *, retry_after: float | None = None,
                               limit_type: str | None = None) -> None:
        """Emit ``TurnFailure`` and hand the recoverable failure to the orchestrator —
        the ONE path for both a classified error and the driver's own verdict
        (:meth:`_on_stream_ended`). A crash-class kind is signalled at most once per
        driver: the same loss surfaces twice (the raised error frame, then the EOF
        behind it) and must recompose the session once."""
        if failure.kind in ("process_crash", "stream_lost"):
            if self._crash_signalled:
                return
            self._crash_signalled = True
        self._emit(ev.TurnFailure(
            kind=failure.kind, account=self.account, retry_after=retry_after,
            limit_type=limit_type))
        if limit_type:
            log.info("driver.rate_limit session=%s account=%s bucket=%s retry_after=%s",
                     self.session_id, self.account, limit_type, retry_after)
        if self._on_recoverable_failure is not None:
            try:
                await self._on_recoverable_failure(self.session_id, failure.kind, retry_after)
            except Exception:
                log.warning("driver.on_recoverable_failure failed session=%s",
                            self.session_id, exc_info=True)

    async def _on_stream_ended(self, *, mid_turn: bool,
                               cause: BaseException | None = None) -> None:
        """The SDK cursor ended with nobody closing this driver.

        Two causes, one consequence. The CLI process exited — or (2026-09-16, session
        1375c82f) its stdout reader died: the CLI echoes the whole ``originalFile`` in
        an Edit result, one line over the SDK's ``max_buffer_size`` raises
        "JSON message exceeded maximum buffer size", and the SDK closes its message
        stream while the process lives on. Either way the daemon can still WRITE to
        the CLI but will never READ from it again: no events, no results, no
        permission requests. Left as a warning, that was a black hole — prompts were
        accepted into it for two hours, the CLI parked on unanswerable permission
        asks, and the stall watchdog interrupted every later turn within seconds.

        So: mark the stream dead (no control request, no dequeue, no listener will
        touch it again — the sibling guards read the flag) and signal ONE recovery.
        Mid-turn it is a ``process_crash`` (recompose and replay the cut-off prompt);
        idle it is ``stream_lost`` (recompose, nothing to replay). The orchestrator's
        crash cap bounds a stream that keeps dying.

        This is the ONE record of a stream death. ``cause`` is the SDK's raised error
        frame when the stream ended by raising rather than at a bare EOF; it rides on
        this line (message + traceback) instead of a second log line at the reader,
        which error triage would file as a separate issue for the same incident
        (#1707 beside #2331).
        """
        self._stream_ended = True
        if self._closing:
            return
        reason = f" cause={type(cause).__name__}: {cause}" if cause is not None else ""
        with bind_session(self.session_id):
            log.error("driver.stream_ended: SDK stream ended outside a close session=%s "
                      "mid_turn=%s — recomposing%s", self.session_id, mid_turn, reason,
                      exc_info=cause)
        await self._signal_recovery(
            TurnFailure(kind="process_crash" if mid_turn else "stream_lost"))

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
        ended = False
        cause: BaseException | None = None
        try:
            while True:
                try:
                    msg = await self._source.next()
                except Exception as exc:
                    # The SDK reports a dead reader as ONE raised error frame, after
                    # which the cursor is at EOF for good (a generator that raised is
                    # finished). Surface the real cause on the turn, then treat it as
                    # the stream ending: this turn can never complete. No log of our
                    # own: _on_stream_ended records this death, with `cause` (#1707).
                    await self._surface_drain_error(exc)
                    ended, cause = True, exc
                    break
                if msg is _EOF:
                    ended = True
                    break
                await self._handle_msg(msg)
                if isinstance(msg, ResultMessage):
                    break
        except Exception as exc:
            log.exception("driver.drain session=%s", self.session_id)
            await self._surface_drain_error(exc)
        finally:
            if ended and not self._closing:
                # BEFORE settling: _end_turn must issue no control request against the
                # dead reader and must not dequeue a prompt onto it.
                self._stream_ended = True
            await self._end_turn()
            if ended:
                await self._on_stream_ended(mid_turn=True, cause=cause)

    async def _handle_msg(self, msg) -> None:
        """Route ONE SDK message through the translator and the per-event side effects.
        Shared by the prompt pump and the idle listener — one turn semantics."""
        if isinstance(msg, TaskStartedMessage):
            self._automation_tasks.add(msg.task_id)
            if msg.tool_use_id:
                self._automation_task_tools[msg.task_id] = msg.tool_use_id
        elif isinstance(msg, (TaskUpdatedMessage, TaskNotificationMessage)):
            if msg.status in TERMINAL_TASK_STATUSES:
                self._automation_tasks.discard(msg.task_id)
                tool_id = self._automation_task_tools.pop(msg.task_id, None)
                tool_id = tool_id or getattr(msg, "tool_use_id", None)
                if tool_id:
                    self._automation_background_tools.discard(tool_id)
        if isinstance(msg, ResultMessage):
            self._automation_terminal_observed = True
        if isinstance(msg, AssistantMessage) and msg.parent_tool_use_id is None:
            # Recorded from the CONSOLIDATED message, not from a stream delta: the
            # partial-message events carry their own uuids, and only this one matches
            # the transcript entry the flush wait looks for (#1012).
            src = getattr(msg, "uuid", None)
            if isinstance(src, str) and src:
                self._last_assistant_uuid = src
        for e in self._tr.feed(msg):
            if isinstance(e, ev.ToolCall) and (
                e.name in _AGENT_TOOLS or _is_background_launch(e.name, e.input)
            ):
                self._automation_background_tools.add(e.tool_id)
            if isinstance(e, ev.RateLimit):
                # Internal: stash the reset for retry timing; don't forward.
                self._last_rate_limit_type = e.rate_limit_type
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
            if isinstance(e, ev.HandBackRequested):
                # Internal (#2647): park the ask and re-derive status NOW, so the
                # orchestrator persists and pushes it beside `in_progress` instead
                # of waiting for a turn end that a still-working session may not
                # reach for a long time. The TurnEnd latch below remains the
                # authority on the final status.
                self._pending_hand_back_reason = e.reason
                await self._emit_status()
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
                if is_assistant_kind(self.kind) and self._turn_first_delta_at is None:
                    self._turn_first_delta_at = time.time()
                    if self._turn_started_at is not None:
                        assistant_ttft_ms.observe(
                            (self._turn_first_delta_at - self._turn_started_at) * 1000)
            elif isinstance(e, ev.TurnEnd):
                self._last_turn_outcome = e.outcome
                self._last_turn_id = e.turn_id
                if e.pr_resolution is not None:
                    # Intentionally sticky: set-only; first-write-wins is enforced
                    # downstream by the rec.pr_resolution is None guard in _persist_status.
                    # The source is latched INSIDE this branch so it has exactly the
                    # same stickiness — an unconditional assignment would let a later
                    # resolution-free turn wipe "tool" back to None, and the
                    # orchestrator's `or "marker"` default would then file a tool call
                    # as a marker.
                    self._last_pr_resolution = e.pr_resolution
                    self._last_pr_resolution_source = e.pr_resolution_source
                    # Latched in the same branch for the same reason as the source:
                    # the note explains THIS resolution, so the two must never be
                    # able to come from different turns.
                    self._last_pr_resolution_note = e.pr_resolution_note
                self._last_hand_back_reason = (
                    e.hand_back_reason if e.outcome == "hand_back" else None)
                # The latched reason takes over from the mid-turn slot (#2647).
                self._pending_hand_back_reason = None
                self._latch_model_request(e)
            elif isinstance(e, ev.ErrorEvent):
                failure = await self._maybe_signal_recovery(
                    e.code, e.raw_message or e.message)
                soft = _SOFT_RECOVERY_MSGS.get(failure.kind)
                if soft is not None:
                    # Self-healing recovery is in flight (signalled above): an
                    # auto-resend for a dropped turn, a relaunch on fresh
                    # credentials for auth_stale. Show a soft "recovering" notice
                    # instead of the alarming red error; the orchestrator pauses
                    # with a red surface only if its retry cap is hit.
                    self._note_progress(e)
                    self._emit(ev.Recovering(turn_id=e.turn_id, message=soft))
                    continue
            if isinstance(e, (ev.SubAgentStarted, ev.SubAgentEnded)):
                if isinstance(e, ev.SubAgentStarted):
                    self._subagent_tasks[e.parent_tool_use_id] = e.task_id
                else:
                    self._subagent_tasks.pop(e.parent_tool_use_id, None)
                if self._on_sub_agents_changed is not None:
                    try:
                        self._on_sub_agents_changed(self.session_id)
                    except Exception:  # noqa: BLE001 - a summary refresh must never break the pump
                        log.debug("driver.on_sub_agents_changed failed session=%s",
                                  self.session_id, exc_info=True)
            if isinstance(e, (ev.BackgroundTaskStarted, ev.BackgroundTaskEnded)):
                # A foreground Bash is a local_bash task too; only backgrounded work is a
                # snapshot change, so only it registers and fires the refresh.
                changed = False
                if isinstance(e, ev.BackgroundTaskStarted):
                    if e.backgrounded:
                        self._subagent_tasks[e.tool_use_id] = e.task_id
                        changed = True
                else:
                    changed = self._subagent_tasks.pop(e.tool_use_id, None) is not None
                if changed and self._on_background_work_changed is not None:
                    try:
                        self._on_background_work_changed(self.session_id)
                    except Exception:  # noqa: BLE001 - a snapshot refresh must never break the pump
                        log.debug("driver.on_background_work_changed failed session=%s",
                                  self.session_id, exc_info=True)
            if isinstance(e, (ev.TextDelta, ev.ToolCall, ev.ToolResult)):
                self._turn_produced_output = True
            self._note_progress(e)
            self._emit(e)
        if self._engine_session_id is None and self._tr.session_id:
            self._engine_session_id = self._tr.session_id
            if self._on_session_id is not None:
                try:
                    await self._on_session_id(self.session_id, self._engine_session_id)
                except Exception:
                    log.exception("driver.on_session_id session=%s", self.session_id)

    async def _surface_drain_error(self, exc: BaseException) -> None:
        """Surface a stream failure to the UI as the real cause, not a generic
        placeholder (e.g. a dead token or a closed subprocess is exactly what the user
        needs to act on; redaction scrubs any secret-shaped substrings downstream), mark
        the turn failed and signal recovery."""
        self._automation_terminal_observed = False
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
        # Before any turn-scoped flag is cleared below (either branch): this turn
        # is settling right now, whether or not another is queued right behind it.
        if is_assistant_kind(self.kind) and self._turn_started_at is not None:
            assistant_turn_ms.observe((time.time() - self._turn_started_at) * 1000)
        # Clear every turn-scoped flag so none outlives the turn that set it.
        self._reset_turn_flags()
        # Nothing queued: settle to idle. The dequeue below is deliberately NOT an
        # `else` — see its comment. A dead stream settles too, whatever is queued:
        # those prompts are held for the rebuilt driver (_reassign carries them).
        if not self._queue or self._stream_ended:
            self._turn_active = False
            self._turn_started_at = None
            self._turn_buffer = []
            self._turns_completed += 1
            # Turn settled — refresh the exact context-window readout. Not on a dead
            # stream: it is a control request the CLI's reader will never answer, so
            # it sat on the SDK's 60 s timeout — a zombie pump the stall watchdog then
            # "interrupted" (2026-09-16).
            if not self._stream_ended:
                await self._emit_context_usage()
                await self._poll_mcp_status()
            await self._emit_status()
        # If queued prompts remain, pop the front and start the next turn. Re-checked
        # AFTER the idle bookkeeping above because `_turn_live()` is `drain_alive or
        # _turn_active`, and `drain_alive` stays true while THIS pump settles: a
        # prompt delivered during those two awaits is therefore QUEUED by send_prompt
        # rather than started, and only this drain would ever pop it — a check-then-
        # await lost wakeup that stranded the message in the queue strip forever, over
        # a "Ready" header (CI, PR #1480: the first typed prompt now lands exactly
        # here, because turn one runs at spawn instead of at the first attach).
        # Nothing below this check awaits, so the pump task completes without
        # reopening the window.
        if self._stream_ended:
            return   # nothing to dequeue onto, nothing to listen to
        if self._queue:
            next_text, next_images, next_context, next_origin = self._queue.pop(0)
            log.debug("driver.drain: dequeuing next prompt for session=%s, remaining=%d",
                      self.session_id, len(self._queue))
            self._emit(ev.QueueUpdate(self.queue_wire()))
            await self._start_turn(next_text, next_images, context=next_context,
                                   origin=next_origin)
            return
        self._maybe_start_suggestions()
        self._start_listener()

    # ---- idle listener: the SDK cursor is never left unread -----------------------
    def _start_listener(self) -> None:
        if self._closing or self._source is None or self._stream_ended:
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
        # Only the LISTENER's cancellation may be swallowed here; ours (a close racing
        # the drain) has to keep unwinding — see server.aio.cancel_and_reap (#1164).
        await cancel_and_reap(task, phase=f"driver.idle-listener:{self.session_id}")

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
        self._automation_terminal_observed = False
        # Forget the previous turn's bucket: unlike _last_rate_reset (a timestamp the
        # max(0.0, resets_at - now) clamp neutralises once it's past), a bucket name
        # has no decay — a stale "five_hour" from an old warning would otherwise be
        # replayed onto a LATER turn's 429 that carries no RateLimitEvent of its own.
        self._last_rate_limit_type = None
        self._turn_active = True
        self._last_progress_at = self._clock()
        self._turn_started_at = time.time()
        self._turn_first_delta_at = None
        self._last_turn_outcome = "success"
        log.info("driver.autonomous_turn: CLI-initiated turn session=%s", self.session_id)
        await self._emit_status()

    async def _idle_listen(self) -> None:
        assert self._source is not None
        me = asyncio.current_task()
        try:
            while self._listener is me:
                try:
                    msg = await self._source.next()
                except Exception as exc:
                    # The other way the cursor ends: a RAISED error frame (the CLI
                    # exited between turns — "Command failed with exit code 1" — or its
                    # reader died on an oversized line), after which the generator is
                    # finished for good. It used to fall to the catch-all below, which
                    # logged and returned without marking the stream dead or
                    # signalling recovery: the pre-#1895 black hole again (#1885).
                    # Same exit as the EOF branch, surfacing the cause on an open
                    # autonomous turn first, as _drain does for a prompt turn.
                    if self._closing:
                        return
                    self._stream_ended = True
                    if self._turn_active:
                        await self._surface_drain_error(exc)
                        await self._end_turn()
                    await self._on_stream_ended(mid_turn=False, cause=exc)
                    return
                if msg is _EOF:
                    if self._closing:
                        return
                    # No log of our own: _on_stream_ended owns the record for a stream
                    # death, and reports this same end at ERROR with mid_turn=False and
                    # the recompose it is about to do. The warning that used to sit here
                    # was all the pre-#1895 code could do about an idle EOF; left in
                    # alongside the handler it reported one incident twice, and error
                    # triage — which fingerprints the normalized message — filed the
                    # pair as two auto-error issues (#1943). The mid-turn EOF in _drain
                    # already delegates the same way.
                    await self._on_stream_ended(mid_turn=False)
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
        return [t for t, _, _, _ in self._queue]

    def queue_wire(self) -> list[dict]:
        """Structured queue snapshot: text + image refs per queued prompt."""
        return [
            {"text": t,
             "images": [{"url": im["url"], "mime": im["mime"], "name": im.get("name", "")}
                        for im in imgs]}
            for t, imgs, _, _ in self._queue
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
    def stream_ended(self) -> bool:
        """True once the SDK stream ended outside a close: this driver can no longer
        hear its CLI and is being recomposed. The orchestrator's ``send_prompt``
        refuses ("recovering") while it is set."""
        return self._stream_ended

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
        """The most recent ``<!-- viberpit:model=… -->`` request, or None.
        ``{"turn_id", "model", "reason"}``."""
        return self._last_model_request

    @property
    def last_pr_resolution(self) -> str | None:
        """Resolution token from the most recent ``mark_no_pr`` call or
        ``<!-- viberpit:no-pr -->`` marker, or ``None`` if no completed turn has
        carried one yet."""
        return self._last_pr_resolution

    @property
    def last_pr_resolution_source(self) -> str | None:
        """How ``last_pr_resolution`` arrived — ``"tool"`` (a ``mark_no_pr`` call) or
        ``"marker"`` — or ``None`` if no completed turn has carried a resolution yet.

        Latched together with ``last_pr_resolution`` and therefore exactly as sticky:
        a turn with no resolution leaves both alone, so the pair can never disagree."""
        return self._last_pr_resolution_source

    @property
    def last_pr_resolution_note(self) -> str | None:
        """The ``note`` the ``mark_no_pr`` call gave for ``last_pr_resolution``, or
        ``None`` (no note, or the marker path, which carries no argument).

        Latched with the resolution and therefore exactly as sticky. Persisted by
        ``_persist_status`` as ``sessions.pr_resolution_note``: it is the session's
        own one-line account of why it finished without a PR, and the Backlog Runner
        reads it back by session id to settle an ended investigation (spec
        2026-09-18 §5)."""
        return self._last_pr_resolution_note

    @property
    def last_hand_back_reason(self) -> str | None:
        """The reason the most recent turn gave ``hand_back`` (tool or marker), or
        ``None`` if that turn did not hand back. Read by ``_persist_status`` when it
        writes ``waiting_question``."""
        return self._last_hand_back_reason

    @property
    def pending_hand_back_reason(self) -> str | None:
        """The reason of a ``hand_back`` call made in the turn that is STILL RUNNING
        (#2647), or ``None``. Read by ``_persist_status`` beside a mid-turn status;
        ``last_hand_back_reason`` takes over once the turn ends."""
        return self._pending_hand_back_reason

    async def signal_message_prose(self, stem: str, *, timeout: float = 2.0) -> str | None:
        """The prose of the top-level message that carried this turn's ``stem``
        signal call (``"hand_back"``), or None if the translator never reads one.

        Called from the in-process MCP handler of that very call. The SDK queues the
        message's stream events and its consolidated AssistantMessage BEFORE it
        spawns the handler (``query.py:_read_messages``), but this driver's consumer
        may not have processed them yet — so wait for the translator to have recorded
        the signal from the tool-use block, then read that message's text. Never
        returns an earlier message's prose: without the signal the answer is
        "unknown", and the handler says nothing rather than guessing.
        """
        deadline = time.monotonic() + timeout
        while not self._tr.has_signal(stem):
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.02)
        return self._tr.message_prose()

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
        # A dead stream answers no control request; the CLI is being replaced anyway.
        if self._client and not self._stream_ended:
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

    def subagent_task_id(self, tool_use_id: str) -> str | None:
        """CLI task id of a live Task sub-agent, or None once it ended / never started."""
        return self._subagent_tasks.get(tool_use_id)

    def subagent_tool_use_id(self, task_id: str) -> str | None:
        """Reverse lookup: the spawning Task tool-use id for a live task id."""
        for tuid, tid in self._subagent_tasks.items():
            if tid == task_id:
                return tuid
        return None

    async def stop_subagent(self, tool_use_id: str) -> str | None:
        """Stop ONE running task — a Task sub-agent or a backgrounded shell/Monitor (#2635) —
        via the SDK's stop_task control request.
        Returns the task id it stopped, or None when the sub-agent is not running
        (unknown id, already ended, or no live client). The map entry is NOT dropped
        here: it is dropped by `_handle_msg` on ANY SubAgentEnded — in the kill
        sequence that is the earlier task_updated(killed), not the notification — so
        a stop that the CLI rejected leaves the sub-agent visibly running rather than
        silently gone."""
        task_id = self.subagent_task_id(tool_use_id)
        if task_id is None or self._client is None:
            return None
        log.info("driver.stop_subagent session=%s tool_use_id=%s task_id=%s",
                 self.session_id, tool_use_id, task_id)
        await self._client.stop_task(task_id)
        return task_id

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

    async def set_model(
        self, model: str | None, *, effort: str | None | _EffortUnchanged = EFFORT_UNCHANGED,
    ) -> None:
        # ``effort`` is part of the engine-neutral contract (`AgentDriver.set_model`),
        # but Claude has no reasoning-effort dial (`AgentCapabilities.reasoning_effort`
        # is False), so there is nothing to apply it to.
        if self._client:
            await self._client.set_model(model)

    async def server_info(self) -> dict | None:
        if self._client is None or self._stream_ended:
            return None
        return await self._client.get_server_info()

    async def context_usage(self) -> dict:
        if self._client is None or self._stream_ended:
            return {}
        return await self._client.get_context_usage()

    @property
    def last_mcp_status(self) -> list:
        """The last sanitized snapshot, without a round-trip (never raises)."""
        return list(self._mcp_last)

    async def mcp_status(self) -> list:
        """Fresh sanitized status when live; the last snapshot otherwise."""
        if self._client is None or self._stream_ended or self._mcp_unsupported:
            return list(self._mcp_last)
        await self._poll_mcp_status()
        return list(self._mcp_last)

    async def reconnect_mcp_server(self, name: str) -> None:
        if self._client is None or self._stream_ended:
            raise RuntimeError("session not live")
        await self._client.reconnect_mcp_server(name)
        await self._poll_mcp_status()

    async def toggle_mcp_server(self, name: str, enabled: bool) -> None:
        """User-initiated live toggle (the CLI mirrors it into its own project state)."""
        if self._client is None or self._stream_ended:
            raise RuntimeError("session not live")
        await self._client.toggle_mcp_server(name, enabled)
        # Keep the enforcement set consistent with the user's choice, or a later CLI restart
        # (/clear reconnect) would re-apply the stale repo rule over it.
        desired = [n for n in (self._desired_disabled_mcp or []) if n != name]
        if not enabled:
            desired.append(name)
        self._desired_disabled_mcp = desired
        await self._poll_mcp_status()

    async def _poll_mcp_status(self) -> list | None:
        """One get_mcp_status round-trip -> emit + persist. Best-effort, DEBUG on failure.

        A failure latches ``_mcp_unsupported`` (old CLI / transient): one DEBUG line and
        no retry storm; a session restart clears it."""
        if (self._mcp_unsupported or self._closing or self._client is None or self._stream_ended
                or not get_settings().mcp_status_enabled):
            return None
        try:
            states = sanitize_statuses(await self._client.get_mcp_status())
        except Exception:
            self._mcp_unsupported = True
            log.debug("mcp_status pull failed session=%s", self.session_id, exc_info=True)
            return None
        self._mcp_last = states
        self._emit(ev.McpStatus(servers=states))
        if self._on_mcp_status is not None:
            try:
                await self._on_mcp_status(self.session_id, states)
            except Exception:
                log.debug("on_mcp_status persist failed session=%s", self.session_id, exc_info=True)
        return states

    def _start_mcp_settle(self) -> None:
        if self._mcp_poll_task is None or self._mcp_poll_task.done():
            if get_settings().mcp_status_enabled:
                self._mcp_poll_task = asyncio.create_task(self._mcp_settle())

    async def _mcp_settle(self) -> None:
        """Poll once at start, then while anything is pending, capped at MCP_SETTLE_MAX_S."""
        deadline = asyncio.get_running_loop().time() + MCP_SETTLE_MAX_S
        states = await self._poll_mcp_status()
        states = await self._enforce_disabled_mcp(states) or states
        while (states and any(s.status == "pending" for s in states)
               and asyncio.get_running_loop().time() < deadline and not self._closing):
            await asyncio.sleep(MCP_SETTLE_INTERVAL_S)
            states = await self._poll_mcp_status()

    async def _enforce_disabled_mcp(self, states: list | None) -> list | None:
        """Re-apply ``desired_disabled_mcp`` through the control channel, once per CLI start.

        Runs on the first successful status poll. Returns the re-polled snapshot when it
        toggled anything, else None. Best-effort: failures are DEBUG and never raise."""
        if (not states or self._mcp_enforced or not self._desired_disabled_mcp or self._closing
                or self._client is None or self._stream_ended
                or not get_settings().mcp_scope_enabled):
            return None
        self._mcp_enforced = True
        reported = {s.name: s.status for s in states}
        toggled = False
        for name in self._desired_disabled_mcp:
            if reported.get(name) in (None, "disabled"):
                continue
            if name not in (self._desired_disabled_mcp or []):
                continue  # the user enabled it mid-pass
            try:
                await self._client.toggle_mcp_server(name, False)
                toggled = True
            except Exception:
                log.debug("mcp enforce toggle failed session=%s server=%s",
                          self.session_id, name, exc_info=True)
        return await self._poll_mcp_status() if toggled else None

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
        await self._automation_fence.enter()
        predecessor_unknown = self._automation_predecessor_unknown
        # A cancelled/failed replacement must never erase the old native lifetime.
        self._automation_predecessor_unknown = True
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
        if self._mcp_poll_task is not None:
            await cancel_and_reap(self._mcp_poll_task,
                                  phase=f"driver.reconnect-mcp-poll:{self.session_id}")
        if self._source is not None:
            await self._source.aclose(grace, phase=f"driver.reconnect-source:{self.session_id}")
            self._source = None
        if self._client is not None:
            closing = asyncio.ensure_future(self._client.__aexit__(None, None, None))
            completed = await bounded_wait(closing, timeout=_CLOSE_AEXIT_TIMEOUT_S,
                                           phase=f"driver.reconnect-aexit:{self.session_id}")
            if (completed and closing.done() and not closing.cancelled()
                    and closing.exception() is None):
                self._automation_predecessor_unknown = predecessor_unknown
            await self._call_sync_back()
        self.resume = None
        self._engine_session_id = None
        self._titled = False
        self._queue.clear()
        self._turn_active = False
        self._turn_started_at = None
        # A fresh client is a fresh stream: a death on the old one must not brand the
        # new one dead (or spend its one crash signal) before it says a word.
        self._stream_ended = False
        self._crash_signalled = False
        self._tr = Translator()
        # A fresh CLI session has no history: a header deduped against the OLD
        # session's last-sent value would leave the new one never told where the
        # user is.
        self._last_context_header = None
        client = self._factory(self.cwd, self.config_dir, self._can_use_tool, self.mode, None,
                               on_pre_compact=self._on_pre_compact)
        if inspect.isawaitable(client):
            client = await client
        self._client = client
        await client.__aenter__()
        self._source = _MessageSource(client)
        self._closing = False
        self._start_listener()
        # Fresh CLI process: forget the old latch/snapshot and settle again.
        self._mcp_unsupported = False
        self._mcp_enforced = False
        self._mcp_last = []
        self._start_mcp_settle()

    def resolve_permission(self, perm_id, decision, identity) -> bool:
        return self._reg.resolve(perm_id, decision, identity)

    async def automation_work_stopped(self) -> bool:
        """Close an observed terminal execution, never infer stop from UI status.

        Resumed transcripts lack a complete live task history, so remain unknown.
        Public SDK task lifecycle messages include Bash/Monitor as well as agents.
        """
        if (self._automation_predecessor_unknown
                or not self._automation_observation_complete or not self._automation_terminal_observed
                or self._turn_live() or self._queue or self._automation_tasks
                or self._automation_background_tools):
            return False
        self._automation_fence.frozen = True
        if not self._closing:
            await self.aclose()
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise asyncio.CancelledError
        return (self._automation_close_ok and not self._automation_tasks
                and not self._automation_background_tools)

    async def _await_transcript_flush(self) -> None:
        """Wait (bounded) for the CLI to persist the turn's last assistant message.

        ``__aexit__`` terminates the subprocess and then kills it, and the completion
        teardown schedules that in the SAME tick as the turn's ResultMessage — while
        the CLI is typically still writing the reply it just streamed us. The SDK's
        own ``subprocess_cli`` warns about closing mid-flush for exactly this reason,
        and ``ensure_driver`` already waits on ``_releasing`` for it on the REBUILD
        side; this is the missing half on the teardown side (#1012, #1009).

        The condition is observable, so this waits on the fact rather than sleeping a
        guessed interval: the CLI names each assistant message with a uuid and writes
        the transcript entry under that same uuid. Bounded like every other teardown
        wait (#846) — an overrun logs the uuid it gave up on and tears down anyway.
        """
        want = self._last_assistant_uuid
        session = self._engine_session_id
        # Nothing streamed, no CLI session to look up, or the stream already ended
        # (the subprocess is gone — there is nobody left to flush anything).
        if not want or not session or not self.config_dir or self._stream_ended:
            return
        grace = getattr(self, "_flush_grace", _TRANSCRIPT_FLUSH_GRACE_S)
        if grace <= 0:
            return
        path = await asyncio.to_thread(find_transcript, self.config_dir, session)
        if not path:
            # Unobservable (no transcript for this engine session): never block a
            # teardown on a question that cannot be answered.
            log.debug("flush wait skipped session=%s: no transcript for %s",
                      self.session_id, session)
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace
        size = -1
        while True:
            found, size = await asyncio.to_thread(
                _scan_transcript_tail, path, want, last_size=size)
            if found:
                self._last_assistant_uuid = None
                return
            if loop.time() >= deadline:
                log.warning(
                    "flush wait: CLI did not persist assistant message %s for "
                    "session=%s within %.1fs — terminating anyway; that reply may be "
                    "missing from the transcript (#1012)", want, self.session_id, grace)
                return
            await asyncio.sleep(_TRANSCRIPT_FLUSH_POLL_S)

    async def aclose(self, *, reason: str | None = None):
        """Tear the driver down. ``reason=CLOSE_REASON_RESTART`` (the orchestrator's
        daemon-shutdown ``stop()``) settles pending asks with the restart message
        rather than as a user dismissal/denial (#2922); every other close keeps the
        user-facing wording."""
        restart = reason == CLOSE_REASON_RESTART
        try:
            # No more listening: an idle driver's listener is parked on the cursor and
            # must not be counted as a live turn to flush (the grace below is for those).
            self._closing = True
            await self._stop_listener()
            self._cancel_suggest()
            if self._mcp_poll_task is not None:
                await cancel_and_reap(self._mcp_poll_task, phase=f"driver.mcp-poll:{self.session_id}")
            self._reg.deny_all(self.session_id,
                               reason=RESTART_ASK_MESSAGE if restart else None)
            self._qreg.deny_all(self.session_id,
                                mode=QUESTION_MODE_INTERRUPTED if restart else None)
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
                # asyncio.wait, not wait_for(shield(...)): both leave the pump running on
                # overrun, but wait_for's CancelledError is indistinguishable from OUR own
                # (this close runs inside the drain), and swallowing that stalls the stop
                # budget (#1164).
                await asyncio.wait({self._pump}, timeout=grace)
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
            # LAST thing before terminate: the CLI is still running here, so it still has
            # a chance to finish writing the reply it just streamed (#1012).
            await self._await_transcript_flush()
            if self._client:
                closing = asyncio.ensure_future(self._client.__aexit__(None, None, None))
                completed = await bounded_wait(closing, timeout=_CLOSE_AEXIT_TIMEOUT_S,
                                               phase=f"driver.aexit:{self.session_id}")
                self._automation_close_ok = (completed and closing.done()
                                             and not closing.cancelled()
                                             and closing.exception() is None)
            # Final sync_back: capture any token rotation that occurred during the
            # session (e.g. a long-running session where the SDK refreshed but the
            # drain's finally sync_back already ran before the close was requested).
            await self._call_sync_back()
            if self._assistant_api is not None:
                try:
                    await self._assistant_api.aclose()
                finally:
                    assistant_sessions_live.dec()
        finally:
            # End every subscriber's stream even when a close step raised: a pump
            # parked on a queue nobody will ever feed again otherwise lives forever
            # (supervise() keeps it alive — #1690).
            for q in self._subscribers:
                q.put_nowait(_STREAM_DONE)
        await self._automation_fence.closed(self)


class ClaudeAgentDriverFactory(AgentDriverFactory):
    """Construct Claude drivers while keeping the SDK factory below the engine seam."""

    #: Republished from the driver class (never restated) so orchestration can ask
    #: what this engine supports before a driver exists — see
    #: ``AgentDriverRegistry.capabilities_for``.
    capabilities = ClaudeAgentDriver.capabilities

    def __init__(self, sdk_factory_for_session: Callable[[str], object],
                 permission_registry: PermissionRegistry) -> None:
        self._sdk_factory_for_session = sdk_factory_for_session
        self._permission_registry = permission_registry

    def create(self, config: AgentDriverConfig) -> ClaudeAgentDriver:
        return ClaudeAgentDriver(
            config.session_id,
            config.account,
            config.cwd,
            config.config_dir,
            mode=config.mode,
            sdk_factory=self._sdk_factory_for_session(config.session_id),
            registry=self._permission_registry,
            identity=config.identity,
            resume=config.resume,
            on_session_id=config.on_engine_session_id,
            on_sync_back=config.on_sync_back,
            on_status=config.on_status,
            on_context_usage=config.on_context_usage,
            on_mcp_status=config.on_mcp_status,
            on_sub_agents_changed=config.on_sub_agents_changed,
            on_background_work_changed=config.on_background_work_changed,
            on_record_condense_ms=config.on_record_condense_ms,
            get_condense_estimate=config.get_condense_estimate,
            on_recoverable_failure=config.on_recoverable_failure,
            clock=config.clock,
            suggester=config.suggester,
            desired_disabled_mcp=config.desired_disabled_mcp,
            assistant_api=config.assistant_api,
            before_execution=config.before_execution,
            automation_execution_id=config.automation_execution_id,
            after_execution_close=config.after_execution_close,
            resumed_after_restart=config.resumed_after_restart,
        )
