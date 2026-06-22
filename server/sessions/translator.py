from __future__ import annotations
import dataclasses as _dc
from claude_agent_sdk import (SystemMessage, StreamEvent, AssistantMessage, UserMessage,
                              ResultMessage, ToolUseBlock, ToolResultBlock, TextBlock)
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
        # Always a string: "" before the first turn opens, a real id after.
        self._turn_id: str = ""
        self._started = False
        self._seen_tools: set[str] = set()
        self._turn_text: str = ""

    def _ensure_turn(self, uuid) -> list:
        if not self._started:
            self._turn_id = self._turn_id or uuid or "turn"
            self._started = True
            return [ev.TurnStart(self._turn_id, uuid)]
        return []

    def _reset(self):
        self._started = False
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
            e = msg.event or {}
            t = e.get("type")
            if t == "message_start":
                return self._ensure_turn(msg.uuid)
            if t == "content_block_delta":
                out = self._ensure_turn(msg.uuid)
                delta = e.get("delta") or {}
                dt = delta.get("type")
                if dt == "text_delta":
                    out.append(ev.TextDelta(self._turn_id, delta.get("text", ""), msg.uuid))
                elif dt == "thinking_delta":
                    out.append(ev.ThinkingDelta(self._turn_id, delta.get("thinking", ""), msg.uuid))
                return out
            return []

        if isinstance(msg, AssistantMessage):
            out = self._ensure_turn(getattr(msg, "uuid", None))
            # Only the top-level agent's OWN final message can signal completion.
            # Sub-agent (Task) messages carry a parent_tool_use_id and may emit the
            # marker for their own sub-objective — exclude them. Reset per top-level
            # message so only the FINAL top-level message's text is matched.
            if getattr(msg, "parent_tool_use_id", None) is None:
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
                    out.append(ev.ToolCall(self._turn_id, b.id, b.name,
                                           ev.tool_kind(b.name), b.input, getattr(msg, "uuid", None)))
            return out

        if isinstance(msg, UserMessage):
            out = []
            for rb in msg.content:
                if isinstance(rb, ToolResultBlock):
                    out.append(ev.ToolResult(rb.tool_use_id, not bool(rb.is_error),
                                             {"content": rb.content}, getattr(msg, "uuid", None)))
            return out

        if isinstance(msg, ResultMessage):
            self.session_id = msg.session_id or self.session_id
            tid = self._turn_id or "turn"
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
                                  outcome))
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
