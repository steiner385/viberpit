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
            return [ev.TurnEnd(tid, _usage_dict(msg.usage),
                               getattr(msg, "total_cost_usd", None), getattr(msg, "uuid", None),
                               outcome)]

        return []
