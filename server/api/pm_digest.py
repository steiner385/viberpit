"""The daily owner digest API (design 2026-10-14 §5): ``GET /api/pm/digest`` lists
every item that truly needs you with its one recommended action
(``server/pm/digest.py``), and ``POST /api/pm/digest/apply`` carries those actions
out — each through the EXISTING path that owns it (``approve_escalation``, the
hygiene executor via ``pm_hygiene.apply_pending``, ``triage_accept.apply_accept``,
``decisions.accept`` + ``commit_and_announce_accept``, the SDLC advance route's
``advance_loaded``, ``holds.release_policy_skipped_holds``), never a
re-implementation. The recommendation is re-derived from current state on apply:
an id names an item, never an action."""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from server.api.pm import _queue_inputs
from server.api.pm_hygiene import (apply_pending, project_slugs, recommendation_to_wire,
                                   recommendations_by_id, refused)
from server.audit import record_audit
from server.auth.middleware import current_identity
from server.automations.product_policy import effective_policy
from server.db import get_db
from server.db.models import (DecisionProposal, Feature, Initiative, InitiativeArtifact,
                              OwnerRecommendation, Project, TriageProposal, WorkItem)
from server.pm import digest as dg
from server.pm.closeout_assess import cut_text
from server.pm.desired_state import HUMAN_WAIT_SESSION_STATUSES

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pm", tags=["pm"])


def _issue_why(row) -> str:
    """Why a needs-you issue the holds read model does not list needs you
    (``tree.issue_tier``'s needs-you branches)."""
    if row.reason == "needs_spec":
        return dg.WHY_NEEDS_SPEC
    if row.session_status in HUMAN_WAIT_SESSION_STATUSES:
        return dg.WHY_SESSION_WAIT
    if row.assigned_to_owner:
        return dg.WHY_ASSIGNED
    return cut_text(row.item.hold_reason or "Held for you.", 300)


async def _held_items(db, now: datetime) -> tuple[list[dg.DigestItem], list, list]:
    """The held issues as digest items, plus the queue rows and pending decisions
    they were read from — THE derivation ``load_digest`` lists and the apply of a
    ``hold:`` id re-derives (the holds read model, ``escalation.product_escalation``
    over ``queue_inputs``, ``GET /api/pm/holds``' composition). A hold is
    releasable when it is among the system hand-offs the product's policy now skips
    (``policy_skip.stale_system_holds`` per product — the needs-you hygiene pass's
    own verdict and grouping)."""
    from server.pm import decisions
    from server.pm.escalation import product_escalation
    from server.pm.policy_skip import stale_system_holds
    rows, policies = await _queue_inputs(db, now)
    pending_decisions = await decisions.pending_list(db)
    proposals_by_ref = {f"{w.repo}#{w.number}": p for p, w in pending_decisions
                        if w is not None and w.repo and w.number}
    by_product: dict[str, list] = {}
    for r in rows:
        by_product.setdefault(r.product, []).append(r)
    items: list[dg.DigestItem] = []
    for slug in sorted(by_product):
        policy = policies.get(slug) or effective_policy(None)
        releasable = {r.ref for r in stale_system_holds(by_product[slug], policies, now=now)}
        items.extend(dg.from_held(i, releasable=i.ref in releasable) for i in product_escalation(
            slug, by_product[slug], proposals_by_ref, policy, now=now).held)
    return items, rows, pending_decisions


@dataclass(frozen=True)
class _GateFacts:
    """What a gate's advance recommendation is derived from (``dg.gate_advance_recommended``)."""
    initiative: Initiative
    #: ``tree.initiative_reason`` over the row's own status/stage and epic progress.
    reason: str | None
    #: ``sdlc.engine.stage_complete`` over the row and its artifacts; never for a program.
    stage_complete: bool

    @property
    def advance_recommended(self) -> bool:
        return dg.gate_advance_recommended(status=self.initiative.status, reason=self.reason,
                                           stage_complete=self.stage_complete)


async def _gate_facts(db, slugs: Iterable[str]) -> dict[str, _GateFacts]:
    """slug → ``_GateFacts`` for each initiative named — the evidence the listing
    passes to ``dg.from_initiative`` and the apply of a ``gate:`` id re-reads. The
    projection is the detail route's (``sdlc.initiative_view`` over
    ``_progress_rolled_up``); a program (an initiative with phases) is never
    advanced directly, so it never has the evidence."""
    from server.api.sdlc import _progress_rolled_up, artifact_views_of, initiative_view
    from server.pm.tree import initiative_reason
    from server.sdlc.engine import stage_complete
    wanted = sorted({s for s in slugs if s})
    if not wanted:
        return {}
    inis = (await db.execute(select(Initiative).where(
        Initiative.slug.in_(wanted)))).scalars().all()
    if not inis:
        return {}
    ids = [i.id for i in inis]
    parents = {pid for (pid,) in (await db.execute(select(Initiative.parent_initiative_id).where(
        Initiative.parent_initiative_id.in_(ids)))).all()}
    progress = await _progress_rolled_up(db, list(inis), include_children=False)
    arts_by: dict[str, list] = {}
    for a in (await db.execute(select(InitiativeArtifact).where(
            InitiativeArtifact.initiative_id.in_(ids))
            .order_by(InitiativeArtifact.created_at))).scalars().all():
        arts_by.setdefault(a.initiative_id, []).append(a)
    out: dict[str, _GateFacts] = {}
    for ini in inis:
        program = ini.id in parents
        view = initiative_view(ini, progress.get(ini.id))
        out[ini.slug or ""] = _GateFacts(
            initiative=ini,
            reason=initiative_reason(ini.status, ini.stage, view.epic_total, view.epic_done,
                                     has_children=program),
            stage_complete=(not program
                            and stage_complete(view, artifact_views_of(arts_by.get(ini.id, [])))))
    return out


async def load_digest(db, product: str | None = None, *, now: datetime | None = None) -> dict:
    """The digest (``digest.assemble``) for one product slug or all. THE composition
    the route serves and the hygiene loop's daily push reads:

    * held issues — the holds read model (``escalation.product_escalation`` over
      ``queue_inputs``, ``GET /api/pm/holds``' composition);
    * the map's other needs-you issues and initiative gates — ``pm_tree.product_nodes``
      (the badge's own tiers, ``issues_out``);
    * pending decisions not already held on a board, pending triage proposals;
    * pending owner recommendations (they stand in for the item they target)."""
    from server.api.pm_tree import IssueAttention, product_nodes
    from server.pm.repo_project import owning_projects_by_repo
    from server.pm.triage_accept import awaiting_decision_clause
    now = now or datetime.now(timezone.utc)
    items, rows, pending_decisions = await _held_items(db, now)
    url_by_ref = {r.ref: r.url for r in rows}
    listed = {i.ref for i in items if i.ref}

    issues: list[IssueAttention] = []
    nodes = await product_nodes(db, issues_out=issues)
    gates = [(node["slug"], ini) for node in nodes for ini in node["initiatives"]
             if ini.get("ownTier", ini["tier"]) == "needs-you"]
    facts = await _gate_facts(db, (str(ini.get("slug") or "") for _slug, ini in gates))
    for slug, ini in gates:
        fact = facts.get(str(ini.get("slug") or ""))
        items.append(dg.from_initiative(slug, ini, stage_complete=bool(
            fact is not None and fact.stage_complete)))
    for issue in issues:
        ref = f"{issue.item.repo}#{issue.item.number}"
        if issue.tier != "needs-you" or ref in listed:
            continue
        listed.add(ref)
        items.append(dg.from_issue(product=issue.product, work_item_id=issue.item.id, ref=ref,
                                   title=issue.item.title, url=issue.item.url,
                                   why=_issue_why(issue)))

    triage = (await db.execute(select(TriageProposal, WorkItem).join(
        WorkItem, WorkItem.id == TriageProposal.work_item_id).where(
        awaiting_decision_clause()))).all()
    repos = ({w.repo for _p, w in triage if w.repo}
             | {w.repo for _p, w in pending_decisions if w is not None and w.repo})
    owner_of = {repo: o.slug for repo, o in (await owning_projects_by_repo(db, repos)).items()} \
        if repos else {}
    for prop, wi in pending_decisions:
        dref = f"{wi.repo}#{wi.number}" if wi is not None and wi.repo and wi.number else None
        owner = owner_of.get(wi.repo) if wi is not None and wi.repo else None
        if owner is None or (dref is not None and dref in listed):
            continue
        items.append(dg.from_decision(proposal_id=prop.id, product=owner, ref=dref,
                                      title=wi.title if wi is not None else None,
                                      url=wi.url if wi is not None else None,
                                      rationale=prop.rationale, options=prop.options or (),
                                      recommended_key=prop.recommended_key))
    feature_ids = {p.proposed_feature_id for p, _w in triage if p.proposed_feature_id}
    feature_slug = dict((await db.execute(select(Feature.id, Feature.slug).where(
        Feature.id.in_(feature_ids)))).all()) if feature_ids else {}
    for prop, wi in triage:
        owner = owner_of.get(wi.repo) if wi.repo else None
        if owner is None:
            continue
        items.append(dg.from_triage(
            proposal_id=prop.id, product=owner, ref=f"{wi.repo}#{wi.number}", title=wi.title,
            url=wi.url, feature_slug=feature_slug.get(prop.proposed_feature_id),
            confidence_level=prop.confidence_level))

    slugs = await project_slugs(db)
    for rec in (await db.execute(select(OwnerRecommendation).where(
            OwnerRecommendation.status == "pending")
            .order_by(OwnerRecommendation.created_at))).scalars().all():
        wire = recommendation_to_wire(rec, slugs.get(rec.project_id))
        items.append(dg.from_recommendation(wire, url=url_by_ref.get(rec.target_ref)))

    known = {node["slug"] for node in nodes}
    if product is not None:
        items = [i for i in items if i.product == product]
        known = {product}
    return dg.assemble(items, products=known, now=now)


@router.get("/digest")
async def owner_digest(product: str | None = None,
                       _identity: str = Depends(current_identity),
                       db=Depends(get_db)) -> dict:
    """Today's owner digest: every needs-you item (one product, or all), each with its
    recommended action or ``null`` when only a person can decide. ``total`` counts
    the items; ``withRecommendation`` those an "approve all" would act on."""
    if product is not None and (await db.execute(select(Project.id).where(
            Project.slug == product))).first() is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": f"Project '{product}' not found"})
    return await load_digest(db, product)


class DigestApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(min_length=1, max_length=dg.APPLY_BATCH_MAX)
    confirm: bool = False


_NO_RECOMMENDATION = {"ok": False, "step": "no_recommendation",
                      "error": "nothing to apply: only a person can decide this one"}


class _ApplyState:
    """What one ``POST /api/pm/digest/apply`` call reads at most once: the held
    items re-derived from current state when the first ``hold:`` id needs them.
    Each underlying path re-checks its own precondition at write time, so an item
    applied earlier in the batch cannot make a later one act on a stale read."""

    def __init__(self) -> None:
        self._held: dict[str, dg.DigestItem] | None = None

    async def held(self, db) -> dict[str, dg.DigestItem]:
        if self._held is None:
            items, _rows, _decisions = await _held_items(db, datetime.now(timezone.utc))
            self._held = {i.id: i for i in items}
        return self._held


async def _apply_hold(db, request: Request, key: str, identity: str,
                      state: _ApplyState) -> dict:
    """Carry out the recommendation a held issue has NOW (``_held_items``, the
    listing's own derivation): approve its model climb, accept its decision's
    recommended option, or release a system hold the policy now skips."""
    item = (await state.held(db)).get(dg.item_id(dg.ID_HOLD, key))
    rec = item.recommendation if item is not None else None
    if item is None or rec is None:
        if await db.get(WorkItem, key) is None:
            return {"ok": False, "step": "not_found", "error": "no such work item"}
        return _NO_RECOMMENDATION
    if rec.action == dg.ACTION_ACCEPT_DECISION:
        return await _accept_decision(db, request, str(rec.params["proposalId"]))
    if rec.action == dg.ACTION_RELEASE_HOLD:
        return await _release_hold(db, request, item.ref or "")
    return await _approve_escalation(db, request, key, str(rec.params["model"]), identity)


async def _approve_escalation(db, request: Request, key: str, model: str,
                              identity: str) -> dict:
    """Approve the escalation a model-approval hold asked for, at the tier it asked
    for (``escalation_approval.approve_escalation`` — the approve-escalation route's
    own sequence)."""
    from server.pm.escalation_approval import EscalationApprovalError, approve_escalation
    from server.pm.holds import actor_for_request
    wi = await db.get(WorkItem, key)
    if wi is None:
        return {"ok": False, "step": "not_found", "error": "no such work item"}
    try:
        result = await approve_escalation(db, request.app.state.sessions, wi, model=model,
                                          actor=actor_for_request(request), identity=identity,
                                          audit_detail={"via": "digest"})
    except EscalationApprovalError as e:
        await db.rollback()
        return {"ok": False, "step": e.code, "error": str(e)}
    return {"ok": True, "step": None, "error": None, "status": result.get("status"),
            "approval": result.get("approval"), "model": result.get("model")}


async def _accept_decision(db, request: Request, proposal_id: str) -> dict:
    """Accept a pending decision's recommended option — the inbox accept route's
    sequence (``decisions.may_resolve`` on ``actor_for_request``, ``decisions.accept``,
    then ``decisions.commit_and_announce_accept``). Re-derived from the row: a
    proposal no longer pending, or whose recommended key is no longer one of its
    options, is refused."""
    from server.pm import decisions
    from server.pm.holds import actor_for_request
    actor = actor_for_request(request)
    if not decisions.may_resolve(actor):
        return {"ok": False, "step": "forbidden", "error": decisions.RESOLVE_DENIED_MESSAGE}
    prop = await db.get(DecisionProposal, proposal_id)
    if prop is None:
        return {"ok": False, "step": "not_found", "error": "no such decision"}
    if prop.status != decisions.STATUS_PENDING:
        return {"ok": False, "step": "not_pending",
                "error": f"this decision is already {prop.status}"}
    rec = dg.decision_recommendation(prop.id, prop.options or (), prop.recommended_key)
    if rec is None:
        return _NO_RECOMMENDATION
    wi = await db.get(WorkItem, prop.work_item_id) if prop.work_item_id else None
    ref = f"{wi.repo}#{wi.number}" if wi is not None and wi.repo and wi.number else None
    try:
        await decisions.accept(db, prop, chosen_key=str(rec.params["key"]), actor=actor,
                               now=datetime.now(timezone.utc), via="digest")
    except decisions.DecisionError as e:
        await db.rollback()
        return {"ok": False, "step": "conflict", "error": str(e)}
    chosen = str(rec.params["key"])
    await decisions.commit_and_announce_accept(db, prop, ref=ref, actor=actor)
    return {"ok": True, "step": None, "error": None, "chosenKey": chosen}


async def _release_hold(db, request: Request, ref: str) -> dict:
    """Release a system hand-off the product's policy now skips
    (``holds.release_policy_skipped_holds``, signal ``policy_skip``), which narrows
    again at write time: a hold a person (or an agent) placed since is kept."""
    from server.pm import holds
    actor = holds.actor_for_request(request)
    try:
        released = await db.run_sync(lambda s: holds.release_policy_skipped_holds(
            s, [ref], actor=actor, via="digest"))
    except holds.HoldPermissionError as e:
        await db.rollback()
        return {"ok": False, "step": "forbidden", "error": str(e)}
    if not released:
        return {"ok": False, "step": "not_held",
                "error": f"{ref} carries no system hold to release any more"}
    await db.commit()
    return {"ok": True, "step": None, "error": None}


async def _apply_gate(db, slug: str, identity: str) -> dict:
    """Advance an initiative's gate one stage when its recommendation still holds
    (``_gate_facts`` re-read now) — the SDLC advance route's own sequence
    (``sdlc.advance_loaded``), its 409s as refusals."""
    from server.api.sdlc import advance_loaded
    fact = (await _gate_facts(db, [slug])).get(slug)
    if fact is None:
        return {"ok": False, "step": "not_found", "error": "no such initiative"}
    if not fact.advance_recommended:
        return _NO_RECOMMENDATION
    ini = fact.initiative
    try:
        await advance_loaded(db, ini, identity=identity, via="digest")
    except HTTPException as e:
        await db.rollback()
        detail: dict = e.detail if isinstance(e.detail, dict) else {}
        return {"ok": False, "step": str(detail.get("error") or "refused"),
                "error": str(detail.get("message") or e.detail)}
    stage = ini.stage
    await db.commit()
    return {"ok": True, "step": None, "error": None, "stage": stage}


async def _apply_recommendation(db, request: Request, key: str, identity: str) -> dict:
    """Apply a pending owner recommendation (``pm_hygiene.apply_pending``)."""
    row = (await recommendations_by_id(db, [key])).get(key)
    refusal = refused(key, row)
    if refusal is not None:
        return refusal
    assert row is not None
    product = (await project_slugs(db)).get(row.project_id, row.project_id)
    await db.commit()
    return await apply_pending(db, row, product=product, seams=request.app.state.sessions,
                               identity=identity)


async def _apply_triage(db, key: str, identity: str) -> dict:
    """Accept a high-confidence triage proposal onto its proposed capability
    (``triage_accept.apply_accept``, the accept route's own write)."""
    from server.pm.repo_project import OwnershipConflict
    from server.pm.triage_accept import (DECIDED_BY_HUMAN, WorkItemMissing, apply_accept,
                                         gather_accept_ownership, pending_proposal_stmt)
    ownership = await gather_accept_ownership([key])
    prop = (await db.execute(pending_proposal_stmt(key))).scalar_one_or_none()
    if prop is None:
        return {"ok": False, "step": "not_found", "error": "no such proposal"}
    if prop.status != "pending":
        return {"ok": False, "step": "not_pending", "error": f"this proposal is {prop.status}"}
    if (prop.confidence_level != dg.TRIAGE_RECOMMEND_CONFIDENCE
            or not prop.proposed_feature_id):
        return _NO_RECOMMENDATION
    try:
        async with db.begin_nested():
            await apply_accept(db, prop, decided_by=DECIDED_BY_HUMAN, audit_identity=identity,
                               ownership=ownership)
    except OwnershipConflict as e:
        return {"ok": False, **e.result()}
    except WorkItemMissing:
        return {"ok": False, "step": "work_item_missing", "error": "the work item is gone"}
    await record_audit(db=db, type="pm.triage.accept", identity=identity,
                       detail={"proposalId": key, "workItemId": prop.work_item_id,
                               "via": "digest"})
    await db.commit()
    return {"ok": True, "step": None, "error": None}


@router.post("/digest/apply")
async def apply_digest(body: DigestApplyBody, request: Request,
                       identity: str = Depends(current_identity),
                       db=Depends(get_db)) -> dict:
    """Carry out the recommended action of each digest item in ``ids`` — "approve
    all" passes every id with a recommendation. ``confirm: true`` is required:
    each one writes (an approved model climb dispatches a session; a recommendation
    closes, unparks or moves an issue; a decision is taken; a gate advances; a
    hold is released). Each id runs through the path that owns
    it, with that path's own audit; the call is audited ``pm.digest.apply`` once.
    Per-id ``{id, ok, step, error}``; a refusal is data — the batch never fails."""
    if not body.confirm:
        raise HTTPException(status_code=422, detail={
            "error": "confirm_required",
            "message": "applying writes to GitHub and may dispatch sessions; "
                       "resend with confirm: true"})
    results: list[dict] = []
    state = _ApplyState()
    for rid in dict.fromkeys(body.ids):
        parsed = dg.parse_id(rid)
        try:
            if parsed is None:
                result = {"ok": False, "step": "bad_id", "error": "not a digest item id"}
            elif parsed[0] == dg.ID_HOLD:
                result = await _apply_hold(db, request, parsed[1], identity, state)
            elif parsed[0] == dg.ID_REC:
                result = await _apply_recommendation(db, request, parsed[1], identity)
            elif parsed[0] == dg.ID_TRIAGE:
                result = await _apply_triage(db, parsed[1], identity)
            elif parsed[0] == dg.ID_DECISION:
                result = await _accept_decision(db, request, parsed[1])
            elif parsed[0] == dg.ID_GATE:
                result = await _apply_gate(db, parsed[1], identity)
            else:
                result = _NO_RECOMMENDATION
        except Exception:
            # One item's unexpected failure must not revert or hide the rest.
            log.warning("pm.digest.apply_failed id=%s", rid, exc_info=True)
            await db.rollback()
            result = {"ok": False, "step": "failed", "error": "unexpected error"}
        results.append({**result, "id": rid})
    await record_audit(db=db, type=dg.APPLY_AUDIT_TYPE, identity=identity, detail={
        "ids": list(dict.fromkeys(body.ids)),
        "results": [{"id": r["id"], "ok": bool(r.get("ok")), "step": r.get("step")}
                    for r in results]})
    await db.commit()
    return {"results": results, "applied": sum(1 for r in results if r.get("ok")),
            "failed": sum(1 for r in results if not r.get("ok"))}
