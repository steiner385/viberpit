"""Internal adapter for permission-approved, owner-delegated session triage.

Never exposed as an HTTP route. The native tool permission boundary must run before
mutations here. Reuse the UI handlers, including their locks, validation and audit;
ordinary HTTP callers still go through their existing identity/actor guards.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from starlette.requests import Request

from server.audit import record_audit
from server.db import db_session
from server.db.models import Initiative, Project, ProjectRepo, WorkItem
from server.mcp_spec import ToolArgError
from server.pm.holds import actor_class
from server.pm.milestone_asks import open_milestone_asks
from server.sessions.tools.context import SessionToolContext


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NeedsYou(Arguments):
    product: str = Field(min_length=1)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


class Item(Arguments):
    kind: Literal["initiative", "work_item"]
    id: str = Field(min_length=1)


class Resolve(Arguments):
    kind: Literal["decision", "classification"]
    proposal_id: str = Field(min_length=1)
    action: Literal["accept", "reject"]
    chosen_key: str | None = None


class Hold(Arguments):
    item_id: str = Field(min_length=1)
    action: Literal["hold", "release"]
    reason: str | None = None


class Advance(Arguments):
    slug: str = Field(min_length=1)
    expected_stage: str = Field(min_length=1)


class MilestonesRead(Arguments):
    product: str = Field(min_length=1)


class MilestoneTick(Arguments):
    product: str = Field(min_length=1)
    milestone: str = Field(min_length=1)
    condition: str = Field(min_length=1)
    met: bool | None = None
    value: float | None = None
    evidence: str
    url: str | None = None


class MilestoneAdvance(Arguments):
    """No override field (R3): an override is the owner's, in the web app."""
    product: str = Field(min_length=1)
    expected_current: str | None


def _owner(ctx: SessionToolContext) -> str:
    identity = ctx.identity.strip()
    if not identity or actor_class(identity) != "person":
        raise ToolArgError("Roadmap mutations require an authenticated human owner of this session.")
    if getattr(ctx.record(), "permission_mode", None) == "bypassPermissions":
        raise ToolArgError("Roadmap mutations require tool approval; switch out of bypassPermissions.")
    return identity


def _request(identity: str) -> Request:
    # This is a server-bound delegation after permission approval, not HTTP auth
    # and not an assistant bearer token. No route or identity comes from tool args.
    request = Request({"type": "http", "headers": []})
    request.state.identity = identity
    request.state.via = "session"
    return request


async def _needs_you(db: Any, args: NeedsYou, identity: str) -> dict:
    from server.api import pm, pm_tree
    roadmap = await pm_tree.get_map(_identity=identity, db=db)
    product = next((p for p in roadmap["products"] if p["slug"] == args.product), None)
    if product is None:
        raise HTTPException(404, detail={"error": "product_not_found"})
    rows = [{"kind": "initiative", **i} for i in product["initiatives"]
            if i["tier"] in ("needs-you", "blocked")]
    seen: set[str] = set()
    pending = list(product["capabilities"])
    while pending:
        capability = pending.pop()
        pending.extend(capability["children"])
        # Map counts summarize issues, while leaves also include PRs. Read the
        # leaf population directly: a zero issue count cannot exclude a held PR.
        leaves = await pm_tree.get_leaves(capability=capability["id"], initiative=None,
                                          _identity=identity, db=db)
        for leaf in leaves["items"]:
            if leaf["tier"] in ("needs-you", "blocked") and leaf["id"] not in seen:
                seen.add(leaf["id"])
                rows.append({**leaf, "itemKind": leaf["kind"], "kind": "work_item"})
    repos = list((await db.execute(select(ProjectRepo.repo).join(
        Project, Project.id == ProjectRepo.project_id).where(
        Project.slug == args.product, ProjectRepo.repo.is_not(None)))).scalars())
    homeless = (await db.execute(pm_tree.needs_home_stmt(repos))).scalars().all()
    for wi in homeless:
        if wi.id not in seen:
            seen.add(wi.id)
            rows.append({**pm._work_item_to_dict(wi), "kind": "work_item",
                         "itemKind": wi.kind, "tier": "needs-you", "reason": "needs_home"})
    decisions = await pm.list_decisions(projectSlug=args.product, impact=False,
                                        _identity=identity, db=db)
    triage = await pm.list_triage(projectSlug=args.product, confidenceLevel=None,
                                  pendingOnly=True, _identity=identity, db=db)
    rows.extend({**p, "kind": "decision"} for p in decisions)
    rows.extend({**p, "kind": "classification"} for p in triage)
    project_id = (await db.execute(select(Project.id).where(
        Project.slug == args.product))).scalar_one_or_none()
    asks = (await open_milestone_asks(db, [project_id])).get(project_id, []) \
        if project_id is not None else []
    rows.extend({"kind": "milestone_ask", "product": args.product, "text": a.text,
                 "milestoneKey": a.milestone_key, "url": a.url, "id": a.id} for a in asks)
    # A deterministic order makes offset pagination useful across unchanged reads.
    # Milestone asks keep open_milestone_asks' oldest-first order (a stable sort).
    order = {"decision": 0, "milestone_ask": 1, "classification": 2, "initiative": 3,
             "work_item": 4}
    rows.sort(key=lambda r: (order[r["kind"]], "" if r["kind"] == "milestone_ask"
                             else str(r.get("slug") or r.get("id"))))
    end = args.offset + args.limit
    return {"product": args.product, "counts": product["counts"], "total": len(rows),
            "items": rows[args.offset:end], "next_offset": end if end < len(rows) else None,
            "note": "Proposal rows may refer to listed work items; total counts rows, not unique issues."}


async def invoke(ctx: SessionToolContext, operation: str, raw: dict[str, Any]) -> Any:
    """Explicit allowlist of internal operations; no general-purpose API proxy."""
    from server.api import pm, pm_milestones, sdlc
    from server.sessions.tools.milestone_note import milestone_tool_error, summarized
    models: dict[str, type[Arguments]] = {"roadmap_needs_you": NeedsYou, "roadmap_item": Item,
              "roadmap_resolve": Resolve, "roadmap_hold": Hold, "roadmap_advance": Advance,
              "roadmap_milestones": MilestonesRead, "roadmap_milestone_tick": MilestoneTick,
              "roadmap_milestone_advance": MilestoneAdvance}
    milestone_op = operation.startswith("roadmap_milestone")
    try:
        args = models[operation].model_validate(raw)
        mutation = isinstance(args, (Resolve, Hold, Advance, MilestoneTick, MilestoneAdvance))
        identity = _owner(ctx) if mutation else ctx.identity
        request = _request(identity)
        async with db_session() as db:
            if mutation:
                # Write BEFORE calling the handler: decision accept commits before
                # its best-effort GitHub comment. Both records land atomically even
                # there; validation/conflicts roll this delegation back.
                await record_audit(db, "pm.session.delegate", identity,
                                   {"sessionId": ctx.session_id, "tool": operation,
                                    "arguments": args.model_dump()}, via="session")
            if isinstance(args, NeedsYou):
                return await _needs_you(db, args, identity)
            if isinstance(args, Item):
                if args.kind == "initiative":
                    return await sdlc.get_initiative(args.id, _identity=identity, db=db)
                wi = await db.get(WorkItem, args.id)
                if wi is None:
                    raise HTTPException(404, detail={"error": "work_item_not_found"})
                if wi.kind == "issue":
                    return await pm.get_work_item_issue(args.id, _identity=identity, db=db)
                return {**pm._work_item_to_dict(wi), "body": wi.body, "url": wi.url}
            if isinstance(args, Resolve):
                if args.kind == "decision":
                    if args.action == "accept":
                        if not args.chosen_key:
                            raise ToolArgError("chosen_key is required when accepting a decision")
                        return await pm.accept_decision(args.proposal_id, request,
                            body=pm.DecisionAcceptBody(chosen_key=args.chosen_key), _identity=identity, db=db)
                    if args.chosen_key is not None:
                        raise ToolArgError("chosen_key is only valid when accepting a decision")
                    return await pm.reject_decision(args.proposal_id, request, _identity=identity, db=db)
                if args.chosen_key is not None:
                    raise ToolArgError("classification proposals do not take chosen_key")
                if args.action == "accept":
                    return await pm.accept_triage(args.proposal_id, request, body=None, identity=identity, db=db)
                return await pm.reject_triage(args.proposal_id, request, identity=identity, db=db)
            if isinstance(args, Hold):
                if args.action == "release":
                    if args.reason is not None:
                        raise ToolArgError("reason is only valid for setting a hold")
                    return await pm.release_work_item_hold(args.item_id, request, identity=identity, db=db)
                return await pm.patch_overlay(args.item_id,
                    pm.OverlayPatchBody(holdKind="owner_action", holdReason=args.reason),
                    request, identity=identity, db=db)
            if isinstance(args, Advance):
                # Lock before checking the supplied stage so concurrent approvals
                # cannot advance twice. The canonical handler enforces gate rules.
                ini = (await db.execute(select(Initiative).where(
                    Initiative.slug == args.slug).with_for_update())).scalar_one_or_none()
                if ini is None:
                    raise HTTPException(404, detail={"error": "not_found"})
                if ini.stage != args.expected_stage:
                    raise HTTPException(409, detail={"error": "stale_stage", "stage": ini.stage})
                return await sdlc.advance_initiative(args.slug, identity=identity, db=db)
            # Milestones: the Phase 2 routes hold every rule; the owner acts via="session".
            if isinstance(args, MilestonesRead):
                return summarized(await pm_milestones.get_milestones(
                    args.product, _identity=identity, db=db), args.product)
            if isinstance(args, MilestoneTick):
                return summarized(await pm_milestones.put_condition_tick(
                    args.product, args.milestone, args.condition, pm_milestones.TickBody(
                        met=args.met, value=args.value, evidence=args.evidence, url=args.url),
                    request, identity=identity, db=db), args.product)
            if isinstance(args, MilestoneAdvance):
                return summarized(await pm_milestones.advance_milestone(
                    args.product, pm_milestones.AdvanceBody(expectedCurrent=args.expected_current),
                    request, identity=identity, db=db), args.product)
    except ValidationError as exc:
        raise ToolArgError(str(exc)) from exc
    except HTTPException as exc:
        if milestone_op:
            raise milestone_tool_error(exc) from exc
        raise ToolArgError(f"{exc.status_code}: {json.dumps(exc.detail)}") from exc
