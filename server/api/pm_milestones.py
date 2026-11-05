"""Release-milestone endpoints (spec 2026-11-05 §10): the view model, the on-demand
refresh request, and (Phase 2) the thin write routes over
``server/pm/milestone_actions.py``, which holds every write rule. Each write runs one
transaction, commits once, and answers with the same full view as GET.

The refresh route never runs a refresh itself (the edge never runs the loop): it
stamps ``milestone_ladders.refresh_requested_at``, a watermark the daemon's
Milestones Refresh loop picks up within one tick, like
``server/pm/sync_request.py:request_project_sync``.
"""
from __future__ import annotations

import math
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from server.api.pm import _load_project
from server.audit import record_audit
from server.auth.middleware import current_identity
from server.automations.milestones_refresh import automatic_refresh_enabled
from server.db import get_db
from server.db.models import (
    Milestone,
    MilestoneLadder,
    MilestoneMeasurement,
    MilestoneNote,
    ProjectRepo,
    _utcnow,
)
from server.pm import milestone_actions as actions
from server.pm.milestone_refresh import REFRESH_MIN_INTERVAL_S, newest_fetched_at
from server.pm.milestone_view import build_milestones_view
from server.time_iso import aware_utc

router = APIRouter(prefix="/api/pm", tags=["pm"])


async def _ladder(db, project_id: str) -> MilestoneLadder | None:
    return (await db.execute(select(MilestoneLadder).where(
        MilestoneLadder.project_id == project_id))).scalar_one_or_none()


async def _view(db, p, ladder: MilestoneLadder) -> dict:
    milestones = (await db.execute(select(Milestone).where(
        Milestone.ladder_id == ladder.id))).scalars().all()
    measurements = (await db.execute(select(MilestoneMeasurement).where(
        MilestoneMeasurement.ladder_id == ladder.id))).scalars().all()
    notes = (await db.execute(select(MilestoneNote).where(
        MilestoneNote.ladder_id == ladder.id))).scalars().all()
    primary = (await db.execute(select(ProjectRepo.repo).where(
        ProjectRepo.project_id == p.id, ProjectRepo.is_primary.is_(True)))).scalars().first()
    return build_milestones_view(ladder, milestones, measurements, notes=notes, now=_utcnow(),
                                 primary_repo=primary,
                                 auto_refresh=await automatic_refresh_enabled())


@router.get("/projects/{slug}/milestones")
async def get_milestones(slug: str, _identity: str = Depends(current_identity),
                         db=Depends(get_db)) -> dict:
    p = await _load_project(db, slug)
    ladder = await _ladder(db, p.id)
    if ladder is None:
        return {"ladder": None}
    return await _view(db, p, ladder)


@router.post("/projects/{slug}/milestones/refresh", status_code=status.HTTP_202_ACCEPTED)
async def refresh_milestones(slug: str, identity: str = Depends(current_identity),
                             db=Depends(get_db)):
    p = await _load_project(db, slug)
    ladder = await _ladder(db, p.id)
    if ladder is None:
        raise HTTPException(status_code=404, detail={"error": "no_ladder"})
    if not await automatic_refresh_enabled():
        return JSONResponse(status_code=status.HTTP_409_CONFLICT,
                            content={"refreshed": False, "reason": "automatic_refresh_off"})
    now = _utcnow()
    fetched = await newest_fetched_at(p.id)
    requested = aware_utc(ladder.refresh_requested_at)
    # A request newer than the last fetch is still pending: it holds the window too.
    pending = requested if requested is not None and (fetched is None or requested > fetched) else None
    anchors = [t for t in (fetched, pending) if t is not None]
    if anchors:
        wait = REFRESH_MIN_INTERVAL_S - (now - max(anchors)).total_seconds()
        if wait > 0:
            retry = max(1, math.ceil(wait))
            return JSONResponse(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                                content={"refreshed": False, "retryAfter": retry},
                                headers={"Retry-After": str(retry)})
    ladder.refresh_requested_at = now
    await db.flush()
    await record_audit(db=db, type="pm.milestone.refresh", identity=identity,
                       detail={"slug": slug})
    return {"refreshed": True}


# ---------------------------------------------------------------- writes (Phase 2)


class TickBody(BaseModel):
    met: bool | None = None
    value: float | None = None
    evidence: str
    url: str | None = None


class StatusNoteBody(BaseModel):
    statusNote: str | None = None


class NoteBody(BaseModel):
    kind: str
    text: str
    ref: str | None = None
    url: str | None = None
    milestoneKey: str | None = None
    status: str | None = None


class NotePatchBody(BaseModel):
    status: str | None = None
    text: str | None = None


class AdvanceBody(BaseModel):
    expectedCurrent: str | None
    overrideReason: str | None = None


async def _write(slug: str, request: Request, identity: str, db,
                 act: Callable[[MilestoneLadder, actions.Actor], Awaitable[object]]) -> dict:
    """THE write wrapper: resolve the ladder, run one rule from ``milestone_actions``
    as ``Actor(identity, via)``, commit once, answer with the full view."""
    p = await _load_project(db, slug)
    ladder = await _ladder(db, p.id)
    if ladder is None:
        raise HTTPException(status_code=404, detail={"error": "no_ladder"})
    actor = actions.Actor(identity, getattr(request.state, "via", None) or "api")
    try:
        await act(ladder, actor)
    except actions.MilestoneActionError as exc:
        raise HTTPException(status_code=exc.status,
                            detail={"error": exc.code, **exc.detail}) from exc
    await db.commit()
    return await _view(db, p, ladder)


# Static segments (notes, advance) are declared before /{key}; both are reserved
# milestone keys in the parser (milestone_def.RESERVED_MILESTONE_KEYS).
@router.post("/projects/{slug}/milestones/notes")
async def post_milestone_note(slug: str, body: NoteBody, request: Request,
                              identity: str = Depends(current_identity),
                              db=Depends(get_db)) -> dict:
    return await _write(slug, request, identity, db, lambda ladder, actor: actions.add_note(
        db, ladder, actor=actor, kind=body.kind, text=body.text, ref=body.ref, url=body.url,
        milestone_key=body.milestoneKey, status=body.status))


@router.patch("/projects/{slug}/milestones/notes/{note_id}")
async def patch_milestone_note(slug: str, note_id: str, body: NotePatchBody, request: Request,
                               identity: str = Depends(current_identity),
                               db=Depends(get_db)) -> dict:
    return await _write(slug, request, identity, db, lambda ladder, actor: actions.update_note(
        db, ladder, note_id, actor=actor, status=body.status, text=body.text))


@router.post("/projects/{slug}/milestones/advance")
async def advance_milestone(slug: str, body: AdvanceBody, request: Request,
                            identity: str = Depends(current_identity),
                            db=Depends(get_db)) -> dict:
    return await _write(slug, request, identity, db, lambda ladder, actor: actions.advance(
        db, ladder, actor=actor, expected_current=body.expectedCurrent,
        override_reason=body.overrideReason, now=_utcnow()))


@router.put("/projects/{slug}/milestones/{key}/conditions/{ckey}")
async def put_condition_tick(slug: str, key: str, ckey: str, body: TickBody, request: Request,
                             identity: str = Depends(current_identity),
                             db=Depends(get_db)) -> dict:
    return await _write(slug, request, identity, db, lambda ladder, actor: actions.tick_condition(
        db, ladder, key, ckey, actor=actor, met=body.met, value=body.value,
        evidence=body.evidence, url=body.url))


@router.patch("/projects/{slug}/milestones/{key}")
async def patch_milestone(slug: str, key: str, body: StatusNoteBody, request: Request,
                          identity: str = Depends(current_identity),
                          db=Depends(get_db)) -> dict:
    return await _write(slug, request, identity, db, lambda ladder, actor: actions.set_status_note(
        db, ladder, key, actor=actor, text=body.statusNote))
