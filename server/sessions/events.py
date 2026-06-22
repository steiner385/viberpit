from __future__ import annotations
from dataclasses import dataclass
from enum import Enum

class ToolKind(str, Enum):
    EDIT = "edit"; WRITE = "write"; READ = "read"; BASH = "bash"
    TODO = "todo"; SEARCH = "search"; UNKNOWN = "unknown"

_TOOL_KIND = {
    "Edit": ToolKind.EDIT, "MultiEdit": ToolKind.EDIT, "NotebookEdit": ToolKind.EDIT,
    "Write": ToolKind.WRITE, "Read": ToolKind.READ, "Bash": ToolKind.BASH,
    "TodoWrite": ToolKind.TODO, "Grep": ToolKind.SEARCH, "Glob": ToolKind.SEARCH,
    "LS": ToolKind.SEARCH,
}
def tool_kind(name: str) -> ToolKind:
    return _TOOL_KIND.get(name, ToolKind.UNKNOWN)

@dataclass
class TurnStart:
    turn_id: str; src_uuid: str | None = None
    def to_wire(self): return {"type": "turnStart", "turnId": self.turn_id, "srcUuid": self.src_uuid}

@dataclass
class SessionMeta:
    model: str; session_id: str
    def to_wire(self): return {"type": "sessionMeta", "model": self.model, "sessionId": self.session_id}

@dataclass
class TurnEnd:
    turn_id: str; usage: dict | None = None; cost_usd: float | None = None
    src_uuid: str | None = None; outcome: str = "success"
    def to_wire(self): return {"type": "turnEnd", "turnId": self.turn_id, "usage": self.usage,
                               "costUsd": self.cost_usd, "srcUuid": self.src_uuid,
                               "outcome": self.outcome}

@dataclass
class TextDelta:
    turn_id: str; text: str; src_uuid: str | None = None
    def to_wire(self): return {"type": "textDelta", "turnId": self.turn_id, "text": self.text, "srcUuid": self.src_uuid}

@dataclass
class ThinkingDelta:
    turn_id: str; text: str; src_uuid: str | None = None
    def to_wire(self): return {"type": "thinkingDelta", "turnId": self.turn_id, "text": self.text, "srcUuid": self.src_uuid}

@dataclass
class ToolCall:
    turn_id: str; tool_id: str; name: str; kind: ToolKind; input: dict; src_uuid: str | None = None
    def to_wire(self): return {"type": "toolCall", "turnId": self.turn_id, "toolId": self.tool_id,
                               "name": self.name, "toolKind": self.kind.value, "input": self.input, "srcUuid": self.src_uuid}

@dataclass
class ToolResult:
    tool_id: str; ok: bool; payload: dict; src_uuid: str | None = None
    def to_wire(self): return {"type": "toolResult", "toolId": self.tool_id, "ok": self.ok, "payload": self.payload, "srcUuid": self.src_uuid}

@dataclass
class PermissionRequest:
    perm_id: str; turn_id: str; tool: str; kind: ToolKind; input: dict; plan: bool = False
    def to_wire(self): return {"type": "permissionRequest", "permId": self.perm_id, "turnId": self.turn_id,
                               "tool": self.tool, "toolKind": self.kind.value, "input": self.input, "plan": self.plan}

@dataclass
class Question:
    question_id: str; turn_id: str; header: str; question: str; options: list[dict]; multi_select: bool = False
    def to_wire(self): return {"type": "question", "questionId": self.question_id, "turnId": self.turn_id,
                               "header": self.header, "question": self.question, "options": self.options, "multiSelect": self.multi_select}

@dataclass
class Title:
    title: str; source: str
    def to_wire(self): return {"type": "title", "title": self.title, "source": self.source}

@dataclass
class Status:
    state: str; count: int = 0
    def to_wire(self): return {"type": "status", "state": self.state, "count": self.count}

@dataclass
class Rotated:
    from_account: str; to_account: str; reason: str; turn_restarted: bool
    def to_wire(self): return {"type": "rotated", "from": self.from_account, "to": self.to_account,
                               "reason": self.reason, "turnRestarted": self.turn_restarted}

@dataclass
class ErrorEvent:
    code: str; message: str; resets_at: str | None = None; turn_id: str | None = None
    def to_wire(self): return {"type": "error", "code": self.code, "message": self.message,
                               "resetsAt": self.resets_at, "turnId": self.turn_id}

@dataclass
class ReplayComplete:
    def to_wire(self): return {"type": "replayComplete"}

@dataclass
class UserMessage:
    turn_id: str; text: str; src_uuid: str | None = None; images: list[dict] | None = None
    def to_wire(self): return {"type": "user", "turnId": self.turn_id, "text": self.text, "srcUuid": self.src_uuid, "images": self.images or []}

@dataclass
class QueueUpdate:
    items: list[str]
    def to_wire(self): return {"type": "queueUpdate", "items": self.items}
