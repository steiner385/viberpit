from __future__ import annotations
import dataclasses as _dc
import logging
from claude_agent_sdk import (SystemMessage, StreamEvent, AssistantMessage, UserMessage,
                              ResultMessage, ToolUseBlock, ToolResultBlock, TextBlock,
                              RateLimitEvent, TaskStartedMessage, TaskProgressMessage,
                              TaskUpdatedMessage, TaskNotificationMessage,
                              TERMINAL_TASK_STATUSES)
from viberpit import brand
from . import events as ev
from .completion import (has_completion_marker, parse_hand_back, parse_hand_back_marker,
                         parse_mark_no_pr, resolution_from_text)
from .model_request import parse_model_request, parse_request_model
from .tools.registry import TURN_SIGNAL_NAMES

log = logging.getLogger(__name__)

# Wire vocabulary for a finished sub-agent (spec §4.1). The SDK's `killed` (a stop_task
# outcome reported by task_updated) is the same end state the notification calls `stopped`.
_ENDED_STATUS = {"completed": "completed", "failed": "failed", "stopped": "stopped",
                 "killed": "stopped"}


def _usage_dict(u):
    if u is None:
        return None
    if isinstance(u, dict):
        return u
    if _dc.is_dataclass(u):
        return _dc.asdict(u)
    if hasattr(u, "__dict__"):
        return dict(vars(u))
    return None


class Translator:
    def __init__(self):
        self.session_id: str | None = None
        self._turn_ids: dict[str | None, str] = {}   # parent_tool_use_id -> turn id (None = main)
        self._open: set[str | None] = set()           # agent keys that emitted TurnStart
        self._turn_id: str = ""                       # mirror of main turn id (driver._turn compat)
        self._seen_tools: set[str] = set()
        self._turn_text: str = ""
        self._turn_model: dict[str, str] = {}   # turn id -> last-emitted model (dedupe)
        # Signalling tool calls made by the TOP-LEVEL agent this turn: stem -> parsed
        # payload. Turn-scoped, NOT message-scoped like `_turn_text` — a session calls
        # mark_complete and then writes a closing paragraph, and a per-message reset
        # would throw the signal away. Cleared only by `_reset()`, at turn end.
        self._turn_signals: dict[str, dict] = {}
        # Task sub-agents: CLI task id -> the spawning Task tool-use id. Lives for the
        # translator's lifetime, NOT the turn: a background sub-agent's notices arrive
        # after the turn that spawned it ended (through the driver's idle listener).
        self._task_tool: dict[str, str] = {}
        # Background (non-agent) tasks: CLI task id -> the launching tool-use id. Same
        # lifetime as _task_tool, same reason (#2635).
        self._bg_task_tool: dict[str, str] = {}
        # Every tool-use id that launched a background task, append-only (one id per
        # launch). The kill sequence is task_updated(killed) — which evicts the map —
        # then task_notification carrying the tool-use id with no map entry left; this
        # set keeps that second frame from falling through to the sub-agent path.
        self._bg_tool_ids: set[str] = set()

    def _ensure_turn(self, uuid, pid=None) -> list:
        if pid in self._open:
            return []
        tid = self._turn_ids.get(pid) or uuid or "turn"
        self._turn_ids[pid] = tid
        self._open.add(pid)
        if pid is None:
            self._turn_id = tid
        return [ev.TurnStart(tid, uuid, parent_tool_use_id=pid)]

    def _turn(self, pid=None) -> str:
        return self._turn_ids.get(pid) or "turn"

    def _reset(self):
        self._turn_ids = {}
        self._open = set()
        self._turn_id = ""
        self._seen_tools = set()
        self._turn_text = ""
        self._turn_model = {}
        self._turn_signals = {}

    def _sub_agent_notice(self, msg) -> list:
        """system/task_* notices → SubAgent* events for a Task sub-agent (spec §4.2), or
        BackgroundTask* events for a backgrounded shell / Monitor / Workflow (#2635). A
        notice with no tool-use id to attach to (and no map entry) is dropped."""
        if isinstance(msg, TaskStartedMessage):
            tuid = msg.tool_use_id
            if not tuid:
                return []
            d = msg.data or {}
            if msg.task_type not in (None, "local_agent"):
                self._bg_task_tool[msg.task_id] = tuid
                self._bg_tool_ids.add(tuid)
                return [ev.BackgroundTaskStarted(
                    tuid, msg.task_id, msg.description or "", str(msg.task_type),
                    backgrounded=bool(d.get("is_backgrounded", True)))]
            self._task_tool[msg.task_id] = tuid
            atype = d.get("subagent_type")
            raw_depth = d.get("spawn_depth")
            return [ev.SubAgentStarted(
                tuid, msg.task_id, msg.description or "",
                atype if isinstance(atype, str) and atype else "agent",
                backgrounded=bool(d.get("is_backgrounded", False)),
                depth=int(raw_depth) if raw_depth is not None else 1)]
        if isinstance(msg, TaskProgressMessage):
            if msg.task_id in self._bg_task_tool:
                return []          # shells report no progress worth a frame
            if msg.tool_use_id and (msg.data or {}).get("task_type") not in (None, "local_agent"):
                return []
            tuid = msg.tool_use_id or self._task_tool.get(msg.task_id)
            if not tuid:
                return []
            u: dict = dict(msg.usage) if msg.usage else {}
            return [ev.SubAgentProgress(
                tuid, msg.task_id, msg.description or "",
                total_tokens=u.get("total_tokens"), tool_uses=u.get("tool_uses"),
                duration_ms=u.get("duration_ms"), last_tool=msg.last_tool_name)]
        if isinstance(msg, TaskUpdatedMessage):
            if msg.status not in TERMINAL_TASK_STATUSES:
                return []
            btuid = self._bg_task_tool.pop(msg.task_id, None)
            if btuid:
                return [ev.BackgroundTaskEnded(
                    btuid, msg.task_id, _ENDED_STATUS.get(msg.status, "failed"))]
            tuid = self._task_tool.get(msg.task_id)
            if not tuid:
                return []
            # The SDK's TaskUpdatedMessage docstring says the terminal state can arrive
            # with no notification following — evict on ANY terminal signal, not just
            # the notification (spec §4.2).
            self._task_tool.pop(msg.task_id, None)
            return [ev.SubAgentEnded(tuid, msg.task_id, _ENDED_STATUS.get(msg.status, "failed"))]
        if isinstance(msg, TaskNotificationMessage):
            btuid = self._bg_task_tool.pop(msg.task_id, None)
            if not btuid and msg.tool_use_id in self._bg_tool_ids:
                btuid = msg.tool_use_id
            if btuid:
                return [ev.BackgroundTaskEnded(
                    btuid, msg.task_id, _ENDED_STATUS.get(msg.status, "failed"))]
            if msg.tool_use_id and (msg.data or {}).get("task_type") not in (None, "local_agent"):
                return []
            tuid = msg.tool_use_id or self._task_tool.pop(msg.task_id, None)
            # The pop above only fires when tool_use_id is absent (the map-resolved
            # path) — this one always evicts, so a notification with its own
            # tool_use_id still clears any stale map entry for this task.
            self._task_tool.pop(msg.task_id, None)
            if not tuid:
                return []
            u = dict(msg.usage) if msg.usage else {}
            return [ev.SubAgentEnded(
                tuid, msg.task_id, _ENDED_STATUS.get(msg.status, "failed"),
                summary=msg.summary or "", total_tokens=u.get("total_tokens"),
                tool_uses=u.get("tool_uses"), duration_ms=u.get("duration_ms"))]
        return []

    def _record_signal(self, stem: str, args) -> None:
        """Store one signalling tool call for the turn.

        Never raises: a malformed argument is dropped (the handler already returned an
        ``is_error`` to the model) rather than failing the turn it was trying to close.
        """
        try:
            if stem == "mark_complete":
                self._turn_signals[stem] = {}
            elif stem == "mark_no_pr":
                self._turn_signals[stem] = parse_mark_no_pr(args or {})
            elif stem == "hand_back":
                self._turn_signals[stem] = parse_hand_back(args or {})
            elif stem == "request_model":
                self._turn_signals[stem] = parse_request_model(args or {})
        except Exception:  # noqa: BLE001 - a bad argument must not fail the turn
            pass

    def has_signal(self, stem: str) -> bool:
        """Has the top-level agent made a usable ``stem`` signal call this turn?"""
        return stem in self._turn_signals

    def message_prose(self) -> str:
        """The text blocks of the LATEST top-level assistant message, as the user sees
        them — never thinking. The consolidated AssistantMessage is queued ahead of the
        in-process MCP call it carries, so once `has_signal` answers True this is the
        prose of the message that made the call."""
        return self._turn_text

    def _turn_end_model_request(self) -> dict | None:
        """Self-directed model request carried by this turn's text, or None.

        Isolated from the ResultMessage branch so it is unit-testable without
        constructing a whole SDK message.
        """
        return parse_model_request(self._turn_text)

    def feed(self, msg) -> list:
        if isinstance(msg, SystemMessage):
            if isinstance(msg, (TaskStartedMessage, TaskProgressMessage,
                                TaskUpdatedMessage, TaskNotificationMessage)):
                return self._sub_agent_notice(msg)
            if msg.subtype == "init":
                data = msg.data or {}
                self.session_id = data.get("session_id") or self.session_id
                model = data.get("model")
                if model:
                    return [ev.SessionMeta(model=model, session_id=self.session_id or "")]
            if msg.subtype == "api_error":
                data = msg.data or {}
                err = data.get("error") or {}
                tid = self._turn_ids.get(None) or "turn"
                return [ev.ApiRetry(
                    turn_id=tid,
                    status=err.get("status"),
                    attempt=data.get("retryAttempt"),
                    max_retries=data.get("maxRetries"),
                )]
            if msg.subtype == "compact_boundary":
                meta = (msg.data or {}).get("compact_metadata") or {}
                return [ev.Condensing(active=False, trigger=meta.get("trigger"),
                                      pre_tokens=meta.get("pre_tokens"))]
            return []

        if isinstance(msg, StreamEvent):
            pid = getattr(msg, "parent_tool_use_id", None)
            e = msg.event or {}
            t = e.get("type")
            if t == "message_start":
                return self._ensure_turn(msg.uuid, pid)
            if t == "content_block_delta":
                out = self._ensure_turn(msg.uuid, pid)
                tid = self._turn(pid)
                delta = e.get("delta") or {}
                dt = delta.get("type")
                if dt == "text_delta":
                    out.append(ev.TextDelta(tid, delta.get("text", ""), msg.uuid, parent_tool_use_id=pid))
                elif dt == "thinking_delta":
                    out.append(ev.ThinkingDelta(tid, delta.get("thinking", ""), msg.uuid, parent_tool_use_id=pid))
                return out
            return []

        if isinstance(msg, AssistantMessage):
            pid = getattr(msg, "parent_tool_use_id", None)
            out = self._ensure_turn(getattr(msg, "uuid", None), pid)
            tid = self._turn(pid)
            model = getattr(msg, "model", None)
            if model and self._turn_model.get(tid) != model:
                self._turn_model[tid] = model
                out.append(ev.TurnModel(tid, model, parent_tool_use_id=pid))
            # Text/thinking blocks are intentionally NOT re-emitted here — they stream live
            # via StreamEvent content_block_delta (driver always sets include_partial_messages=True).
            # Only tool calls (not streamed as deltas) are emitted from the consolidated message.
            #
            # Only the top-level agent's OWN final message can signal completion.
            # Sub-agent (Task) messages carry a parent_tool_use_id and may emit the
            # marker for their own sub-objective — exclude them. Reset per top-level
            # message so only the FINAL top-level message's text is matched.
            if pid is None:
                self._turn_text = ""
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        self._turn_text += b.text
            for b in msg.content:
                if isinstance(b, ToolUseBlock) and b.id not in self._seen_tools:
                    self._seen_tools.add(b.id)
                    # AskUserQuestion is handled by the driver (emits ev.Question);
                    # emitting a generic ToolCall alongside it would render a raw JSON
                    # card on top of the QuestionCard in the frontend.
                    if b.name == "AskUserQuestion":
                        continue
                    # A TOP-LEVEL TURN-signalling call is recorded, not rendered: the
                    # ToolCall card for "mark_complete" would be noise on top of the
                    # status change it causes. The set is TURN_SIGNAL_NAMES, not the
                    # wider SIGNAL_TOOL_NAMES permission class — a SIGNAL tool with a
                    # lasting effect and no turn outcome (`set_hold`) falls through to
                    # a card, which is the transcript's only record of it. Only the
                    # top-level agent's call counts
                    # — a Task sub-agent calling mark_complete finished its own
                    # sub-objective, the same rule the text scan applies at
                    # `pid is None` above. A sub-agent's call therefore falls THROUGH
                    # to an ordinary ToolCall card (the AskUserQuestion precedent in
                    # replay.py:120): it had no effect, and the card is the only way
                    # a human reading the transcript can see the tool was misused.
                    stem = brand.mcp_tool_stem(b.name)
                    if stem is not None and stem in TURN_SIGNAL_NAMES and pid is None:
                        self._record_signal(stem, b.input)
                        continue
                    out.append(ev.ToolCall(tid, b.id, b.name, ev.tool_kind(b.name),
                                           b.input, getattr(msg, "uuid", None),
                                           parent_tool_use_id=pid))
            return out

        if isinstance(msg, UserMessage):
            pid = getattr(msg, "parent_tool_use_id", None)
            out = []
            for rb in msg.content:
                if isinstance(rb, ToolResultBlock):
                    out.append(ev.ToolResult(rb.tool_use_id, not bool(rb.is_error),
                                             {"content": rb.content}, getattr(msg, "uuid", None),
                                             parent_tool_use_id=pid))
            return out

        if isinstance(msg, RateLimitEvent):
            # Surface the rate-limit reset so the driver can use it as a Retry-After
            # when a hard 429 lands this turn. Internal — not forwarded to the UI.
            info = getattr(msg, "rate_limit_info", None)
            return [ev.RateLimit(resets_at=getattr(info, "resets_at", None),
                                 status=getattr(info, "status", None),
                                 rate_limit_type=getattr(info, "rate_limit_type", None))]

        if isinstance(msg, ResultMessage):
            self.session_id = msg.session_id or self.session_id
            tid = self._turn_ids.get(None) or "turn"
            # Precedence within one turn: the TOOL wins (spec §7). A tool call is a
            # deliberate declaration; a marker is a string that may have been quoted,
            # echoed from a plan, or copied out of documentation.
            signals = self._turn_signals
            no_pr = signals.get("mark_no_pr")
            inferred = resolution_from_text(self._turn_text)
            # The note travels with the resolution it explains and only with it —
            # the marker path has no argument to carry one, so a marker turn's note
            # is None rather than a guess scraped out of the prose.
            pr_note: str | None = None
            if no_pr is not None:
                pr_resolution = no_pr["resolution"]
                pr_source: str | None = "tool"
                pr_note = no_pr.get("note")
                if inferred is not None and inferred != pr_resolution:
                    # Spec §8: the declared value against what the prose heuristic
                    # would have guessed. Emitted here because this is the only place
                    # that holds both — a tool handler never sees the turn text.
                    # Logged only on disagreement; agreement is the boring case.
                    log.info("sessions.no_pr_resolution_disagreement declared=%s inferred=%s",
                             pr_resolution, inferred)
            else:
                pr_resolution = inferred
                pr_source = "marker" if pr_resolution is not None else None
            model_request = signals.get("request_model") or self._turn_end_model_request()
            # `pr_resolution is not None` covers BOTH a mark_no_pr tool call and the
            # marker fallback: finishing without a PR is still finishing.
            completed = ("mark_complete" in signals or pr_resolution is not None
                         or has_completion_marker(self._turn_text))
            # The explicit "I need the user" signal: tool first, marker as fallback.
            hand_back = signals.get("hand_back") or parse_hand_back_marker(self._turn_text)
            if msg.subtype != "success" or bool(getattr(msg, "is_error", False)):
                outcome = "failed"
            elif hand_back is not None:
                # Spec §5: a hand-back outranks a completion signal in the same turn.
                # Keeping a session on the board is the recoverable error; archiving
                # one that was asking a question is the failure open_question.py
                # exists to catch. The dropped resolution must not latch either.
                if completed:
                    log.info("sessions.hand_back_over_completion declared_complete=%s "
                             "pr_resolution=%s",
                             "mark_complete" in signals or has_completion_marker(self._turn_text),
                             pr_resolution)
                outcome = "hand_back"
                pr_resolution, pr_source, pr_note = None, None, None
            elif completed:
                outcome = "complete"
            else:
                outcome = "success"
            self._reset()
            out = []
            # An error result carries the agent's failure (max-turns, execution error,
            # or a failing API call's HTTP status). The SDK reports it on the terminal
            # ResultMessage; without surfacing it here the UI sees a normal turnEnd and
            # the user gets silence. Emit it inline (anchored to this turn) BEFORE the
            # turnEnd so it renders in place, then still close the turn with usage/cost
            # (and the coarse outcome classification).
            err = _error_from_result(msg, tid)
            if err is not None:
                out.append(err)
            out.append(ev.TurnEnd(tid, _usage_dict(msg.usage),
                                  getattr(msg, "total_cost_usd", None), getattr(msg, "uuid", None),
                                  outcome=outcome, pr_resolution=pr_resolution,
                                  pr_resolution_source=pr_source,
                                  pr_resolution_note=pr_note,
                                  model_request=model_request,
                                  hand_back_reason=(hand_back.get("reason")
                                                    if outcome == "hand_back" and hand_back else None)))
            return out

        return []


def _error_from_result(msg, turn_id: str):
    """Map a ResultMessage's error surface to an ev.ErrorEvent, or None on success.

    Returns None unless the result is an error (``is_error`` true or an
    ``api_error_status`` is present). A successful result's text already streamed
    via deltas, so nothing new is emitted for it.
    """
    api_status = getattr(msg, "api_error_status", None)
    if not getattr(msg, "is_error", False) and api_status is None:
        return None

    subtype = getattr(msg, "subtype", None) or ""
    if api_status is not None:
        code = f"api_{api_status}"
        if api_status in (500, 502, 503, 529):
            default = (f"Anthropic is overloaded (HTTP {api_status}). The model API was "
                       f"retried and is still overloaded — re-send when capacity recovers.")
        else:
            default = f"The model API returned an error (HTTP {api_status})."
    elif subtype == "error_max_turns":
        code = "max_turns"
        default = "The agent reached the maximum number of turns."
    elif subtype == "error_during_execution":
        code = "execution_error"
        default = "The agent stopped on an execution error."
    else:
        code = "agent_error"
        default = "The agent ended with an error."

    message = getattr(msg, "result", None)
    if not message:
        errors = getattr(msg, "errors", None)
        if errors:
            message = "; ".join(str(e) for e in errors)
    if not message:
        message = default

    return ev.ErrorEvent(code=code, message=_humanize(code, message),
                         turn_id=turn_id, raw_message=message)


# The CLI tags a turn that ended on a pending tool_use (no matching tool_result) with
# this diagnostic. It's an interrupted-mid-tool-call transcript, not anything the user
# did wrong — and ViberPit heals it on the next resume (see transcript_repair). Replace
# the cryptic internals with an honest, actionable message.
_EDE_MARKER = "[ede_diagnostic]"
_EDE_HUMAN = ("The previous turn was interrupted while a tool was running, leaving the "
              "conversation inconsistent. Send another message to continue — the "
              "transcript is repaired automatically on the next turn.")
_EDE_HUMAN_DROPPED = ("The turn ended before the model responded — resending your last "
                      "message automatically to continue.")


def _humanize(code: str, message: str) -> str:
    if code == "execution_error" and _EDE_MARKER in message:
        if "stop_reason=tool_use" in message:
            return _EDE_HUMAN
        if "stop_reason=null" in message:
            return _EDE_HUMAN_DROPPED
    return message
