"""The `ccswitch` MCP tool surface (spec §4.2). Each tool is a thin adapter over ONE API
endpoint: validate args, call, trim, frame as untrusted data. No business logic here."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool
from mcp.types import ToolAnnotations

from .client import AssistantApi, AssistantApiError

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
    # Mutation / destructive specs are appended in Task 3.3.
]}


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
        return _TAG.sub("&lt;", s).replace(">", "&gt;") if "<" in s else s
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


_SESSION_KEYS = ("id", "name", "status", "repo", "branch", "host", "accountName", "model",
                 "summary", "prs", "pendingPermissions", "pendingQuestions", "lastTurnAt",
                 "queuePosition", "pendingModel", "origin")


def _trim_session(s: dict[str, Any]) -> dict[str, Any]:
    return {k: s[k] for k in _SESSION_KEYS if k in s}


def build_tools(api: AssistantApi) -> list[SdkMcpTool[Any]]:
    def guarded(fn: Any) -> Any:
        async def run(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return _ok(await fn(args or {}))
            except AssistantApiError as e:
                return _err(f"ccswitch API error {e.message}")
        return run

    def spec_tool(name: str, description: str, schema: dict[str, Any]) -> Any:
        return tool(name, description, schema, annotations=_annotations(TOOL_SPECS[name]))

    @spec_tool("list_sessions",
               "List live sessions with status, repo, host, account, model, one-line summary, PRs "
               "and whether they are waiting on a permission or question. Filter by status "
               "(waiting_input, in_progress, ready_review, limited, queued…), host or repo. "
               "Prefer search_sessions when looking for one session by topic.",
               {"status": str, "host": str, "repo": str})
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
               "it is stuck.", {"id": str, "last_n": int})
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

    @spec_tool("list_archived_sessions", "Recently archived sessions (newest first).", {"limit": int})
    @guarded
    async def list_archived_sessions(args: dict[str, Any]) -> Any:
        rows = await api.get("/api/sessions/archived")
        return [_trim_session(s) for s in rows[: max(1, min(int(args.get("limit") or 50), 200))]]

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
               "and the owning session. Optional repo filter 'owner/name'.", {"repo": str})
    @guarded
    async def list_prs(args: dict[str, Any]) -> Any:
        rows = await api.get("/api/prs")
        return [p for p in rows if not args.get("repo") or p.get("repo") == args["repo"]]

    @spec_tool("get_pipeline_health", "Slowest and flakiest CI checks for a repo over a window "
               "(24h, 7d, 14d, 30d).", {"repo": str, "window": str})
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
               "why).", {"id": str, "limit": int})
    @guarded
    async def get_automation_events(args: dict[str, Any]) -> Any:
        rows = await api.get(f"/api/automations/{args['id']}/events")
        return rows[: max(1, min(int(args.get("limit") or 20), 100))]

    @spec_tool("get_deploy_status", "Current deploy-controller status.", {})
    @guarded
    async def get_deploy_status(args: dict[str, Any]) -> Any:
        return await api.get("/api/deploy/status")

    return [list_sessions, get_session, get_session_transcript, search_sessions,
            list_archived_sessions, list_accounts, list_hosts, list_prs, get_pipeline_health,
            list_automations, get_automation_events, get_deploy_status]
