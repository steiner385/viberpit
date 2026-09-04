"""SDLC initiative API (#802) + build-stage correlation (#801)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import GhProjectItem, Initiative, InitiativeArtifact, Session as DbSession, WorkItem
from server.pm.desired_state import STATUS_BLOCKED, STATUS_NEEDS_HUMAN
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
from server.sdlc.slug import slugify

router = APIRouter(prefix="/api/sdlc", tags=["sdlc"])


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
        "epicRepo": i.epic_repo,
        "epicNumber": i.epic_number,
        "createdAt": i.created_at.isoformat() if i.created_at else None,
        "updatedAt": i.updated_at.isoformat() if i.updated_at else None,
    }


async def _epic_progress_batch(
    db, initiatives: list[Initiative], *, include_children: bool = True,
) -> dict[str, dict | None]:
    """Sub-issue progress for every initiative that points at an epic (spec 003
    §7c), computed in a fixed small number of grouped queries — not one call per
    initiative — so `GET /initiatives` can carry it on every row without an N+1
    (the same idiom `list_initiatives` already uses to batch artifacts). ccswitch
    deep-links to the GitHub epic rather than rebuilding a board, so ``url``
    matters as much as the counts.

    A child WorkItem is counted by ``WorkItem.id`` regardless of how many
    ``GhProjectItem`` rows reference it — nothing constrains a work item to a
    single project board, so an outer join alone can silently double-count a
    child mirrored onto more than one board. When more than one status row
    exists for a child, the most recently updated one wins.

    ``include_children`` (default ``True``) controls whether the per-child
    array is serialized into each entry. `list_initiatives` (the LIST
    endpoint) passes ``False``: `InitiativeTrack` only ever reads
    `url`/`done`/`total`/`blocked`/`needsHuman`, but that endpoint's query is
    re-run on every websocket status frame (`InitiativesTab` invalidates it),
    so an unread one-object-per-sub-issue array would be re-serialized on
    every fleet status change for no reader. `get_initiative` (the per-slug
    detail endpoint) keeps the default — the plan's Task 10 shape has
    `children` there. One helper either way, so the two endpoints can't drift
    apart on the counts themselves (constitution VI)."""
    result: dict[str, dict | None] = {i.id: None for i in initiatives}
    pointers = [(i.id, i.epic_repo, i.epic_number) for i in initiatives
                if i.epic_repo and i.epic_number]
    if not pointers:
        return result

    repos = {r for _, r, _ in pointers}
    numbers = {n for _, _, n in pointers}
    epic_rows = (
        await db.execute(
            select(WorkItem).where(WorkItem.repo.in_(repos), WorkItem.number.in_(numbers))
        )
    ).scalars().all()
    epic_by_pointer = {(w.repo, w.number): w for w in epic_rows}

    epic_ids = [w.id for w in epic_by_pointer.values()]
    children_by_parent: dict[str, list[WorkItem]] = {}
    status_by_child: dict[str, str | None] = {}
    if epic_ids:
        children = (
            await db.execute(
                select(WorkItem).where(WorkItem.parent_work_item_id.in_(epic_ids))
            )
        ).scalars().all()
        for c in children:
            children_by_parent.setdefault(c.parent_work_item_id, []).append(c)
        child_ids = [c.id for c in children]
        if child_ids:
            status_rows = (
                await db.execute(
                    select(GhProjectItem.work_item_id, GhProjectItem.status)
                    .where(GhProjectItem.work_item_id.in_(child_ids))
                    .order_by(GhProjectItem.updated_at)
                )
            ).all()
            for wid, st in status_rows:
                status_by_child[wid] = st

    for init_id, repo, number in pointers:
        epic_wi = epic_by_pointer.get((repo, number))
        children = children_by_parent.get(epic_wi.id, []) if epic_wi is not None else []
        entry = {
            "ref": f"{repo}#{number}",
            "url": f"https://github.com/{repo}/issues/{number}",
            "total": len(children),
            "done": sum(1 for c in children if c.state == "closed"),
            "blocked": sum(1 for c in children if status_by_child.get(c.id) == STATUS_BLOCKED),
            "needsHuman": sum(1 for c in children if status_by_child.get(c.id) == STATUS_NEEDS_HUMAN),
        }
        if include_children:
            entry["children"] = [
                {"ref": f"{c.repo}#{c.number}", "title": c.title, "state": c.state,
                 "status": status_by_child.get(c.id)}
                for c in children
            ]
        result[init_id] = entry
    return result


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
    """Return all initiatives ordered by created_at, each with its committed
    artifact chain and (for those adopted from a GitHub epic) sub-issue
    progress. Both are fetched in a fixed number of grouped queries so the web
    board renders N tracks (#803) from a single call rather than a detail fetch
    per row (the detail endpoint additionally computes nextAction + build
    sessions, which the board doesn't need per row) — this is the only place
    the web board reads from, so epic progress has to live here, not only on
    the per-slug detail endpoint (#991)."""
    rows = (
        await db.execute(select(Initiative).order_by(Initiative.created_at))
    ).scalars().all()
    if not rows:
        return []
    ids = [i.id for i in rows]
    artifacts = (
        await db.execute(
            select(InitiativeArtifact)
            .where(InitiativeArtifact.initiative_id.in_(ids))
            .order_by(InitiativeArtifact.created_at)
        )
    ).scalars().all()
    by_init: dict[str, list[InitiativeArtifact]] = {}
    for a in artifacts:
        by_init.setdefault(a.initiative_id, []).append(a)
    epics = await _epic_progress_batch(db, rows, include_children=False)
    result = []
    for i in rows:
        d = _initiative_to_dict(i)
        d["artifacts"] = [_artifact_to_dict(a) for a in by_init.get(i.id, [])]
        d["epic"] = epics.get(i.id)
        result.append(d)
    return result


@router.post("/initiatives", status_code=status.HTTP_201_CREATED)
async def create_initiative(
    body: InitiativeCreateBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Create an initiative. Returns 409 if slug already exists."""
    slug = body.slug or slugify(body.title)
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
    result["epic"] = (await _epic_progress_batch(db, [ini]))[ini.id]
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
