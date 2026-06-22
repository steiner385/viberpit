from __future__ import annotations
import dataclasses as _dc
from claude_agent_sdk import (SystemMessage, StreamEvent, AssistantMessage, UserMessage,
                              ResultMessage, ToolUseBlock, ToolResultBlock, TextBlock,
                              RateLimitEvent)
from . import events as ev
from .completion import COMPLETION_MARKER


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
        self._open: set = set()                       # agent keys that emitted TurnStart
        self._turn_id: str = ""                       # mirror of main turn id (driver._turn compat)
        self._seen_tools: set[str] = set()
        self._turn_text: str = ""

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

    def feed(self, msg) -> list:
        if isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                data = msg.data or {}
                self.session_id = data.get("session_id") or self.session_id
                model = data.get("model")
                if model:
                    return [ev.SessionMeta(model=model, session_id=self.session_id or "")]
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
                                 status=getattr(info, "status", None))]

        if isinstance(msg, ResultMessage):
            self.session_id = msg.session_id or self.session_id
            tid = self._turn_ids.get(None) or "turn"
            if msg.subtype != "success" or bool(getattr(msg, "is_error", False)):
                outcome = "failed"
            elif COMPLETION_MARKER in self._turn_text:
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
                                  outcome=outcome))
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

    return ev.ErrorEvent(code=code, message=message, turn_id=turn_id)
