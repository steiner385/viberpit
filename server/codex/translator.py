"""Codex app-server notifications → the ONE normalized ccswitch event vocabulary.

Why this module is pure. Everything here is synchronous, side-effect free, and
ignorant of the driver, the app-server client, and the WebSocket. That is what
makes the whole Codex → UI mapping exhaustively table-testable in the same way
`server/sessions/status.py` is, and it is also the strongest available guarantee
about the *content* flowing through it: a module with no logger, no I/O and no
clock cannot spill a command, a patch, a prompt, or model output into a log line.
Native strings reach UI events (that is the job) and nothing else.

Why there are no new event kinds. `server/sessions/events.py` is the single
vocabulary every engine speaks, and the web reducer is engine-neutral because of
it. A Codex-shaped event dataclass would fork that contract in two, so every
Codex item type maps onto an existing member — usually a `ToolCall`/`ToolResult`
pair whose `name` is the Claude tool name the presenter already renders (`Bash`,
`Edit`, `Read`, `Task`, …). The mapping table below is the contract; deviating
from it is a UI regression, not an implementation detail.

Why unknown shapes are counted instead of guessed at. An upstream release that
adds a nineteenth item variant or a new notification must not end a live turn.
`schema.parse_thread_item` already degrades an unmodelled item to
`UnknownThreadItem`; this module drops it, bumps a counter, and keeps going. The
counters carry no method-derived or content-derived keys, so an observability
read of them can never leak.

Two rules here are policy, not preference:

* only the reasoning *summary* is ever forwarded. `item/reasoning/textDelta`
  (raw chain of thought) is deliberately absent from
  :data:`TRANSLATED_NOTIFICATIONS`, so a caller that hands it in gets `[]`;
* `TurnEnd.model_request` is always `None`. Codex has no self-directed model
  switching (`AgentCapabilities.self_model_switch=False`), so the instruction is
  never sent and a marker that appears anyway must never be honoured.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from ccswitch import brand
from server.codex import schema
from server.sessions import events as ev
from server.sessions.completion import COMPLETION_MARKER, resolution_from_text

#: Upper bound on the aggregated command output carried in a `ToolResult`
#: payload. The payload rides the WebSocket to every attached client and then
#: into the daemon's event ring, so an unbounded `cat` of a large file would be
#: paid for once per viewer.
MAX_TOOL_OUTPUT_CHARS: Final = 16_000
OUTPUT_TRUNCATION_MARKER: Final = f"\n… [truncated by {brand.NAME}]"

#: Error code for a thread that reported `systemError`. `ThreadStatus` carries
#: no message, so the text is fixed here rather than synthesized from native
#: state that does not exist.
THREAD_SYSTEM_ERROR_CODE: Final = "codex_thread_error"
THREAD_SYSTEM_ERROR_MESSAGE: Final = (
    "The Codex thread reported a system error and cannot continue."
)
#: Fallback code when a failed turn carries no `codexErrorInfo` category.
TURN_ERROR_CODE: Final = "codex_turn_error"
TURN_ERROR_MESSAGE: Final = "The Codex turn ended with an error."

#: Separator between the parts of a reasoning summary. Each part is a
#: self-contained section upstream, so they are joined as paragraphs.
_SUMMARY_JOIN: Final = "\n\n"

#: The notifications this translator understands. Exported so a driver can route
#: only these through it — anything else is the driver's own business (account
#: and auth notifications, for instance) and routing it here would inflate
#: `unknown_notifications` with frames that were handled perfectly well.
TRANSLATED_NOTIFICATIONS: Final[frozenset[str]] = frozenset(
    {
        schema.METHOD_THREAD_STARTED,
        schema.METHOD_THREAD_STATUS_CHANGED,
        schema.METHOD_THREAD_TOKEN_USAGE_UPDATED,
        schema.METHOD_TURN_STARTED,
        schema.METHOD_TURN_COMPLETED,
        schema.METHOD_ITEM_STARTED,
        schema.METHOD_ITEM_COMPLETED,
        schema.METHOD_ITEM_AGENT_MESSAGE_DELTA,
        schema.METHOD_ITEM_REASONING_SUMMARY_TEXT_DELTA,
        schema.METHOD_ITEM_COMMAND_EXECUTION_OUTPUT_DELTA,
    }
)

#: `TurnStatus` → `TurnEnd.outcome`. `inProgress` should never arrive on a
#: `turn/completed`, but if it does the turn is over as far as the client is
#: concerned and there is no failure signal in it — painting one would be a
#: fabricated error, so it reads as a plain success.
_OUTCOME_BY_STATUS: Final[dict[schema.TurnStatus, str]] = {
    schema.TurnStatus.COMPLETED: "success",
    schema.TurnStatus.INTERRUPTED: "interrupted",
    schema.TurnStatus.FAILED: "error",
    schema.TurnStatus.IN_PROGRESS: "success",
}

#: Item statuses that mean the work finished successfully. Everything else
#: (`failed`, `declined`, `interrupted`, a still-`inProgress` item on a
#: completion) reads as not-ok.
_OK_STATUS: Final = "completed"


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TranslatorCounters:
    """An observability snapshot. Every field is a plain count.

    Deliberately no per-item-type or per-method breakdown: an unmodelled item
    `type` is an arbitrary upstream string, and a counter keyed by one would put
    native text into a metrics label.
    """

    unknown_items: int = 0
    unknown_notifications: int = 0
    mismatched_params: int = 0
    orphan_deltas: int = 0
    suppressed_duplicate_texts: int = 0
    truncated_outputs: int = 0
    skipped_context_usage: int = 0
    collab_agent_states_seen: int = 0


@dataclass
class _MutableCounters:
    unknown_items: int = 0
    unknown_notifications: int = 0
    mismatched_params: int = 0
    orphan_deltas: int = 0
    suppressed_duplicate_texts: int = 0
    truncated_outputs: int = 0
    skipped_context_usage: int = 0
    collab_agent_states_seen: int = 0

    def snapshot(self) -> TranslatorCounters:
        return TranslatorCounters(
            unknown_items=self.unknown_items,
            unknown_notifications=self.unknown_notifications,
            mismatched_params=self.mismatched_params,
            orphan_deltas=self.orphan_deltas,
            suppressed_duplicate_texts=self.suppressed_duplicate_texts,
            truncated_outputs=self.truncated_outputs,
            skipped_context_usage=self.skipped_context_usage,
            collab_agent_states_seen=self.collab_agent_states_seen,
        )


@dataclass(slots=True)
class _ToolShape:
    """The `ToolCall` half of one item's normalized form."""

    name: str
    kind: ev.ToolKind
    input: dict[str, Any]


@dataclass(slots=True)
class _StreamState:
    """Per-thread streaming state. Reset wholesale by :meth:`reset`."""

    turn_id: str | None = None
    turn_text: str = ""
    #: turn id → that turn's last token-usage breakdown. Keyed rather than a
    #: single slot because `thread/tokenUsage/updated` names its own turn, and a
    #: late update for a previous turn must not be billed to the live one.
    usage_by_turn: dict[str, dict[str, int]] = field(default_factory=dict)
    #: Item ids whose text/summary has already been rendered — via deltas or via
    #: a delta-less completion. The delta/final dedupe rule lives here.
    rendered_text: set[str] = field(default_factory=set)
    #: Item ids announced by an `item/started`, whatever their type. Only used to
    #: tell a normal delta from an orphan one.
    started: set[str] = field(default_factory=set)
    #: Item ids that already emitted a `ToolCall`.
    called: set[str] = field(default_factory=set)
    #: Item ids already replayed as a `UserMessage`.
    user_messages: set[str] = field(default_factory=set)
    #: item id → aggregated command output assembled from output deltas.
    command_output: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------


class CodexEventTranslator:
    """Turn parsed Codex notification DTOs into normalized ccswitch events.

    One instance per live thread. `thread_id` and `model` are optional seeds for
    the resume path, where the thread was created by an earlier process and
    `thread/started` is never re-sent; a `thread/started` that does arrive wins.
    """

    def __init__(self, *, thread_id: str | None = None, model: str | None = None) -> None:
        self._thread_id = thread_id
        self._model = model
        self._counters = _MutableCounters()
        self._state = _StreamState()

    # -- introspection -----------------------------------------------------

    @property
    def counters(self) -> TranslatorCounters:
        return self._counters.snapshot()

    @property
    def turn_id(self) -> str | None:
        """The in-flight turn, or None between turns."""
        return self._state.turn_id

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    @property
    def model(self) -> str | None:
        return self._model

    def reset(self) -> None:
        """Drop all streaming state, keeping the thread identity and counters.

        Called when a runner reconnects: the item ids and half-streamed text of
        the previous connection describe events no attached client can still be
        waiting on, and carrying them over would suppress a legitimate re-render.
        """
        self._state = _StreamState()

    # -- notification dispatch ---------------------------------------------

    def translate(
        self, method: str, params: schema.ServerNotificationParams
    ) -> list[object]:
        """Translate one supported server notification.

        Returns `[]` — never raises — for a method this translator does not own
        and for params whose type does not match the method, so a routing
        mistake degrades to silence plus a counter rather than to a dead session.
        """
        if method not in TRANSLATED_NOTIFICATIONS:
            self._counters.unknown_notifications += 1
            return []
        if method == schema.METHOD_THREAD_STARTED:
            return self._thread_started(params)
        if method == schema.METHOD_THREAD_STATUS_CHANGED:
            return self._thread_status_changed(params)
        if method == schema.METHOD_THREAD_TOKEN_USAGE_UPDATED:
            return self._token_usage(params)
        if method == schema.METHOD_TURN_STARTED:
            return self._turn_started(params)
        if method == schema.METHOD_TURN_COMPLETED:
            return self._turn_completed(params)
        if method == schema.METHOD_ITEM_STARTED:
            if not isinstance(params, schema.ItemStartedParams):
                return self._mismatch()
            return self.translate_item(
                params.item, turn_id=params.turn_id, completed=False
            )
        if method == schema.METHOD_ITEM_COMPLETED:
            if not isinstance(params, schema.ItemCompletedParams):
                return self._mismatch()
            return self.translate_item(
                params.item, turn_id=params.turn_id, completed=True
            )
        if method == schema.METHOD_ITEM_AGENT_MESSAGE_DELTA:
            return self._agent_message_delta(params)
        if method == schema.METHOD_ITEM_REASONING_SUMMARY_TEXT_DELTA:
            return self._reasoning_summary_delta(params)
        return self._command_output_delta(params)

    def _mismatch(self) -> list[object]:
        self._counters.mismatched_params += 1
        return []

    # -- thread ------------------------------------------------------------

    def _thread_started(
        self, params: schema.ServerNotificationParams
    ) -> list[object]:
        if not isinstance(params, schema.ThreadStartedParams):
            return self._mismatch()
        thread = params.thread
        self._thread_id = thread.id
        self._model = thread.model or self._model
        # No `SessionMeta` here, deliberately. This notification cannot be relied
        # on to carry it: the driver only learns its thread id from the
        # `thread/start` RESPONSE, so anything the app-server emits before that
        # response is parsed has no registered owner and is dropped by the
        # manager (which counts it) — and a RESUMED thread never sends this
        # notification at all. `SessionMeta` is minted from the authoritative
        # response instead, by the driver live and by `CodexThreadReplay` on
        # attach, exactly as Claude mints it from the SDK and from its
        # transcript's meta entry. Two writers for one frame is how a session
        # ends up announcing two different models.
        return []

    def _thread_status_changed(
        self, params: schema.ServerNotificationParams
    ) -> list[object]:
        if not isinstance(params, schema.ThreadStatusChangedParams):
            return self._mismatch()
        status = params.status.type
        if status == "active":
            return [ev.Status("working")]
        if status == "idle":
            return [ev.Status("idle")]
        if status == "systemError":
            return [
                ev.ErrorEvent(
                    code=THREAD_SYSTEM_ERROR_CODE,
                    message=THREAD_SYSTEM_ERROR_MESSAGE,
                    turn_id=self._state.turn_id,
                )
            ]
        # `notLoaded` is a lifecycle detail of the app-server's thread cache, not
        # a state any attached client can act on.
        return []

    def _token_usage(self, params: schema.ServerNotificationParams) -> list[object]:
        if not isinstance(params, schema.ThreadTokenUsageUpdatedParams):
            return self._mismatch()
        usage = params.token_usage
        self._state.usage_by_turn[params.turn_id] = _usage_dict(usage.last)
        window = usage.model_context_window
        if window is None or window <= 0:
            # Never divide by zero and never invent a window size: a wrong
            # denominator here shows the user a fabricated context percentage.
            self._counters.skipped_context_usage += 1
            return []
        used = usage.total.total_tokens
        return [
            ev.ContextUsage(
                used_tokens=used,
                max_tokens=window,
                percentage=used / window * 100,
                model=self._model,
            )
        ]

    # -- turns -------------------------------------------------------------

    def _turn_started(self, params: schema.ServerNotificationParams) -> list[object]:
        if not isinstance(params, schema.TurnStartedParams):
            return self._mismatch()
        turn = params.turn
        self._begin_turn(turn.id)
        # The turn's `items` are not translated: on `turn/started` they are
        # empty, and anything that follows arrives as its own `item/started`.
        return [ev.TurnStart(turn.id)]

    def _turn_completed(self, params: schema.ServerNotificationParams) -> list[object]:
        if not isinstance(params, schema.TurnCompletedParams):
            return self._mismatch()
        turn = params.turn
        text = self._state.turn_text
        pr_resolution = resolution_from_text(text)
        outcome = _OUTCOME_BY_STATUS[turn.status]
        if outcome == "success" and (
            COMPLETION_MARKER in text or pr_resolution is not None
        ):
            outcome = "complete"
        out: list[object] = []
        if turn.error is not None:
            # Emit the failure inline, anchored to this turn and BEFORE the
            # turnEnd, so it renders in place rather than after the turn closed.
            out.append(
                ev.ErrorEvent(
                    code=turn.error.codex_error_info or TURN_ERROR_CODE,
                    message=turn.error.message or TURN_ERROR_MESSAGE,
                    turn_id=turn.id,
                )
            )
        out.append(
            ev.TurnEnd(
                turn.id,
                usage=self._state.usage_by_turn.get(turn.id),
                outcome=outcome,
                pr_resolution=pr_resolution,
                # Never honoured for Codex — see the module docstring.
                model_request=None,
            )
        )
        # The completed turn's `items` are the full transcript of what already
        # streamed item by item; replaying them here would double every tool
        # card and every paragraph of text.
        self._end_turn()
        return out

    def _begin_turn(self, turn_id: str) -> None:
        state = self._state
        state.turn_id = turn_id
        state.turn_text = ""
        state.usage_by_turn.clear()
        state.rendered_text.clear()
        state.started.clear()
        state.called.clear()
        state.user_messages.clear()
        state.command_output.clear()

    def _end_turn(self) -> None:
        self._state.turn_id = None
        self._state.turn_text = ""
        self._state.usage_by_turn.clear()

    # -- deltas ------------------------------------------------------------

    def _agent_message_delta(
        self, params: schema.ServerNotificationParams
    ) -> list[object]:
        if not isinstance(params, schema.AgentMessageDeltaParams):
            return self._mismatch()
        self._note_delta(params.item_id)
        self._state.rendered_text.add(params.item_id)
        self._accumulate(params.turn_id, params.delta)
        return [ev.TextDelta(params.turn_id, params.delta)]

    def _reasoning_summary_delta(
        self, params: schema.ServerNotificationParams
    ) -> list[object]:
        if not isinstance(params, schema.ReasoningSummaryTextDeltaParams):
            return self._mismatch()
        self._note_delta(params.item_id)
        self._state.rendered_text.add(params.item_id)
        # Reasoning is NOT part of the turn text: the completion marker is a
        # contract about the agent's final message, and scanning a summary for
        # it would let a musing about the marker close a session.
        return [ev.ThinkingDelta(params.turn_id, params.delta)]

    def _command_output_delta(
        self, params: schema.ServerNotificationParams
    ) -> list[object]:
        if not isinstance(params, schema.CommandExecutionOutputDeltaParams):
            return self._mismatch()
        self._note_delta(params.item_id)
        state = self._state
        buffered = state.command_output.get(params.item_id, "")
        # Bound the buffer as it grows, not only at emit time: a command
        # streaming megabytes must not be held in memory in full.
        if len(buffered) < MAX_TOOL_OUTPUT_CHARS:
            state.command_output[params.item_id] = (buffered + params.delta)[
                : MAX_TOOL_OUTPUT_CHARS + 1
            ]
        # There is no normalized "tool output delta" event; the aggregate rides
        # the ToolResult when the item completes.
        return []

    def _note_delta(self, item_id: str) -> None:
        """Count the FIRST delta for an item id no `item/started` announced.

        Not an error — the text still streams, because dropping what the user is
        reading would be a far worse failure than a mis-ordered notification.
        It is the signal that the app-server's item lifecycle and ccswitch's
        view of it have diverged.
        """
        state = self._state
        if (
            item_id in state.started
            or item_id in state.rendered_text
            or item_id in state.command_output
        ):
            return
        self._counters.orphan_deltas += 1

    # -- items -------------------------------------------------------------

    def translate_item(
        self, item: schema.ThreadItem, *, turn_id: str, completed: bool
    ) -> list[object]:
        """Translate one thread item.

        Also the replay entry point: `thread/read` hands back finished items with
        no matching `item/started`, so a `completed=True` item that never emitted
        a `ToolCall` emits the call and the result together, in that order.
        """
        if isinstance(item, schema.UnknownThreadItem):
            self._counters.unknown_items += 1
            return []
        if not completed:
            self._state.started.add(item.id)
        if isinstance(item, schema.UserMessageItem):
            if item.id in self._state.user_messages:
                return []
            self._state.user_messages.add(item.id)
            return [ev.UserMessage(turn_id, item.text)]
        if isinstance(item, (schema.AgentMessageItem, schema.ReasoningItem)):
            # An `item/started` for a text item announces an EMPTY item — the
            # content arrives as deltas or on the completion. Rendering (and so
            # marking) it here would suppress the real text that follows.
            if not completed:
                return []
            if isinstance(item, schema.AgentMessageItem):
                return self._final_text(item.id, item.text, turn_id, thinking=False)
            return self._final_text(
                item.id, _SUMMARY_JOIN.join(item.summary), turn_id, thinking=True
            )
        if isinstance(item, schema.ContextCompactionItem):
            return [ev.Condensing(active=not completed)]

        shape = _tool_shape(item)
        out: list[object] = []
        if item.id not in self._state.called:
            self._state.called.add(item.id)
            out.append(
                ev.ToolCall(turn_id, item.id, shape.name, shape.kind, shape.input)
            )
        if completed:
            out.append(self._tool_result(item))
        return out

    def _final_text(
        self, item_id: str, text: str, turn_id: str, *, thinking: bool
    ) -> list[object]:
        """Render an item's final text only when nothing streamed for it.

        This is the delta/final dedupe: the app-server sends both a delta stream
        and a terminal item for the same content, and the replay path sends only
        the terminal item.
        """
        if item_id in self._state.rendered_text:
            self._counters.suppressed_duplicate_texts += 1
            return []
        self._state.rendered_text.add(item_id)
        if not text:
            return []
        if thinking:
            return [ev.ThinkingDelta(turn_id, text)]
        self._accumulate(turn_id, text)
        return [ev.TextDelta(turn_id, text)]

    def _accumulate(self, turn_id: str, text: str) -> None:
        """Collect the in-flight turn's agent text for the completion-marker scan.

        Scoped to the in-flight turn: a replayed item, or a notification for a
        turn other than the live one, must not contribute a marker that would
        then close the turn actually running.
        """
        if turn_id == self._state.turn_id:
            self._state.turn_text += text

    def _tool_result(self, item: schema.ThreadItem) -> ev.ToolResult:
        if isinstance(item, schema.CommandExecutionItem):
            return ev.ToolResult(
                item.id, self._command_ok(item), self._command_payload(item)
            )
        if isinstance(item, schema.FileChangeItem):
            return ev.ToolResult(
                item.id, item.status == _OK_STATUS, {"status": item.status}
            )
        if isinstance(item, (schema.McpToolCallItem, schema.DynamicToolCallItem)):
            return ev.ToolResult(
                item.id, item.status == _OK_STATUS, {"status": item.status}
            )
        if isinstance(item, schema.CollabAgentToolCallItem):
            # `agentsStates` was unread before #1274; it is real per-collaborator
            # progress (`CollabAgentState.status`/`.message`), but its keys are
            # the collaborators' own thread ids, and per
            # `test_sub_agent_activity_never_carries_the_native_agent_thread_id`
            # a native id must never ride in a generic wire payload — so only the
            # count reaches the counters (an operator signal that this vendor
            # surface is actually exercised), never the ids or messages.
            if item.agents_states:
                self._counters.collab_agent_states_seen += len(item.agents_states)
            return ev.ToolResult(
                item.id, item.status == _OK_STATUS, {"status": item.status}
            )
        if isinstance(item, schema.SubAgentActivityItem):
            # `kind` is the lifecycle marker rather than a status; only an
            # interruption is a failure.
            return ev.ToolResult(
                item.id, item.kind != "interrupted", {"kind": item.kind}
            )
        # webSearch / plan / functionCallOutput / imageView carry no status: the
        # item exists only once the work is done.
        return ev.ToolResult(item.id, True, {})

    def _command_ok(self, item: schema.CommandExecutionItem) -> bool:
        # The brief's rule is `exitCode == 0`; a declined or still-running
        # command has no exit code at all, and `None == 0` would silently render
        # every one of them as a plain failure with no distinction from a real
        # non-zero exit, so the status decides when the code is absent.
        if item.exit_code is not None:
            return item.exit_code == 0
        return item.status == _OK_STATUS

    def _command_payload(self, item: schema.CommandExecutionItem) -> dict[str, Any]:
        # Pop rather than read: the item is finished, so the buffer is released
        # here instead of living until the next turn starts.
        buffered = self._state.command_output.pop(item.id, "")
        raw = item.aggregated_output
        if raw is None:
            raw = buffered
        if len(raw) > MAX_TOOL_OUTPUT_CHARS:
            self._counters.truncated_outputs += 1
            raw = raw[:MAX_TOOL_OUTPUT_CHARS] + OUTPUT_TRUNCATION_MARKER
        return {
            "exitCode": item.exit_code,
            "output": raw,
            "durationMs": item.duration_ms,
        }


# ---------------------------------------------------------------------------
# Item → tool shape
# ---------------------------------------------------------------------------


def _tool_shape(item: schema.ThreadItem) -> _ToolShape:
    """The canonical `ToolCall` name/kind/input for one tool-shaped item.

    The names are the Claude tool names on purpose: the web presenter, the
    permission card and the transcript renderer already key off them, so a Codex
    `commandExecution` renders as the Bash card the user already knows.
    """
    if isinstance(item, schema.CommandExecutionItem):
        return _ToolShape(
            "Bash", ev.ToolKind.BASH, {"command": item.command, "cwd": item.cwd}
        )
    if isinstance(item, schema.FileChangeItem):
        return _ToolShape(
            "Edit",
            ev.ToolKind.EDIT,
            {
                "changes": [
                    {"path": change.path, "kind": change.kind}
                    for change in item.changes
                ]
            },
        )
    if isinstance(item, schema.McpToolCallItem):
        name = f"mcp__{item.server}__{item.tool}"
        # `tool_kind` owns the `mcp__` prefix rule (including the ccswitch
        # app-action carve-out); re-deriving it here would fork that decision.
        return _ToolShape(name, ev.tool_kind(name), _arguments_dict(item.arguments))
    if isinstance(item, schema.WebSearchItem):
        return _ToolShape("WebSearch", ev.ToolKind.WEB, {"query": item.query})
    if isinstance(item, schema.PlanItem):
        return _ToolShape("Plan", ev.ToolKind.TODO, {"text": item.text})
    if isinstance(item, schema.SubAgentActivityItem):
        # `agent_thread_id` is a native id and stays out of the payload: ids
        # belong only in the correlation fields. `subagent_type`/`description`
        # are the Claude-shaped keys `server/sessions/subagents.py` and its web
        # mirror already read for the dashboard sub-agent summary (#1274) — the
        # older `agent`/`kind` keys stay too, since they predate that contract
        # and nothing has stopped reading them.
        return _ToolShape(
            "Task",
            ev.ToolKind.TASK,
            {
                "agent": item.agent_path,
                "kind": item.kind,
                "subagent_type": item.agent_path,
                "description": item.kind,
            },
        )
    if isinstance(item, schema.CollabAgentToolCallItem):
        # Never the prompt — the upstream item carries one and schema.py does
        # not even read it. Same identity note as above: `subagent_type` names
        # the collab tool invoked, never a receiver/sender thread id.
        return _ToolShape(
            "Task",
            ev.ToolKind.TASK,
            {
                "tool": item.tool,
                "model": item.model,
                "subagent_type": item.tool,
                "description": item.model or item.tool,
            },
        )
    if isinstance(item, schema.DynamicToolCallItem):
        # A dynamic tool's name is chosen by the Codex side and only looks like
        # a ccswitch tool name by coincidence, so it is never kind-mapped: an
        # accidental `Edit` must not render an edit card over a foreign payload.
        return _ToolShape(
            item.tool, ev.ToolKind.UNKNOWN, _arguments_dict(item.arguments)
        )
    if isinstance(item, schema.FunctionCallOutputItem):
        return _ToolShape(item.name, ev.ToolKind.UNKNOWN, {})
    if isinstance(item, schema.ImageViewItem):
        return _ToolShape("Read", ev.ToolKind.READ, {"file_path": item.path})
    raise AssertionError("item type is not tool-shaped")  # pragma: no cover


def _arguments_dict(arguments: Any) -> dict[str, Any]:
    """Normalize a tool's `arguments` to the dict `ToolCall.input` requires."""
    if isinstance(arguments, dict):
        return dict(arguments)
    if arguments is None:
        return {}
    return {"arguments": arguments}


def _usage_dict(breakdown: schema.TokenUsageBreakdown) -> dict[str, int]:
    """Codex token usage in the key names `turnEnd.usage` already carries.

    The web reducer reads `input_tokens` / `output_tokens` /
    `cache_read_input_tokens` from this dict, and it is engine-neutral — so the
    Codex numbers are expressed in that vocabulary rather than a second one.
    """
    return {
        "input_tokens": breakdown.input_tokens,
        "output_tokens": breakdown.output_tokens,
        "cache_read_input_tokens": breakdown.cached_input_tokens,
        "cache_creation_input_tokens": breakdown.cache_write_input_tokens,
        "reasoning_output_tokens": breakdown.reasoning_output_tokens,
        "total_tokens": breakdown.total_tokens,
    }
