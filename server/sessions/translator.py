from __future__ import annotations
import dataclasses as _dc
from claude_agent_sdk import (SystemMessage, StreamEvent, AssistantMessage, UserMessage,
                              ResultMessage, ToolUseBlock, ToolResultBlock)
from . import events as ev


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
        self._turn_id: str | None = None
        self._started = False
        self._seen_tools: set[str] = set()

    def _ensure_turn(self, uuid) -> list:
        if not self._started:
            self._turn_id = self._turn_id or uuid or "turn"
            self._started = True
            return [ev.TurnStart(self._turn_id, uuid)]
        return []

    def _reset(self):
        self._started = False
        self._turn_id = None
        self._seen_tools = set()

    def feed(self, msg) -> list:
        if isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                self.session_id = (msg.data or {}).get("session_id") or self.session_id
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
            # Text/thinking blocks are intentionally NOT re-emitted here — they stream live
            # via StreamEvent content_block_delta (driver always sets include_partial_messages=True).
            # Only tool calls (not streamed as deltas) are emitted from the consolidated message.
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
            for b in msg.content:
                if isinstance(b, ToolResultBlock):
                    out.append(ev.ToolResult(b.tool_use_id, not bool(b.is_error),
                                             {"content": b.content}, getattr(msg, "uuid", None)))
            return out

        if isinstance(msg, ResultMessage):
            self.session_id = msg.session_id or self.session_id
            tid = self._turn_id or "turn"
            self._reset()
            return [ev.TurnEnd(tid, _usage_dict(msg.usage), getattr(msg, "uuid", None))]

        return []
