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
        self._turn_ids: dict[str | None, str] = {}   # parent_tool_use_id -> turn id (None = main)
        self._open: set = set()                       # agent keys that emitted TurnStart
        self._turn_id: str = ""                       # mirror of main turn id (driver._turn compat)
        self._seen_tools: set[str] = set()

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

        if isinstance(msg, ResultMessage):
            self.session_id = msg.session_id or self.session_id
            tid = self._turn_ids.get(None) or "turn"
            self._reset()
            return [ev.TurnEnd(tid, _usage_dict(msg.usage),
                               getattr(msg, "total_cost_usd", None), getattr(msg, "uuid", None))]

        return []
