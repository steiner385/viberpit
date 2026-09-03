"""PM product-layer API — project registry + sync-now + edges (phase 1).

Design: specs/001-ccswitch-web/spec.md §7.
Mirrors server/api/sdlc.py conventions: camelCase JSON, audit on mutation,
slug identity, 409 on conflicts.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select, tuple_

from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import (
    PROJECT_EDGE_KINDS,
    Feature,
    GhProject,
    GhProjectItem,
    Project,
    ProjectEdge,
    ProjectRepo,
    TriageProposal,
    WorkItem,
    WorkItemDep,
)

router = APIRouter(prefix="/api/pm", tags=["pm"])


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "project"


def _project_to_dict(p: Project) -> dict:
    return {
        "id": p.id,
        "slug": p.slug,
        "name": p.name,
        "description": p.description,
        "status": p.status,
        "portfolioTags": p.portfolio_tags or [],
        "onboardingState": p.onboarding_state,
        "createdAt": p.created_at.isoformat() if p.created_at else None,
        "updatedAt": p.updated_at.isoformat() if p.updated_at else None,
    }


def _repo_to_dict(r: ProjectRepo) -> dict:
    return {
        "id": r.id,
        "repo": r.repo,
        "isPrimary": bool(r.is_primary),
        "backfillDone": bool(r.backfill_done),
        "lastSyncedAt": r.last_synced_at.isoformat() if r.last_synced_at else None,
    }


def _edge_to_dict(e: ProjectEdge, slug_by_id: dict[str, str | None]) -> dict:
    return {
        "id": e.id,
        "srcSlug": slug_by_id.get(e.src_project_id),
        "dstSlug": slug_by_id.get(e.dst_project_id),
        "kind": e.kind,
        "note": e.note,
    }


# ``owner/name`` with GitHub's own charset: each segment is alphanumerics plus
# ``._-``. Deliberately strict — the Projects v2 / relations GraphQL builders
# %-interpolate these strings, so a quote/space/brace must never reach them
# (issue #973). Anchored + single slash, so "a/b/c" and "no-slash" also fail.
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_repo(repo: str) -> None:
    if not _REPO_RE.match(repo or ""):
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_repo",
                    "message": f"'{repo}' is not a valid owner/name repository"})


class RepoInput(BaseModel):
    repo: str
    isPrimary: bool = False


class ProjectCreateBody(BaseModel):
    name: str
    slug: str | None = None
    description: str | None = None
    portfolioTags: list[str] | None = None
    repos: list[RepoInput]


class EdgeCreateBody(BaseModel):
    dstSlug: str
    kind: str
    note: str | None = None


async def _load_project(db, slug: str) -> Project:
    p = (await db.execute(
        select(Project).where(Project.slug == slug))).scalar_one_or_none()
    if p is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Project '{slug}' not found"})
    return p


@router.post("/projects", status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreateBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    if not body.repos:
        raise HTTPException(
            status_code=422,
            detail={"error": "no_repos", "message": "at least one repo is required"})
    for repo_in in body.repos:
        _validate_repo(repo_in.repo)
    slug = body.slug or _slugify(body.name)
    existing = (await db.execute(
        select(Project).where(Project.slug == slug))).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "slug_conflict", "message": f"Project '{slug}' already exists"})
    project = Project(
        slug=slug,
        name=body.name,
        description=body.description,
        portfolio_tags=body.portfolioTags,
    )
    db.add(project)
    await db.flush()
    # Exactly one primary: honour the first isPrimary=True, else the first repo.
    primary_idx = next(
        (i for i, r in enumerate(body.repos) if r.isPrimary), 0)
    for i, repo_in in enumerate(body.repos):
        db.add(ProjectRepo(
            project_id=project.id,
            repo=repo_in.repo,
            is_primary=(i == primary_idx),
        ))
    await db.flush()
    # Fetch the repos we just created so we can return the full shape (repos,
    # edges, featureCount, workItemCount) that the frontend Project type requires.
    repos_created = (await db.execute(
        select(ProjectRepo).where(ProjectRepo.project_id == project.id)
    )).scalars().all()
    await record_audit(db=db, type="pm.project.create", identity=identity,
                       detail={"slug": slug, "name": body.name})
    d = _project_to_dict(project)
    d["repos"] = [_repo_to_dict(r) for r in repos_created]
    d["edges"] = []
    d["featureCount"] = 0
    d["workItemCount"] = 0
    return d


@router.get("/projects")
async def list_projects(
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> list[dict]:
    projects = (await db.execute(
        select(Project).order_by(Project.created_at))).scalars().all()
    if not projects:
        return []
    ids = [p.id for p in projects]
    slug_by_id: dict[str, str | None] = {p.id: p.slug for p in projects}
    repos = (await db.execute(
        select(ProjectRepo).where(ProjectRepo.project_id.in_(ids))
    )).scalars().all()
    edges = (await db.execute(
        select(ProjectEdge).where(ProjectEdge.src_project_id.in_(ids))
    )).scalars().all()
    feature_counts: dict[str, int] = dict((await db.execute(
        select(Feature.project_id, func.count(Feature.id))
        .where(Feature.project_id.in_(ids)).group_by(Feature.project_id)
    )).all())
    repo_names_by_project: dict[str, list[str]] = {}
    for r in repos:
        repo_names_by_project.setdefault(r.project_id, []).append(r.repo or "")
    all_repo_names = [name for names in repo_names_by_project.values() for name in names]
    wi_counts: dict[str, int] = {}
    if all_repo_names:
        wi_counts = dict((await db.execute(
            select(WorkItem.repo, func.count(WorkItem.id))
            .where(WorkItem.repo.in_(all_repo_names)).group_by(WorkItem.repo)
        )).all())
    out = []
    for p in projects:
        d = _project_to_dict(p)
        d["repos"] = [_repo_to_dict(r) for r in repos if r.project_id == p.id]
        d["edges"] = [_edge_to_dict(e, slug_by_id) for e in edges
                      if e.src_project_id == p.id]
        d["featureCount"] = int(feature_counts.get(p.id, 0))
        d["workItemCount"] = sum(
            int(wi_counts.get(name, 0))
            for name in repo_names_by_project.get(p.id, []))
        out.append(d)
    return out


@router.get("/projects/{slug}")
async def get_project(
    slug: str,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    p = await _load_project(db, slug)
    repos = (await db.execute(
        select(ProjectRepo).where(ProjectRepo.project_id == p.id))).scalars().all()
    edges = (await db.execute(
        select(ProjectEdge).where(ProjectEdge.src_project_id == p.id))).scalars().all()
    dst_ids: set[str] = {e.dst_project_id for e in edges} | {p.id}
    slug_rows = (await db.execute(
        select(Project.id, Project.slug).where(Project.id.in_(dst_ids)))).all()
    slug_by_id: dict[str, str | None] = dict(slug_rows)
    d = _project_to_dict(p)
    d["repos"] = [_repo_to_dict(r) for r in repos]
    d["edges"] = [_edge_to_dict(e, slug_by_id) for e in edges]
    return d


@router.post("/projects/{slug}/sync", status_code=status.HTTP_202_ACCEPTED)
async def request_sync(
    slug: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    p = await _load_project(db, slug)
    now = datetime.now(timezone.utc)
    repos = (await db.execute(
        select(ProjectRepo).where(ProjectRepo.project_id == p.id))).scalars().all()
    for r in repos:
        r.sync_requested_at = now
    await db.flush()
    await record_audit(db=db, type="pm.project.sync_now", identity=identity,
                       detail={"slug": slug})
    return {"requested": True}


@router.post("/projects/{slug}/edges", status_code=status.HTTP_201_CREATED)
async def create_edge(
    slug: str,
    body: EdgeCreateBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    if body.kind not in PROJECT_EDGE_KINDS:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "bad_kind",
                "message": f"kind must be one of {list(PROJECT_EDGE_KINDS)}",
            })
    src = await _load_project(db, slug)
    dst = await _load_project(db, body.dstSlug)
    edge = ProjectEdge(
        src_project_id=src.id,
        dst_project_id=dst.id,
        kind=body.kind,
        note=body.note,
    )
    db.add(edge)
    await db.flush()
    await record_audit(db=db, type="pm.edge.create", identity=identity,
                       detail={"src": slug, "dst": body.dstSlug, "kind": body.kind})
    return _edge_to_dict(edge, {src.id: src.slug, dst.id: dst.slug})


@router.get("/projects/{slug}/deps")
async def project_deps(
    slug: str,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Return dep edges + drift audit for a project (spec 003 Phase A)."""
    from server.pm.drift import DepEdgeView, compute_drift

    p = await _load_project(db, slug)
    repos = {r.repo for r in (await db.execute(
        select(ProjectRepo).where(ProjectRepo.project_id == p.id)
    )).scalars().all() if r.repo}
    work_items_raw = (await db.execute(
        select(WorkItem).where(WorkItem.repo.in_(repos), WorkItem.kind == "issue")
    )).scalars().all()
    items: dict[str, WorkItem] = {f"{w.repo}#{w.number}": w for w in work_items_raw}
    open_refs = {ref for ref, w in items.items() if (w.state or "open") == "open"}

    gh = (await db.execute(
        select(GhProject).where(GhProject.project_id == p.id)
    )).scalar_one_or_none()
    project_refs: set[str] = set()
    if gh is not None:
        linked = (await db.execute(
            select(GhProjectItem).where(GhProjectItem.gh_project_id == gh.id)
        )).scalars().all()
        by_id = {w.id: ref for ref, w in items.items()}
        # work_item_id=None (draft/unresolved items) never matches a by_id key
        project_refs = {by_id[i.work_item_id] for i in linked if i.work_item_id in by_id}

    # Push the src_ref repo-prefix filter into SQL (issue #973) — keep only deps
    # whose src belongs to a project repo, instead of scanning the whole
    # WorkItemDep table in Python. ``startswith`` auto-escapes each repo string so
    # a LIKE wildcard (``_``) in a repo name can't widen the match.
    if repos:
        dep_rows = list((await db.execute(
            select(WorkItemDep).where(or_(*[
                WorkItemDep.src_ref.startswith(f"{r}#", autoescape=True)
                for r in repos]))
        )).scalars().all())
    else:
        dep_rows = []

    # Cross-product visibility (spec goal): a dep may target another onboarded
    # product's mirrored issue. Resolve dst refs that are NOT in the project-scoped
    # items map against ALL mirrored WorkItems (single query, kind "issue"), so their
    # dstState is populated and they don't land in unresolvedTargets. Genuinely
    # unmirrored targets stay null → unresolvedTargets.
    cross_states: dict[str, str | None] = {}
    cross_refs = {d.dst_ref for d in dep_rows
                  if d.dst_ref and "#" in d.dst_ref and d.dst_ref not in items}
    if cross_refs:
        parsed: list[tuple[str, int]] = []
        for ref in cross_refs:
            r_repo, _, r_num = ref.rpartition("#")
            if r_repo and r_num.isdigit():
                parsed.append((r_repo, int(r_num)))
        if parsed:
            cross_items = (await db.execute(
                select(WorkItem).where(
                    WorkItem.kind == "issue",
                    tuple_(WorkItem.repo, WorkItem.number).in_(parsed),
                )
            )).scalars().all()
            cross_states = {f"{w.repo}#{w.number}": w.state for w in cross_items}

    def _dst_state(ref: str) -> str | None:
        if ref in items:
            return items[ref].state
        return cross_states.get(ref)

    edges = [DepEdgeView(
        src_ref=d.src_ref or "",
        dst_ref=d.dst_ref or "",
        source=d.source or "",
        active=bool(d.active),
        dst_state=_dst_state(d.dst_ref or ""),
    ) for d in dep_rows]

    drift = compute_drift(edges, open_refs, project_refs,
                          {ref: items[ref].body for ref in open_refs},
                          epic_refs={ref for ref, w in items.items() if w.is_epic})
    if gh is None:
        drift.missing_from_project = []

    return {
        "project": None if gh is None else {
            "number": gh.gh_project_number,
            "title": gh.title,
            "lastSyncedAt": gh.last_synced_at.isoformat() if gh.last_synced_at else None,
        },
        "edges": [{"src": e.src_ref, "dst": e.dst_ref, "source": e.source,
                   "active": e.active, "dstState": e.dst_state} for e in edges],
        "drift": {
            "proseOnly": drift.prose_only,
            "unresolvedTargets": drift.unresolved_targets,
            "missingFromProject": drift.missing_from_project,
            "checklistEpics": drift.checklist_epics,
        },
    }


@router.delete("/projects/{slug}/edges/{edge_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_edge(
    slug: str,
    edge_id: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> None:
    src = await _load_project(db, slug)
    edge = (await db.execute(
        select(ProjectEdge).where(
            ProjectEdge.id == edge_id,
            ProjectEdge.src_project_id == src.id,
        )
    )).scalar_one_or_none()
    if edge is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Edge '{edge_id}' not found"})
    await db.delete(edge)
    await record_audit(db=db, type="pm.edge.delete", identity=identity,
                       detail={"src": slug, "edgeId": edge_id})


# --- phase 2: onboarding wizard + classification + triage (design §5, §7) --- #

from server.pm.onboarding import (  # noqa: E402 — appended after router setup
    AwaitBackfill,
    AwaitMapPr,
    AwaitTriage,
    Classify,
    Live,
    OnboardingNeedsHuman,
    ProposeMap,
    Register,
    next_onboarding_action,
)


def _onboarding_action_to_dict(action: object) -> dict:
    if isinstance(action, Register):
        return {"type": "register"}
    if isinstance(action, AwaitBackfill):
        return {"type": "await_backfill"}
    if isinstance(action, ProposeMap):
        return {"type": "propose_map"}
    if isinstance(action, AwaitMapPr):
        return {"type": "await_map_pr"}
    if isinstance(action, Classify):
        return {"type": "classify"}
    if isinstance(action, AwaitTriage):
        return {"type": "await_triage"}
    if isinstance(action, Live):
        return {"type": "live"}
    if isinstance(action, OnboardingNeedsHuman):
        return {"type": "needs_human", "reason": action.reason}
    return {"type": "unknown"}  # pragma: no cover — union is closed


def _counts_to_dict(v: object) -> dict:
    return {
        "repoCount": v.repo_count,  # type: ignore[attr-defined]
        "backfillDoneCount": v.backfill_done_count,  # type: ignore[attr-defined]
        "featureCount": v.feature_count,  # type: ignore[attr-defined]
        "openUnclassifiedCount": v.open_unclassified_count,  # type: ignore[attr-defined]
        "pendingProposalCount": v.pending_proposal_count,  # type: ignore[attr-defined]
    }


@router.get("/projects/{slug}/onboarding")
async def get_onboarding(
    slug: str,
    request: Request,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """The wizard's view over the onboarding engine (spec §5): current state,
    the next action to offer, and the counts behind the decision."""
    p = await _load_project(db, slug)
    orch = request.app.state.sessions
    view = await orch.pm_onboarding_counts(p.id)
    return {
        "state": view.state,
        "action": _onboarding_action_to_dict(next_onboarding_action(view)),
        "counts": _counts_to_dict(view),
    }


def _rec_id(rec: object) -> str | None:
    """Extract the session id from a spawn result that is either an ORM/dataclass
    object (in-process orchestrator) or a plain dict (remote RPC serialized form)."""
    if isinstance(rec, dict):
        return rec.get("id")
    return getattr(rec, "id", None)


@router.post("/projects/{slug}/propose-map", status_code=status.HTTP_202_ACCEPTED)
async def propose_map(
    slug: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Spawn the archaeology map-synthesis session (idempotent). 409 if backfill
    is incomplete or the map step is already past/in flight."""
    p = await _load_project(db, slug)
    orch = request.app.state.sessions
    view = await orch.pm_onboarding_counts(p.id)
    if view.repo_count == 0 or view.backfill_done_count < view.repo_count:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "backfill_incomplete",
            "message": "wait for backfill to finish before proposing the map"})
    if p.onboarding_state not in ("registered", "syncing", "map_proposed"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "in_flight",
            "message": f"map step already past in state '{p.onboarding_state}'"})
    rec = await orch.pm_onboard_spawn_map(slug)
    if rec is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "no_capacity",
            "message": "no healthy account or primary repo to spawn the session"})
    await record_audit(db=db, type="pm.onboard.propose_map", identity=identity,
                       detail={"slug": slug, "sessionId": _rec_id(rec)})
    return {"sessionId": _rec_id(rec)}


@router.post("/projects/{slug}/advance-map", status_code=status.HTTP_200_OK)
async def advance_map(
    slug: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Advance the project from ``map_proposed`` → ``map_merged`` after the
    human merges the archaeology map PR. 409 if the project is not in the
    ``map_proposed`` state."""
    p = await _load_project(db, slug)
    if p.onboarding_state != "map_proposed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "not_map_proposed",
            "message": f"project is in state '{p.onboarding_state}', not 'map_proposed'"})
    orch = request.app.state.sessions
    await orch.pm_advance_map_merged(slug)
    await record_audit(db=db, type="pm.onboard.advance_map", identity=identity,
                       detail={"slug": slug})
    return {"onboardingState": "map_merged"}


@router.post("/projects/{slug}/classify", status_code=status.HTTP_202_ACCEPTED)
async def classify(
    slug: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Spawn one backlog-classification session over the next open, unclassified
    chunk (idempotent — a full chunk with no pending proposals). 409 before the
    map is merged."""
    p = await _load_project(db, slug)
    if p.onboarding_state not in ("map_merged", "classifying"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "map_not_merged",
            "message": "merge the feature map before classifying the backlog"})
    orch = request.app.state.sessions
    dispatched = await orch.pm_classify_run(slug)
    await record_audit(db=db, type="pm.onboard.classify", identity=identity,
                       detail={"slug": slug, "dispatched": dispatched})
    return {"dispatched": dispatched}


def _triage_to_dict(prop: TriageProposal, wi: WorkItem | None,
                    feat_slug: str | None) -> dict:
    return {
        "id": prop.id,
        "workItemId": prop.work_item_id,
        "repo": wi.repo if wi else None,
        "number": wi.number if wi else None,
        "kind": wi.kind if wi else None,
        "title": wi.title if wi else None,
        "url": wi.url if wi else None,
        "proposedFeatureId": prop.proposed_feature_id,
        "proposedFeatureSlug": feat_slug,
        "noFitSuggestion": prop.no_fit_suggestion,
        "reach": prop.reach,
        "impact": prop.impact,
        "effort": prop.effort,
        "confidence": prop.confidence,
        "rationale": prop.rationale,
        "confidenceLevel": prop.confidence_level,
        "status": prop.status,
        "createdAt": prop.created_at.isoformat() if prop.created_at else None,
    }


@router.get("/triage")
async def list_triage(
    projectSlug: str | None = None,
    confidenceLevel: str | None = None,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> list[dict]:
    """Pending triage proposals, newest first, filterable by project + confidence
    (spec §7). Phase 2 is a flat list; the keyboard inbox is phase 3."""
    stmt = select(TriageProposal).where(TriageProposal.status == "pending")
    if confidenceLevel:
        stmt = stmt.where(TriageProposal.confidence_level == confidenceLevel)
    props = (await db.execute(
        stmt.order_by(TriageProposal.created_at.desc()))).scalars().all()
    if not props:
        return []
    wi_ids = [p.work_item_id for p in props]
    work_items = {w.id: w for w in (await db.execute(
        select(WorkItem).where(WorkItem.id.in_(wi_ids)))).scalars().all()}
    feat_ids = [p.proposed_feature_id for p in props if p.proposed_feature_id]
    feat_slugs: dict[str, str | None] = dict((await db.execute(
        select(Feature.id, Feature.slug).where(Feature.id.in_(feat_ids)))).all()) \
        if feat_ids else {}
    allowed_repos: set[str] | None = None
    if projectSlug:
        proj = (await db.execute(select(Project).where(
            Project.slug == projectSlug))).scalar_one_or_none()
        if proj is None:
            return []
        allowed_repos = {r for r in (await db.execute(select(
            ProjectRepo.repo).where(
                ProjectRepo.project_id == proj.id))).scalars().all() if r}
    out = []
    for p in props:
        wi = work_items.get(p.work_item_id)
        if allowed_repos is not None and (wi is None or wi.repo not in allowed_repos):
            continue
        out.append(_triage_to_dict(
            p, wi, feat_slugs.get(p.proposed_feature_id)))
    return out


async def _load_pending_proposal(db, proposal_id: str) -> TriageProposal:
    prop = (await db.execute(select(TriageProposal).where(
        TriageProposal.id == proposal_id))).scalar_one_or_none()
    if prop is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    if prop.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "already_resolved",
            "message": f"proposal is '{prop.status}', not pending"})
    return prop


async def _apply_accept(db, proposal: TriageProposal, identity: str, *,
                        edits: dict | None = None) -> None:
    """Shared accept logic (phase 3 EXTENDS this for accept-bulk — do NOT copy
    it into a second helper). Copies the proposal onto the WorkItem overlay,
    applying ``edits`` over the copied values when provided, marks the item
    ``classified`` and the proposal ``accepted``.

    ``classified_by`` is the constant ``"human"`` (a human accepted it), NOT the
    tailnet identity — the identity is carried ONLY on the audit event by the
    caller. The proposal row remains as audit."""
    wi = (await db.execute(select(WorkItem).where(
        WorkItem.id == proposal.work_item_id))).scalar_one_or_none()
    if wi is None:
        raise HTTPException(status_code=404, detail={"error": "work_item_missing"})
    now = datetime.now(timezone.utc)
    edits = edits or {}
    wi.feature_id = edits.get("featureId", proposal.proposed_feature_id)
    wi.reach = edits.get("reach", proposal.reach)
    wi.impact = edits.get("impact", proposal.impact)
    wi.effort = edits.get("effort", proposal.effort)
    wi.confidence = edits.get("confidence", proposal.confidence)
    wi.triage_state = "classified"
    wi.classified_by = "human"
    wi.classified_at = now
    proposal.status = "accepted"
    proposal.resolved_at = now
    await db.flush()


@router.post("/triage/{proposal_id}/accept")
async def accept_triage(
    proposal_id: str,
    body: dict | None = Body(default=None),
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Copy the proposal onto the WorkItem overlay and mark it classified
    (spec §7), via the shared ``_apply_accept`` helper. An OPTIONAL request body
    ``{edits?: {reach?, impact?, effort?, confidence?, featureId?}}`` overrides
    the copied values (lets phase-3 edit-then-accept work without a route
    change); absent body = plain accept. No accept-bulk (phase 3)."""
    prop = await _load_pending_proposal(db, proposal_id)
    edits = (body or {}).get("edits")
    await _apply_accept(db, prop, identity, edits=edits)
    wi_id = prop.work_item_id
    await record_audit(db=db, type="pm.triage.accept", identity=identity,
                       detail={"proposalId": proposal_id, "workItemId": wi_id})
    return {"id": prop.id, "status": "accepted", "workItemId": wi_id,
            "triageState": "classified"}


@router.post("/triage/{proposal_id}/reject")
async def reject_triage(
    proposal_id: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Reject the proposal → the work item goes to needs_human (no blind retry,
    spec §6). The proposal row remains as audit."""
    prop = await _load_pending_proposal(db, proposal_id)
    wi = (await db.execute(select(WorkItem).where(
        WorkItem.id == prop.work_item_id))).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if wi is not None:
        wi.triage_state = "needs_human"
    prop.status = "rejected"
    prop.resolved_at = now
    await db.flush()
    await record_audit(db=db, type="pm.triage.reject", identity=identity,
                       detail={"proposalId": proposal_id})
    return {"id": prop.id, "status": "rejected"}
