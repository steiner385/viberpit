"""The `ccswitch` MCP tool surface (spec §4.2). Each tool is a thin adapter over ONE API
endpoint: validate args, call, trim, frame as untrusted data. No business logic here."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool
from mcp.types import ToolAnnotations

from .client import AssistantApi, AssistantApiError

log = logging.getLogger(__name__)

MCP_PREFIX = "mcp__ccswitch__"


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
    ToolSpec("search_sessions", ToolClass.READ, "Search sessions"),
    ToolSpec("list_archived_sessions", ToolClass.READ, "List archived sessions"),
    ToolSpec("list_accounts", ToolClass.READ, "List accounts"),
    ToolSpec("list_hosts", ToolClass.READ, "List fleet hosts"),
    ToolSpec("list_prs", ToolClass.READ, "List pull requests"),
    ToolSpec("get_pipeline_health", ToolClass.READ, "Get CI check health"),
    ToolSpec("list_automations", ToolClass.READ, "List automations"),
    ToolSpec("get_automation_events", ToolClass.READ, "Get automation events"),
    ToolSpec("get_deploy_status", ToolClass.READ, "Get deploy status"),
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
    ToolSpec("set_autopilot_paused", ToolClass.MUTATION, "Pause/resume autopilot"),
    ToolSpec("fix_pr", ToolClass.MUTATION, "Fix a pull request"),
    ToolSpec("set_automation_enabled", ToolClass.MUTATION, "Enable/disable automation"),
    # DESTRUCTIVE — irreversible. `PermissionRegistry.request` gives these a card in
    # EVERY mode (including full-auto) and never offers "Always allow" (spec §4.4).
    ToolSpec("delete_session", ToolClass.DESTRUCTIVE, "Permanently delete session"),
    ToolSpec("cancel_queued_session", ToolClass.DESTRUCTIVE, "Cancel queued session"),
    ToolSpec("ready_merge_pr", ToolClass.DESTRUCTIVE, "Ready + auto-merge PR"),
]}


class ToolArgError(ValueError):
    """An argument a handler rejects LOCALLY, before any HTTP call — so the model gets a
    corrective message without the API ever seeing the request (spec §4.4)."""


def annotation_for(mcp_name: str) -> ToolSpec | None:
    if not mcp_name.startswith(MCP_PREFIX):
        return None
    return TOOL_SPECS.get(mcp_name[len(MCP_PREFIX):])


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


def wrap_untrusted(data: Any) -> str:
    return json.dumps({"source": "ccswitch-api", "trust": "untrusted-data",
                       "data": _scrub(data)}, separators=(",", ":"), ensure_ascii=False)


def _ok(data: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": wrap_untrusted(data)}]}


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


def _schema(required: tuple[str, ...] = (), **fields: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {k: (t if isinstance(t, dict) else {"type": _JSON_TYPE[t]})
                       for k, t in fields.items()},
        "required": list(required),
    }


def _trim_session(s: dict[str, Any]) -> dict[str, Any]:
    return {k: s[k] for k in _SESSION_KEYS if k in s}


def _trim_archived(s: dict[str, Any]) -> dict[str, Any]:
    return {k: s[k] for k in _ARCHIVED_KEYS if k in s}


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
        async with db_session() as db:
            await record_audit(db, "assistant.tool_call", api.identity,
                               {"tool": spec.name, "class": spec.tool_class.value,
                                "ok": ok, "ms": round(ms)}, via="assistant")
    except Exception:  # noqa: BLE001 - an audit failure must never fail the tool
        log.warning("assistant.tool_call audit failed tool=%s", spec.name, exc_info=True)


def build_tools(api: AssistantApi) -> list[SdkMcpTool[Any]]:
    def guarded(fn: Any) -> Any:
        # Looked up eagerly: a handler with no ToolSpec is a build-time error here
        # rather than a tool the permission layer silently treats as unannotated.
        spec = TOOL_SPECS[fn.__name__]

        async def run(args: dict[str, Any]) -> dict[str, Any]:
            t0 = time.monotonic()
            ok = True
            try:
                return _ok(await fn(args or {}))
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
               "it is stuck.", _schema(("id",), id=str, last_n=int))
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
                events.append({"tool": ev.get("name")})
            elif t in ("permissionRequest", "question"):
                events.append({"pending": t, "tool": ev.get("tool"), "text": ev.get("question")})
        return {"events": events[-n * 4:], "totalTurns": data.get("totalTurns")}

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
        return [_trim_archived(s) for s in rows[: max(1, min(int(args.get("limit") or 50), 200))]]

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

    return [list_sessions, get_session, get_session_transcript, search_sessions,
            list_archived_sessions, list_accounts, list_hosts, list_prs, get_pipeline_health,
            list_automations, get_automation_events, get_deploy_status,
            create_session, send_prompt, interrupt_session, answer_question,
            resolve_permission, set_model, set_permission_mode, resolve_model_approval,
            archive_session, unarchive_session, set_autopilot_paused, fix_pr,
            set_automation_enabled,
            delete_session, cancel_queued_session, ready_merge_pr]
