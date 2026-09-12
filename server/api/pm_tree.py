"""Roadmap tree endpoints (spec 2026-09-07 §10): the map every product renders
from, and the issue leaves under one node. Reads only; a fixed number of grouped
queries per request, never one per node."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, tuple_

from server.api.sdlc import _capabilities_batch, _epic_progress_batch
from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import (
    Feature, FeatureLink, GhProjectItem, Initiative, Project, ProjectRepo,
    Session as DbSession, TriageItem, TriageProposal, WorkItem, WorkItemDep)
from server.pm.desired_state import ACTIVE_SESSION_STATUSES, HUMAN_WAIT_SESSION_STATUSES
from server.pm.refs import split_ref
from server.pm.score import rice_score
from server.pm.tree import CapabilityIn, InitiativeIn, ProductIn, build_product_tree, issue_tier

router = APIRouter(prefix="/api/pm", tags=["pm"])

_LIVE = ACTIVE_SESSION_STATUSES | HUMAN_WAIT_SESSION_STATUSES


def _ref(w: WorkItem) -> str:
    return f"{w.repo}#{w.number}"


async def _issue_facts(db, items: list[WorkItem]) -> tuple[
        dict[str, bool], dict[str, str | None], dict[str, list[str]], dict[str, str | None]]:
    """For each work item id: has an open blocker; the live session status if any;
    the open blocker refs; the mirrored board Status if any. Blockers fail closed:
    an unmirrored destination counts as open (spec 003 §10)."""
    if not items:
        return {}, {}, {}, {}
    ids = [w.id for w in items]
    refs = [_ref(w) for w in items]
    deps = (await db.execute(select(WorkItemDep).where(
        WorkItemDep.src_ref.in_(refs), WorkItemDep.active.is_(True)))).scalars().all()
    dst_refs = {d.dst_ref for d in deps if d.dst_ref}
    parsed = [p for p in (split_ref(r) for r in dst_refs) if p is not None]
    dst_state: dict[str, str] = {}
    if parsed:
        dst_state = {_ref(w): w.state or "open" for w in (await db.execute(select(WorkItem).where(
            WorkItem.kind == "issue", tuple_(WorkItem.repo, WorkItem.number).in_(parsed)))).scalars().all()}
    blockers: dict[str, list[str]] = {}
    for d in deps:
        if d.src_ref and d.dst_ref and dst_state.get(d.dst_ref, "open") == "open":
            blockers.setdefault(d.src_ref, []).append(d.dst_ref)
    sessions = (await db.execute(select(DbSession.origin_ref, DbSession.status).where(
        DbSession.origin_ref.in_(refs), DbSession.status.in_(_LIVE)))).all()
    session_by_ref = {ref: st for ref, st in sessions}
    board_status: dict[str, str | None] = {}
    if ids:
        rows = (await db.execute(select(GhProjectItem.work_item_id, GhProjectItem.status).where(
            GhProjectItem.work_item_id.in_(ids)).order_by(GhProjectItem.updated_at))).all()
        for wid, st in rows:
            board_status[wid] = st  # last write wins (ordered by updated_at)
    by_id_blocked = {w.id: bool(blockers.get(_ref(w))) for w in items}
    by_id_session = {w.id: session_by_ref.get(_ref(w)) for w in items}
    by_id_blockers = {w.id: blockers.get(_ref(w), []) for w in items}
    by_id_board_status = {w.id: board_status.get(w.id) for w in items}
    return by_id_blocked, by_id_session, by_id_blockers, by_id_board_status


@router.get("/map")
async def get_map(_identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    projects = (await db.execute(select(Project).order_by(Project.created_at))).scalars().all()
    if not projects:
        return {"products": [], "generatedAt": datetime.now(timezone.utc).isoformat()}
    pids = [p.id for p in projects]
    slug_of = {p.id: p.slug for p in projects}

    features = (await db.execute(select(Feature).where(
        Feature.project_id.in_(pids), Feature.orphaned.is_not(True)))).scalars().all()
    feat_by_id = {f.id: f for f in features}
    # Guard every `in_` on a possibly-empty list: SQLAlchemy warns on `in_([])`.
    links = (await db.execute(select(FeatureLink).where(
        FeatureLink.src_feature_id.in_(list(feat_by_id))))).scalars().all() if feat_by_id else []
    # Link destinations may be orphaned or in a project outside `pids`; load them too.
    dst_ids = {link.dst_feature_id for link in links} - set(feat_by_id)
    dst_extra = {f.id: f for f in (await db.execute(select(Feature).where(
        Feature.id.in_(dst_ids)))).scalars().all()} if dst_ids else {}
    dst_projects = {}
    extra_pids = {f.project_id for f in dst_extra.values()} - set(pids)
    if extra_pids:
        dst_projects = {p.id: p.slug for p in (await db.execute(select(Project).where(
            Project.id.in_(extra_pids)))).scalars().all()}
    slug_of.update(dst_projects)
    links_by_src: dict[str, list[tuple[str, str, str]]] = {}
    for link in links:
        dst = feat_by_id.get(link.dst_feature_id) or dst_extra.get(link.dst_feature_id)
        if dst is None:
            continue
        links_by_src.setdefault(link.src_feature_id, []).append(
            (link.kind or "", slug_of.get(dst.project_id) or "", dst.slug or ""))

    open_items = (await db.execute(select(WorkItem).where(
        WorkItem.feature_id.in_(list(feat_by_id)), WorkItem.state == "open",
        WorkItem.kind == "issue"))).scalars().all() \
        if feat_by_id else []
    blocked, session, _, board_status = await _issue_facts(db, open_items)
    tiers_by_feature: dict[str, list[str]] = {}
    for w in open_items:
        tiers_by_feature.setdefault(w.feature_id, []).append(
            issue_tier(w.state, w.triage_state, blocked[w.id], session[w.id],
                      board_status[w.id]))

    initiatives = (await db.execute(select(Initiative).where(
        Initiative.project_id.in_(pids)).order_by(Initiative.created_at))).scalars().all()
    epics = await _epic_progress_batch(db, initiatives, include_children=False)
    caps = await _capabilities_batch(db, initiatives)

    repos = (await db.execute(select(ProjectRepo).where(ProjectRepo.project_id.in_(pids)))).scalars().all()
    repos_by_project: dict[str, list[str]] = {}
    for r in repos:
        if r.repo:
            repos_by_project.setdefault(r.project_id, []).append(r.repo)
    all_repos = [r for rs in repos_by_project.values() for r in rs]
    pending_by_repo: dict[str, int] = {}
    needs_home_by_repo: dict[str, int] = {}
    incidents_by_repo: dict[str, list[str]] = {}
    if all_repos:
        pending = (await db.execute(select(WorkItem.repo).join(
            TriageProposal, TriageProposal.work_item_id == WorkItem.id).where(
            WorkItem.repo.in_(all_repos), WorkItem.state == "open",
            TriageProposal.status == "pending"))).all()
        for (repo,) in pending:
            pending_by_repo[repo] = pending_by_repo.get(repo, 0) + 1
        needs_home = (await db.execute(select(WorkItem.repo).where(
            WorkItem.repo.in_(all_repos), WorkItem.state == "open",
            WorkItem.triage_state == "needs_human", WorkItem.feature_id.is_(None)))).all()
        for (repo,) in needs_home:
            needs_home_by_repo[repo] = needs_home_by_repo.get(repo, 0) + 1
        incidents = (await db.execute(select(TriageItem.repo, TriageItem.status).where(
            TriageItem.status.in_(("open", "scheduled", "diagnosing", "queued")),
            TriageItem.repo.in_(all_repos)))).all()
        for repo, st in incidents:
            incidents_by_repo.setdefault(repo, []).append(st or "")

    products = []
    for p in projects:
        my_feats = [f for f in features if f.project_id == p.id]
        slug_by_fid = {f.id: f.slug for f in my_feats}
        cap_inputs = tuple(CapabilityIn(
            id=f.id, slug=f.slug or "", title=f.title, state=f.state, summary=f.summary,
            doc_path=f.repo_path, commit_sha=f.commit_sha, pr_number=f.pr_number,
            parent_slug=slug_by_fid.get(f.parent_feature_id) if f.parent_feature_id else None,
            issue_tiers=tuple(tiers_by_feature.get(f.id, [])),
            links=tuple(links_by_src.get(f.id, [])),
        ) for f in my_feats)
        ini_inputs = []
        for i in initiatives:
            if i.project_id != p.id:
                continue
            cap_list, primary = caps[i.id]
            ini_inputs.append(InitiativeIn(
                slug=i.slug or "", title=i.title, stage=i.stage, status=i.status,
                primary_slug=primary["slug"] if primary else None,
                capability_slugs=tuple(c["slug"] for c in cap_list),
                epic=epics.get(i.id)))
        my_repos = repos_by_project.get(p.id, [])
        products.append(build_product_tree(ProductIn(
            slug=p.slug or "", name=p.name,
            capabilities=cap_inputs, initiatives=tuple(ini_inputs),
            awaiting_triage=sum(pending_by_repo.get(r, 0) for r in my_repos),
            needs_home=sum(needs_home_by_repo.get(r, 0) for r in my_repos),
            incident_statuses=tuple(s for r in my_repos for s in incidents_by_repo.get(r, [])),
        )))
    return {"products": products, "generatedAt": datetime.now(timezone.utc).isoformat()}


def _leaf(w: WorkItem, tier: str, blockers: list[str], session_status: str | None,
          parent_ref: str | None) -> dict:
    score = rice_score(w.reach, w.impact, w.effort, w.confidence)
    return {
        "id": w.id, "ref": _ref(w), "repo": w.repo, "number": w.number, "kind": w.kind,
        "title": w.title, "url": w.url, "state": w.state, "triageState": w.triage_state,
        "score": score, "reach": w.reach, "impact": w.impact, "effort": w.effort,
        "confidence": w.confidence, "tier": tier, "blockedBy": blockers,
        "parentRef": parent_ref, "sessionStatus": session_status,
        "ghCreatedAt": w.gh_created_at.isoformat() if w.gh_created_at else None,
    }


def _leaf_sort_key(leaf: dict) -> tuple:
    return (
        0 if leaf["state"] == "open" else 1,
        0 if leaf["score"] is not None else 1,
        -(leaf["score"] or 0.0),
        leaf["ghCreatedAt"] or "9999",
        leaf["number"] or 0,
    )


@router.get("/leaves")
async def get_leaves(
    capability: str | None = Query(default=None),
    initiative: str | None = Query(default=None),
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    if bool(capability) == bool(initiative):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={
            "error": "bad_scope", "message": "pass exactly one of capability= or initiative="})
    if capability:
        feat = (await db.execute(select(Feature).where(Feature.id == capability))).scalar_one_or_none()
        if feat is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        items = (await db.execute(select(WorkItem).where(WorkItem.feature_id == feat.id))).scalars().all()
    else:
        ini = (await db.execute(select(Initiative).where(Initiative.slug == initiative))).scalar_one_or_none()
        if ini is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        items = []
        if ini.epic_repo and ini.epic_number:
            epic = (await db.execute(select(WorkItem).where(
                WorkItem.repo == ini.epic_repo, WorkItem.number == ini.epic_number,
                WorkItem.kind == "issue"))).scalar_one_or_none()
            if epic is not None:
                items = (await db.execute(select(WorkItem).where(
                    WorkItem.parent_work_item_id == epic.id))).scalars().all()
    blocked, session, blockers, board_status = await _issue_facts(db, items)
    parent_ids = {w.parent_work_item_id for w in items if w.parent_work_item_id}
    parents = {p.id: _ref(p) for p in (await db.execute(select(WorkItem).where(
        WorkItem.id.in_(parent_ids)))).scalars().all()} if parent_ids else {}
    leaves = [_leaf(w, issue_tier(w.state, w.triage_state, blocked[w.id], session[w.id],
                                  board_status[w.id]),
                    blockers[w.id], session[w.id], parents.get(w.parent_work_item_id))
              for w in items]
    leaves.sort(key=_leaf_sort_key)
    return {"items": leaves}
