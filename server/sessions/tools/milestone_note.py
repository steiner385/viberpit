"""`roadmap_milestone_note`: a session writes milestone notes, its status line and a
refresh request AS ITSELF (spec 2026-11-05 M8; Phase 3 rulings R1, R2).

SIGNAL, so no permission card: an unattended session must be able to record "the owner
needs to fill X" with nobody there to answer a card. The actor is ``session:<id>``,
never the human owner, and ``_owner`` is deliberately NOT called — the write rules in
``server/pm/milestone_actions.py`` decide what a session may change (only notes it
created). Like ``session_triage.invoke`` this calls the Phase 2 route handlers
in-process; no rule lives here.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError

from server.audit import record_audit
from server.db import db_session
from server.mcp_spec import Summarized, ToolArgError
from server.pm.holds import SESSION_PREFIX
from server.pm.milestone_tool_result import (
    milestone_refusal,
    milestone_tool_error,
    summarized,
)
from server.pm.session_triage import Arguments, delegated_request
from server.sessions.tools.context import SessionToolContext

TOOL = "roadmap_milestone_note"

class MilestoneNoteArgs(Arguments):
    product: str = Field(min_length=1)
    action: Literal["add", "set_status", "status_line", "refresh"]
    kind: str | None = None
    text: str | None = None
    ref: str | None = None
    url: str | None = None
    milestone: str | None = None
    note_id: str | None = None
    status: str | None = None


#: (required, optional) fields per action beyond product/action.
_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "add": (frozenset({"kind", "text"}), frozenset({"ref", "url", "milestone", "status"})),
    "set_status": (frozenset({"note_id"}), frozenset({"status", "text"})),
    "status_line": (frozenset({"milestone"}), frozenset({"text"})),
    "refresh": (frozenset(), frozenset()),
}


def _check_fields(args: MilestoneNoteArgs) -> None:
    required, optional = _FIELDS[args.action]
    given = {k for k, v in args.model_dump(exclude={"product", "action"}).items() if v is not None}
    if missing := sorted(required - given):
        raise ToolArgError(f"action {args.action!r} needs {', '.join(missing)}")
    if extra := sorted(given - required - optional):
        raise ToolArgError(f"action {args.action!r} does not take {', '.join(extra)}")


def _refused_refresh(resp: JSONResponse) -> ToolArgError:
    body = json.loads(bytes(resp.body))
    if resp.status_code == 429:
        return milestone_refusal(429, "refresh_too_soon", {"retryAfter": body.get("retryAfter")})
    return milestone_refusal(resp.status_code, body.get("reason") or "refused")


async def milestone_note(ctx: SessionToolContext, raw: dict[str, Any]) -> Summarized:
    from server.api import pm_milestones as routes
    try:
        args = MilestoneNoteArgs.model_validate(raw)
        _check_fields(args)
        identity = f"{SESSION_PREFIX}{ctx.session_id}"
        request = delegated_request(identity)
        slug = args.product
        async with db_session() as db:
            # Same transaction as the write: a refusal rolls the delegation back too.
            await record_audit(db, "pm.session.delegate", identity,
                               {"sessionId": ctx.session_id, "tool": TOOL,
                                "arguments": args.model_dump()}, via="session")
            if args.action == "add":
                view = await routes.post_milestone_note(slug, routes.NoteBody(
                    kind=args.kind or "", text=args.text or "", ref=args.ref, url=args.url,
                    milestoneKey=args.milestone, status=args.status),
                    request, identity=identity, db=db)
            elif args.action == "set_status":
                view = await routes.patch_milestone_note(slug, args.note_id or "",
                    routes.NotePatchBody(status=args.status, text=args.text),
                    request, identity=identity, db=db)
            elif args.action == "status_line":
                view = await routes.patch_milestone(slug, args.milestone or "",
                    routes.StatusNoteBody(statusNote=args.text), request,
                    identity=identity, db=db)
            else:
                done = await routes.refresh_milestones(slug, identity=identity, db=db)
                if isinstance(done, JSONResponse):
                    raise _refused_refresh(done)
                view = await routes.get_milestones(slug, _identity=identity, db=db)
            return summarized(view, slug, viewer=identity)
    except ValidationError as exc:
        raise ToolArgError(str(exc)) from exc
    except HTTPException as exc:
        raise milestone_tool_error(exc) from exc
