"""PM product-layer API — project registry + sync-now + edges (phase 1).

Design: specs/001-ccswitch-web/spec.md §7.
Mirrors server/api/sdlc.py conventions: camelCase JSON, audit on mutation,
slug identity, 409 on conflicts.
"""
from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select, tuple_

from server.audit import record_audit
from server.auth.middleware import current_identity
from server.automations.product_policy import (
    PRODUCT_POLICY_SCHEMA, PolicyEditConflict, effective_policy, load_policies, policies_by_slug,
    patch_project_policy)
from server.time_iso import iso_utc
from server.automations.product_admission import (
    ProductMissing, gather_implementation_snapshot, gather_product_coverage,
)
from server.db import get_db
from server.db.models import (
    PROJECT_EDGE_KINDS,
    WORK_ITEM_TRIAGE_STATES,
    DecisionProposal,
    Feature,
    GhProject,
    GhProjectItem,
    Initiative,
    InitiativeFeature,
    Project,
    ProjectEdge,
    ProjectRepo,
    TriageProposal,
    WorkItem,
    WorkItemDep,
)
from server.pm.desired_state import STATUS_READY
from server.pm.explain import triage_guidance
from server.pm.featuredoc import FeatureLinkRef
from server.pm.health import roadmap_health
from server.pm.issue_disposition import IssueDecision
from server.pm.map_changes import NoOpenMapChanges, NoPrimaryRepo, NotBehind, open_pointer_row
from server.pm.map_changes_gh import GhWriteError
from server.pm.map_changes_ops import SLUG_RE, MapOp, MapOpError
from server.pm.mirror_store import deps_in_repos
from server.pm.outcomes import list_outcomes_stmt, outcome_to_dict
from server.pm.project_slugs import (
    PROJECT_UPDATE_AUDIT_TYPE, former_slugs_after_rename)
from server.pm.queue import MIN_STATUS_OPTIONS, rank, rank_key
from server.pm.queue_store import load_queue_rows, runner_claims
from server.pm.repo_project import (
    OwnershipConflict, gather_repo_ownership_snapshot, lock_repo_registration,
    owning_projects_by_repo, require_unregistered_repos,
)
from server.pm.score import RICE_FIELDS, rice_score, validate_rice
from server.pm.sync_request import request_project_sync
# The accept write path and the undo-snapshot format live in server/pm/triage_accept.py
# (spec 2026-09-12 §8, D6) so the PM Triage loop's auto-accept shares them; these
# routes are callers, not owners. Private names are imported deliberately — they are
# module-internal to the accept path, not part of a public API.
from server.pm.triage_accept import (
    DECIDED_BY_AUTO,
    DECIDED_BY_HUMAN,
    WorkItemMissing,
    _aware,
    _overlay_snapshot,
    _restore_overlay,
    apply_accept,
    pending_proposal_stmt,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pm", tags=["pm"])

# How long after accept/reject a proposal can be undone. The UI toast lasts
# six seconds; the server keeps a wider window so a slow click still lands.
TRIAGE_UNDO_WINDOW_S = 60


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
        # The RAW validated blob, or null when the product never opted in — the web
        # renders it through PRODUCT_POLICY_SCHEMA's defaults (server/automations/product_policy.py).
        "automationPolicy": p.automation_policy,
        "automationPolicyUpdatedAt": iso_utc(p.automation_policy_updated_at),
        "priorityRank": p.priority_rank,
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


class ProjectUpdateBody(BaseModel):
    """Sparse patch: only the fields present are changed. `repo` repoints the
    PRIMARY repo in place (rename-to-new-location, not add/remove-repos)."""

    name: str | None = None
    slug: str | None = None
    repo: str | None = None
    priorityRank: int | None = None


class EdgeCreateBody(BaseModel):
    dstSlug: str
    kind: str
    note: str | None = None


class PolicyPatchBody(BaseModel):
    # `None` clears the policy (opts the product back out entirely); a dict is
    # validated against PRODUCT_POLICY_SCHEMA before it's stored.
    policy: dict | None
    expectedUpdatedAt: datetime | None = None


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
    ownership = await gather_repo_ownership_snapshot([repo.repo for repo in body.repos])
    await lock_repo_registration(db)
    existing = (await db.execute(
        select(Project).where(Project.slug == slug))).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "slug_conflict", "message": f"Project '{slug}' already exists"})
    try:
        canonical = await require_unregistered_repos(
            db, [repo.repo for repo in body.repos], snapshot=ownership)
    except OwnershipConflict as exc:
        raise HTTPException(status_code=409, detail={
            "error": "ownership_conflict", "ownershipState": exc.state, "message": str(exc),
        }) from exc
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
    primary_repo = canonical[body.repos[primary_idx].repo].casefold()
    registered: set[str] = set()
    for repo_in in body.repos:
        repo = canonical[repo_in.repo]
        key = repo.casefold()
        if key in registered:
            continue
        registered.add(key)
        db.add(ProjectRepo(
            project_id=project.id,
            repo=repo,
            is_primary=(key == primary_repo),
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


async def _project_detail_dict(db, p: Project) -> dict:
    """`_project_to_dict` plus repos/edges — the full shape the frontend Project type
    requires. Shared by `get_project` and `update_project_policy` so the two response
    shapes cannot drift."""
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


@router.get("/projects/{slug}/health")
async def project_health(slug: str, _identity: str = Depends(current_identity),
                         db=Depends(get_db)) -> dict:
    """Roadmap drift for one product (5S plan, Sustain): loose capabilities,
    initiatives with no capability, stale `proposed` leaves, old inbox items."""
    p = await _load_project(db, slug)
    feats = (await db.execute(select(Feature).where(
        Feature.project_id == p.id, Feature.orphaned.isnot(True)))).scalars().all()
    slug_by_id = {f.id: f.slug for f in feats}
    caps = [(f.slug, slug_by_id.get(f.parent_feature_id), f.state, f.updated_at) for f in feats]
    inits = (await db.execute(select(Initiative).where(Initiative.project_id == p.id))).scalars().all()
    ids = [i.id for i in inits]
    link_counts: dict[str, int] = {}
    parents_with_kids: set[str] = set()
    if ids:
        for iid, in (await db.execute(select(InitiativeFeature.initiative_id).where(
                InitiativeFeature.initiative_id.in_(ids)))).all():
            link_counts[iid] = link_counts.get(iid, 0) + 1
        parents_with_kids = {pid for pid, in (await db.execute(
            select(Initiative.parent_initiative_id).where(
                Initiative.parent_initiative_id.in_(ids)))).all() if pid}
    repos = (await db.execute(
        select(ProjectRepo).where(ProjectRepo.project_id == p.id))).scalars().all()
    repo_names = [r.repo for r in repos if r.repo]
    props: list[datetime] = []
    if repo_names:
        props = [c for c, in (await db.execute(select(TriageProposal.created_at).join(
            WorkItem, WorkItem.id == TriageProposal.work_item_id).where(
            TriageProposal.status == "pending",
            WorkItem.repo.in_(repo_names)))).all()]
    now = datetime.now(timezone.utc)
    unindexed = [(d.get("path", ""), d.get("error", "")) for d in (p.unindexed_docs or [])]
    h = roadmap_health(capabilities=caps,
                       initiatives=[(i.slug or "", link_counts.get(i.id, 0), i.id in parents_with_kids,
                                    i.stage == "done" or i.status == "done") for i in inits],
                       proposals=props, now=now, unindexed_docs=unindexed)
    return {"unparentedCapabilities": list(h.unparented_capabilities),
            "unlinkedInitiatives": list(h.unlinked_initiatives),
            "staleProposed": list(h.stale_proposed), "staleInbox": h.stale_inbox,
            "unindexedDocs": [{"path": path, "error": error} for path, error in h.unindexed_docs],
            "generatedAt": now.isoformat()}


@router.get("/policy-schema")
async def policy_schema(
    _identity: str = Depends(current_identity),
) -> list[dict]:
    return [f.to_dict() for f in PRODUCT_POLICY_SCHEMA]


@router.get("/projects/{slug}")
async def get_project(
    slug: str,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    p = await _load_project(db, slug)
    return await _project_detail_dict(db, p)


@router.patch("/projects/{slug}")
async def update_project(
    slug: str,
    body: ProjectUpdateBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Rename a product's name/slug and/or repoint its primary repo.

    Slug and primary-repo changes are refused while a map-changes branch/PR is
    open: both `map_changes_branch_name` and the service's primary-repo target
    are derived from the CURRENT slug/repo at use time, so either change would
    silently orphan the open branch. Merge or abandon it first, then retry.
    Renaming touches no foreign key (every FK is on `Project.id`, never slug),
    but a repo repoint resets that repo's sync state — it is now a different
    upstream, so history is re-mirrored from scratch rather than kept stale.

    A slug change also records the old slug in `former_slugs`. Feature-doc
    `links:` blocks are the one reference that names a product by slug instead
    of id, they live in other repos, and pm-sync rebuilds every outgoing edge
    from those docs — so without the alias a rename silently drops each inbound
    cross-product edge (#1881). See server/pm/project_slugs.py.
    """
    name = body.name.strip() if body.name is not None else None
    if body.name is not None and not name:
        raise HTTPException(status_code=422, detail={
            "error": "bad_name", "message": "name cannot be empty"})
    new_slug = body.slug.strip() if body.slug is not None else None
    if new_slug is not None and not SLUG_RE.match(new_slug):
        raise HTTPException(status_code=422, detail={
            "error": "bad_slug",
            "message": "slug must be lowercase letters, digits and hyphens (branch-safe)"})
    repo = body.repo.strip() if body.repo is not None else None
    if repo is not None:
        _validate_repo(repo)

    rank_set = "priorityRank" in body.model_fields_set
    if rank_set and body.priorityRank is not None and body.priorityRank < 1:
        raise HTTPException(status_code=422, detail={
            "error": "bad_priority_rank",
            "message": "priorityRank must be 1 or higher, or null to unrank"})

    # Ownership preflight must run, and the registration lock must be taken,
    # before any other query on this request's session (server/pm/repo_project.py
    # `lock_repo_registration` docstring) — so this happens before `_load_project`.
    snapshot = None
    if repo is not None:
        snapshot = await gather_repo_ownership_snapshot([repo])
        await lock_repo_registration(db)

    p = await _load_project(db, slug)
    primary = (await db.execute(
        select(ProjectRepo).where(
            ProjectRepo.project_id == p.id, ProjectRepo.is_primary.is_(True))
    )).scalar_one_or_none()

    slug_changing = new_slug is not None and new_slug != p.slug
    repo_changing = repo is not None and (
        primary is None or (primary.repo or "").casefold() != repo.casefold())
    if slug_changing or repo_changing:
        open_pointer = await open_pointer_row(db, p.id)
        if open_pointer is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
                "error": "open_map_changes",
                "message": "Merge or abandon the open map-changes PR before renaming "
                           "the slug or repointing the primary repo."})

    changes: dict = {}
    # Repo ownership must be re-verified against `snapshot` — gathered before any
    # write in this request — while `p`/`primary` are still unmodified: the
    # re-check's SELECT autoflushes pending session writes first, so a slug/name
    # assignment made here before the repo check would leak into that SELECT and
    # make this project's own row look like it changed since the snapshot,
    # tripping OwnershipConflict("changed") on every combined rename+repoint.
    if repo is not None and repo_changing:
        assert snapshot is not None  # gathered above whenever `repo is not None`
        try:
            canonical = await require_unregistered_repos(db, [repo], snapshot=snapshot)
        except OwnershipConflict as exc:
            raise HTTPException(status_code=409, detail={
                "error": "ownership_conflict", "ownershipState": exc.state, "message": str(exc),
            }) from exc
        new_repo = canonical[repo]
        changes["repo"] = {"from": primary.repo if primary else None, "to": new_repo}
        if primary is None:
            db.add(ProjectRepo(project_id=p.id, repo=new_repo, is_primary=True))
        else:
            primary.repo = new_repo
            # A different upstream: nothing already mirrored belongs to it.
            primary.backfill_cursor = None
            primary.backfill_phase = "issues"
            primary.backfill_done = False
            primary.watermark = None
            primary.last_synced_at = None

    if name is not None and name != p.name:
        changes["name"] = {"from": p.name, "to": name}
        p.name = name
    if slug_changing:
        assert new_slug is not None  # implied by slug_changing, not narrowed through it
        existing = (await db.execute(
            select(Project).where(Project.slug == new_slug, Project.id != p.id)
        )).scalar_one_or_none()
        if existing:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
                "error": "slug_conflict", "message": f"Project '{new_slug}' already exists"})
        changes["slug"] = {"from": p.slug, "to": new_slug}
        p.former_slugs = former_slugs_after_rename(
            p.former_slugs, old_slug=p.slug, new_slug=new_slug) or None
        p.slug = new_slug

    if rank_set and body.priorityRank != p.priority_rank:
        changes["priorityRank"] = {"from": p.priority_rank, "to": body.priorityRank}
        p.priority_rank = body.priorityRank

    if not changes:
        return await _project_detail_dict(db, p)

    await db.flush()
    # This record is also READ back: it is the only durable trace of a rename
    # made before `former_slugs` existed (server/pm/project_slugs.py).
    await record_audit(db=db, type=PROJECT_UPDATE_AUDIT_TYPE, identity=identity,
                       detail={"slug": p.slug, "changes": changes})
    return await _project_detail_dict(db, p)


@router.get("/projects/{slug}/policy/coverage")
async def project_policy_coverage(
    slug: str,
    _identity: str = Depends(current_identity),
) -> dict:
    try:
        evidence = await gather_product_coverage(requested_product=slug)
    except ProductMissing:
        raise HTTPException(status_code=404, detail="Project not found") from None
    coverage = evidence.ownership
    return {
        "projectSlug": slug,
        "observedAt": evidence.observed_at,
        "scope": "repository-coverage",
        "explanation": (
            "Repository coverage compares current GitHub identity evidence with registered "
            "products. Unavailable identities remain unresolved; registration alone does "
            "not verify coverage. This preview is not permission to start work."),
        "repositories": [{
            "requestedRepo": item.requested_repo,
            "canonicalRepo": item.canonical_repo,
            "state": item.state,
            "owner": ({"id": item.owner.project_id, "slug": item.owner.slug}
                      if item.owner else None),
            "candidates": [{"id": owner.project_id, "slug": owner.slug}
                           for owner in item.candidates],
            "unresolvedRepos": list(item.unresolved_repos),
            "reason": item.reason,
        } for item in coverage.values()],
    }


@router.get("/projects/{slug}/policy/effective")
async def project_policy_effective(
    slug: str,
    _identity: str = Depends(current_identity),
) -> dict:
    from dataclasses import asdict
    from typing import Literal

    try:
        snapshot = await gather_implementation_snapshot(requested_product=slug)
    except ProductMissing:
        raise HTTPException(status_code=404, detail="Project not found") from None
    repositories = []
    for repo, ownership in snapshot.coverage.ownership.items():
        decisions = {}
        actions: tuple[tuple[str, Literal["automatic", "manual"],
                             Literal["implementation", "proposal"]], ...] = (
            ("automaticImplementation", "automatic", "implementation"),
            ("automaticProposal", "automatic", "proposal"),
            ("manualImplementation", "manual", "implementation"),
        )
        for key, trigger, effect in actions:
            decision, _ = snapshot.decide(
                repo, requested_product=slug, trigger=trigger, effect=effect)
            decisions[key] = {
                "state": decision.state,
                "constraints": [asdict(c) for c in decision.constraints],
                "configuredDispatch": decision.configured_dispatch,
                "policySource": decision.policy_source,
                "productId": decision.product_id, "productSlug": decision.product_slug,
                "canonicalRepo": decision.canonical_repo,
                "approvalRequired": decision.approval_required,
                "observedAt": decision.observed_at, "policyRevision": decision.policy_revision,
                "remainingChecks": list(decision.remaining_checks), "note": decision.note,
            }
        repositories.append({"requestedRepo": repo, "canonicalRepo": ownership.canonical_repo,
                             "coverageState": ownership.state, "decisions": decisions})
    return {"projectSlug": slug, "observedAt": snapshot.coverage.observed_at,
            "scope": "implementation-policy-eligibility",
            "explanation": "Current implementation policy eligibility only. Target readiness, "
                           "engine, security and ownership checks still apply before execution. "
                           "This preview is not a future authorization token. Other responsibilities "
                           "retain their existing behavior.",
            "repositories": repositories}


@router.patch("/projects/{slug}/policy")
async def update_project_policy(
    slug: str,
    body: PolicyPatchBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    p = await _load_project(db, slug)
    try:
        validated = await patch_project_policy(
            db, p, body.policy, expected_updated_at=body.expectedUpdatedAt,
            check_expected="expectedUpdatedAt" in body.model_fields_set,
        )
    except PolicyEditConflict as e:
        raise HTTPException(status_code=409, detail={
            "error": "policy_conflict",
            "message": "Automation settings changed. Refresh and try again.",
        }) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail={
            "error": "bad_policy", "message": str(e),
        }) from e
    await record_audit(db=db, type="pm.project.policy", identity=identity,
                       detail={"slug": slug, "policy": validated})
    return await _project_detail_dict(db, p)


@router.post("/projects/{slug}/sync", status_code=status.HTTP_202_ACCEPTED)
async def request_sync(
    slug: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    p = await _load_project(db, slug)
    await request_project_sync(db, p.id, datetime.now(timezone.utc))
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
    # WorkItemDep table in Python. Shared with the reconciler's facts loader.
    dep_rows = list((await db.execute(
        select(WorkItemDep).where(deps_in_repos(repos)))).scalars().all())

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


@router.get("/reconcile/journal")
async def reconcile_journal(
    state: str | None = None,
    limit: int = 200,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Reconciler write-ahead journal, filterable by state (spec 003 Phase B).
    One unfiltered ``list_rows(state=None, limit=100_000)`` load backs both the
    counts (across all five states) and the filtered/limited ``rows`` — not a
    second, state-scoped query — since the two share the same source rows at
    Phase B scale. State validation mirrors ``journal.list_rows``'s own check
    so an unknown state still 400s identically."""
    from server.pm import journal

    clamped_limit = min(limit, 1000)

    def _load(s) -> tuple[list, dict[str, int]]:
        if state is not None and state not in journal.STATES:
            raise ValueError(f"unknown journal state {state!r}")
        all_rows = journal.list_rows(s, state=None, limit=100_000)
        counts = {k: 0 for k in journal.STATES}
        for r in all_rows:
            counts[journal.state_of(r)] += 1
        filtered = all_rows if state is None else [
            r for r in all_rows if journal.state_of(r) == state]
        return filtered[:clamped_limit], counts

    try:
        rows, counts = await db.run_sync(_load)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "rows": [{
            "id": r.id,
            "targetNodeId": r.target_node_id,
            "field": r.field,
            "oldValue": (r.old_value or {}).get("value"),
            "newValue": (r.new_value or {}).get("value"),
            "mutationId": r.mutation_id,
            "initiatedBy": r.initiated_by,
            "batchId": r.batch_id,
            "state": journal.state_of(r),
            "error": r.error,
            "createdAt": r.created_at.isoformat() if r.created_at else None,
            "updatedAt": r.updated_at.isoformat() if r.updated_at else None,
        } for r in rows],
        "counts": counts,
    }


@router.get("/queue")
async def queue(
    min_status: str = STATUS_READY,
    respect_dependencies: bool = True,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """The global priority queue (spec 003 §7a/§7c): one ranking over every onboarded
    product with a linked board, via the same ``rank(load_queue_rows(...))`` composition
    the backlog runner uses — "what you see is what agents will do next". ``body`` is
    deliberately omitted from the payload (large, UI/runner don't need it here).

    The runner's claims ride the SAME derivation the runner's own pool read uses
    (``runner_claims``), so a claimed issue reads as ``claimed`` here exactly as
    selection sees it; a held item reads as ``held`` off its own ``hold_kind``
    (spec 2026-09-18 §3), with no runner state involved at all. Ranking is PER
    PRODUCT with that product's ``give_up_after_bounces`` (D8) and the per-product
    rankings are merged with ``rank``'s own ``rank_key`` — the runner's
    ``_backlog_query_queue`` shape — so a bounced-out issue the runner skips as
    ``needs_spec`` is not shown here as "up next" either, and cross-product ties
    still break by issue age rather than by product slug.

    Two knobs still differ from the runner's composition:
    - ``min_status`` and ``respect_dependencies`` come from query params here; the
      runner reads them from its stored config (its per-product policy), so they
      apply uniformly to every product in this view.
    - SCOPE: the runner ranks only products whose ``dispatch`` is on; this view is a
      view, not a dispatch decision, and ranks every onboarded product's board.
    """
    if min_status not in MIN_STATUS_OPTIONS:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_min_status",
                    "message": f"min_status must be one of {MIN_STATUS_OPTIONS}, "
                               f"got {min_status!r}"})

    now = datetime.now(timezone.utc)

    def _rows(s):
        # The policies are read in the SAME session as the rows: which threshold a
        # product's issues are judged against decides what this view shows, so a
        # policy edit must not be able to land between the two reads.
        claimed = runner_claims(s)
        return (load_queue_rows(s, now=now, claimed_refs=claimed),
                policies_by_slug(s))

    rows, policies = await db.run_sync(_rows)
    by_product: dict[str, list] = {}
    for r in rows:
        by_product.setdefault(r.product, []).append(r)
    entries: list = []
    skipped: dict[str, int] = {}
    for slug in sorted(by_product):
        # A product with no row of its own (an unmirrored slug) reads as the policy
        # SCHEMA's defaults, never as a hand-written 0.
        policy = policies.get(slug) or effective_policy(None)
        entries_p, skipped_p = rank(
            by_product[slug], min_status=min_status,
            respect_dependencies=respect_dependencies, now=now,
            # D8: THE bounce threshold, from this product's own policy — the same
            # reading the runner's pool uses.
            give_up_after_bounces=int(policy["give_up_after_bounces"]),
            extra_skip_labels=frozenset(
                lbl.strip().lower() for lbl in policy["skip_labels"]))
        entries.extend(entries_p)
        for reason, n in skipped_p.items():
            skipped[reason] = skipped.get(reason, 0) + n
    # Merge with ``rank``'s OWN key rather than a hand-copied tuple: ranking per
    # product and re-sorting on score alone would break every cross-product tie by
    # product slug (the loop order) instead of by issue age.
    entries.sort(key=rank_key)

    return {
        "entries": [{
            "ref": e.ref,
            "repo": e.repo,
            "number": e.number,
            "product": e.product,
            "title": e.title,
            "url": e.url,
            "score": e.score,
        } for e in entries],
        "skipped": skipped,
        "generatedAt": now.isoformat(),
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
from urllib.parse import quote_plus  # noqa: E402

from server.pm.classification import map_branch_name  # noqa: E402


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
    primary = (await db.execute(select(ProjectRepo).where(
        ProjectRepo.project_id == p.id,
        ProjectRepo.is_primary.is_(True)))).scalars().first()
    map_pr = None
    if primary is not None and primary.repo:
        branch = map_branch_name(slug)
        # No PR number to link: this is the ARCHAEOLOGY map PR
        # (`classification.map_branch_name`, a one-shot synthesis), which has no
        # pointer row and nothing that records its number. Deliberately not to be
        # confused with `Feature.pr_number`, which IS written — by the index pass
        # over the long-lived map-changes branch (`pm_sync` → `plan_feature_index
        # (proposed=…)`, R8b-4), for the DIFFERENT branch
        # `map_changes.map_changes_branch_name`. The branch here is
        # deterministic, so a GitHub PR search on it is the link.
        map_pr = {
            "branch": branch,
            "url": f"https://github.com/{primary.repo}/pulls?q="
                   + quote_plus(f"is:pr head:{branch}"),
        }
    return {
        "state": view.state,
        "action": _onboarding_action_to_dict(next_onboarding_action(view)),
        "counts": _counts_to_dict(view),
        "mapPr": map_pr,
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
    ``map_proposed`` state, or if the merged map left feature docs pm-sync
    could not parse — those never became Feature rows (issue #2166), so
    advancing anyway would silently under-count the map the wizard just
    congratulated the human for finishing."""
    p = await _load_project(db, slug)
    if p.onboarding_state != "map_proposed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "not_map_proposed",
            "message": f"project is in state '{p.onboarding_state}', not 'map_proposed'"})
    unindexed = p.unindexed_docs or []
    if unindexed:
        paths = [d.get("path", "") for d in unindexed]
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "unindexed_docs",
            "message": (f"{len(unindexed)} feature doc(s) could not be parsed and are "
                        f"NOT on the map: " + "; ".join(paths[:5])
                        + ("; …" if len(paths) > 5 else "") + " — fix them, wait for the "
                        "next sync pass, and try again"),
            "unindexedDocs": unindexed})
    orch = request.app.state.sessions
    await orch.pm_advance_map_merged(slug)
    await record_audit(db=db, type="pm.onboard.advance_map", identity=identity,
                       detail={"slug": slug})
    return {"onboardingState": "map_merged"}


class MapChangeBody(BaseModel):
    """One capability edit. ``op`` selects which of the optional fields matter
    (``create``: title/parentSlug/summary/links; ``rename``: title; …)."""

    op: str
    slug: str
    title: str | None = None
    parentSlug: str | None = None
    summary: str | None = None
    links: list[dict] | None = None     # [{kind, project, feature}]


def _map_changes_http(e: Exception) -> HTTPException:
    """The one place map-changes failures become status codes, so the three
    routes cannot drift apart."""
    if isinstance(e, MapOpError):
        return HTTPException(status_code=422, detail={"error": e.code, "message": e.message})
    if isinstance(e, NoPrimaryRepo):
        return HTTPException(status_code=422, detail={
            "error": "no_primary_repo", "message": str(e)})
    if isinstance(e, NoOpenMapChanges):
        return HTTPException(status_code=404, detail={
            "error": "no_open_map_changes", "message": str(e)})
    if isinstance(e, NotBehind):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "not_behind", "message": str(e)})
    if isinstance(e, GhWriteError):
        if e.conflict:
            return HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
                "error": "conflict", "step": e.step, "message": e.detail})
        return HTTPException(status_code=502, detail={
            "error": "github", "step": e.step, "message": e.detail})
    if isinstance(e, subprocess.TimeoutExpired):
        # `gh` hung past _run_gh's timeout — an upstream failure like any other
        # GitHub error, not a 500: the edit simply did not happen.
        return HTTPException(status_code=502, detail={
            "error": "github", "step": "timeout", "message": str(e)})
    raise e


@router.post("/projects/{slug}/map-changes", status_code=status.HTTP_200_OK)
async def map_changes_edit(
    slug: str,
    body: MapChangeBody,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """One capability edit on the product's map-changes branch (spec §8/§9).

    The first edit opens the branch and the PR; every later one appends a commit
    to the same PR. No agent session is involved (R8b-1): the edit is committed
    through GitHub's API and pm-sync is asked for a pass so the map shows it."""
    p = await _load_project(db, slug)
    svc = request.app.state.sessions.pm_map_changes()
    op = MapOp(
        op=body.op, slug=body.slug, title=body.title, parent_slug=body.parentSlug,
        summary=body.summary,
        links=tuple(FeatureLinkRef(kind=str(link.get("kind")), project=str(link.get("project")),
                                   feature=str(link.get("feature")))
                    for link in (body.links or [])))
    try:
        pointer, path, sha = await svc.edit(db, p, op, identity)
    except (MapOpError, NoPrimaryRepo, GhWriteError, subprocess.TimeoutExpired) as e:
        raise _map_changes_http(e) from e
    return {"pointer": pointer.to_dict(), "path": path, "commitSha": sha}


@router.delete("/projects/{slug}/map-changes", status_code=status.HTTP_200_OK)
async def map_changes_abandon(
    slug: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Abandon the open map-changes PR: close it, delete the branch, mark the
    pointer ``abandoned``, then drop the capabilities that only ever lived on
    that branch (R8b-7) — nothing will ever read their docs again, so no index
    pass could retire them.

    With no pointer row, a branch or PR stranded on GitHub (the commit lands
    before the row) is adopted first; 404 means neither exists."""
    p = await _load_project(db, slug)
    svc = request.app.state.sessions.pm_map_changes()
    try:
        pointer = await svc.abandon(db, p, identity)
        cleanup = await svc.cleanup_after_abandon(db, p, pointer.pr_number)
    except (MapOpError, NoOpenMapChanges, NoPrimaryRepo, GhWriteError,
            subprocess.TimeoutExpired) as e:
        raise _map_changes_http(e) from e
    return {"pointer": pointer.to_dict(), "cleanup": cleanup}


@router.post("/projects/{slug}/map-changes/rebase", status_code=status.HTTP_200_OK)
async def map_changes_rebase(
    slug: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Merge the base branch into the map-changes PR through GitHub's own
    update-branch (R8b-8) — only while GitHub reports the PR behind."""
    p = await _load_project(db, slug)
    svc = request.app.state.sessions.pm_map_changes()
    try:
        pointer = await svc.rebase(db, p, identity)
    except (NoOpenMapChanges, NotBehind, NoPrimaryRepo, GhWriteError,
            subprocess.TimeoutExpired) as e:
        raise _map_changes_http(e) from e
    return {"pointer": pointer.to_dict()}


@router.post("/projects/{slug}/advance-live", status_code=status.HTTP_200_OK)
async def advance_live(
    slug: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Advance the project to ``live`` once the human has drained the triage
    queue. Allowed from ``map_merged`` (nothing needed classifying) and
    ``classifying``; 409 otherwise, including from ``live`` itself."""
    p = await _load_project(db, slug)
    if p.onboarding_state not in ("map_merged", "classifying"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "not_classifying",
            "message": f"project is in state '{p.onboarding_state}', "
                       "not 'map_merged' or 'classifying'"})
    orch = request.app.state.sessions
    await orch.pm_advance_live(slug)
    await record_audit(db=db, type="pm.onboard.advance_live", identity=identity,
                       detail={"slug": slug})
    return {"onboardingState": "live"}


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


def _decided_by(prop: TriageProposal) -> str | None:
    """Who resolved *prop*: the ``decidedBy`` ``apply_accept`` stamps into the undo
    snapshot (``human`` | ``auto``), or ``None`` for a pending row or a decision
    taken before the snapshot carried it."""
    return (prop.undo_snapshot or {}).get("decidedBy")


def _triage_to_dict(prop: TriageProposal, wi: WorkItem | None,
                    feat_slug: str | None, project_slug: str | None,
                    guidance: dict | None = None) -> dict:
    return {
        "id": prop.id,
        "workItemId": prop.work_item_id,
        "repo": wi.repo if wi else None,
        "projectSlug": project_slug,
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
        # Provenance of a RESOLVED proposal comes from the proposal itself (see
        # ``list_triage``); a pending one has none.
        "classifiedBy": _decided_by(prop) if prop.status != "pending" else None,
        "resolvedAt": prop.resolved_at.isoformat() if prop.resolved_at else None,
        # What Accept / Reject would do under the owning product's dispatch policy
        # (spec 2026-09-17 §4.3); None only where a caller has no policy to hand.
        "guidance": guidance,
    }


@router.get("/triage")
async def list_triage(
    projectSlug: str | None = None,
    confidenceLevel: str | None = None,
    pendingOnly: bool = False,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> list[dict]:
    """Pending triage proposals, newest first, filterable by project + confidence
    (spec §7). Phase 2 is a flat list; the keyboard inbox is phase 3.

    Pending, PLUS the loop's fresh auto-accepts for the undo window (spec §8):
    the inbox shows them with an "auto-accepted · undo" chip, then they leave
    like a human accept. A human accept never appears here — its undo lives in
    the toast of the browser that made it.

    ``pendingOnly=true`` suppresses that widened leg and answers exactly
    ``status == "pending"``. The widening exists for ONE surface — the inbox,
    which renders the chip. Every other consumer is COUNTING "awaiting a human
    decision" (the assistant's ``list_triage``, and anything that asks the API
    rather than filtering client-side), and for those a fresh auto-accept is a
    decision already taken: including it over-counts the queue and invites a
    "3 proposals need you" that names rows nobody has to touch.
    """
    since = datetime.now(timezone.utc) - timedelta(seconds=TRIAGE_UNDO_WINDOW_S)
    stmt = select(TriageProposal).where(
        TriageProposal.status == "pending" if pendingOnly else or_(
            TriageProposal.status == "pending",
            and_(TriageProposal.status == "accepted",
                 TriageProposal.resolved_at >= since)))
    if confidenceLevel:
        stmt = stmt.where(TriageProposal.confidence_level == confidenceLevel)
    props = (await db.execute(
        stmt.order_by(TriageProposal.created_at.desc()))).scalars().all()
    # Who decided is read off the PROPOSAL (``undo_snapshot["decidedBy"]``, written
    # by ``apply_accept``), never off ``WorkItem.classified_by``: an overlay PATCH
    # inside the window re-stamps that column ``human``, and keying the widened
    # leg on it made the row — and its undo chip — vanish while the undo was
    # still valid. The window holds a handful of rows, so the filter is Python.
    props = [p for p in props if p.status == "pending"
             or _decided_by(p) == DECIDED_BY_AUTO]
    if not props:
        return []
    wi_ids = [p.work_item_id for p in props]
    work_items = {w.id: w for w in (await db.execute(
        select(WorkItem).where(WorkItem.id.in_(wi_ids)))).scalars().all()}
    feat_ids = [p.proposed_feature_id for p in props if p.proposed_feature_id]
    feat_slugs: dict[str, str | None] = dict((await db.execute(
        select(Feature.id, Feature.slug).where(Feature.id.in_(feat_ids)))).all()) \
        if feat_ids else {}
    # repo → owning project slug, resolved server-side in ONE query so the inbox
    # header can group rows by project (the client must not infer it from repo).
    repos = {w.repo for w in work_items.values() if w.repo}
    slug_by_repo: dict[str, str] = {
        repo: owner.slug for repo, owner
        in (await owning_projects_by_repo(db, repos)).items()}
    policies = await load_policies(db)
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
        slug = slug_by_repo.get(wi.repo) if wi and wi.repo else None
        out.append(_triage_to_dict(
            p, wi, feat_slugs.get(p.proposed_feature_id), slug,
            guidance=triage_guidance(
                feature_slug=feat_slugs.get(p.proposed_feature_id),
                score=rice_score(p.reach, p.impact, p.effort, p.confidence),
                policy=(policies.get(slug) if slug else None) or effective_policy(None))))
    return out


async def _load_pending_proposal(db, proposal_id: str) -> TriageProposal:
    """Re-read the proposal under a row lock and refuse it unless it is still
    pending — the human half of the human-vs-loop race.

    The lock lives in the shared ``pending_proposal_stmt`` (server/pm/
    triage_accept.py), which explains it: without one, this route and the PM
    Triage loop's auto-accept can both read "pending" under Postgres READ
    COMMITTED and both write, so whoever commits last wins — a human's
    accept-with-edits silently replaced by the loop's plain copy, stamped
    ``classified_by="auto"``. Locking here is what makes the loser re-read the
    row as resolved and answer 409 instead."""
    prop = (await db.execute(
        pending_proposal_stmt(proposal_id))).scalar_one_or_none()
    if prop is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    if prop.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "already_resolved",
            "message": f"proposal is '{prop.status}', not pending"})
    return prop


async def _validate_accept_edits(db, edits: dict | None) -> None:
    """Validate an accept body's ``edits`` exactly as the overlay PATCH does.

    Accept-with-edits writes the same five fields the overlay PATCH does, so it
    needs the same guards — an out-of-range value reaches the GitHub Projects
    ``Score`` field and inverts roadmap ordering, an ``effort`` of 0 makes the
    score undefined, and a ``featureId`` that names no row would leave the work
    item pointing at nothing. Bounds live in ``server/pm/score.py`` beside the
    formula; do NOT inline a second copy of them here, and do not let the client
    be the only thing enforcing them (the inbox no longer PATCHes the overlay
    before accepting).
    """
    if not edits:
        return
    for attr in RICE_FIELDS:
        if attr not in edits:
            continue
        value = edits[attr]
        # ``validate_rice`` compares against ints; a string would raise
        # TypeError out of the route as a 500, which is not what a bad request
        # deserves. ``bool`` IS an ``int`` in Python, and ``True`` would land in
        # the overlay as a 1 nobody typed — excluded explicitly.
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise HTTPException(status_code=422, detail={
                "error": "bad_rice_value", "message": f"{attr} must be a whole number"})
        msg = validate_rice(attr, value)
        if msg:
            raise HTTPException(status_code=422, detail={
                "error": "bad_rice_value", "message": msg})
    # Same 404 shape ``patch_overlay`` answers with, for the same reason: the id
    # comes from a client-side capability list that a rename or a delete can
    # outdate between load and accept.
    feature_id = edits.get("featureId")
    if feature_id is not None:
        feat = (await db.execute(select(Feature).where(
            Feature.id == feature_id))).scalar_one_or_none()
        if feat is None:
            raise HTTPException(status_code=404,
                                detail={"error": "feature_not_found"})


@router.post("/triage/{proposal_id}/accept")
async def accept_triage(
    proposal_id: str,
    body: dict | None = Body(default=None),
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Copy the proposal onto the WorkItem overlay and mark it classified
    (spec §7), via the shared ``apply_accept`` (server/pm/triage_accept.py). An
    OPTIONAL request body
    ``{edits?: {reach?, impact?, effort?, confidence?, featureId?}}`` overrides
    the copied values (lets phase-3 edit-then-accept work without a route
    change); absent body = plain accept. The batch form is ``accept-bulk``,
    which reuses the same helper."""
    from server.pm.triage_accept import gather_accept_ownership
    ownership = await gather_accept_ownership([proposal_id])
    prop = await _load_pending_proposal(db, proposal_id)
    edits = (body or {}).get("edits")
    await _validate_accept_edits(db, edits)
    try:
        await apply_accept(db, prop, decided_by=DECIDED_BY_HUMAN,
                           audit_identity=identity, edits=edits, ownership=ownership)
    except OwnershipConflict as e:
        raise HTTPException(status_code=409, detail=e.result()) from e
    except WorkItemMissing as e:
        raise HTTPException(status_code=404,
                            detail={"error": "work_item_missing"}) from e
    wi_id = prop.work_item_id
    await record_audit(db=db, type="pm.triage.accept", identity=identity,
                       detail={"proposalId": proposal_id, "workItemId": wi_id})
    return {"id": prop.id, "status": "accepted", "workItemId": wi_id,
            "triageState": "classified"}


@router.post("/triage/{proposal_id}/reject")
async def reject_triage(
    proposal_id: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Reject the proposal → the work item is parked with an owner-action hold
    (no blind retry, spec 2026-09-18 §3). The proposal row remains as audit.
    The batch form is ``reject-bulk``, which reuses the same ``apply_reject``."""
    prop = await _load_pending_proposal(db, proposal_id)
    await apply_reject(db, prop, request=request, audit_identity=identity)
    return {"id": prop.id, "status": "rejected", "workItemId": prop.work_item_id}


async def apply_reject(
    db, prop: TriageProposal, *, request: Request, audit_identity: str,
) -> None:
    """The one reject write path — single-reject and reject-bulk are both callers.

    Parks the work item under an owner-action hold, snapshots the overlay so
    the undo route can put it back, and flips the proposal to ``rejected``.
    Spec 2026-09-18 §3: a rejection is a hold, not a triage state — and §8 names
    "the two reject flows" as authorised to write owner_action whoever drives
    them, so this goes through ``set_reject_hold`` (no actor-class gate) rather
    than ``set_hold``. The ACTOR is resolved exactly as patch_overlay resolves it
    so an assistant or loopback rejection is recorded as what it was."""
    wi = (await db.execute(select(WorkItem).where(
        WorkItem.id == prop.work_item_id))).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if wi is not None:
        prop.undo_snapshot = _overlay_snapshot(wi)
        from server.pm.holds import REJECT_HOLD_REASON, actor_class, actor_for_request, set_reject_hold
        actor = actor_for_request(request)
        set_reject_hold(wi, actor=actor, now=now)
        await record_audit(db=db, type="pm.hold.set", identity=audit_identity, detail={
            "workItemId": wi.id, "kind": "owner_action",
            "reason": REJECT_HOLD_REASON, "actorClass": actor_class(actor)})
    prop.status = "rejected"
    prop.resolved_at = now
    await db.flush()
    await record_audit(db=db, type="pm.triage.reject", identity=audit_identity,
                       detail={"proposalId": prop.id})


@router.post("/triage/{proposal_id}/undo")
async def undo_triage(
    proposal_id: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Put an accepted or rejected proposal back to pending and restore the work
    item's overlay from the snapshot accept/reject took (roadmap spec §10, §12).
    Only within TRIAGE_UNDO_WINDOW_S of resolution; siblings re-proposed by the
    drift latch are NOT restored (their no_fit_suggestion is gone)."""
    prop = (await db.execute(select(TriageProposal).where(
        TriageProposal.id == proposal_id))).scalar_one_or_none()
    if prop is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    if prop.status == "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "not_resolved", "message": "proposal is pending; nothing to undo"})
    # Snapshot BEFORE window: a decision taken before this column existed has a
    # `resolved_at` older than the window too, so checking the window first would
    # answer `undo_expired` for every such row and `no_snapshot` for none. The
    # missing snapshot is the more specific reason, so it is the one reported.
    snap = prop.undo_snapshot
    if not snap:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "no_snapshot",
            "message": "nothing to restore: this decision was taken without an undo snapshot"})
    resolved = _aware(prop.resolved_at)
    now = datetime.now(timezone.utc)
    if resolved is None or (now - resolved).total_seconds() > TRIAGE_UNDO_WINDOW_S:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "undo_expired",
            "message": f"undo is only available for {TRIAGE_UNDO_WINDOW_S}s after a decision"})
    wi = (await db.execute(select(WorkItem).where(
        WorkItem.id == prop.work_item_id))).scalar_one_or_none()
    if snap.get("decidedBy") == DECIDED_BY_AUTO and prop.status == "accepted":
        # The decision being undone was the machine's: never let it re-decide.
        #
        # Read from the SNAPSHOT (`apply_accept` stamps `decidedBy` there), not
        # from `WorkItem.classified_by`. That column is live overlay state: any
        # overlay PATCH rewrites it to "human", and the undo window is exactly
        # when a human is invited to look at the auto-accept and tweak it. Scored
        # first, then undone, the machine's authorship was gone — the flag stayed
        # NULL and the loop auto-accepted the same proposal on its next tick,
        # undoing the human who had just undone it.
        prop.auto_undone = True
    if wi is not None:
        _restore_overlay(wi, snap)
    previous = prop.status
    prop.status = "pending"
    prop.resolved_at = None
    prop.undo_snapshot = None
    prop.map_resolution = None
    await db.flush()
    await record_audit(db=db, type="pm.triage.undo", identity=identity,
                       detail={"proposalId": proposal_id, "workItemId": prop.work_item_id,
                               "previousStatus": previous, "autoUndone": prop.auto_undone})
    return {"id": prop.id, "status": "pending", "workItemId": prop.work_item_id,
            "triageState": wi.triage_state if wi is not None else None}


class BulkIdsBody(BaseModel):
    """The selection a bulk sweep (`accept-bulk`, `reject-bulk`) acts on."""
    ids: list[str]


@router.post("/triage/accept-bulk")
async def accept_bulk(
    body: BulkIdsBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Accept many proposals in one call (the keyboard inbox's bulk action).
    Every id runs the SAME ``apply_accept`` path as single-accept; per-id
    failures are reported in ``results`` rather than failing the batch, so the
    response is always HTTP 200 with a 207-style aggregate.

    Each id runs inside its OWN savepoint. Without one, any exception that is not
    an ``HTTPException`` escapes the route, ``get_db`` rolls the whole session
    back, and the ids that already succeeded are silently reverted behind a
    generic 500 — the inbox's bulk key would lose the entire selection."""
    from server.pm.triage_accept import gather_accept_ownership
    ownership = await gather_accept_ownership(body.ids)
    results: list[dict] = []
    landed: list[str] = []          # ids that actually committed, for the audit
    for pid in body.ids:
        proposal = (await db.execute(
            select(TriageProposal).where(TriageProposal.id == pid)
        )).scalar_one_or_none()
        if proposal is None:
            results.append({"id": pid, "ok": False, "error": "not_found"})
            continue
        if proposal.status != "pending":
            results.append({"id": pid, "ok": False, "error": "not_pending"})
            continue
        try:
            async with db.begin_nested():
                await apply_accept(db, proposal, decided_by=DECIDED_BY_HUMAN,
                                   audit_identity=identity, ownership=ownership)
            results.append({"id": pid, "ok": True})
            landed.append(pid)
        except OwnershipConflict as e:
            results.append({"id": pid, **e.result()})
        except WorkItemMissing:
            # The per-id twin of single-accept's 404: same ``work_item_missing``
            # code, reported as this id's failure instead of the batch's.
            results.append({"id": pid, "ok": False, "error": "work_item_missing"})
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, dict) else {"error": "failed"}
            results.append({"id": pid, "ok": False, "error": detail.get("error")})
        except Exception:
            log.warning("accept-bulk: proposal %s failed", pid, exc_info=True)
            results.append({"id": pid, "ok": False, "error": "failed"})
    await db.flush()
    await record_audit(db=db, type="pm.triage.accept_bulk", identity=identity,
                       # the ids that actually landed, NOT the requested list
                       detail={"proposalIds": landed, "requestedIds": body.ids,
                               "accepted": len(landed)})
    return {"accepted": len(landed), "results": results}


@router.post("/triage/reject-bulk")
async def reject_bulk(
    body: BulkIdsBody,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Reject many proposals in one call (the inbox's reject-selected action).
    The reject twin of ``accept_bulk``, and the same contract: every id runs
    the SAME ``apply_reject`` path as single-reject inside its OWN savepoint,
    per-id failures land in ``results``, and the response is always HTTP 200
    with a 207-style aggregate. A batch-wide failure would silently revert the
    ids that already parked — the inbox would lose the whole selection."""
    results: list[dict] = []
    landed: list[str] = []          # ids that actually committed, for the audit
    for pid in body.ids:
        proposal = (await db.execute(
            select(TriageProposal).where(TriageProposal.id == pid)
        )).scalar_one_or_none()
        if proposal is None:
            results.append({"id": pid, "ok": False, "error": "not_found"})
            continue
        if proposal.status != "pending":
            results.append({"id": pid, "ok": False, "error": "not_pending"})
            continue
        try:
            async with db.begin_nested():
                await apply_reject(db, proposal, request=request, audit_identity=identity)
            results.append({"id": pid, "ok": True})
            landed.append(pid)
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, dict) else {"error": "failed"}
            results.append({"id": pid, "ok": False, "error": detail.get("error")})
        except Exception:
            log.warning("reject-bulk: proposal %s failed", pid, exc_info=True)
            results.append({"id": pid, "ok": False, "error": "failed"})
    await db.flush()
    await record_audit(db=db, type="pm.triage.reject_bulk", identity=identity,
                       # the ids that actually landed, NOT the requested list
                       detail={"proposalIds": landed, "requestedIds": body.ids,
                               "rejected": len(landed)})
    return {"rejected": len(landed), "results": results}


def _work_item_to_dict(w: WorkItem) -> dict:
    # classified_at goes through _aware(): a WorkItem freshly re-queried after the
    # undo route's restore round-tripped through SQLite (which stores
    # DateTime(timezone=True) as a naive string, dropping tzinfo on the way IN,
    # not just on the way out — see _aware's docstring) would otherwise render an
    # offset-less ISO string here, the exact naive/aware slip the DB clock rule
    # warns about.
    at = _aware(w.classified_at)
    held_at = _aware(w.hold_set_at)
    return {
        "id": w.id, "repo": w.repo, "number": w.number, "kind": w.kind,
        "title": w.title, "state": w.state,
        "featureId": w.feature_id,
        "reach": w.reach, "impact": w.impact, "effort": w.effort,
        "confidence": w.confidence,
        "triageState": w.triage_state,
        "classifiedBy": w.classified_by,
        "classifiedAt": at.isoformat() if at else None,
        "holdKind": w.hold_kind, "holdReason": w.hold_reason, "holdBy": w.hold_by,
        "holdSetAt": held_at.isoformat() if held_at else None,
        "url": w.url,
    }


class OverlayPatchBody(BaseModel):
    featureId: str | None = None
    reach: int | None = None
    impact: int | None = None
    effort: int | None = None
    confidence: int | None = None
    triageState: str | None = None
    # Holds (spec 2026-09-18 §3/§8). ``holdKind`` absent = no change; ``clearHold``
    # is the explicit release, so "absent" and "null" cannot be confused.
    holdKind: str | None = None
    holdReason: str | None = None
    clearHold: bool = False


@router.patch("/work-items/{item_id}/overlay")
async def patch_overlay(
    item_id: str,
    body: OverlayPatchBody,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Manual classify / inline score edit / hold on the ViberPit-owned overlay.
    All fields optional. A ``clearHold`` here is a release like any other and audits
    the shared ``signal`` key as ``"overlay"`` — but unlike the Retry route it ALSO
    stamps ``classified_by``/``classified_at``, because it arrives as an overlay edit
    the person (or agent) made, not as a bare release.
    ``classified_by`` records the ACTOR (spec 2026-09-18 §8):
    a person's login, ``assistant:<login>``, or ``lan@local`` — never a blanket
    "human". A body that applies NO field is a read-through no-op — including one
    whose only would-be effect is itself a no-op (e.g. ``clearHold`` on an item
    with no hold): nothing stamps ``classified_by`` or fires an audit unless
    something on the work item actually changed.

    A ``decision`` hold is out of this route's reach in BOTH directions (spec §8):
    setting one is refused by ``set_hold`` (only a proposal may park an item on a
    question) and clearing one is refused by ``refuse_decision_release`` — the same
    guard the Retry route gets, so the overlay is not a way around it."""
    from server.pm.holds import (HoldPermissionError, actor_class, actor_for_request,
                                 clear_hold, refuse_decision_release, set_hold)
    if body.triageState is not None and body.triageState not in WORK_ITEM_TRIAGE_STATES:
        raise HTTPException(
            status_code=422,
            detail={"error": "bad_triage_state",
                    "message": f"triageState must be one of {WORK_ITEM_TRIAGE_STATES}"})
    if body.holdKind is not None and body.clearHold:
        raise HTTPException(status_code=422, detail={
            "error": "bad_hold", "message": "holdKind and clearHold are exclusive"})
    if body.holdReason is not None and body.holdKind is None:
        raise HTTPException(status_code=422, detail={
            "error": "bad_hold", "message": "holdReason requires holdKind"})
    if body.holdKind is not None and not (body.holdReason or "").strip():
        raise HTTPException(status_code=422, detail={
            "error": "hold_reason_required", "message": "holdReason is required with holdKind"})
    # Range bounds live in server/pm/score.py next to the formula they protect —
    # an out-of-range value reaches the GitHub Projects Score field and inverts
    # roadmap ordering. Do NOT inline a second copy of the bounds here.
    for attr in RICE_FIELDS:
        msg = validate_rice(attr, getattr(body, attr))
        if msg:
            raise HTTPException(status_code=422, detail={
                "error": "bad_rice_value", "message": msg})
    wi = (await db.execute(
        select(WorkItem).where(WorkItem.id == item_id))).scalar_one_or_none()
    if wi is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    provided = [k for k, v in body.model_dump(exclude_none=True).items() if v is not False]
    if not provided:
        return _work_item_to_dict(wi)
    actor = actor_for_request(request)
    now = datetime.now(timezone.utc)
    # ``applied`` tracks fields that ACTUALLY mutated `wi` — narrower than
    # ``provided`` (the raw body), because a hold field can be a no-op (e.g.
    # ``clearHold`` on an item with no hold to release). Only a real mutation
    # stamps classified_by/at and fires the overlay audit.
    applied: list[str] = []
    if body.featureId is not None:
        feat = (await db.execute(
            select(Feature).where(Feature.id == body.featureId))).scalar_one_or_none()
        if feat is None:
            raise HTTPException(status_code=404,
                                detail={"error": "feature_not_found"})
        wi.feature_id = body.featureId
        applied.append("featureId")
    for attr in RICE_FIELDS:
        val = getattr(body, attr)
        if val is not None:
            setattr(wi, attr, val)
            applied.append(attr)
    if body.triageState is not None:
        wi.triage_state = body.triageState
        applied.append("triageState")
    try:
        if body.holdKind is not None:
            set_hold(wi, kind=body.holdKind, reason=body.holdReason or "", actor=actor, now=now)
            applied.append("holdKind")
            await record_audit(db=db, type="pm.hold.set", identity=identity, detail={
                "workItemId": item_id, "kind": body.holdKind, "reason": wi.hold_reason,
                "actorClass": actor_class(actor)})
        elif body.clearHold:
            # THE same refusal the Retry route gets (spec §8): a decision hold ends
            # when a person picks an option, never on an overlay edit. Without this
            # the overlay was a second, ungated release path around
            # ``release_via_api`` — ``clear_hold`` itself has no decision rule.
            refuse_decision_release(wi)
            released = wi.hold_kind
            clear_hold(wi, actor=actor)
            if released is not None:
                applied.append("clearHold")
                # The same ``signal`` key every other path that ends a hold writes
                # (the reconcile pass, the Retry route, the session tool), from the
                # one ``RELEASE_SIGNALS`` tuple in ``server/pm/holds.py``.
                await record_audit(db=db, type="pm.hold.release", identity=identity, detail={
                    "workItemId": item_id, "kind": released, "actorClass": actor_class(actor),
                    "signal": "overlay"}, via="api")
    except HoldPermissionError as e:
        raise HTTPException(status_code=422, detail={
            "error": "hold_not_permitted", "message": str(e)}) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"error": "bad_hold", "message": str(e)}) from e
    if not applied:
        return _work_item_to_dict(wi)
    wi.classified_by = actor
    wi.classified_at = now
    await db.flush()
    await record_audit(db=db, type="pm.workitem.overlay", identity=identity,
                       detail={"workItemId": item_id, "fields": applied,
                               "actorClass": actor_class(actor)})
    return _work_item_to_dict(wi)


@router.get("/work-items/{item_id}/outcomes")
async def get_work_item_outcomes(
    item_id: str,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Every Backlog Runner dispatch this issue has had, newest first (spec §6/§7)
    — the history behind the roadmap's ``needs_spec`` bounce chip."""
    wi = (await db.execute(
        select(WorkItem).where(WorkItem.id == item_id))).scalar_one_or_none()
    if wi is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    target_ref = f"{wi.repo}#{wi.number}"
    rows = (await db.execute(list_outcomes_stmt(target_ref))).scalars().all()
    return {"items": [outcome_to_dict(r) for r in rows]}


_REF_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+$")


@router.delete("/work-items/{item_id}/deps")
async def delete_work_item_dep(
    item_id: str,
    request: Request,
    blocker: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Remove the blocked-by edge ``item ← blocker`` on GitHub (roadmap spec §8,
    §10; #1472) — the disposition for a `blocked` leaf. ``blocker`` is the
    canonical ``owner/repo#N`` ref the leaf's ``blockedBy`` carries; a query
    parameter because a ref has a slash and a hash in it.

    The GitHub write belongs to the orchestrator seam (its gh ladder, its
    journal); this route decides that a human asked and records that in the
    audit trail whether or not GitHub agreed. A refusal is a 502 carrying the
    seam's ``step`` so the web can say what to do (an unmirrored side means
    "wait for the sync", a GitHub error means "look at the issue")."""
    if not _REF_RE.match(blocker):
        raise HTTPException(status_code=422, detail={
            "error": "bad_blocker", "message": "blocker must be owner/repo#N"})
    wi = (await db.execute(select(WorkItem).where(WorkItem.id == item_id))).scalar_one_or_none()
    if wi is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    src_ref = f"{wi.repo}#{wi.number}"
    dep = (await db.execute(select(WorkItemDep.id).where(
        WorkItemDep.src_ref == src_ref, WorkItemDep.dst_ref == blocker,
        WorkItemDep.active.is_(True)))).scalars().first()
    if dep is None:
        raise HTTPException(status_code=404, detail={
            "error": "dep_not_found", "message": f"{src_ref} is not blocked by {blocker}"})
    result = await request.app.state.sessions.pm_remove_dependency(item_id, blocker)
    await record_audit(db=db, type="pm.workitem.dep_remove", identity=identity,
                       detail={"workItemId": item_id, "blocker": blocker,
                               "ok": bool(result.get("ok")), "step": result.get("step")})
    if not result.get("ok"):
        await db.commit()  # the failed attempt is exactly what the trail must keep
        raise HTTPException(status_code=409 if result.get("step") == "ownership" else 502, detail={
            "error": "dep_remove_failed", "step": result.get("step"),
            "message": str(result.get("error") or "dependency removal failed")})
    return result


@router.post("/work-items/{item_id}/hold/release")
async def release_work_item_hold(
    item_id: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """The Retry signal (spec 2026-09-18 §7): a person releases an owner_action
    hold so the item re-enters the queue at the product default. The other two
    signals (board edit, body edit) run in the reconcile pass through the same
    rule module. Idempotent on an unheld item.

    A ``decision`` hold is refused (422): spec §8 resolves one through its proposal,
    and Retry would drop the question on the floor and re-queue the item with the
    decision still unmade. Unlike the overlay's ``clearHold`` this records only the
    release — no ``classified_by`` stamp, because nothing on the overlay was edited."""
    from server.pm.holds import (
        DecisionHoldError,
        HoldPermissionError,
        actor_for_request,
        release_via_api,
    )
    wi = (await db.execute(
        select(WorkItem).where(WorkItem.id == item_id))).scalar_one_or_none()
    if wi is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    try:
        released = await release_via_api(db, wi, actor=actor_for_request(request),
                                         identity=identity, signal="retry")
    except DecisionHoldError as e:
        raise HTTPException(status_code=422, detail={
            "error": "bad_hold", "message": str(e)}) from e
    except HoldPermissionError as e:
        raise HTTPException(status_code=422, detail={
            "error": "hold_not_permitted", "message": str(e)}) from e
    return {"ok": True, "released": released, "item": _work_item_to_dict(wi)}


# --------------------------------------------------------------------------- #
# Decision proposals (spec 2026-09-18 §6)                                      #
# --------------------------------------------------------------------------- #
#: How long the accept route waits for the issue comment before giving up on it.
#: ``gh issue comment`` is the FIRST call of that seam inside a request handler
#: (``announce_claim`` / ``announce_needs_human`` run in the background runner) and
#: ``gitrepos._run_gh`` bounds nothing, so a stalled token mint would otherwise hold
#: the request and a threadpool worker open indefinitely — for work that is purely
#: decorative and happens after the decision is already committed.
DECISION_COMMENT_TIMEOUT_S = 15.0
# Thin wrappers over server/pm/decisions.py, which is THE writer, accepter and
# rejecter (constitution VI): the `propose_decision` session tool and the
# assistant's `resolve_decision` reach the same functions, so no validation, hold
# write or audit shape may be re-spelled in a route here.

def _decision_to_dict(prop: DecisionProposal, wi: WorkItem | None,
                      project_slug: str | None, guidance: dict | None = None) -> dict:
    """The wire shape of one decision proposal.

    Keys mirror ``_triage_to_dict`` wherever the two carry the same fact
    (``workItemId``, ``repo``, ``number``, ``title``, ``url``, ``projectSlug``,
    ``rationale``, ``status``, ``createdAt``, ``guidance``): spec §6 lists decision
    proposals in the SAME inbox as classification proposals, "same ordering spec
    and keys", and a second name for the owning product would fork the inbox's
    grouping. ``ref`` is the ``owner/repo#N`` the proposal was made against,
    carried so a card can link out without re-joining repo and number.
    """
    return {
        "id": prop.id,
        "workItemId": prop.work_item_id,
        "repo": wi.repo if wi else None,
        "number": wi.number if wi else None,
        "ref": f"{wi.repo}#{wi.number}" if wi and wi.repo and wi.number else None,
        "projectSlug": project_slug,
        "title": wi.title if wi else None,
        "url": wi.url if wi else None,
        "options": prop.options or [],
        "recommendedKey": prop.recommended_key,
        "rationale": prop.rationale,
        "chosenKey": prop.chosen_key,
        "status": prop.status,
        "proposedBy": prop.proposed_by,
        "resolvedBy": prop.resolved_by,
        "createdAt": prop.created_at.isoformat() if prop.created_at else None,
        "resolvedAt": prop.resolved_at.isoformat() if prop.resolved_at else None,
        # What Accept / Reject do under the owning product's dispatch policy; the
        # list route computes it, the single-item routes answer None (the caller
        # has just acted — there is nothing left to explain).
        "guidance": guidance,
    }


async def _owning_slug(db, wi: WorkItem | None) -> str | None:
    """The slug of the product that owns *wi*'s repo, through THE repo→project
    helper — never inferred client-side or from the repo name."""
    if wi is None or not wi.repo:
        return None
    owner = (await owning_projects_by_repo(db, {wi.repo})).get(wi.repo)
    return owner.slug if owner else None


async def _load_decision(db, proposal_id: str) -> DecisionProposal:
    """The proposal or a 404, re-read under a row lock.

    Whether it is still PENDING is not asked here: the service module owns that
    rule and raises ``DecisionConflict``, which every route below maps to 409.

    ``with_for_update()`` is what makes that check mean anything, exactly as in the
    classification twin (``pending_proposal_stmt``, server/pm/triage_accept.py):
    reading ``status == "pending"`` and then writing is a read-then-write race, and
    under Postgres READ COMMITTED two in-flight resolutions both read "pending" and
    both pass ``decisions._require_pending``. Two accepts would then write two
    ``pm.decision.accept`` rows and post two issue comments naming different
    options, with the later ``chosen_key`` silently winning; an accept interleaved
    with a reject would leave the proposal ``accepted`` while the reject re-parks
    the item ``owner_action`` — a decision taken and an issue that never moves. The
    lock makes the loser re-read the row as resolved and answer 409."""
    prop = (await db.execute(select(DecisionProposal).where(
        DecisionProposal.id == proposal_id).with_for_update())).scalar_one_or_none()
    if prop is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    return prop


def _require_may_resolve(actor: str) -> None:
    """403 unless *actor* may take a decision. The predicate itself lives in
    ``decisions.may_resolve`` (ONE definition, shared with the assistant tool);
    this is only its HTTP face, called by accept AND reject.

    Checked BEFORE the proposal is loaded so an agent cannot use the 404/200 split
    to probe which proposal ids exist."""
    from server.pm.decisions import RESOLVE_DENIED_MESSAGE, may_resolve
    if not may_resolve(actor):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={
            "error": "decision_not_permitted", "message": RESOLVE_DENIED_MESSAGE})


def _decision_http_error(exc: Exception) -> HTTPException:
    """THE mapping of a ``DecisionError`` onto a status code, so the four routes
    cannot disagree: a state conflict (already pending, already resolved, a
    person's park) is 409 — nothing about the request needs fixing — and a
    malformed proposal is 422."""
    from server.pm.decisions import DecisionConflict
    if isinstance(exc, DecisionConflict):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "decision_conflict", "message": str(exc)})
    return HTTPException(status_code=422, detail={
        "error": "bad_decision", "message": str(exc)})


class DecisionOptionBody(BaseModel):
    """One proposed option as it arrives on the wire. Typed at the boundary so a
    wrongly-shaped ``options`` (a number, a list of strings — exactly what a
    model-driven ``propose_decision`` call gets wrong) is FastAPI's 422 and never
    reaches ``normalize_options``, whose ``for raw in options`` would raise
    ``TypeError`` out of the route as a 500. The SEMANTIC rules (2–6 options,
    unique non-empty keys, a recommendation among them) stay in the service —
    this only pins the types."""

    key: str
    label: str
    detail: str | None = None


class DecisionProposalBody(BaseModel):
    options: list[DecisionOptionBody] = []
    recommended_key: str = ""
    rationale: str = ""


class DecisionAcceptBody(BaseModel):
    chosen_key: str = ""


@router.post("/work-items/{item_id}/decisions", status_code=status.HTTP_201_CREATED)
async def propose_work_item_decision(
    item_id: str,
    request: Request,
    body: DecisionProposalBody,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Propose a decision on this item (spec §6): two to six options, the one the
    proposer recommends, and why — recorded together with the item's ``decision``
    hold in ONE transaction by ``server/pm/decisions.py:propose``.

    Every actor class may call it (a person, a coding session's
    ``propose_decision`` tool as ``session:<id>``, the assistant, an
    unattributed loopback caller), because a ``decision`` hold is the one kind an
    agent may set AND only THROUGH THIS ROUTE (spec §8): the proposal is the
    authority for the park, and accepting it is the only thing that lifts it.
    """
    from sqlalchemy.exc import IntegrityError

    from server.pm import decisions
    from server.pm.holds import actor_for_request
    wi = (await db.execute(select(WorkItem).where(
        WorkItem.id == item_id))).scalar_one_or_none()
    if wi is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    try:
        prop = await decisions.propose(
            db, wi,
            options=[o.model_dump() for o in body.options],
            recommended_key=body.recommended_key,
            rationale=body.rationale,
            actor=actor_for_request(request),
            now=datetime.now(timezone.utc))
    except decisions.DecisionError as e:
        raise _decision_http_error(e) from e
    except IntegrityError as e:
        # ``ux_decision_proposals_pending`` is what actually enforces "one pending
        # proposal per item" — the SELECT above it is only the friendly path, and a
        # genuinely concurrent second proposer passes it and fails at the flush.
        # Same outcome, same message: a race is a conflict, not a server fault.
        raise _decision_http_error(
            decisions.DecisionConflict(decisions.PENDING_CONFLICT_MESSAGE)) from e
    return _decision_to_dict(prop, wi, await _owning_slug(db, wi))


@router.get("/decisions")
async def list_decisions(
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> list[dict]:
    """Pending decision proposals, newest first — the inbox's second card kind.

    Pending ONLY, with no ``pendingOnly`` escape hatch: the widened leg on
    ``/triage`` exists to keep a fresh auto-accept visible for its undo window,
    and a decision has no such window (spec decision 3 — the machine drafts, the
    person decides, and once taken it is taken).
    """
    from server.pm import decisions
    from server.pm.explain import decision_guidance
    rows = await decisions.pending_list(db)
    if not rows:
        return []
    repos = {w.repo for _p, w in rows if w is not None and w.repo}
    slug_by_repo: dict[str, str] = {
        repo: owner.slug for repo, owner
        in (await owning_projects_by_repo(db, repos)).items()} if repos else {}
    policies = await load_policies(db)
    out = []
    for prop, wi in rows:
        slug = slug_by_repo.get(wi.repo) if wi is not None and wi.repo else None
        policy = (policies.get(slug) if slug else None) or effective_policy(None)
        out.append(_decision_to_dict(
            prop, wi, slug, guidance=decision_guidance(policy=policy)))
    return out


@router.post("/decisions/{proposal_id}/accept")
async def accept_decision(
    proposal_id: str,
    request: Request,
    body: DecisionAcceptBody = Body(default_factory=DecisionAcceptBody),
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Take the decision (spec §6): record ``chosen_key`` and clear the item's
    ``decision`` hold — the ONE path that may — then say so on the issue.

    A person's call, or the assistant's on their behalf (``decisions.may_resolve``);
    anything else is 403. The proposer must not be able to answer its own question.

    The comment is posted AFTER the commit and can never fail the request. The
    accepted ROW is what the next implement run quotes
    (``decisions.accepted_text_for_refs``); the comment is only the GitHub-side
    record of it, and announcing before the commit would risk publishing a
    decision a rollback then erased. It runs on a worker thread because it shells
    out to ``gh``.
    """
    import asyncio

    from server.automations import backlog_github
    from server.pm import decisions
    from server.pm.holds import actor_for_request
    actor = actor_for_request(request)
    _require_may_resolve(actor)
    prop = await _load_decision(db, proposal_id)
    try:
        await decisions.accept(db, prop, chosen_key=body.chosen_key,
                               actor=actor, now=datetime.now(timezone.utc))
    except decisions.DecisionError as e:
        raise _decision_http_error(e) from e
    wi = (await db.execute(select(WorkItem).where(
        WorkItem.id == prop.work_item_id))).scalar_one_or_none()
    # Read everything the response and the comment need BEFORE committing: an
    # async session expires its instances on commit, and touching one afterwards
    # would be lazy IO in a place that cannot await it.
    out = _decision_to_dict(prop, wi, await _owning_slug(db, wi))
    chosen = decisions.chosen_option(prop) or {}
    ref = out["ref"]
    await db.commit()
    if ref:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    backlog_github.announce_decision, ref,
                    label=str(chosen.get("label") or out["chosenKey"] or ""),
                    detail=chosen.get("detail"), chosen_by=actor),
                timeout=DECISION_COMMENT_TIMEOUT_S)
        except Exception:
            # Including the timeout (``asyncio.TimeoutError`` IS the builtin
            # ``TimeoutError``). ``CancelledError`` is a ``BaseException`` and is
            # deliberately NOT caught: a shutdown must still unwind through here.
            log.warning("pm.decision.accept_comment_failed proposal=%s ref=%s",
                        proposal_id, ref, exc_info=True)
    return out


@router.post("/decisions/{proposal_id}/reject")
async def reject_decision(
    proposal_id: str,
    request: Request,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Refuse the options (spec §6): the proposal is rejected and the item becomes
    the caller's ``owner_action`` park with ``DECISION_REJECT_REASON``. Nothing
    re-proposes on its own — Retry and Implement now are the ways back out, and
    they are a person's to press.

    Gated by ``decisions.may_resolve`` like accept: refusing the options is taking
    the decision as much as choosing one of them is."""
    from server.pm import decisions
    from server.pm.holds import actor_for_request
    actor = actor_for_request(request)
    _require_may_resolve(actor)
    prop = await _load_decision(db, proposal_id)
    try:
        await decisions.reject(db, prop, actor=actor,
                               now=datetime.now(timezone.utc))
    except decisions.DecisionError as e:
        raise _decision_http_error(e) from e
    wi = (await db.execute(select(WorkItem).where(
        WorkItem.id == prop.work_item_id))).scalar_one_or_none()
    return _decision_to_dict(prop, wi, await _owning_slug(db, wi))


@router.get("/work-items/{item_id}/issue")
async def get_work_item_issue(item_id: str, _identity: str = Depends(current_identity),
                              db=Depends(get_db)) -> dict:
    from server.pm.explain import hold_guidance
    from server.pm.issue_disposition import detail
    wi = await db.get(WorkItem, item_id)
    if wi is None or wi.kind != "issue":
        raise HTTPException(status_code=404, detail={"error": "issue_not_found"})
    # `hold_guidance` is what decides which hold kinds carry copy (owner_action and
    # evidence today) — this endpoint never spells that list, it just supplies a
    # policy and lets the function answer None for anything it doesn't know. The
    # owning-product lookup and the policy load are the expensive half of that, so
    # they run only for owner_action, whose copy actually varies with the product's
    # dispatch policy; evidence's copy is policy-independent (see hold_guidance's
    # docstring), so it gets the cheap `effective_policy(None)` default rather than
    # paying for a lookup its answer can never use.
    guidance = None
    if wi.hold_kind is not None:
        policy = effective_policy(None)
        if wi.hold_kind == "owner_action" and wi.repo:
            slug_by_repo = {repo: owner.slug for repo, owner
                            in (await owning_projects_by_repo(db, {wi.repo})).items()}
            slug = slug_by_repo.get(wi.repo)
            if slug:
                policies = await load_policies(db)
                policy = policies.get(slug) or policy
        guidance = hold_guidance(hold_kind=wi.hold_kind, policy=policy)
    return {**detail(wi), "guidance": guidance}


@router.post("/work-items/{item_id}/issue")
async def decide_work_item_issue(item_id: str, body: IssueDecision, request: Request,
                                 identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    wi = await db.get(WorkItem, item_id)
    if wi is None or wi.kind != "issue":
        raise HTTPException(status_code=404, detail={"error": "issue_not_found"})
    result = await request.app.state.sessions.pm_issue_decision(item_id, body.model_dump())
    await record_audit(db=db, type="pm.workitem.issue_decision", identity=identity,
                       detail={"workItemId": item_id, "action": body.action,
                               "ok": bool(result.get("ok")), "step": result.get("step")})
    if not result.get("ok"):
        await db.commit()
        code = 409 if result.get("step") in {"conflict", "ownership"} else 502
        if result.get("step") == "load":
            code = 404
        raise HTTPException(status_code=code, detail={"error": "issue_decision_failed",
            "step": result.get("step"), "message": result.get("error") or "Issue decision failed"})
    return result
