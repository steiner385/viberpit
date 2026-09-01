"""PM product-layer API — project registry + sync-now + edges (phase 1).

Design: specs/001-ccswitch-web/spec.md §7.
Mirrors server/api/sdlc.py conventions: camelCase JSON, audit on mutation,
slug identity, 409 on conflicts.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select

from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import (
    PROJECT_EDGE_KINDS,
    Feature,
    Project,
    ProjectEdge,
    ProjectRepo,
    WorkItem,
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
