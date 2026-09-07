"""Session tool handlers. Each does ONE thing: read the record, or call
``record_feedback`` once. No business logic here (spec §12)."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from ccswitch import brand
from server.mcp_spec import ToolArgError, ToolSpec, annotations_for, tool_err
from server.obs.metrics import session_tool_calls_total
from server.sessions.feedback import CATEGORIES, IMPACTS, record_feedback, validate_item

from .context import SessionToolContext, context_snapshot
from .registry import SESSION_TOOL_SPECS

log = logging.getLogger(__name__)


def tool_ok(data: Any) -> dict[str, Any]:
    env = {"source": brand.SLUG, "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "data": data}
    return {"content": [{"type": "text", "text": json.dumps(env, separators=(",", ":"), ensure_ascii=False)}]}


async def _audit_tool_call(ctx: SessionToolContext, spec: ToolSpec, ok: bool, ms: float) -> None:
    """Best-effort `session.tool_call` audit row, attributed to the session owner with
    ``via="session"``. Own db_session: a tool runs inside the SDK loop, not a request."""
    try:
        from server.audit import record_audit
        from server.db import db_session
        async with db_session() as db:
            await record_audit(db, "session.tool_call", ctx.identity,
                               {"tool": spec.name, "class": spec.tool_class.value, "ok": ok,
                                "ms": round(ms), "session_id": ctx.session_id}, via="session")
            await db.commit()
    except Exception:  # noqa: BLE001 - an audit failure must never fail the tool
        log.warning("session.tool_call audit failed tool=%s", spec.name, exc_info=True)


def build_session_tools(ctx: SessionToolContext) -> list[SdkMcpTool[Any]]:
    def guarded(fn: Any) -> Any:
        spec = SESSION_TOOL_SPECS[fn.__name__]   # build-time KeyError for a spec-less handler

        async def run(args: dict[str, Any]) -> dict[str, Any]:
            t0 = time.monotonic()
            ok = True
            try:
                return tool_ok(await fn(args or {}))
            except ToolArgError as e:
                ok = False
                return tool_err(str(e))
            except Exception as e:  # noqa: BLE001 - never let a tool crash the SDK loop
                ok = False
                log.warning("session tool %s failed", spec.name, exc_info=True)
                return tool_err(f"{brand.NAME} tool error: {e.__class__.__name__}")
            finally:
                session_tool_calls_total.labels(spec.name, spec.tool_class.value,
                                                "ok" if ok else "error").inc()
                await _audit_tool_call(ctx, spec, ok, (time.monotonic() - t0) * 1000)
        return run

    def spec_tool(name: str, description: str, schema: dict[str, Any]) -> Any:
        return tool(name, description, schema, annotations=annotations_for(SESSION_TOOL_SPECS[name]))

    @spec_tool("session_context",
               f"What {brand.NAME} knows about THIS session: id, name, repo, branch, cwd, host, "
               "engine, model, account, the automation that dispatched it (origin) and its "
               "reference (originRef, e.g. owner/repo#123), the effective goal, and the "
               "permission mode. Call it when you need to know how you were dispatched.",
               {"type": "object", "properties": {}, "required": []})
    @guarded
    async def session_context(args: dict[str, Any]) -> Any:
        return context_snapshot(ctx)

    @spec_tool("report_feedback",
               f"Report a problem with {brand.NAME}, the harness that dispatched and supervises "
               "this session — never a bug in the project you are working on or a choice the "
               f"user made. Call it when {brand.NAME}'s instructions contradict each other or the "
               "task, the dispatch prompt was wrong or ambiguous about the repo, branch or "
               f"issue, {brand.NAME} interrupted or switched model or account at a bad moment, or a "
               "worktree, credential, tool or marker it provides is broken. One call per "
               "distinct problem, at most three per session. It records the report for the "
               f"{brand.NAME} backlog and returns immediately — keep working. "
               f"category: one of {', '.join(CATEGORIES)}. impact: one of {', '.join(IMPACTS)}. "
               f"title: 8-120 chars. body: what happened, what you expected, what {brand.NAME} "
               "should change (quote the exact text when the problem is an instruction). "
               "evidence (optional): the offending prompt excerpt, command, or error.",
               {"type": "object",
                "properties": {"category": {"type": "string", "enum": list(CATEGORIES)},
                               "impact": {"type": "string", "enum": list(IMPACTS)},
                               "title": {"type": "string"}, "body": {"type": "string"},
                               "evidence": {"type": "string"}},
                "required": ["category", "impact", "title", "body"]})
    @guarded
    async def report_feedback(args: dict[str, Any]) -> Any:
        from server.config import get_settings
        item = validate_item(args)
        res = await record_feedback(session_id=ctx.session_id, identity=ctx.identity, item=item,
                                    context=context_snapshot(ctx),
                                    cap=get_settings().session_feedback_cap)
        return {"recorded": res.recorded, "status": res.status}

    return [session_context, report_feedback]
