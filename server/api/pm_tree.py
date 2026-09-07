"""Roadmap tree endpoints (spec 2026-09-07 §10): the map every product renders
from, and the issue leaves under one node. Reads only; a fixed number of grouped
queries per request, never one per node."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select, tuple_

from server.api.sdlc import _capabilities_batch, _epic_progress_batch
from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import (
    Feature, FeatureLink, Initiative, Project, ProjectRepo, Session as DbSession,
    TriageItem, TriageProposal, WorkItem, WorkItemDep)
from server.pm.desired_state import ACTIVE_SESSION_STATUSES, HUMAN_WAIT_SESSION_STATUSES
from server.pm.tree import CapabilityIn, InitiativeIn, ProductIn, build_product_tree, issue_tier

router = APIRouter(prefix="/api/pm", tags=["pm"])

_LIVE = ACTIVE_SESSION_STATUSES | HUMAN_WAIT_SESSION_STATUSES


def _ref(w: WorkItem) -> str:
    return f"{w.repo}#{w.number}"


def _split_ref(ref: str) -> tuple[str, int] | None:
    repo, _, num = ref.rpartition("#")
    return (repo, int(num)) if repo and num.isdigit() else None


async def _issue_facts(db, items: list[WorkItem]) -> tuple[dict[str, bool], dict[str, str | None], dict[str, list[str]]]:
    """For each work item id: has an open blocker; the live session status if any;
    the open blocker refs. Blockers fail closed: an unmirrored destination counts
    as open (spec 003 §10)."""
    if not items:
        return {}, {}, {}
    refs = [_ref(w) for w in items]
    deps = (await db.execute(select(WorkItemDep).where(
        WorkItemDep.src_ref.in_(refs), WorkItemDep.active.is_(True)))).scalars().all()
    dst_refs = {d.dst_ref for d in deps if d.dst_ref}
    parsed = [p for p in (_split_ref(r) for r in dst_refs) if p is not None]
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
    by_id_blocked = {w.id: bool(blockers.get(_ref(w))) for w in items}
    by_id_session = {w.id: session_by_ref.get(_ref(w)) for w in items}
    by_id_blockers = {w.id: blockers.get(_ref(w), []) for w in items}
    return by_id_blocked, by_id_session, by_id_blockers


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
        WorkItem.feature_id.in_(list(feat_by_id)), WorkItem.state == "open"))).scalars().all() \
        if feat_by_id else []
    blocked, session, _ = await _issue_facts(db, open_items)
    tiers_by_feature: dict[str, list[str]] = {}
    for w in open_items:
        tiers_by_feature.setdefault(w.feature_id, []).append(
            issue_tier(w.state, w.triage_state, blocked[w.id], session[w.id]))

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
            WorkItem.repo.in_(all_repos), TriageProposal.status == "pending"))).all()
        for (repo,) in pending:
            pending_by_repo[repo] = pending_by_repo.get(repo, 0) + 1
        needs_home = (await db.execute(select(WorkItem.repo).where(
            WorkItem.repo.in_(all_repos), WorkItem.state == "open",
            WorkItem.triage_state == "needs_human"))).all()
        for (repo,) in needs_home:
            needs_home_by_repo[repo] = needs_home_by_repo.get(repo, 0) + 1
        incidents = (await db.execute(select(TriageItem.repo, TriageItem.status).where(
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
