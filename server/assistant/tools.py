"""The `ccswitch` MCP tool surface (spec §4.2). Each tool is a thin adapter over ONE API
endpoint: validate args, call, trim, frame as untrusted data. No business logic here."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from claude_agent_sdk import SdkMcpTool, tool
from mcp.types import ToolAnnotations

from ccswitch import brand
from server.obs.metrics import assistant_gap_reports_total, assistant_tool_calls_total

from .client import AssistantApi, AssistantApiError

log = logging.getLogger(__name__)

MCP_PREFIX = brand.MCP_TOOL_PREFIX


class ToolClass(str, Enum):
    READ = "read"
    MUTATION = "mutation"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    tool_class: ToolClass
    title: str          # human label for permission cards ("Archive session")


TOOL_SPECS: dict[str, ToolSpec] = {s.name: s for s in [
    ToolSpec("list_sessions", ToolClass.READ, "List sessions"),
    ToolSpec("get_session", ToolClass.READ, "Get session"),
    ToolSpec("get_session_transcript", ToolClass.READ, "Read session transcript"),
    ToolSpec("get_tool_result", ToolClass.READ, "Read a tool result"),
    ToolSpec("export_session", ToolClass.READ, "Export session transcript"),
    ToolSpec("search_sessions", ToolClass.READ, "Search sessions"),
    ToolSpec("list_archived_sessions", ToolClass.READ, "List archived sessions"),
    ToolSpec("list_accounts", ToolClass.READ, "List accounts"),
    ToolSpec("list_hosts", ToolClass.READ, "List fleet hosts"),
    ToolSpec("list_prs", ToolClass.READ, "List pull requests"),
    ToolSpec("get_pipeline_health", ToolClass.READ, "Get CI check health"),
    ToolSpec("list_repos", ToolClass.READ, "List repositories"),
    ToolSpec("list_branches", ToolClass.READ, "List branches"),
    ToolSpec("list_stage_models", ToolClass.READ, "Get CI stage models"),
    ToolSpec("list_automations", ToolClass.READ, "List automations"),
    ToolSpec("get_automation_events", ToolClass.READ, "Get automation events"),
    ToolSpec("get_deploy_status", ToolClass.READ, "Get deploy status"),
    ToolSpec("list_adoptable_sessions", ToolClass.READ, "List adoptable sessions"),
    ToolSpec("suggest_next_prompt", ToolClass.READ, "Suggest next prompt"),
    ToolSpec("list_notification_channels", ToolClass.READ, "List notification channels"),
    ToolSpec("list_notification_rules", ToolClass.READ, "List notification rules"),
    ToolSpec("list_watches", ToolClass.READ, "List watches"),
    ToolSpec("get_quiet_hours", ToolClass.READ, "Get quiet hours"),
    ToolSpec("test_notification", ToolClass.READ, "Preview notification routing"),
    ToolSpec("list_initiatives", ToolClass.READ, "List initiatives"),
    ToolSpec("get_initiative", ToolClass.READ, "Get initiative"),
    ToolSpec("list_products", ToolClass.READ, "List products"),
    ToolSpec("get_product", ToolClass.READ, "Get product"),
    ToolSpec("get_product_deps", ToolClass.READ, "Get product dependencies"),
    ToolSpec("get_priority_queue", ToolClass.READ, "Get priority queue"),
    ToolSpec("list_triage", ToolClass.READ, "List triage proposals"),
    ToolSpec("get_onboarding_status", ToolClass.READ, "Get product onboarding status"),
    ToolSpec("get_automation_actionable", ToolClass.READ, "Get automation actionable items"),
    ToolSpec("list_automation_sessions", ToolClass.READ, "List automation sessions"),
    ToolSpec("list_backlog_candidates", ToolClass.READ, "List backlog candidates"),
    ToolSpec("get_backlog_groups", ToolClass.READ, "Get backlog groups"),
    ToolSpec("get_cicd_findings", ToolClass.READ, "Get CI/CD doctor findings"),
    ToolSpec("get_pr_doctor_consolidation", ToolClass.READ, "Get PR consolidation candidates"),
    ToolSpec("get_pr_doctor_job", ToolClass.READ, "Get PR doctor job status"),
    ToolSpec("preview_schedule", ToolClass.READ, "Preview an automation schedule"),
    # Pack C — session-start conveniences (spec v2 §4.5): presets, templates and
    # permission profiles a session can be started from.
    ToolSpec("list_presets", ToolClass.READ, "List presets"),
    ToolSpec("list_templates", ToolClass.READ, "List templates"),
    ToolSpec("render_template", ToolClass.READ, "Render a template"),
    ToolSpec("list_permission_profiles", ToolClass.READ, "List permission profiles"),
    # READ because it changes nothing a user can see: it increments a counter and
    # writes an audit row. The ONE tool that calls no endpoint (see build_tools).
    ToolSpec("report_gap", ToolClass.READ, "Record a capability gap"),
    ToolSpec("create_session", ToolClass.MUTATION, "Start a session"),
    ToolSpec("send_prompt", ToolClass.MUTATION, "Send a prompt"),
    ToolSpec("interrupt_session", ToolClass.MUTATION, "Interrupt session"),
    ToolSpec("answer_question", ToolClass.MUTATION, "Answer a question"),
    ToolSpec("resolve_permission", ToolClass.MUTATION, "Resolve a permission"),
    ToolSpec("set_model", ToolClass.MUTATION, "Switch model"),
    ToolSpec("set_permission_mode", ToolClass.MUTATION, "Change permission mode"),
    ToolSpec("resolve_model_approval", ToolClass.MUTATION, "Resolve model approval"),
    ToolSpec("archive_session", ToolClass.MUTATION, "Archive session"),
    ToolSpec("unarchive_session", ToolClass.MUTATION, "Unarchive session"),
    # Batch tools (spec v2 Pack B4): each acts on up to 20 sessions in one confirmed
    # action. The permission card enumerates every target by title (PermissionRequest.
    # items, via batch_items below) rather than just a count.
    ToolSpec("archive_sessions", ToolClass.MUTATION, "Archive sessions"),
    ToolSpec("unarchive_sessions", ToolClass.MUTATION, "Unarchive sessions"),
    ToolSpec("resume_sessions", ToolClass.MUTATION, "Resume sessions"),
    ToolSpec("rotate_sessions", ToolClass.MUTATION, "Rotate sessions"),
    ToolSpec("set_autopilot_paused", ToolClass.MUTATION, "Pause/resume autopilot"),
    ToolSpec("fix_pr", ToolClass.MUTATION, "Fix a pull request"),
    ToolSpec("set_automation_enabled", ToolClass.MUTATION, "Enable/disable automation"),
    ToolSpec("rename_session", ToolClass.MUTATION, "Rename session"),
    ToolSpec("move_in_queue", ToolClass.MUTATION, "Reorder queued session"),
    ToolSpec("pin_account", ToolClass.MUTATION, "Pin session to account"),
    ToolSpec("rotate_session", ToolClass.MUTATION, "Rotate to a different account"),
    ToolSpec("reload_session", ToolClass.MUTATION, "Reload session"),
    ToolSpec("adopt_session", ToolClass.MUTATION, "Adopt an untracked session"),
    ToolSpec("create_notification_rule", ToolClass.MUTATION, "Create notification rule"),
    ToolSpec("delete_notification_rule", ToolClass.MUTATION, "Delete notification rule"),
    ToolSpec("watch", ToolClass.MUTATION, "Create a watch"),
    ToolSpec("delete_watch", ToolClass.MUTATION, "Delete a watch"),
    ToolSpec("set_quiet_hours", ToolClass.MUTATION, "Set quiet hours"),
    ToolSpec("advance_initiative", ToolClass.MUTATION, "Advance initiative"),
    ToolSpec("resolve_triage", ToolClass.MUTATION, "Accept/reject triage proposal"),
    ToolSpec("request_product_sync", ToolClass.MUTATION, "Request product sync"),
    # DESTRUCTIVE — irreversible. `PermissionRegistry.request` gives these a card in
    # EVERY mode (including full-auto) and never offers "Always allow" (spec §4.4).
    ToolSpec("delete_session", ToolClass.DESTRUCTIVE, "Permanently delete session"),
    ToolSpec("delete_sessions", ToolClass.DESTRUCTIVE, "Permanently delete sessions"),
    ToolSpec("cancel_queued_session", ToolClass.DESTRUCTIVE, "Cancel queued session"),
    ToolSpec("ready_merge_pr", ToolClass.DESTRUCTIVE, "Ready + auto-merge PR"),
    # Automation fan-out (spec v2 §4.3, Pack A). DESTRUCTIVE not because they delete
    # anything but because each one STARTS an unattended coding session that then acts
    # on its own — the card is the only point at which a human can stop it.
    ToolSpec("trigger_automation_action", ToolClass.DESTRUCTIVE, "Trigger automation action"),
    ToolSpec("dispatch_backlog", ToolClass.DESTRUCTIVE, "Dispatch backlog work"),
    ToolSpec("consolidate_prs", ToolClass.DESTRUCTIVE, "Consolidate pull requests"),
    # Operator tier (spec v2 §7, Pack O) — only registered and route-allowlisted when
    # `operator_tools_enabled(db)` is true (Task 9.2). See OPERATOR_TOOLS below.
    ToolSpec("start_deploy", ToolClass.DESTRUCTIVE, "Start deploy"),
    ToolSpec("upgrade_host", ToolClass.DESTRUCTIVE, "Upgrade host"),
    ToolSpec("refresh_account", ToolClass.MUTATION, "Refresh account"),
    ToolSpec("set_account_enabled", ToolClass.MUTATION, "Enable/disable account"),
    ToolSpec("start_reauth", ToolClass.MUTATION, "Start account re-auth"),
]}

# Operator-tier tool names (spec v2 §7, Task 9.2): omitted from `build_tools()` and
# rejected by `route_allowed()` unless `operator_tools_enabled(db)` is true. Kept as a
# frozenset (not derived from tool_class) because DESTRUCTIVE/MUTATION also covers
# tools that are NOT operator-gated (e.g. delete_session, ready_merge_pr).
OPERATOR_TOOLS: frozenset[str] = frozenset({
    "start_deploy", "upgrade_host", "refresh_account", "set_account_enabled", "start_reauth",
})


class ToolArgError(ValueError):
    """An argument a handler rejects LOCALLY, before any HTTP call — so the model gets a
    corrective message without the API ever seeing the request (spec §4.4)."""


# Matches the allowlist's initiative-, product- and automation-id character class
# (server/assistant/policy.py) so a bad slug fails locally, not as a 403 from the route
# allowlist. `server/api/pm.py:_slugify` mints product slugs from the same class, and every
# registered automation id (`autopilot`, `backlog-runner`, `cicd-doctor`, `pr-doctor`…) is
# already in it — one class for all three rather than a near-duplicate per pack.
_SLUG = re.compile(r"\A[a-z0-9][a-z0-9-]{0,63}\Z")

# Opaque server-minted ids the assistant only ever echoes back: triage proposal ids
# (`_new_uuid` strings, server/db/models.py:TriageProposal) and pr-doctor job ids
# (`prd-<hex>`, server/automations/pr_doctor_jobs.py). The allowlist rows for both are the
# looser `[A-Za-z0-9-]+`; keep the local check to the same class so a rejection is a
# corrective message rather than a 403.
_OPAQUE_ID = re.compile(r"\A[A-Za-z0-9-]{1,64}\Z")

# The priority queue can rank every open issue across every onboarded product. Only the
# head of it is decision-relevant, and the whole thing would crowd out the conversation.
_QUEUE_ENTRY_CAP = 50

# Same reasoning for the automations' unbounded detection lists: CI/CD findings are served
# 200-at-a-time and every eligible backlog issue is a candidate. `Trimmed` reports the drop,
# so a capped list is never mistaken for the whole one.
_AUTOMATION_LIST_CAP = 50


def _slug(s: str, *, field: str = "slug") -> str:
    # `field` names the ARGUMENT in the corrective message: a tool whose argument is `id`
    # must not tell the model to fix a "slug" it was never given.
    if not _SLUG.match(s):
        raise ToolArgError(f"{field} must be lowercase letters, digits and dashes")
    return s


def _nonempty(s: Any, *, field: str) -> str:
    if not isinstance(s, str) or not s.strip():
        raise ToolArgError(f"{field} must be a non-empty string")
    return s.strip()


def _str_list(v: Any, *, field: str) -> list[str]:
    """A required list-of-strings argument. A JSON-Schema array does not stop the model
    from sending a bare string or an empty list, and both reach the endpoint as an opaque
    422/400 — rejecting here turns that into a corrective message the model can act on."""
    if not isinstance(v, list) or not v:
        raise ToolArgError(f"{field} must be a non-empty array")
    return [_nonempty(x, field=field) for x in v]


def _int_list(v: Any, *, field: str) -> list[int]:
    """Same, for list-of-int arguments. Values are coerced because the model routinely
    emits "42" for an integer field, but only whole numbers survive: parsing `str(x)`
    (rather than `int(x)`) is what rejects 42.7 and True instead of silently reading
    them as 42 and 1."""
    if not isinstance(v, list) or not v:
        raise ToolArgError(f"{field} must be a non-empty array of numbers")
    try:
        return [int(str(x).strip()) for x in v]
    except (TypeError, ValueError):
        raise ToolArgError(f"{field} must contain only whole numbers") from None


def annotation_for(mcp_name: str) -> ToolSpec | None:
    if not mcp_name.startswith(MCP_PREFIX):
        return None
    return TOOL_SPECS.get(mcp_name[len(MCP_PREFIX):])


# Batch tools (spec v2 B4): each takes `ids` + `titles` (equal length, capped) and acts
# on every id in one confirmed action.
BATCH_TOOLS: frozenset[str] = frozenset({"archive_sessions", "unarchive_sessions",
                                         "resume_sessions", "rotate_sessions",
                                         "delete_sessions"})
_BATCH_MAX = 20


def batch_items(mcp_name: str, tool_input: dict[str, Any]) -> list[str] | None:
    """Titles for a batch tool's permission card (spec v2 B4): the card must name every
    target, not count them. Called by the driver BEFORE `PermissionRegistry.request`, so
    it works from the raw, not-yet-validated tool input — a malformed `titles` yields an
    empty list rather than raising (the tool handler itself, `_batch_args`, is what turns
    that into a corrective error for the model). None for non-batch tools."""
    spec = annotation_for(mcp_name)
    if spec is None or spec.name not in BATCH_TOOLS:
        return None
    titles = tool_input.get("titles")
    return [str(t)[:80] for t in titles][:_BATCH_MAX] if isinstance(titles, list) else []


def _batch_args(args: dict[str, Any]) -> list[str]:
    ids, titles = args.get("ids"), args.get("titles")
    if not isinstance(ids, list) or not isinstance(titles, list) or not ids:
        raise ToolArgError("ids and titles are required lists")
    if len(ids) != len(titles):
        raise ToolArgError("titles must name every id, in order — read them off list_sessions")
    if len(ids) > _BATCH_MAX:
        raise ToolArgError(f"at most {_BATCH_MAX} sessions per batch; split the request")
    return [str(i) for i in ids]


def _annotations(spec: ToolSpec) -> ToolAnnotations:
    return ToolAnnotations(
        title=spec.title,
        readOnlyHint=spec.tool_class is ToolClass.READ,
        destructiveHint=spec.tool_class is ToolClass.DESTRUCTIVE,
        idempotentHint=spec.tool_class is ToolClass.READ,
        openWorldHint=False,
    )


_TAG = re.compile(r"<(?=/?[A-Za-z!?])")
_MAX_STR = 2000


def _scrub(v: Any) -> Any:
    if isinstance(v, str):
        s = v if len(v) <= _MAX_STR else v[:_MAX_STR] + "…"
        return _TAG.sub("&lt;", s).replace(">", "&gt;")
    if isinstance(v, list):
        return [_scrub(x) for x in v]
    if isinstance(v, dict):
        return {k: _scrub(x) for k, x in v.items()}
    return v


@dataclass(frozen=True)
class Trimmed:
    """A list a handler cut down: `guarded` reports `dropped` in the envelope so the
    model knows it is looking at a partial answer and can ask for more, rather than
    stating a truncated list as if it were the whole one."""
    data: Any
    dropped: int


def wrap_untrusted(data: Any, *, truncated: int = 0) -> str:
    env: dict[str, Any] = {
        "source": "ccswitch-api", "trust": "untrusted-data",
        # B6: the model's ONLY clock. The system prompt is a frozen cache prefix with
        # no timestamp, and tool results are never part of that prefix, so stamping
        # every result costs no cache hit and is what makes "in the last hour" or
        # "today" answerable at all.
        "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data": _scrub(data),
    }
    if truncated:
        env["truncated"] = truncated
    return json.dumps(env, separators=(",", ":"), ensure_ascii=False)


def _ok(data: Any, truncated: int = 0) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": wrap_untrusted(data, truncated=truncated)}]}


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


# Matches the LIVE-session wire shape from `_record_to_dict`
# (server/api/sessions.py) — that endpoint never emits `branch`, and pending
# permission/question state is already carried by `status`
# (`waiting_input` / `waiting_question`), not separate counters.
_SESSION_KEYS = ("id", "name", "status", "repo", "host", "accountName", "model",
                 "summary", "prs", "queuePosition", "pendingModel", "origin")

# Matches the ARCHIVED-session wire shape from `SessionOrchestrator.list_archived`
# (server/sessions/orchestrator.py) — a DIFFERENT, narrower shape than a live
# session's; it has no `status`/`host`/`model`/`prs` but does carry `archivedAt`.
_ARCHIVED_KEYS = ("id", "name", "account", "repo", "createdAt", "archivedAt")

# JSON-Schema type names for the plain python types used in tool argument specs
# below. `create_sdk_mcp_server` treats a dict-shorthand schema's keys as ALL
# required (`"required": list(properties.keys())` unconditionally) before a
# tool's handler ever runs, so any tool with an optional argument must build
# its schema explicitly rather than rely on the shorthand.
_JSON_TYPE: dict[type, str] = {str: "string", int: "integer", bool: "boolean"}

# For fields no plain python type covers; passed through verbatim by `_schema`.
_STR_ARRAY: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
_INT_ARRAY: dict[str, Any] = {"type": "array", "items": {"type": "integer"}}


def _schema(required: tuple[str, ...] = (), **fields: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {k: (t if isinstance(t, dict) else {"type": _JSON_TYPE[t]})
                       for k, t in fields.items()},
        "required": list(required),
    }


def _result_text(payload: Any) -> str:
    """Render a `ToolResult` payload to plain text.

    `ToolResult.to_wire` (server/sessions/events.py) carries `payload = {"content": …}`
    where the SDK's content is EITHER a plain string OR a list of content blocks
    (`{"type": "text", "text": …}`), depending on the tool — so both shapes are handled
    here rather than at the call site.
    """
    if not isinstance(payload, dict):
        return "" if payload is None else str(payload)
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return "" if content is None else str(content)


def _trim_session(s: dict[str, Any]) -> dict[str, Any]:
    return {k: s[k] for k in _SESSION_KEYS if k in s}


def _trim_archived(s: dict[str, Any]) -> dict[str, Any]:
    return {k: s[k] for k in _ARCHIVED_KEYS if k in s}


# A channel's `url` can embed secrets (a Slack webhook token in the path, an ntfy
# topic, a bearer query param) — never let the raw url reach the model. `headers`
# is dropped outright for the same reason (spec §4.7).
def _trim_channel(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": c.get("id"), "name": c.get("name"), "kind": c.get("kind"),
        "url": urlparse(c["url"]).hostname if c.get("url") else None,
        "enabled": c.get("enabled"), "createdAt": c.get("createdAt"),
    }


async def _audit_tool_call(api: AssistantApi, spec: ToolSpec, ok: bool, ms: float) -> None:
    """Best-effort `assistant.tool_call` audit row (spec §4.10), attributed to the
    drawer's owner with ``via="assistant"``.

    Opens its OWN ``db_session()`` because a tool call runs inside the driver's SDK
    loop, not a request, so there is no request-scoped session to borrow. Every
    failure — DB down, DB not initialised in a unit test — is swallowed: losing an
    audit row must never turn a successful action into a tool error the model then
    retries.
    """
    try:
        from server.audit import record_audit
        from server.db import db_session
        detail: dict[str, Any] = {"tool": spec.name, "class": spec.tool_class.value,
                                  "ok": ok, "ms": round(ms)}
        if spec.name in OPERATOR_TOOLS:
            detail["operatorTier"] = True
        async with db_session() as db:
            await record_audit(db, "assistant.tool_call", api.identity, detail, via="assistant")
    except Exception:  # noqa: BLE001 - an audit failure must never fail the tool
        log.warning("assistant.tool_call audit failed tool=%s", spec.name, exc_info=True)


def build_tools(api: AssistantApi, *, operator: bool = False) -> list[SdkMcpTool[Any]]:
    def guarded(fn: Any) -> Any:
        # Looked up eagerly: a handler with no ToolSpec is a build-time error here
        # rather than a tool the permission layer silently treats as unannotated.
        spec = TOOL_SPECS[fn.__name__]

        async def run(args: dict[str, Any]) -> dict[str, Any]:
            t0 = time.monotonic()
            ok = True
            try:
                res = await fn(args or {})
                return _ok(res.data, res.dropped) if isinstance(res, Trimmed) else _ok(res)
            except ToolArgError as e:
                ok = False
                return _err(str(e))
            except AssistantApiError as e:
                ok = False
                return _err(f"ccswitch API error {e.message}")
            except Exception as e:  # noqa: BLE001 - never let a tool crash the SDK loop
                ok = False
                return _err(f"ccswitch tool error: {e.__class__.__name__}")
            finally:
                assistant_tool_calls_total.labels(
                    spec.name, spec.tool_class.value, "ok" if ok else "error").inc()
                await _audit_tool_call(api, spec, ok, (time.monotonic() - t0) * 1000)
        return run

    def spec_tool(name: str, description: str, schema: dict[str, Any]) -> Any:
        return tool(name, description, schema, annotations=_annotations(TOOL_SPECS[name]))

    @spec_tool("list_sessions",
               "List live sessions with status, repo, host, account, model, one-line summary, PRs "
               "and whether they are waiting on a permission or question. Filter by status "
               "(waiting_input, in_progress, ready_review, limited, queued…), host or repo. "
               "Prefer search_sessions when looking for one session by topic.",
               _schema(status=str, host=str, repo=str))
    @guarded
    async def list_sessions(args: dict[str, Any]) -> Any:
        rows = await api.get("/api/sessions")
        out = [_trim_session(s) for s in rows
               if (not args.get("status") or s.get("status") == args["status"])
               and (not args.get("host") or s.get("host") == args["host"])
               and (not args.get("repo") or s.get("repo") == args["repo"])]
        return out

    @spec_tool("get_session", "Full record for one session id, including pending permission and "
               "question ids, queue position and any parked model approval.", {"id": str})
    @guarded
    async def get_session(args: dict[str, Any]) -> Any:
        return await api.get(f"/api/sessions/{args['id']}")

    @spec_tool("get_session_transcript",
               "The last N turns of a session: user prompts, assistant text and tool NAMES. "
               "Tool inputs/outputs are elided. Use to explain what a session is doing or why "
               "it is stuck. Each tool row carries a toolId — pass it to get_tool_result to "
               "read that one tool call's input and output.",
               _schema(("id",), id=str, last_n=int))
    @guarded
    async def get_session_transcript(args: dict[str, Any]) -> Any:
        n = max(1, min(int(args.get("last_n") or 20), 50))
        data = await api.get(f"/api/sessions/{args['id']}/transcript", params={"limit": n})
        events = []
        for ev in data.get("events", [])[-400:]:
            t = ev.get("type")
            if t == "user":
                events.append({"role": "user", "text": (ev.get("text") or "")[:1000]})
            elif t == "textDelta":
                events.append({"role": "assistant", "text": (ev.get("text") or "")[:1000]})
            elif t == "toolCall":
                # `turnId`/`toolId` are the wire names from ToolCall.to_wire
                # (server/sessions/events.py); toolId is what get_tool_result matches on.
                events.append({"tool": ev.get("name"), "turnId": ev.get("turnId"),
                               "toolId": ev.get("toolId")})
            elif t in ("permissionRequest", "question"):
                events.append({"pending": t, "tool": ev.get("tool"), "text": ev.get("question")})
        keep = events[-n * 4:]
        # Counts rows dropped by THIS tail cut. The `[-400:]` above is a scan bound on
        # raw events, not rows, and only some raw types become rows — so folding it in
        # would report a number that is neither rows nor events. `totalTurns` is the
        # honest whole-transcript size, and it is already in the payload.
        return Trimmed({"events": keep, "totalTurns": data.get("totalTurns")},
                       len(events) - len(keep))

    @spec_tool("get_tool_result",
               "The full input and output of ONE tool call in a session's transcript, by the "
               "toolId from get_session_transcript. Use it to read a failing test's output "
               "or a command's result. Output is cut at 4000 characters.",
               {"id": str, "tool_id": str})
    @guarded
    async def get_tool_result(args: dict[str, Any]) -> Any:
        data = await api.get(f"/api/sessions/{args['id']}/transcript", params={"limit": 50})
        call = result = None
        for ev in data.get("events", []):
            if ev.get("toolId") != args["tool_id"]:
                continue
            if ev.get("type") == "toolCall":
                call = ev
            elif ev.get("type") == "toolResult":
                result = ev
        if call is None and result is None:
            raise ToolArgError("no tool call with that toolId in the last 50 turns")
        out = _result_text((result or {}).get("payload"))
        return {"tool": (call or {}).get("name"), "input": (call or {}).get("input"),
                "ok": (result or {}).get("ok"),
                "output": out if len(out) <= 4000 else out[:4000] + "…"}

    @spec_tool("export_session",
               "The session's transcript as markdown, tail-first, capped at max_chars "
               "(default 20000). The escape hatch when get_session_transcript is too coarse.",
               _schema(("id",), id=str, max_chars=int))
    @guarded
    async def export_session(args: dict[str, Any]) -> Any:
        cap = max(500, min(int(args.get("max_chars") or 20000), 60000))
        text = await api.get_text(f"/api/sessions/{args['id']}/export.md")
        return {"markdown": text[-cap:], "truncatedChars": max(0, len(text) - cap)}

    @spec_tool("search_sessions", "Find sessions whose name or transcript mention all the given "
               "words. Returns ids; call get_session for details.", {"query": str})
    @guarded
    async def search_sessions(args: dict[str, Any]) -> Any:
        return await api.get("/api/sessions/search", params={"q": args["query"]})

    @spec_tool("list_archived_sessions", "Archived sessions, up to limit (no ordering guarantee).",
               _schema(limit=int))
    @guarded
    async def list_archived_sessions(args: dict[str, Any]) -> Any:
        rows = await api.get("/api/sessions/archived")
        lim = max(1, min(int(args.get("limit") or 50), 200))
        return Trimmed([_trim_archived(s) for s in rows[:lim]], max(0, len(rows) - lim))

    @spec_tool("list_accounts", "Accounts with provider, enabled flag, auth status, session/weekly "
               "usage percent and reset times, and how many sessions each is running.", {})
    @guarded
    async def list_accounts(args: dict[str, Any]) -> Any:
        return await api.get("/api/accounts")

    @spec_tool("list_hosts", "Fleet hosts: reachability, running commit, heartbeat age.", {})
    @guarded
    async def list_hosts(args: dict[str, Any]) -> Any:
        return await api.get("/api/fleet/hosts")

    @spec_tool("list_prs", "Open pull requests across repos with check status, merge-queue state "
               "and the owning session. Optional repo filter 'owner/name'.", _schema(repo=str))
    @guarded
    async def list_prs(args: dict[str, Any]) -> Any:
        rows = await api.get("/api/prs")
        return [p for p in rows if not args.get("repo") or p.get("repo") == args["repo"]]

    @spec_tool("get_pipeline_health", "Slowest and flakiest CI checks for a repo over a window "
               "(24h, 7d, 14d, 30d).", _schema(("repo",), repo=str, window=str))
    @guarded
    async def get_pipeline_health(args: dict[str, Any]) -> Any:
        return await api.get("/api/pipeline/insights/checks",
                             params={"repo": args["repo"], "window": args.get("window") or "7d"})

    @spec_tool("list_repos",
               "GitHub repositories the operator's credentials can access, as owner/name with "
               "default branch. Use it to resolve a repo the user names loosely ('the billing "
               "repo') BEFORE create_session. Optional query filters by substring.",
               _schema(query=str))
    @guarded
    async def list_repos(args: dict[str, Any]) -> Any:
        rows = await api.get("/api/github/repos")
        q = (args.get("query") or "").lower()
        out = [r for r in rows if not q or q in str(r.get("nameWithOwner", "")).lower()]
        return Trimmed(out[:100], max(0, len(out) - 100))

    @spec_tool("list_branches",
               "Branches of one repository ('owner/name'), with which one is the default.",
               {"repo": str})
    @guarded
    async def list_branches(args: dict[str, Any]) -> Any:
        return await api.get("/api/github/branches", params={"repo": args["repo"]})

    @spec_tool("list_stage_models",
               "For 'what regressed in CI recently': per-repository CI stage model — which "
               "checks run, typical durations and each repo's regressedChecks (not the "
               "CI/CD doctor's own findings — that's get_cicd_findings). Pair with "
               "get_pipeline_health for a window.", {})
    @guarded
    async def list_stage_models(args: dict[str, Any]) -> Any:
        return await api.get("/api/pipeline/repos")

    @spec_tool("list_automations", "Automations (autopilot, backlog runner, PR doctor, CI/CD "
               "doctor…) with enabled flag, last run and last error.", {})
    @guarded
    async def list_automations(args: dict[str, Any]) -> Any:
        return await api.get("/api/automations")

    @spec_tool("get_automation_events", "Recent events one automation produced (what it did and "
               "why).", _schema(("id",), id=str, limit=int))
    @guarded
    async def get_automation_events(args: dict[str, Any]) -> Any:
        n = max(1, min(int(args.get("limit") or 20), 100))
        return await api.get(f"/api/automations/{args['id']}/events", params={"limit": n})

    @spec_tool("get_deploy_status", "Current deploy-controller status.", {})
    @guarded
    async def get_deploy_status(args: dict[str, Any]) -> Any:
        return await api.get("/api/deploy/status")

    @spec_tool("list_adoptable_sessions",
               "Discover live claude sessions running outside ccswitch that could be adopted "
               "(force-taken-over) into tracking. Each candidate is flagged with a skipReason "
               "when it is not actually adoptable.", {})
    @guarded
    async def list_adoptable_sessions(args: dict[str, Any]) -> Any:
        return await api.get("/api/sessions/adoptable")

    @spec_tool("suggest_next_prompt",
               "Ask the model to suggest what to prompt a session next, based on its current "
               "state. Read-only: this only proposes a prompt, it never sends one.", {"id": str})
    @guarded
    async def suggest_next_prompt(args: dict[str, Any]) -> Any:
        return await api.post(f"/api/sessions/{args['id']}/suggest")

    @spec_tool("list_notification_channels",
               "Configured notification channels (webhook targets etc). URLs are reduced to "
               "just their host — never the full webhook URL, which can embed a secret.", {})
    @guarded
    async def list_notification_channels(args: dict[str, Any]) -> Any:
        rows = await api.get("/api/notifications/channels")
        return [_trim_channel(c) for c in rows]

    @spec_tool("list_notification_rules",
               "Notification routing rules: which events go to which channel, and any repo, "
               "account or automation scope.", {})
    @guarded
    async def list_notification_rules(args: dict[str, Any]) -> Any:
        return await api.get("/api/notifications/rules")

    @spec_tool("get_quiet_hours",
               "The configured quiet-hours window ('HH:MM' start/end, or null if unset) during "
               "which non-critical notifications are suppressed.", {})
    @guarded
    async def get_quiet_hours(args: dict[str, Any]) -> Any:
        return await api.get("/api/notifications/quiet-hours")

    @spec_tool("test_notification",
               "Answers 'what would happen if X event happened right now' (e.g. a session "
               "needing input, a PR going limited) WITHOUT a real event or session — this is "
               "a simulation, not a lookup. Dry-runs the notification routing engine for a "
               "hypothetical event_class (default 'needs_input') and shows which rule would "
               "match and whether quiet hours would suppress it. Delivers nothing.",
               _schema(event_class=str, repo=str, account=str, automation=str))
    @guarded
    async def test_notification(args: dict[str, Any]) -> Any:
        # `TestBody.eventClass` is a plain `str` with a server-side default of
        # "needs_input" (server/api/notifications.py) — Pydantic requires the key be
        # absent or a str, so an explicit `None` would 422. Omit the key when unset
        # and let the server default apply.
        body: dict[str, Any] = {}
        if args.get("event_class"):
            body["eventClass"] = args["event_class"]
        if args.get("repo") is not None:
            body["repo"] = args["repo"]
        if args.get("account") is not None:
            body["account"] = args["account"]
        if args.get("automation") is not None:
            body["automation"] = args["automation"]
        return await api.post("/api/notifications/test", body)
    # The one tool with NO backing endpoint: the gap it records is precisely that no
    # endpoint exists. `test_every_tool_hits_an_allowed_route` exempts it by name.
    @spec_tool("report_gap",
               "MANDATORY second step, every time you tell the user that no tool can do what "
               "they asked (a deploy, a credential/settings change, editing code directly…): "
               "explaining the limitation is not enough on its own — also call this in the "
               "same turn, or the gap goes uncounted. capability is a short kebab-case label "
               "('deploy', 'edit-settings', 'edit-code', 'merge-pr'); detail is one sentence. "
               "It records the gap for the product team and returns immediately.",
               _schema(("capability",), capability=str, detail=str))
    @guarded
    async def report_gap(args: dict[str, Any]) -> Any:
        cap = re.sub(r"[^a-z0-9-]", "-", str(args["capability"]).lower())[:40] or "unknown"
        assistant_gap_reports_total.labels(cap).inc()
        try:
            from server.audit import record_audit
            from server.db import db_session
            async with db_session() as db:
                await record_audit(db, "assistant.gap", api.identity,
                                   {"capability": cap, "detail": str(args.get("detail") or "")[:300]},
                                   via="assistant")
                await db.commit()
        except Exception:  # noqa: BLE001 - telemetry must never fail the answer
            log.warning("assistant.gap audit failed", exc_info=True)
        return {"recorded": cap}

    @spec_tool("list_initiatives",
               "Delivery initiatives (intent → spec → plan → build) with each one's stage, "
               "status and linked repo — NOT which session (if any) is working on it; use "
               "get_initiative(slug) for that. Only intent/spec/plan stage, or a 'blocked' "
               "status, means no active build session; 'build' stage + 'active' status means "
               "one is. Optional status filter.", _schema(status=str))
    @guarded
    async def list_initiatives(args: dict[str, Any]) -> Any:
        rows = await api.get("/api/sdlc/initiatives")
        return [r for r in rows if not args.get("status") or r.get("status") == args["status"]]

    @spec_tool("get_initiative", "One initiative by slug: artifacts, stage history, blockers.",
               {"slug": str})
    @guarded
    async def get_initiative(args: dict[str, Any]) -> Any:
        return await api.get(f"/api/sdlc/initiatives/{_slug(args['slug'])}")

    # -- PM product layer (spec v2 Pack P) ---------------------------------------------
    # "Product" is the product vocabulary for what the PM API calls a project; the tools
    # are named for the word the user says and the path does the renaming.

    @spec_tool("list_products",
               "Onboarded products (the PM layer's projects) with their repos, "
               "cross-product dependency edges, onboarding state and how many features "
               "and work items each has. workItemCount is the open+closed issue count "
               "mirrored for the product's repos.", {})
    @guarded
    async def list_products(args: dict[str, Any]) -> Any:
        return await api.get("/api/pm/projects")

    @spec_tool("get_product",
               "One product by slug: description, status, portfolio tags, onboarding "
               "state, its repos and its dependency edges to other products.",
               {"slug": str})
    @guarded
    async def get_product(args: dict[str, Any]) -> Any:
        return await api.get(f"/api/pm/projects/{_slug(args['slug'])}")

    @spec_tool("get_product_deps",
               "Issue-level dependency edges for a product (what blocks what), each with "
               "the target's state, plus a drift audit: prose-only deps, dependency "
               "targets that resolve to nothing, issues missing from the GitHub board and "
               "checklist-style epics.", {"slug": str})
    @guarded
    async def get_product_deps(args: dict[str, Any]) -> Any:
        return await api.get(f"/api/pm/projects/{_slug(args['slug'])}/deps")

    @spec_tool("get_priority_queue",
               "The global priority queue — what agents will pick up next, ranked across "
               "every onboarded product. `skipped` counts why items did NOT make the "
               "queue, keyed by reason ('deps' = blocked by an open dependency). "
               "min_status limits to items at or above a status ('backlog' or 'ready'; "
               "default 'ready').",
               _schema(min_status=str))
    @guarded
    async def get_priority_queue(args: dict[str, Any]) -> Any:
        params = {"min_status": args["min_status"]} if args.get("min_status") else None
        data = await api.get("/api/pm/queue", params=params)
        # `skipped` (reason -> count) is small and is the whole answer to "what is
        # blocked?", so only `entries` is cut. The drop count rides the envelope, so a
        # model seeing 50 rows never reports "50 items queued".
        entries = data.get("entries") or []
        return Trimmed({**data, "entries": entries[:_QUEUE_ENTRY_CAP]},
                       max(0, len(entries) - _QUEUE_ENTRY_CAP))

    @spec_tool("list_triage",
               "The AI triage inbox: classification proposals awaiting a human decision, "
               "newest first — each with the issue it is about, the feature it proposes, "
               "RICE scores, the model's rationale and its confidence level. Use "
               "resolve_triage to accept or reject one.", {})
    @guarded
    async def list_triage(args: dict[str, Any]) -> Any:
        return await api.get("/api/pm/triage")

    @spec_tool("get_onboarding_status",
               "Where a product is in the onboarding wizard: its state, the next action "
               "to take (register, await_backfill, propose_map, classify, live…) and the "
               "counts behind that decision.", {"slug": str})
    @guarded
    async def get_onboarding_status(args: dict[str, Any]) -> Any:
        return await api.get(f"/api/pm/projects/{_slug(args['slug'])}/onboarding")

    # -- Automations control, read half (spec v2 §4.3, Pack A) -------------------------
    # What each automation has DETECTED and would act on — the half of Pack A that only
    # looks. The fan-out half (trigger/dispatch/consolidate) is DESTRUCTIVE and lands in
    # Task 6.4; nothing here starts work.

    @spec_tool("get_automation_actionable",
               "What one automation is proposing right now: the items it has detected "
               "and could act on (e.g. the PR doctor's failing-CI and merge-conflict "
               "PRs), each with the action kinds it offers. Use the id from "
               "list_automations — for the PR doctor that id is 'pr-doctor'. Looking is "
               "free — acting on an item is a separate, unattended action.", {"id": str})
    @guarded
    async def get_automation_actionable(args: dict[str, Any]) -> Any:
        return await api.get(f"/api/automations/{_slug(args['id'], field='id')}/actionable")

    @spec_tool("list_automation_sessions",
               "The most recent sessions one automation spawned, with their status and the "
               "issue or PR each was started for. Use it to answer 'what has the backlog "
               "runner been working on?'.", {"id": str})
    @guarded
    async def list_automation_sessions(args: dict[str, Any]) -> Any:
        return await api.get(f"/api/automations/{_slug(args['id'], field='id')}/sessions")

    @spec_tool("list_backlog_candidates",
               "Issues the backlog runner considers eligible right now — unclaimed, open "
               "and label-gated — in the order it would pick them. This is what it WOULD "
               "do next; it dispatches nothing.", {})
    @guarded
    async def list_backlog_candidates(args: dict[str, Any]) -> Any:
        data = await api.get("/api/automations/backlog-runner/candidates")
        rows = data.get("candidates") or []
        return Trimmed({**data, "candidates": rows[:_AUTOMATION_LIST_CAP]},
                       max(0, len(rows) - _AUTOMATION_LIST_CAP))

    @spec_tool("get_backlog_groups",
               "The backlog runner's current grouping plan in priority order: which issues "
               "it intends to batch into one session, and the generation the plan came "
               "from.", {})
    @guarded
    async def get_backlog_groups(args: dict[str, Any]) -> Any:
        return await api.get("/api/automations/backlog-runner/groups")

    @spec_tool("get_cicd_findings",
               "For 'CI/CD doctor findings': that automation's own detections (flaky checks, "
               "broken workflows, stuck runs), newest first, each with its repo, kind, status "
               "and any issue it filed (not per-repo timing regressions — that's "
               "list_stage_models). status defaults to 'open'; pass 'all' to include resolved "
               "ones, or a repo 'owner/name' to narrow it.",
               _schema(status=str, repo=str))
    @guarded
    async def get_cicd_findings(args: dict[str, Any]) -> Any:
        params = {k: args[k] for k in ("status", "repo") if args.get(k)}
        rows = await api.get("/api/automations/cicd-doctor/findings", params=params or None)
        return Trimmed(rows[:_AUTOMATION_LIST_CAP],
                       max(0, len(rows) - _AUTOMATION_LIST_CAP))

    @spec_tool("get_pr_doctor_consolidation",
               "Specifically the PR doctor's MERGE-clustering proposal: which open PRs "
               "per repo it thinks should be combined into one (not the failing-CI/"
               "merge-conflict items — those are get_automation_actionable). A proposal "
               "only; nothing is consolidated by reading it.", {})
    @guarded
    async def get_pr_doctor_consolidation(args: dict[str, Any]) -> Any:
        return await api.get("/api/automations/pr-doctor/consolidation")

    @spec_tool("get_pr_doctor_job",
               "Live phase of one async PR-doctor job (cloning → assessing → spawning → "
               "ready) by its job id, and which phase failed if it errored. The job is "
               "gone — and this returns not-found — after a daemon restart.",
               {"job_id": str})
    @guarded
    async def get_pr_doctor_job(args: dict[str, Any]) -> Any:
        jid = str(args["job_id"])
        if not _OPAQUE_ID.match(jid):
            raise ToolArgError("job_id must be a pr-doctor job id (e.g. 'prd-1a2b3c4d')")
        return await api.get(f"/api/automations/pr-doctor/jobs/{jid}")

    @spec_tool("preview_schedule",
               "Validate a candidate automation schedule and return its next 3 fire times "
               "plus the named presets. schedule is an object with an optional 5-field "
               "'cron' ('0 8 * * *') and an optional 'activeHours' "
               "({start:'09:00', end:'18:00', days:[0-6], 0=Monday}). Changes nothing — it "
               "only answers 'when would this run?'.",
               _schema(("schedule",), schedule={"type": "object"}))
    @guarded
    async def preview_schedule(args: dict[str, Any]) -> Any:
        return await api.post("/api/automations/schedule/preview",
                              {"schedule": args["schedule"]})

    # -- Session-start conveniences (spec v2 §4.5, Pack C) ------------------------------
    # Presets, templates and permission profiles a session can be started from. This
    # task only reads them; Task 7.4 wires preset/profile/branch into create_session.

    @spec_tool("list_presets",
               "Saved session-start presets: repo, account, model, permission mode, "
               "initial prompt and permission profile bundled together for reuse.", {})
    @guarded
    async def list_presets(args: dict[str, Any]) -> Any:
        return await api.get("/api/presets")

    @spec_tool("list_templates",
               "Saved prompt templates with their {{variable}} placeholders and tags. "
               "Optional tag narrows the list to templates carrying it.", _schema(tag=str))
    @guarded
    async def list_templates(args: dict[str, Any]) -> Any:
        params = {"tag": args["tag"]} if args.get("tag") else None
        return await api.get("/api/templates", params=params)

    @spec_tool("render_template",
               "Render a saved prompt template by id, substituting {{variable}} "
               "placeholders from vars. Missing variables are left as-is; extra ones are "
               "ignored. Changes nothing — it only returns the rendered text.",
               _schema(("id",), id=str, vars={"type": "object"}))
    @guarded
    async def render_template(args: dict[str, Any]) -> Any:
        tid = str(args["id"])
        if not _OPAQUE_ID.match(tid):
            raise ToolArgError("id must be a template id")
        return await api.post(f"/api/templates/{tid}/render",
                              {"variables": args.get("vars") or {}})

    @spec_tool("list_permission_profiles",
               "Saved permission profiles: named, ordered tool-pattern allowlists (e.g. "
               "'Bash(git *)', 'Read') that can be applied at session spawn.", {})
    @guarded
    async def list_permission_profiles(args: dict[str, Any]) -> Any:
        return await api.get("/api/permission-profiles")

    # -- Mutations (spec §4.2, class M) ------------------------------------------------
    # Each is a thin adapter over ONE endpoint: rename args to the endpoint's field
    # names, call, return. Business logic lives behind the endpoint
    # (server/sessions/session_actions.py), never here.

    @spec_tool("create_session",
               "Start a NEW coding session on a repo with an initial prompt, and return its id "
               "and the URL to open it. Use only when the user wants new work started; to add "
               "to work already running use send_prompt instead.",
               _schema(("prompt",), prompt=str, repo=str, model=str, permission_mode=str,
                       account=str))
    @guarded
    async def create_session(args: dict[str, Any]) -> Any:
        rec = await api.post("/api/sessions", {
            "name": None, "initialPrompt": args["prompt"], "repo": args.get("repo"),
            "model": args.get("model"), "permissionMode": args.get("permission_mode"),
            "account": args.get("account")})
        return {"id": rec["id"], "url": f"/sessions/{rec['id']}"}

    @spec_tool("send_prompt",
               "Send a message to a live session. It is queued behind the turn in flight "
               "unless interrupt is true, which stops that turn and runs this prompt next.",
               _schema(("id", "text"), id=str, text=str, interrupt=bool))
    @guarded
    async def send_prompt(args: dict[str, Any]) -> Any:
        return await api.post(f"/api/sessions/{args['id']}/prompt",
                              {"text": args["text"], "interrupt": bool(args.get("interrupt"))})

    @spec_tool("interrupt_session", "Stop a session's in-flight turn. The session stays live "
               "and can be prompted again straight away.", {"id": str})
    @guarded
    async def interrupt_session(args: dict[str, Any]) -> Any:
        return await api.post(f"/api/sessions/{args['id']}/interrupt")

    @spec_tool("answer_question",
               "Answer a session's pending question. `selected` holds the chosen option labels "
               "— read them off the pending question in get_session first.",
               _schema(("id", "question_id", "selected"), id=str, question_id=str,
                       selected=_STR_ARRAY))
    @guarded
    async def answer_question(args: dict[str, Any]) -> Any:
        return await api.post(f"/api/sessions/{args['id']}/questions/{args['question_id']}",
                              {"selected": list(args["selected"]), "mode": "option"})

    @spec_tool("resolve_permission",
               "Allow or deny one pending permission card on a session. decision is 'allow' or "
               "'deny' — a standing 'always' grant is the human's to give, never yours.",
               {"id": str, "perm_id": str, "decision": str})
    @guarded
    async def resolve_permission(args: dict[str, Any]) -> Any:
        if args.get("decision") not in ("allow", "deny"):
            raise ToolArgError("decision must be 'allow' or 'deny'; only a person can "
                               "grant 'always'")
        return await api.post(f"/api/sessions/{args['id']}/permissions/{args['perm_id']}",
                              {"decision": args["decision"]})

    @spec_tool("set_model", "Switch a session's model (e.g. 'opus', 'sonnet', 'haiku'). Policy "
               "may refuse the change or park it for human approval.", {"id": str, "model": str})
    @guarded
    async def set_model(args: dict[str, Any]) -> Any:
        return await api.post(f"/api/sessions/{args['id']}/model", {"model": args["model"]})

    @spec_tool("set_permission_mode",
               "Change how a session asks for permission: interactive, auto, allowlist, "
               "full-auto, acceptEdits or bypassPermissions.", {"id": str, "mode": str})
    @guarded
    async def set_permission_mode(args: dict[str, Any]) -> Any:
        return await api.post(f"/api/sessions/{args['id']}/permission-mode",
                              {"mode": args["mode"]})

    @spec_tool("resolve_model_approval",
               "Approve or deny a session's parked model-approval hold. Pass model to approve "
               "it at a DIFFERENT model than the one the session asked for.",
               _schema(("id", "decision"), id=str, decision=str, model=str))
    @guarded
    async def resolve_model_approval(args: dict[str, Any]) -> Any:
        return await api.post(f"/api/sessions/{args['id']}/model-approval",
                              {"decision": args["decision"], "model": args.get("model")})

    @spec_tool("archive_session", "Archive a session: it stops running, but its transcript and "
               "row are kept and unarchive_session brings it back.", {"id": str})
    @guarded
    async def archive_session(args: dict[str, Any]) -> Any:
        return await api.delete(f"/api/sessions/{args['id']}") or {"ok": True}

    @spec_tool("unarchive_session", "Bring an archived session back into the live list.",
               {"id": str})
    @guarded
    async def unarchive_session(args: dict[str, Any]) -> Any:
        return await api.post(f"/api/sessions/{args['id']}/unarchive")

    @spec_tool("archive_sessions",
               "Archive SEVERAL sessions in one confirmed action (max 20). titles must list each "
               "session's title in the same order as ids — the confirmation card shows them.",
               _schema(("ids", "titles"), ids=_STR_ARRAY, titles=_STR_ARRAY))
    @guarded
    async def archive_sessions(args: dict[str, Any]) -> Any:
        return await api.post("/api/sessions/batch/archive", {"ids": _batch_args(args)})

    @spec_tool("unarchive_sessions",
               "Bring SEVERAL archived sessions back into the live list in one confirmed action "
               "(max 20). titles must list each session's title in the same order as ids.",
               _schema(("ids", "titles"), ids=_STR_ARRAY, titles=_STR_ARRAY))
    @guarded
    async def unarchive_sessions(args: dict[str, Any]) -> Any:
        return await api.post("/api/sessions/batch/unarchive", {"ids": _batch_args(args)})

    @spec_tool("resume_sessions",
               "Resume SEVERAL restart-interrupted sessions in one confirmed action (max 20): "
               "re-attaches each and nudges it forward. titles must list each session's title in "
               "the same order as ids.",
               _schema(("ids", "titles"), ids=_STR_ARRAY, titles=_STR_ARRAY))
    @guarded
    async def resume_sessions(args: dict[str, Any]) -> Any:
        return await api.post("/api/sessions/batch/resume", {"ids": _batch_args(args)})

    @spec_tool("rotate_sessions",
               "Move SEVERAL sessions to whichever account currently has the most headroom in "
               "one confirmed action (max 20) — the 'Rotate stalled' bulk action. titles must "
               "list each session's title in the same order as ids.",
               _schema(("ids", "titles"), ids=_STR_ARRAY, titles=_STR_ARRAY))
    @guarded
    async def rotate_sessions(args: dict[str, Any]) -> Any:
        return await api.post("/api/sessions/batch/rotate", {"ids": _batch_args(args)})

    @spec_tool("set_autopilot_paused",
               "Pin a session to manual (paused=true) so autopilot leaves it alone, or hand it "
               "back to autopilot (paused=false).", {"id": str, "paused": bool})
    @guarded
    async def set_autopilot_paused(args: dict[str, Any]) -> Any:
        return await api.post(f"/api/sessions/{args['id']}/autopilot",
                              {"paused": bool(args["paused"])})

    @spec_tool("fix_pr", "Ask the session that owns pull request `number` to fix it — resumes "
               "the session and posts a branch-aware fix prompt.", {"id": str, "number": int})
    @guarded
    async def fix_pr(args: dict[str, Any]) -> Any:
        return await api.post(f"/api/sessions/{args['id']}/fix-pr",
                              {"number": int(args["number"])})

    @spec_tool("set_automation_enabled",
               "Turn one automation (autopilot, backlog-runner, pr-doctor…) on or off. Use the "
               "id from list_automations.", {"id": str, "enabled": bool})
    @guarded
    async def set_automation_enabled(args: dict[str, Any]) -> Any:
        return await api.patch(f"/api/automations/{args['id']}",
                               {"enabled": bool(args["enabled"])})

    @spec_tool("rename_session", "Rename a session. name must be 1-120 characters after "
               "trimming whitespace.", {"id": str, "name": str})
    @guarded
    async def rename_session(args: dict[str, Any]) -> Any:
        name = str(args.get("name") or "").strip()
        if not name or len(name) > 120:
            raise ToolArgError("name must be 1-120 characters")
        return await api.patch(f"/api/sessions/{args['id']}", {"name": name})

    @spec_tool("move_in_queue",
               "Reorder a QUEUED session: op is 'up', 'down', 'top' or 'position' (with "
               "position as a 0-based index, required only for 'position'). Fails if the "
               "session is not currently queued.",
               _schema(("id", "op"), id=str, op=str, position=int))
    @guarded
    async def move_in_queue(args: dict[str, Any]) -> Any:
        op = args.get("op")
        if op not in ("up", "down", "top", "position"):
            raise ToolArgError("op must be 'up', 'down', 'top' or 'position'")
        if op == "position" and not isinstance(args.get("position"), int):
            raise ToolArgError("position is required (an integer) when op is 'position'")
        return await api.post(f"/api/sessions/{args['id']}/queue-move",
                              {"op": op, "position": args.get("position")})

    @spec_tool("pin_account",
               "Pin a session to keep using its CURRENT account (pinned=true), or clear the "
               "pin so it can rotate freely again (pinned=false). Unpinning a limited session "
               "rotates it immediately.", {"id": str, "pinned": bool})
    @guarded
    async def pin_account(args: dict[str, Any]) -> Any:
        return await api.post(f"/api/sessions/{args['id']}/account-pin",
                              {"pinned": bool(args["pinned"])})

    @spec_tool("rotate_session",
               "Move a session to whichever account currently has the most headroom and "
               "continue the conversation there. Fails if nothing has headroom or the "
               "session is parked somewhere a move makes no sense.", {"id": str})
    @guarded
    async def rotate_session(args: dict[str, Any]) -> Any:
        return await api.post(f"/api/sessions/{args['id']}/rotate")

    @spec_tool("reload_session",
               "Rebuild a session's driver in place — the recovery for a session that stopped "
               "responding (answers rejected, a stuck turn, a driver the daemon lost).",
               {"id": str})
    @guarded
    async def reload_session(args: dict[str, Any]) -> Any:
        return await api.post(f"/api/sessions/{args['id']}/reload")

    @spec_tool("adopt_session",
               "Force-take-over an untracked live claude session found by "
               "list_adoptable_sessions: terminate it and resume it under ccswitch tracking.",
               {"claude_session_id": str})
    @guarded
    async def adopt_session(args: dict[str, Any]) -> Any:
        return await api.post("/api/sessions/adopt",
                              {"claudeSessionId": args["claude_session_id"]})

    @spec_tool("create_notification_rule",
               "Add a notification routing rule: which event class routes to which channel "
               "(or 'push'/'ignore'), optionally scoped to a repo, account or automation.",
               _schema(("event_class", "action"), event_class=str, action=str, channel_id=str,
                       scope_repo=str, scope_account=str, scope_automation=str))
    @guarded
    async def create_notification_rule(args: dict[str, Any]) -> Any:
        return await api.post("/api/notifications/rules", {
            "eventClass": args["event_class"], "action": args["action"],
            "channelId": args.get("channel_id"), "scopeRepo": args.get("scope_repo"),
            "scopeAccount": args.get("scope_account"),
            "scopeAutomation": args.get("scope_automation")})

    @spec_tool("delete_notification_rule", "Remove a notification routing rule. Use the id from "
               "list_notification_rules.", {"id": str})
    @guarded
    async def delete_notification_rule(args: dict[str, Any]) -> Any:
        return await api.delete(f"/api/notifications/rules/{args['id']}") or {"ok": True}

    @spec_tool("watch",
               "Get notified in the drawer (plus push) the next time something happens, "
               "without starting a new model turn — good for 'let me know when X'. "
               "kind='pr' with condition='breaks' watches target='owner/repo' for its CI "
               "going red; kind='automation' with condition='error' or 'warn' watches "
               "target=<automation id> for that severity; kind='session' with "
               "condition='finishes' watches target=<session id> for that session handing "
               "a turn back; kind='account' with condition='limited' watches "
               "target=<account name> (or 'any' for every account) hitting its usage "
               "ceiling. once=true (default) disables the watch after it fires once; "
               "ttl_hours caps how long it stays armed (default 72).",
               _schema(("kind", "target", "condition"), kind=str, target=str, condition=str,
                       once=bool, ttl_hours=int))
    @guarded
    async def watch(args: dict[str, Any]) -> Any:
        body: dict[str, Any] = {"kind": args["kind"], "target": args["target"],
                                "condition": args["condition"]}
        if "once" in args:
            body["once"] = args["once"]
        if "ttl_hours" in args:
            body["ttl_hours"] = args["ttl_hours"]
        return await api.post("/api/assistant/watches", body)

    @spec_tool("list_watches", "List this identity's active watches.", {})
    @guarded
    async def list_watches(args: dict[str, Any]) -> Any:
        return await api.get("/api/assistant/watches")

    @spec_tool("delete_watch", "Cancel a watch before it fires. Use the id from "
               "list_watches.", {"id": str})
    @guarded
    async def delete_watch(args: dict[str, Any]) -> Any:
        return await api.delete(f"/api/assistant/watches/{args['id']}") or {"ok": True}

    @spec_tool("set_quiet_hours",
               "Set (or clear, by omitting a field) the quiet-hours window during which "
               "non-critical notifications are suppressed. Times are 'HH:MM', 24h, local.",
               _schema(start=str, end=str))
    @guarded
    async def set_quiet_hours(args: dict[str, Any]) -> Any:
        return await api.put("/api/notifications/quiet-hours",
                             {"start": args.get("start"), "end": args.get("end")})

    @spec_tool("advance_initiative",
               "Move an initiative to its next stage (e.g. spec → plan). The stage's gate must "
               "already be satisfied; the API refuses otherwise.", {"slug": str})
    @guarded
    async def advance_initiative(args: dict[str, Any]) -> Any:
        return await api.post(f"/api/sdlc/initiatives/{_slug(args['slug'])}/advance")

    @spec_tool("resolve_triage",
               "Accept or reject ONE AI triage proposal from the inbox — list_triage gives "
               "the ids. Accepting copies the proposed feature and RICE scores onto the "
               "issue and marks it classified; rejecting sends the issue to needs_human. "
               "decision is 'accept' or 'reject'.", {"proposal_id": str, "decision": str})
    @guarded
    async def resolve_triage(args: dict[str, Any]) -> Any:
        decision = args.get("decision")
        if decision not in ("accept", "reject"):
            raise ToolArgError("decision must be 'accept' or 'reject'")
        pid = str(args["proposal_id"])
        if not _OPAQUE_ID.match(pid):
            raise ToolArgError("proposal_id must be a proposal id from list_triage")
        return await api.post(f"/api/pm/triage/{pid}/{decision}")

    @spec_tool("request_product_sync",
               "Ask the reconciler to re-sync a product from GitHub on its next pass. It "
               "is a request, not the sync itself — nothing is synced by the time this "
               "returns.", {"slug": str})
    @guarded
    async def request_product_sync(args: dict[str, Any]) -> Any:
        return await api.post(f"/api/pm/projects/{_slug(args['slug'])}/sync")

    # -- Destructive (spec §4.2, class D) ----------------------------------------------
    # Irreversible. These always surface a permission card, in every mode, and the card
    # never offers "Always allow" (server/sessions/permissions.py).

    @spec_tool("delete_session",
               "PERMANENTLY delete a session: its archive folder and database row are removed "
               "and the transcript is gone for good. archive_session is the reversible option.",
               {"id": str})
    @guarded
    async def delete_session(args: dict[str, Any]) -> Any:
        return await api.delete(f"/api/sessions/{args['id']}/permanent") or {"ok": True}

    @spec_tool("delete_sessions",
               "PERMANENTLY delete SEVERAL sessions in one confirmed action (max 20): each "
               "archive folder and database row is removed and its transcript is gone for good. "
               "There is no undo. titles must list each session's title in the same order as ids.",
               _schema(("ids", "titles"), ids=_STR_ARRAY, titles=_STR_ARRAY))
    @guarded
    async def delete_sessions(args: dict[str, Any]) -> Any:
        return await api.post("/api/sessions/batch/permanent-delete", {"ids": _batch_args(args)})

    @spec_tool("cancel_queued_session",
               "Drop a session that is still QUEUED off the queue. It never runs; its prompt "
               "is lost.", {"id": str})
    @guarded
    async def cancel_queued_session(args: dict[str, Any]) -> Any:
        return await api.delete(f"/api/sessions/{args['id']}/queue") or {"ok": True}

    @spec_tool("ready_merge_pr",
               "Mark a draft pull request ready for review and arm squash auto-merge. It then "
               "merges by itself as soon as CI is green — there is no undo after that.",
               {"repo": str, "number": int})
    @guarded
    async def ready_merge_pr(args: dict[str, Any]) -> Any:
        return await api.post("/api/pipeline/pr/ready-merge",
                              {"repo": args["repo"], "number": int(args["number"])})

    # -- Automation fan-out (spec v2 §4.3, Pack A, class D) -----------------------------
    # The acting half of Pack A. Each spawns an unattended coding session, so each
    # description states that plainly: the model chooses a tool from its description,
    # and a card the user waves through is the last stop before real work starts.
    # Nothing about WHICH items are eligible is decided here — that is the automation's
    # own config (e.g. backlog-runner's `max_issues_per_pr`) behind the endpoint.

    @spec_tool("trigger_automation_action",
               "Fire ONE of an automation's offered actions on ONE detected item — use "
               "get_automation_actionable first for the item id and the kinds it offers. "
               "This launches a coding session that will act on its own; there is no "
               "undo. The user confirms via the permission card this call raises — call "
               "it directly once the user has asked for the action, don't ask again in chat.",
               {"id": str, "item_id": str, "kind": str})
    @guarded
    async def trigger_automation_action(args: dict[str, Any]) -> Any:
        return await api.post(
            f"/api/automations/{_slug(args['id'], field='id')}/trigger",
            {"item_id": _nonempty(args.get("item_id"), field="item_id"),
             "kind": _nonempty(args.get("kind"), field="kind")})

    @spec_tool("dispatch_backlog",
               "Claim the given backlog issues and start work on them NOW, without "
               "waiting for the backlog runner's next pass. refs are 'owner/repo#123' "
               "strings from list_backlog_candidates; they are bundled into one session "
               "and one PR, with the first as the primary. This launches a coding "
               "session that will act on its own; there is no undo. The user confirms "
               "via the permission card this call raises — call it directly once the "
               "user has asked for the dispatch, don't ask again in chat.",
               _schema(("refs",), refs=_STR_ARRAY))
    @guarded
    async def dispatch_backlog(args: dict[str, Any]) -> Any:
        return await api.post("/api/automations/backlog-runner/dispatch",
                              {"refs": _str_list(args.get("refs"), field="refs")})

    @spec_tool("consolidate_prs",
               "Start a session that combines the given open pull requests in one repo "
               "into a single PR — get_pr_doctor_consolidation suggests the clusters. "
               "The session rewrites branches and closes the PRs it folds in. This "
               "launches a coding session that will act on its own; there is no undo. "
               "The user confirms via the permission card this call raises — call it "
               "directly once the user has asked for the consolidation, don't ask again "
               "in chat.",
               _schema(("repo", "numbers"), repo=str, numbers=_INT_ARRAY))
    @guarded
    async def consolidate_prs(args: dict[str, Any]) -> Any:
        return await api.post("/api/automations/pr-doctor/consolidate",
                              {"repo": _nonempty(args.get("repo"), field="repo"),
                               "numbers": _int_list(args.get("numbers"), field="numbers")})

    # -- Operator tier (spec v2 §7, Pack O, class D/M) ---------------------------------
    # Registered only when `operator=True` (Task 9.2). Each is still a thin adapter
    # over ONE endpoint; the route allowlist independently requires `operator=True` too
    # (server/assistant/policy.py), so a disabled setting is enforced at both layers.

    @spec_tool("start_deploy",
               "Starts a production deploy of the given ref (or the current tip if "
               "omitted); there is no undo once it begins.",
               _schema(ref=str))
    @guarded
    async def start_deploy(args: dict[str, Any]) -> Any:
        body = {"targetSha": args["ref"]} if args.get("ref") else None
        return await api.post("/api/deploy/start", body)

    @spec_tool("upgrade_host",
               "Upgrades and restarts the ccswitch daemon on the named fleet host — this "
               "drains or kills every session currently live on that host.",
               {"name": str})
    @guarded
    async def upgrade_host(args: dict[str, Any]) -> Any:
        return await api.post(f"/api/fleet/hosts/{args['name']}/upgrade")

    @spec_tool("refresh_account",
               "Force a fresh usage/status fetch for one account, bypassing the cache.",
               {"name": str})
    @guarded
    async def refresh_account(args: dict[str, Any]) -> Any:
        return await api.post(f"/api/accounts/{args['name']}/refresh")

    @spec_tool("set_account_enabled",
               "Enable or disable an account. A disabled account is blocked everywhere — "
               "never auto-selected, never a rotation target — but its credentials stay "
               "intact and the toggle is fully reversible.",
               {"name": str, "enabled": bool})
    @guarded
    async def set_account_enabled(args: dict[str, Any]) -> Any:
        return await api.patch(f"/api/accounts/{args['name']}/enabled",
                               {"enabled": bool(args["enabled"])})

    @spec_tool("start_reauth",
               "Start a re-authentication flow for an existing account so its credentials "
               "can be refreshed.", {"name": str})
    @guarded
    async def start_reauth(args: dict[str, Any]) -> Any:
        return await api.post(f"/api/accounts/{args['name']}/reauth")

    base_tools = [list_sessions, get_session, get_session_transcript, get_tool_result,
            export_session, search_sessions,
            list_archived_sessions, list_accounts, list_hosts, list_prs, get_pipeline_health,
            list_automations, get_automation_events, get_deploy_status,
            list_adoptable_sessions, suggest_next_prompt,
            list_notification_channels, list_notification_rules, get_quiet_hours,
            test_notification, list_watches,
            list_repos, list_branches, list_stage_models,
            list_initiatives, get_initiative,
            list_products, get_product, get_product_deps, get_priority_queue, list_triage,
            get_onboarding_status,
            get_automation_actionable, list_automation_sessions, list_backlog_candidates,
            get_backlog_groups, get_cicd_findings, get_pr_doctor_consolidation,
            get_pr_doctor_job, preview_schedule,
            list_presets, list_templates, render_template, list_permission_profiles,
            report_gap,
            create_session, send_prompt, interrupt_session, answer_question,
            resolve_permission, set_model, set_permission_mode, resolve_model_approval,
            archive_session, unarchive_session, set_autopilot_paused, fix_pr,
            archive_sessions, unarchive_sessions, resume_sessions, rotate_sessions,
            set_automation_enabled,
            rename_session, move_in_queue, pin_account, rotate_session, reload_session,
            adopt_session,
            create_notification_rule, delete_notification_rule,
            watch, delete_watch,
            set_quiet_hours, advance_initiative, resolve_triage, request_product_sync,
            delete_session, delete_sessions, cancel_queued_session, ready_merge_pr,
            trigger_automation_action, dispatch_backlog, consolidate_prs]
    operator_tools = [start_deploy, upgrade_host, refresh_account, set_account_enabled,
                      start_reauth]
    return base_tools + (operator_tools if operator else [])
