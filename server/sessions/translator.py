from __future__ import annotations
import dataclasses as _dc
from claude_agent_sdk import (SystemMessage, StreamEvent, AssistantMessage, UserMessage,
                              ResultMessage, ToolUseBlock, ToolResultBlock)
from . import events as ev


def _usage_dict(u):
    if u is None: return None
    if isinstance(u, dict): return u
    if _dc.is_dataclass(u): return _dc.asdict(u)
    if hasattr(u, "__dict__"): return dict(vars(u))
    return None


class _Ctx:
    __slots__ = ("started", "turn_id", "seen_tools")
    def __init__(self):
        self.started = False
        self.turn_id = ""
        self.seen_tools = set()


class Translator:
    def __init__(self):
        self.session_id: str | None = None
        self._ctx: dict[str | None, _Ctx] = {}
        self._meta_emitted = False
        # Known Task tool-uses: agent_id -> {"type":..., "description":..., "parent": parent_agent_id}
        self._tasks: dict[str, dict] = {}

    def _c(self, agent_id) -> _Ctx:
        c = self._ctx.get(agent_id)
        if c is None:
            c = self._ctx[agent_id] = _Ctx()
        return c

    def _ensure_turn(self, agent_id, uuid) -> list:
        c = self._c(agent_id)
        if not c.started:
            c.turn_id = c.turn_id or uuid or "turn"
            c.started = True
            return [ev.TurnStart(c.turn_id, uuid, agent_id=agent_id)]
        return []

    @staticmethod
    def _agent(msg):
        return getattr(msg, "parent_tool_use_id", None)

    def feed(self, msg) -> list:
        if isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                data = msg.data or {}
                self.session_id = data.get("session_id") or self.session_id
                model = data.get("model")
                if model and not self._meta_emitted:
                    self._meta_emitted = True
                    return [ev.SessionMeta(model=model, session_id=self.session_id or "")]
            return []

        if isinstance(msg, StreamEvent):
            agent_id = self._agent(msg)
            e = msg.event or {}
            t = e.get("type")
            if t == "message_start":
                return self._ensure_turn(agent_id, msg.uuid)
            if t == "content_block_delta":
                out = self._ensure_turn(agent_id, msg.uuid)
                c = self._c(agent_id)
                delta = e.get("delta") or {}
                dt = delta.get("type")
                if dt == "text_delta":
                    out.append(ev.TextDelta(c.turn_id, delta.get("text", ""), msg.uuid, agent_id=agent_id))
                elif dt == "thinking_delta":
                    out.append(ev.ThinkingDelta(c.turn_id, delta.get("thinking", ""), msg.uuid, agent_id=agent_id))
                return out
            return []

        if isinstance(msg, AssistantMessage):
            agent_id = self._agent(msg)
            uuid = getattr(msg, "uuid", None)
            out = self._ensure_turn(agent_id, uuid)
            c = self._c(agent_id)
            for b in msg.content:
                if isinstance(b, ToolUseBlock) and b.id not in c.seen_tools:
                    c.seen_tools.add(b.id)
                    # AskUserQuestion is handled by the driver (emits ev.Question);
                    # emitting a generic ToolCall alongside it would render a raw JSON
                    # card on top of the QuestionCard in the frontend.
                    if b.name == "AskUserQuestion":
                        continue
                    out.append(ev.ToolCall(c.turn_id, b.id, b.name, ev.tool_kind(b.name),
                                           b.input, uuid, agent_id=agent_id))
                    if b.name == "Task":
                        inp = b.input if isinstance(b.input, dict) else {}
                        atype = inp.get("subagent_type") or "agent"
                        desc = inp.get("description") or ""
                        self._tasks[b.id] = {"type": atype, "description": desc, "parent": agent_id}
                        out.append(ev.SubAgentUpdate(agent_id=b.id, parent_agent_id=agent_id,
                                                     agent_type=str(atype), description=str(desc),
                                                     status="running"))
            return out

        if isinstance(msg, UserMessage):
            agent_id = self._agent(msg)
            uuid = getattr(msg, "uuid", None)
            out = []
            for rb in msg.content:
                if isinstance(rb, ToolResultBlock):
                    ok = not bool(rb.is_error)
                    out.append(ev.ToolResult(rb.tool_use_id, ok, {"content": rb.content},
                                             uuid, agent_id=agent_id))
                    task = self._tasks.get(rb.tool_use_id)
                    if task is not None:
                        out.append(ev.SubAgentUpdate(agent_id=rb.tool_use_id, parent_agent_id=task["parent"],
                                                     agent_type=str(task["type"]), description=str(task["description"]),
                                                     status="done" if ok else "failed"))
            return out

        if isinstance(msg, ResultMessage):
            agent_id = self._agent(msg)
            self.session_id = msg.session_id or self.session_id
            c = self._c(agent_id)
            tid = c.turn_id or "turn"
            # Reset only this context.
            self._ctx[agent_id] = _Ctx()
            return [ev.TurnEnd(tid, _usage_dict(msg.usage),
                               getattr(msg, "total_cost_usd", None), getattr(msg, "uuid", None),
                               agent_id=agent_id)]

        return []
