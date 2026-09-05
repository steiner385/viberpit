from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Typing-only, so this module's runtime imports stay stdlib-only. PromptContext is
    # a leaf TypedDict (server/assistant/context.py imports nothing from
    # server.sessions), so this direction can never form a cycle.
    from server.assistant.context import PromptContext


def _now_ms() -> float:
    return time.time() * 1000

class ToolKind(str, Enum):
    EDIT = "edit"; WRITE = "write"; READ = "read"; BASH = "bash"
    TODO = "todo"; SEARCH = "search"; WEB = "web"; TASK = "task"
    MCP = "mcp"; APP_ACTION = "app_action"; UNKNOWN = "unknown"

_TOOL_KIND = {
    "Edit": ToolKind.EDIT, "MultiEdit": ToolKind.EDIT, "NotebookEdit": ToolKind.EDIT,
    "Write": ToolKind.WRITE, "Read": ToolKind.READ, "Bash": ToolKind.BASH,
    "TodoWrite": ToolKind.TODO, "Grep": ToolKind.SEARCH, "Glob": ToolKind.SEARCH,
    "LS": ToolKind.SEARCH, "WebFetch": ToolKind.WEB, "WebSearch": ToolKind.WEB,
    "Task": ToolKind.TASK,
}
# The ccswitch MCP server (server/assistant/tools.py) is the app talking to
# itself, not a third-party integration — it gets its own kind so the web
# presenter can render "Listed sessions" instead of the raw mcp__ name and the
# permission card can apply its non-grantable-destructive rule (spec §4.4).
# Checked before the generic "mcp__" branch so it wins the more specific match.
_APP_ACTION_PREFIX = "mcp__ccswitch__"

def tool_kind(name: str) -> ToolKind:
    # MCP tools are namespaced `mcp__<server>__<tool>`; the server/tool parts are
    # open-ended, so match the prefix rather than enumerate every name.
    if name.startswith(_APP_ACTION_PREFIX):
        return ToolKind.APP_ACTION
    if name.startswith("mcp__"):
        return ToolKind.MCP
    return _TOOL_KIND.get(name, ToolKind.UNKNOWN)

@dataclass
class TurnStart:
    turn_id: str; src_uuid: str | None = None; parent_tool_use_id: str | None = None
    # Ordinal of this assistant entry in the transcript's turn space — the SAME space
    # `ReplayWindow.earliest_turn` and the `?before=` page cursor index. Stamped only by
    # the disk reader (`replay._window`); live-translated and sub-agent turns carry None.
    # The client uses it to verify page merges keep turn order (telemetry only).
    turn_index: int | None = None
    def to_wire(self): return {"type": "turnStart", "turnId": self.turn_id,
                               "srcUuid": self.src_uuid, "parentToolUseId": self.parent_tool_use_id,
                               "turnIndex": self.turn_index}

@dataclass
class TurnModel:
    turn_id: str; model: str; parent_tool_use_id: str | None = None
    def to_wire(self): return {"type": "turnModel", "turnId": self.turn_id,
                               "model": self.model, "parentToolUseId": self.parent_tool_use_id}

@dataclass
class SessionMeta:
    model: str; session_id: str
    # "assistant" for the AI-drawer session's own SDK-level meta event; None for a
    # coding session. Mirrors SessionRecord.kind (server/sessions/types.py) so a
    # live-attaching client can tell the two apart without a separate REST call.
    kind: str | None = None
    def to_wire(self): return {"type": "sessionMeta", "model": self.model,
                               "sessionId": self.session_id, "kind": self.kind}

@dataclass
class TurnEnd:
    turn_id: str; usage: dict | None = None; cost_usd: float | None = None
    src_uuid: str | None = None; parent_tool_use_id: str | None = None; outcome: str = "success"
    pr_resolution: str | None = None
    # Self-directed model request parsed from this turn's text, or None.
    # {"model": str, "reason": str | None}
    model_request: dict | None = None
    def to_wire(self): return {"type": "turnEnd", "turnId": self.turn_id, "usage": self.usage,
                               "costUsd": self.cost_usd, "srcUuid": self.src_uuid,
                               "parentToolUseId": self.parent_tool_use_id, "outcome": self.outcome,
                               "prResolution": self.pr_resolution,
                               "modelRequest": self.model_request}

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
    # False only for destructive `mcp__ccswitch__*` tools (spec §4.4) — those
    # cards never offer "Always allow", so the client must hide the button
    # rather than infer it from toolKind (a mutation app_action IS grantable).
    always_allowed: bool = True
    ts: float | None = field(default_factory=_now_ms)
    def to_wire(self): return {"type": "permissionRequest", "permId": self.perm_id, "turnId": self.turn_id,
                               "tool": self.tool, "toolKind": self.kind.value, "input": self.input, "plan": self.plan,
                               "alwaysAllowed": self.always_allowed, "ts": self.ts}

@dataclass
class PermissionResolved:
    """A pending permission's future settled — allowed or denied (deny_all
    dismissals land here as allowed=False). Same rationale as QuestionResolved:
    the resolution must ride the same stream as the PermissionRequest frame so a
    mid-turn reattach's ring resupply doesn't resurrect an already-decided card."""
    perm_id: str; turn_id: str; allowed: bool
    ts: float | None = field(default_factory=_now_ms)
    def to_wire(self): return {"type": "permissionResolved", "permId": self.perm_id,
                               "turnId": self.turn_id, "allowed": self.allowed, "ts": self.ts}

@dataclass
class Question:
    question_id: str; turn_id: str; header: str; question: str; options: list[dict]; multi_select: bool = False
    ts: float | None = field(default_factory=_now_ms)
    # True when this question's waiter died with the daemon (see
    # server/sessions/orphaned_asks.py). The card is re-offered as answerable, but
    # answering sends a NEW prompt — there is no future left to resolve.
    orphaned: bool = False
    def to_wire(self): return {"type": "question", "questionId": self.question_id, "turnId": self.turn_id,
                               "header": self.header, "question": self.question, "options": self.options,
                               "multiSelect": self.multi_select, "ts": self.ts,
                               "orphaned": self.orphaned}

@dataclass
class QuestionResolved:
    """A pending question's future settled — answered (``selected`` carries the
    labels) or dismissed (``selected`` is None). Emitted through the driver
    fan-out so the resolution rides the same stream as the Question frame: the
    daemon ring replays the original Question on a mid-turn reattach, and without
    this frame an already-answered question resurrects as pending in every
    reattached client (other live tabs would never learn it was answered)."""
    question_id: str; turn_id: str; selected: list[str] | None; mode: str = "option"
    ts: float | None = field(default_factory=_now_ms)
    def to_wire(self): return {"type": "questionResolved", "questionId": self.question_id,
                               "turnId": self.turn_id, "selected": self.selected,
                               "mode": self.mode, "ts": self.ts}

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
    # True ONLY on the copy `replay._window` prepends to a window whose head lands
    # mid-logical-turn: the window's OWNING prompt, re-sent so the turn's content
    # travels with the prompt that asked for it. Such a copy is at a PAGE BOUNDARY,
    # not at the prompt's real transcript position — the same prompt id arrives once
    # per page until the page holding its real position loads. The client uses the
    # mark to drop boundary copies deterministically on merge, and to detect one left
    # pinned mid-transcript (the page-boundary ordering bug, #1000).
    owner_prefix: bool = False
    # The page (route/sessionId) the user was on when they sent this prompt — the
    # "context chip" (spec §4.5). Only ever set on an assistant-kind session; a
    # coding session's UserMessage always carries None. Always the ORIGINAL user
    # text/context pair: the `<ccswitch-context>` header the driver prepends in
    # _start_turn before handing the prompt to the SDK never appears here — this is
    # what the transcript bubble renders.
    context: PromptContext | None = None
    def to_wire(self): return {"type": "user", "turnId": self.turn_id, "text": self.text, "srcUuid": self.src_uuid,
                               "images": self.images or [], "ts": self.ts,
                               "ownerPrefix": self.owner_prefix, "context": self.context}

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
