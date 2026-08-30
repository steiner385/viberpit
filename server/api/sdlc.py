"""SDLC initiative API (#802) + build-stage correlation (#801)."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import Initiative, InitiativeArtifact, Session as DbSession
from server.sdlc.engine import (
    ArtifactView,
    AwaitApproval,
    Done,
    HandoffToBuild,
    InitiativeView,
    NeedsHuman,
    Propose,
    advance_stage,
    next_action,
)

router = APIRouter(prefix="/api/sdlc", tags=["sdlc"])


def _slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s or "initiative"


def _initiative_to_dict(i: Initiative) -> dict:
    return {
        "id": i.id,
        "slug": i.slug,
        "repo": i.repo,
        "title": i.title,
        "stage": i.stage,
        "status": i.status,
        "origin": i.origin,
        "originRef": i.origin_ref,
        "createdAt": i.created_at.isoformat() if i.created_at else None,
        "updatedAt": i.updated_at.isoformat() if i.updated_at else None,
    }


def _artifact_to_dict(a: InitiativeArtifact) -> dict:
    return {
        "id": a.id,
        "kind": a.kind,
        "repoPath": a.repo_path,
        "commitSha": a.commit_sha,
        "prNumber": a.pr_number,
        "prUrl": a.pr_url,
        "sessionId": a.session_id,
        "model": a.model,
        "createdAt": a.created_at.isoformat() if a.created_at else None,
    }


def _action_to_dict(action: object) -> dict:
    if isinstance(action, Propose):
        return {"type": "propose", "kind": action.kind}
    if isinstance(action, AwaitApproval):
        return {"type": "await_approval", "kind": action.kind}
    if isinstance(action, HandoffToBuild):
        return {"type": "handoff_to_build", "issue": action.issue}
    if isinstance(action, Done):
        return {"type": "done"}
    if isinstance(action, NeedsHuman):
        return {"type": "needs_human", "reason": action.reason}
    return {"type": "unknown"}  # pragma: no cover — union is closed


class InitiativeCreateBody(BaseModel):
    title: str
    repo: str
    slug: str | None = None
    origin: str | None = None
    originRef: str | None = None


@router.get("/initiatives")
async def list_initiatives(
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> list[dict]:
    """Return all initiatives ordered by created_at."""
    rows = (
        await db.execute(select(Initiative).order_by(Initiative.created_at))
    ).scalars().all()
    return [_initiative_to_dict(i) for i in rows]


@router.post("/initiatives", status_code=status.HTTP_201_CREATED)
async def create_initiative(
    body: InitiativeCreateBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Create an initiative. Returns 409 if slug already exists."""
    slug = body.slug or _slugify(body.title)
    existing = (
        await db.execute(select(Initiative).where(Initiative.slug == slug))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "slug_conflict", "message": f"Initiative '{slug}' already exists"},
        )
    ini = Initiative(
        slug=slug,
        repo=body.repo,
        title=body.title,
        origin=body.origin,
        origin_ref=body.originRef,
    )
    db.add(ini)
    await db.flush()
    await record_audit(
        db=db,
        type="sdlc.initiative.create",
        identity=identity,
        detail={"slug": slug, "title": body.title},
    )
    return _initiative_to_dict(ini)


@router.get("/initiatives/{slug}")
async def get_initiative(
    slug: str,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Return initiative detail including artifacts, buildSessions, and nextAction."""
    ini = (
        await db.execute(select(Initiative).where(Initiative.slug == slug))
    ).scalar_one_or_none()
    if ini is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    artifacts = (
        await db.execute(
            select(InitiativeArtifact)
            .where(InitiativeArtifact.initiative_id == ini.id)
            .order_by(InitiativeArtifact.created_at)
        )
    ).scalars().all()

    # Build nextAction via the pure engine (display-only, mode="propose")
    state = InitiativeView(
        slug=ini.slug or "",
        stage=ini.stage,
        status=ini.status,
        repo=ini.repo or "",
        issue=ini.origin_ref,
    )
    artifact_views = [
        ArtifactView(
            kind=a.kind or "",
            commit_sha=a.commit_sha,
            pr_number=a.pr_number,
            pr_url=a.pr_url,
        )
        for a in artifacts
    ]
    action = next_action(state, artifact_views, "propose", {})

    # Build-stage correlation (#801): find backlog-runner sessions with same origin_ref
    build_sessions: list[dict] = []
    if ini.origin_ref:
        session_rows = (
            await db.execute(
                select(DbSession).where(
                    DbSession.origin == "backlog-runner",
                    DbSession.origin_ref == ini.origin_ref,
                )
            )
        ).scalars().all()
        build_sessions = [
            {"id": s.id, "status": s.status, "branch": s.branch}
            for s in session_rows
        ]

    result = _initiative_to_dict(ini)
    result["artifacts"] = [_artifact_to_dict(a) for a in artifacts]
    result["buildSessions"] = build_sessions
    result["nextAction"] = _action_to_dict(action)
    return result


@router.post("/initiatives/{slug}/advance")
async def advance_initiative(
    slug: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Advance the initiative to the next stage. Returns 409 when already done."""
    ini = (
        await db.execute(select(Initiative).where(Initiative.slug == slug))
    ).scalar_one_or_none()
    if ini is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    if ini.stage == "done":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "already_done", "message": "Initiative is already done"},
        )
    ini.stage = advance_stage(ini.stage)
    ini.status = "active"
    await db.flush()
    await record_audit(
        db=db,
        type="sdlc.initiative.advance",
        identity=identity,
        detail={"slug": slug, "stage": ini.stage},
    )
    return _initiative_to_dict(ini)


@router.get("/triage")
async def triage(
    _identity: str = Depends(current_identity),
) -> dict:
    # Phase 2 (#804) stub
    return {"items": []}
