from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import time


def _now_ms() -> float:
    return time.time() * 1000

class ToolKind(str, Enum):
    EDIT = "edit"; WRITE = "write"; READ = "read"; BASH = "bash"
    TODO = "todo"; SEARCH = "search"; WEB = "web"; TASK = "task"
    MCP = "mcp"; UNKNOWN = "unknown"

_TOOL_KIND = {
    "Edit": ToolKind.EDIT, "MultiEdit": ToolKind.EDIT, "NotebookEdit": ToolKind.EDIT,
    "Write": ToolKind.WRITE, "Read": ToolKind.READ, "Bash": ToolKind.BASH,
    "TodoWrite": ToolKind.TODO, "Grep": ToolKind.SEARCH, "Glob": ToolKind.SEARCH,
    "LS": ToolKind.SEARCH, "WebFetch": ToolKind.WEB, "WebSearch": ToolKind.WEB,
    "Task": ToolKind.TASK,
}
def tool_kind(name: str) -> ToolKind:
    # MCP tools are namespaced `mcp__<server>__<tool>`; the server/tool parts are
    # open-ended, so match the prefix rather than enumerate every name.
    if name.startswith("mcp__"):
        return ToolKind.MCP
    return _TOOL_KIND.get(name, ToolKind.UNKNOWN)

@dataclass
class TurnStart:
    turn_id: str; src_uuid: str | None = None; parent_tool_use_id: str | None = None
    def to_wire(self): return {"type": "turnStart", "turnId": self.turn_id,
                               "srcUuid": self.src_uuid, "parentToolUseId": self.parent_tool_use_id}

@dataclass
class TurnModel:
    turn_id: str; model: str; parent_tool_use_id: str | None = None
    def to_wire(self): return {"type": "turnModel", "turnId": self.turn_id,
                               "model": self.model, "parentToolUseId": self.parent_tool_use_id}

@dataclass
class SessionMeta:
    model: str; session_id: str
    def to_wire(self): return {"type": "sessionMeta", "model": self.model,
                               "sessionId": self.session_id}

@dataclass
class TurnEnd:
    turn_id: str; usage: dict | None = None; cost_usd: float | None = None
    src_uuid: str | None = None; parent_tool_use_id: str | None = None; outcome: str = "success"
    def to_wire(self): return {"type": "turnEnd", "turnId": self.turn_id, "usage": self.usage,
                               "costUsd": self.cost_usd, "srcUuid": self.src_uuid,
                               "parentToolUseId": self.parent_tool_use_id, "outcome": self.outcome}

@dataclass
class TextDelta:
    turn_id: str; text: str; src_uuid: str | None = None; parent_tool_use_id: str | None = None
    ts: float | None = field(default_factory=_now_ms)
    def to_wire(self): return {"type": "textDelta", "turnId": self.turn_id, "text": self.text,
                               "srcUuid": self.src_uuid, "parentToolUseId": self.parent_tool_use_id,
                               "ts": self.ts}

@dataclass
class ThinkingDelta:
    turn_id: str; text: str; src_uuid: str | None = None; parent_tool_use_id: str | None = None
    ts: float | None = field(default_factory=_now_ms)
    def to_wire(self): return {"type": "thinkingDelta", "turnId": self.turn_id, "text": self.text,
                               "srcUuid": self.src_uuid, "parentToolUseId": self.parent_tool_use_id,
                               "ts": self.ts}

@dataclass
class ToolCall:
    turn_id: str; tool_id: str; name: str; kind: ToolKind; input: dict
    src_uuid: str | None = None; parent_tool_use_id: str | None = None
    ts: float | None = field(default_factory=_now_ms)
    def to_wire(self): return {"type": "toolCall", "turnId": self.turn_id, "toolId": self.tool_id,
                               "name": self.name, "toolKind": self.kind.value, "input": self.input,
                               "srcUuid": self.src_uuid, "parentToolUseId": self.parent_tool_use_id,
                               "ts": self.ts}

@dataclass
class ToolResult:
    tool_id: str; ok: bool; payload: dict; src_uuid: str | None = None
    parent_tool_use_id: str | None = None
    ts: float | None = field(default_factory=_now_ms)
    def to_wire(self): return {"type": "toolResult", "toolId": self.tool_id, "ok": self.ok,
                               "payload": self.payload, "srcUuid": self.src_uuid,
                               "parentToolUseId": self.parent_tool_use_id, "ts": self.ts}

@dataclass
class PermissionRequest:
    perm_id: str; turn_id: str; tool: str; kind: ToolKind; input: dict; plan: bool = False
    ts: float | None = field(default_factory=_now_ms)
    def to_wire(self): return {"type": "permissionRequest", "permId": self.perm_id, "turnId": self.turn_id,
                               "tool": self.tool, "toolKind": self.kind.value, "input": self.input, "plan": self.plan,
                               "ts": self.ts}

@dataclass
class Question:
    question_id: str; turn_id: str; header: str; question: str; options: list[dict]; multi_select: bool = False
    ts: float | None = field(default_factory=_now_ms)
    def to_wire(self): return {"type": "question", "questionId": self.question_id, "turnId": self.turn_id,
                               "header": self.header, "question": self.question, "options": self.options,
                               "multiSelect": self.multi_select, "ts": self.ts}

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
class RateLimit:
    """Decoded SDK RateLimitEvent. Internal carrier only: the driver consumes it for
    retry timing (resets_at, epoch seconds) and never forwards it, so it has no
    wire form."""
    resets_at: int | None = None; status: str | None = None

@dataclass
class ApiRetry:
    """Decoded SDK SystemMessage(subtype='api_error'). Internal carrier only: the
    driver consumes it to keep the stall watchdog alive during backoff and to flag
    the transient 'retrying' status. Never forwarded to the client (no wire form)."""
    turn_id: str; status: int | None = None; attempt: int | None = None; max_retries: int | None = None

@dataclass
class Condensing:
    """Context auto-compaction state. Forwarded to the UI: ``active`` toggles the
    progress bar; ``expected_ms`` (set on the active=True start event) is the learned
    expected duration the bar animates against. ``pre_tokens`` is informational."""
    active: bool
    trigger: str | None = None
    pre_tokens: int | None = None
    expected_ms: float | None = None
    def to_wire(self):
        return {"type": "condensing", "active": self.active, "expectedMs": self.expected_ms}

@dataclass
class TurnFailure:
    """A turn failed with a recoverable kind (rate_limit / auth_401). Informational
    for the UI; the orchestrator drives the actual recovery off the same signal."""
    kind: str; account: str; retry_after: float | None = None
    def to_wire(self): return {"type": "turnFailure", "kind": self.kind,
                               "account": self.account, "retryAfter": self.retry_after}

@dataclass
class ErrorEvent:
    code: str; message: str; resets_at: str | None = None; turn_id: str | None = None
    ts: float | None = field(default_factory=_now_ms)
    raw_message: str | None = None  # pre-humanize diagnostic text; internal, not wired
    def to_wire(self): return {"type": "error", "code": self.code, "message": self.message,
                               "resetsAt": self.resets_at, "turnId": self.turn_id, "ts": self.ts}

@dataclass
class Recovering:
    """A turn dropped before the model produced any output and is being auto-resent.
    Self-healing — surfaced to the UI as a soft notice (NOT a red error). The
    orchestrator drives the actual resend off the TurnFailure signal."""
    turn_id: str | None; message: str
    ts: float | None = field(default_factory=_now_ms)
    def to_wire(self): return {"type": "recovering", "turnId": self.turn_id,
                               "message": self.message, "ts": self.ts}

@dataclass
class ReplayComplete:
    def to_wire(self): return {"type": "replayComplete"}

@dataclass
class UserMessage:
    turn_id: str; text: str; src_uuid: str | None = None; images: list[dict] | None = None
    ts: float | None = field(default_factory=_now_ms)
    def to_wire(self): return {"type": "user", "turnId": self.turn_id, "text": self.text, "srcUuid": self.src_uuid,
                               "images": self.images or [], "ts": self.ts}

@dataclass
class QueueUpdate:
    items: list[dict]  # each: {"text": str, "images": [{"url","mime","name"}]}
    def to_wire(self): return {"type": "queueUpdate", "items": self.items}

@dataclass
class Suggestions:
    session_id: str; turn_id: str; items: list[str]
    def to_wire(self): return {"type": "suggestions", "sessionId": self.session_id,
                               "turnId": self.turn_id, "items": self.items}

@dataclass
class ContextUsage:
    """Exact context-window occupancy, pulled from the SDK once a turn settles —
    the same data the `/context` command shows (totalTokens / maxTokens). Drives
    the persistent `ctx %` readout. This REPLACES approximating remaining context
    from ``turnEnd.usage``: that usage is the CLI's per-turn aggregate (cache reads
    summed across every API round-trip in the turn), so a multi-tool turn blows
    past the window and pins the readout at 0%. ``percentage`` is percent USED
    (0–100), mirroring the SDK; ``model`` is the model the usage was computed for."""
    used_tokens: int; max_tokens: int; percentage: float; model: str | None = None
    def to_wire(self): return {"type": "contextUsage", "usedTokens": self.used_tokens,
                               "maxTokens": self.max_tokens, "percentage": self.percentage,
                               "model": self.model}
