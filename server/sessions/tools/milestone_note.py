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

from viberpit import brand
from server.audit import record_audit
from server.db import db_session
from server.mcp_spec import Summarized, ToolArgError
from server.pm.holds import SESSION_PREFIX
from server.pm.session_triage import Arguments, _request
from server.sessions.tools.context import SessionToolContext

TOOL = "roadmap_milestone_note"

#: Plain words for each refusal code the milestone write routes answer with
#: (``{"error": code}``), for a model rather than a person at the page. The web keeps
#: its own copy (``web/src/api/milestones.ts:MILESTONE_ERROR_COPY``); the override
#: codes are absent because no session tool can send an override (R3).
MILESTONE_ERROR_COPY: dict[str, str] = {
    "stale_current": "Someone else advanced this ladder; re-read it with roadmap_milestones.",
    "gate_unmet": (f"The gate is not met yet. Only the owner can advance with an override, "
                   f"from the {brand.NAME} app."),
    "evidence_required": "Add evidence for this tick.",
    "owner_required": "Only the owner can change what someone else recorded.",
    "person_required": "Only a person can make this change, not an automation.",
    "person_or_session_required": "Only a person or a session can write this, not an automation.",
    "ladder_complete": "This ladder is complete. There is nothing left to advance.",
    "no_milestones": "This ladder has no milestones to start.",
    "not_manual": "This condition is measured automatically, so it cannot be ticked.",
    "bad_tick": "Give exactly one of met (true or false) or value (a number).",
    "too_long": "That text is too long. Shorten it and try again.",
    "text_required": "Write the note's text.",
    "bad_kind": "kind must be decision or ask.",
    "bad_status": "That status does not apply to this kind of note.",
    "unknown_milestone": "That milestone is not on the ladder; re-read it with roadmap_milestones.",
    "unknown_condition": "That condition is not in the definition file; re-read the ladder.",
    "unknown_note": "That note no longer exists; re-read the ladder.",
    "nothing_to_update": "There was nothing to change.",
    "no_ladder": "This product has no milestone ladder.",
    "not_found": "No product has that slug.",
    "bad_url": "A link must be a full web address that starts with http:// or https://.",
    "concurrent_update": "Someone else just changed this. Re-read and try again.",
    "automatic_refresh_off": "Automatic refresh is off, so measurements cannot be refreshed now.",
    "refresh_too_soon": "Measurements were refreshed moments ago; try again later.",
}


def milestone_tool_error(exc: HTTPException) -> ToolArgError:
    """``<status> <code>: <plain words>``, plus any extra detail as JSON."""
    detail = exc.detail if isinstance(exc.detail, dict) else {"message": exc.detail}
    code = detail.get("error")
    extra = {k: v for k, v in detail.items() if k not in ("error", "message")}
    words = MILESTONE_ERROR_COPY.get(code) if isinstance(code, str) else None
    text = f"{exc.status_code} {code}: {words or detail.get('message') or 'refused'}"
    return ToolArgError(f"{text} {json.dumps(extra)}" if extra else text)


def summarized(view: dict[str, Any], product: str) -> Summarized:
    """A milestones view as the tool result: the text summary first, the view after
    (null for a product with no ladder)."""
    # Lazy: a cold import of milestone_view before server.automations hits that
    # package's import cycle (milestone_refresh ↔ automations.milestones_refresh).
    from server.pm.milestone_view import summarize_milestones
    return Summarized(summarize_milestones(view, product=product),
                      view if view.get("ladder") else None)


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
        return ToolArgError(f"429 refresh_too_soon: {MILESTONE_ERROR_COPY['refresh_too_soon']} "
                            f"Retry after {body.get('retryAfter')}s.")
    code = body.get("reason") or "refused"
    return ToolArgError(f"{resp.status_code} {code}: {MILESTONE_ERROR_COPY.get(code, 'refused')}")


async def milestone_note(ctx: SessionToolContext, raw: dict[str, Any]) -> Summarized:
    from server.api import pm_milestones as routes
    try:
        args = MilestoneNoteArgs.model_validate(raw)
        _check_fields(args)
        identity = f"{SESSION_PREFIX}{ctx.session_id}"
        request = _request(identity)
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
            return summarized(view, slug)
    except ValidationError as exc:
        raise ToolArgError(str(exc)) from exc
    except HTTPException as exc:
        raise milestone_tool_error(exc) from exc
