"""The daily owner digest API (design 2026-10-14 §5): ``GET /api/pm/digest`` lists
every item that truly needs you with its one recommended action
(``server/pm/digest.py``), and ``POST /api/pm/digest/apply`` carries those actions
out — each through the EXISTING path that owns it (``approve_escalation``, the
hygiene executor via ``pm_hygiene.apply_pending``, ``triage_accept.apply_accept``,
``decisions.accept``, ``holds.release_policy_skipped_holds``,
``sdlc.advance_initiative_row``), never a re-implementation. The recommendation is
re-derived from current state on apply: an id names an item, never an action."""
from __future__ import annotations

import logging
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
from server.db.models import Feature, OwnerRecommendation, Project, TriageProposal, WorkItem
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
    from server.pm import decisions
    from server.pm.escalation import product_escalation
    from server.pm.policy_skip import stale_system_holds
    from server.pm.repo_project import owning_projects_by_repo
    from server.pm.triage_accept import awaiting_decision_clause
    now = now or datetime.now(timezone.utc)
    rows, policies = await _queue_inputs(db, now)
    pending_decisions = await decisions.pending_list(db)
    proposals_by_ref = {f"{w.repo}#{w.number}": p for p, w in pending_decisions
                        if w is not None and w.repo and w.number}
    by_product: dict[str, list] = {}
    for r in rows:
        by_product.setdefault(r.product, []).append(r)
    url_by_ref = {r.ref: r.url for r in rows}
    # #3112: the system hand-offs the queue skips anyway — the hygiene loop's rule 3,
    # read here so the digest recommends their release even for a product whose
    # hygiene is off or cautious.
    stale = {r.ref for r in stale_system_holds(rows, policies, now=now)}
    items: list[dg.DigestItem] = []
    for slug in sorted(by_product):
        policy = policies.get(slug) or effective_policy(None)
        items.extend(dg.from_held(i, releasable=i.ref in stale) for i in product_escalation(
            slug, by_product[slug], proposals_by_ref, policy, now=now).held)
    listed = {i.ref for i in items if i.ref}

    issues: list[IssueAttention] = []
    nodes = await product_nodes(db, issues_out=issues)
    for node in nodes:
        for ini in node["initiatives"]:
            if ini.get("ownTier", ini["tier"]) == "needs-you":
                items.append(dg.from_initiative(node["slug"], ini))
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
                                      rationale=prop.rationale,
                                      recommended_key=prop.recommended_key,
                                      options=prop.options or ()))
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


async def _stale_hold_refs(db, now: datetime) -> set[str]:
    """The refs ``load_digest`` recommended releasing (``policy_skip.stale_system_holds``
    over the queue's own rows), re-read now — the apply never trusts the id."""
    from server.pm.policy_skip import stale_system_holds
    rows, policies = await _queue_inputs(db, now)
    return {r.ref for r in stale_system_holds(rows, policies, now=now)}


async def _apply_release(db, request: Request, wi: WorkItem, identity: str) -> dict:
    """Release a system hand-off the queue skips anyway (#3112), through the hygiene
    loop's own write (``holds.release_policy_skipped_holds``, signal ``policy_skip``),
    which re-checks the hold is still system-written before ending it."""
    from server.pm import holds
    from server.pm.holds import actor_for_request
    ref = f"{wi.repo}#{wi.number}"
    actor = actor_for_request(request)
    released = await db.run_sync(lambda s: holds.release_policy_skipped_holds(
        s, [ref], actor=actor, via="digest"))
    if not released:
        return {"ok": False, "step": "not_released",
                "error": "the hold changed hands or is already released"}
    # ``release_policy_skipped_holds`` wrote the ``pm.hold.release`` row itself
    # (signal ``policy_skip``, via ``digest``); the call's own ``pm.digest.apply``
    # row names *identity*. No third audit shape.
    await db.commit()
    return {"ok": True, "step": None, "error": None, "released": "owner_action"}


async def _apply_decision(db, request: Request, proposal, identity: str) -> dict:
    """Accept the option the proposer recommended (#3112) — ``decisions.accept``, the
    ONE release of a decision hold, gated by ``decisions.may_resolve`` exactly as
    the accept route is, then the same post-commit issue comment."""
    from server.pm import decisions
    from server.pm.holds import actor_for_request
    actor = actor_for_request(request)
    if not decisions.may_resolve(actor):
        return {"ok": False, "step": "forbidden", "error": decisions.RESOLVE_DENIED_MESSAGE}
    if proposal.status != decisions.STATUS_PENDING:
        return {"ok": False, "step": "not_pending",
                "error": f"this decision is already {proposal.status}"}
    key = proposal.recommended_key
    if not key or dg.option_label(proposal.options or (), key) is None:
        return _NO_RECOMMENDATION
    try:
        await decisions.accept(db, proposal, chosen_key=key, actor=actor,
                               now=datetime.now(timezone.utc))
    except decisions.DecisionError as e:
        await db.rollback()
        return {"ok": False, "step": "decision_conflict", "error": str(e)}
    wi = await db.get(WorkItem, proposal.work_item_id)
    chosen = decisions.chosen_option(proposal) or {}
    ref = f"{wi.repo}#{wi.number}" if wi is not None and wi.repo and wi.number else None
    await db.commit()
    if ref:
        await decisions.announce_accept_best_effort(
            ref, label=str(chosen.get("label") or key), detail=chosen.get("detail"),
            actor=actor)
    return {"ok": True, "step": None, "error": None, "chosenKey": key}


async def _pending_proposal_for(db, work_item_id: str):
    """The pending decision proposal parked on *work_item_id*, row-locked like the
    accept route's own load (``server/api/pm.py:_load_decision``)."""
    from server.db.models import DecisionProposal
    from server.pm.decisions import STATUS_PENDING
    return (await db.execute(select(DecisionProposal).where(
        DecisionProposal.work_item_id == work_item_id,
        DecisionProposal.status == STATUS_PENDING).with_for_update())).scalar_one_or_none()


async def _apply_decision_id(db, request: Request, key: str, identity: str) -> dict:
    """A ``decision:<proposal id>`` item (a pending proposal the holds read model did
    not list): accept its recommended option."""
    from server.db.models import DecisionProposal
    proposal = (await db.execute(select(DecisionProposal).where(
        DecisionProposal.id == key).with_for_update())).scalar_one_or_none()
    if proposal is None:
        return {"ok": False, "step": "not_found", "error": "no such proposal"}
    return await _apply_decision(db, request, proposal, identity)


async def _apply_gate(db, key: str, identity: str) -> dict:
    """Advance an initiative whose build epic is done (#3112): re-derive
    ``tree.initiative_reason`` from the row and its epic progress — the same facts
    the map served — and only then run the advance route's own sequence
    (``sdlc.advance_initiative_row``). An ``awaiting_human`` gate, a program, a
    done or blocked row all answer a refusal rather than move."""
    from server.api.sdlc import _epic_progress_batch, advance_initiative_row, has_phases
    from server.db.models import Initiative
    from server.pm.tree import initiative_reason
    ini = (await db.execute(select(Initiative).where(
        Initiative.slug == key).with_for_update())).scalar_one_or_none()
    if ini is None:
        return {"ok": False, "step": "not_found", "error": "no such initiative"}
    progress = (await _epic_progress_batch(db, [ini], include_children=False))[ini.id] or {}
    reason = initiative_reason(ini.status, ini.stage, int(progress.get("total", 0)),
                               int(progress.get("done", 0)),
                               has_children=await has_phases(db, ini))
    if reason != dg.GATE_REASON_ADVANCE:
        return _NO_RECOMMENDATION
    try:
        out = await advance_initiative_row(db, ini, identity=identity,
                                           audit_detail={"via": "digest"})
    except HTTPException as e:
        await db.rollback()
        detail: dict = e.detail if isinstance(e.detail, dict) else {}
        return {"ok": False, "step": str(detail.get("error") or "refused"),
                "error": str(detail.get("message") or e.detail)}
    await db.commit()
    return {"ok": True, "step": None, "error": None, "stage": out.get("stage")}


async def _apply_hold(db, request: Request, key: str, identity: str) -> dict:
    """A ``hold:<work item id>`` item, by what its hold IS now: a decision hold →
    accept the recommended option; a system hand-off the queue skips → release it;
    a model-approval hold → approve the escalation at the tier it asked for
    (``escalation_approval.approve_escalation`` — the approve-escalation route's
    own sequence). The release check comes first, as in ``digest.from_held``: an
    approve on non-work would re-dispatch it."""
    from server.pm.escalation_approval import EscalationApprovalError, approve_escalation
    from server.pm.holds import actor_for_request
    from server.pm.outcomes import escalation_for
    wi = await db.get(WorkItem, key)
    if wi is None:
        return {"ok": False, "step": "not_found", "error": "no such work item"}
    if wi.hold_kind == "decision":
        proposal = await _pending_proposal_for(db, wi.id)
        if proposal is None:
            return _NO_RECOMMENDATION
        return await _apply_decision(db, request, proposal, identity)
    if wi.hold_kind == "owner_action" and wi.repo and wi.number and (
            f"{wi.repo}#{wi.number}" in await _stale_hold_refs(db, datetime.now(timezone.utc))):
        return await _apply_release(db, request, wi, identity)
    escalation = escalation_for(wi.hold_kind, wi.hold_reason)
    if escalation is None:
        return _NO_RECOMMENDATION
    try:
        result = await approve_escalation(db, request.app.state.sessions, wi,
                                          model=escalation.needs_approval,
                                          actor=actor_for_request(request), identity=identity,
                                          audit_detail={"via": "digest"})
    except EscalationApprovalError as e:
        await db.rollback()
        return {"ok": False, "step": e.code, "error": str(e)}
    return {"ok": True, "step": None, "error": None, "status": result.get("status"),
            "approval": result.get("approval"), "model": result.get("model")}


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
    closes, unparks or moves an issue). Each id runs through the path that owns
    it, with that path's own audit; the call is audited ``pm.digest.apply`` once.
    Per-id ``{id, ok, step, error}``; a refusal is data — the batch never fails."""
    if not body.confirm:
        raise HTTPException(status_code=422, detail={
            "error": "confirm_required",
            "message": "applying writes to GitHub and may dispatch sessions; "
                       "resend with confirm: true"})
    results: list[dict] = []
    for rid in dict.fromkeys(body.ids):
        parsed = dg.parse_id(rid)
        try:
            if parsed is None:
                result = {"ok": False, "step": "bad_id", "error": "not a digest item id"}
            elif parsed[0] == dg.ID_HOLD:
                result = await _apply_hold(db, request, parsed[1], identity)
            elif parsed[0] == dg.ID_REC:
                result = await _apply_recommendation(db, request, parsed[1], identity)
            elif parsed[0] == dg.ID_TRIAGE:
                result = await _apply_triage(db, parsed[1], identity)
            elif parsed[0] == dg.ID_DECISION:
                result = await _apply_decision_id(db, request, parsed[1], identity)
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
