"""Automation registry API — list/read/configure code-defined automations."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from server.auth.middleware import current_identity
from server.automations.base import validate_config
from server.automations.context import AutomationContext
from server.automations.registry import registry
from server.automations.schedule import PRESETS, ScheduleError, next_runs, parse_schedule
from server.db import get_db
from server.db.models import (
    INACTIVE_SESSION_STATUSES,
    Automation as AutomationRow,
    Session as DbSession,
)

router = APIRouter(prefix="/api/automations", tags=["automations"])


class PatchBody(BaseModel):
    enabled: bool | None = None
    mode: str | None = None
    config: dict | None = None
    # Optional cadence (#603). Pass {} to clear the schedule, omit to leave it.
    schedule: dict | None = None


class TriggerBody(BaseModel):
    item_id: str
    kind: str


class ConsolidateBody(BaseModel):
    repo: str
    numbers: list[int]


async def _row(db, automation_id: str) -> AutomationRow:
    row = (await db.execute(
        select(AutomationRow).where(AutomationRow.id == automation_id))).scalar_one_or_none()
    if row is None:
        row = AutomationRow(id=automation_id, enabled=False)
        db.add(row)
        await db.flush()
    return row


def _automation_or_404(automation_id: str):
    try:
        return registry.get(automation_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown automation")


@router.get("")
async def list_automations(request: Request, _identity: str = Depends(current_identity),
                           db=Depends(get_db)) -> list[dict]:
    # Count live (non-ended/-archived) spawned sessions per automation in one grouped
    # query, so the list can show "N active" without a follow-up call per card.
    active_counts = dict((await db.execute(
        select(DbSession.origin, func.count())
        .where(DbSession.origin.is_not(None),
               DbSession.status.notin_(INACTIVE_SESSION_STATUSES))
        .group_by(DbSession.origin))).all())
    # Live badge status is rendered by the orchestrator (only it can reach the
    # in-process ctx where each loop stashes its counts). Best-effort: if the
    # orchestrator is unreachable, fall back to the idle status per automation.
    statuses: dict = {}
    orch = getattr(request.app.state, "sessions", None)
    if orch is not None:
        try:
            statuses = await orch.automation_statuses()
        except Exception:
            statuses = {}
    out = []
    for a in registry.all():
        row = await _row(db, a.id)
        out.append({
            "id": a.id, "name": a.name, "description": a.description,
            "enabled": row.enabled, "mode": row.mode, "modes": list(a.modes),
            "status": statuses.get(a.id) or a.status(ctx=None).to_dict(),
            "configSchema": [f.to_dict() for f in a.config_schema],
            "config": row.config or {},
            "schedule": row.schedule or None,
            "lastActiveAt": row.last_active_at.isoformat() if row.last_active_at else None,
            "activeSessions": int(active_counts.get(a.id, 0)),
        })
    return out


class SchedulePreviewBody(BaseModel):
    schedule: dict


@router.post("/schedule/preview")
async def schedule_preview(body: SchedulePreviewBody,
                           _identity: str = Depends(current_identity)) -> dict:
    """Validate a candidate schedule and return its next fire times so the editor
    can show a live "next 3 runs" preview. 422 on a malformed schedule/cron."""
    from datetime import datetime
    try:
        parse_schedule(body.schedule)
    except ScheduleError as e:
        raise HTTPException(status_code=422, detail=str(e))
    runs = next_runs(body.schedule, datetime.now(), count=3)
    return {"nextRuns": [r.isoformat() for r in runs], "presets": PRESETS}


@router.get("/{automation_id}/events")
async def list_events(automation_id: str, limit: int = 100, target: str | None = None,
                      kind: str | None = None,
                      _identity: str = Depends(current_identity)) -> list[dict]:
    _automation_or_404(automation_id)
    ctx = AutomationContext(automation_id)
    return await ctx.list_events(limit=limit, target=target, kind=kind)


@router.get("/{automation_id}/sessions")
async def spawned_sessions(automation_id: str, _identity: str = Depends(current_identity),
                           db=Depends(get_db)) -> list[dict]:
    _automation_or_404(automation_id)
    rows = (await db.execute(
        select(DbSession).where(DbSession.origin == automation_id)
        .order_by(DbSession.created_at.desc()).limit(50))).scalars().all()
    return [{"id": s.id, "name": s.name, "status": s.status, "originRef": s.origin_ref}
            for s in rows]


@router.patch("/{automation_id}")
async def patch_automation(automation_id: str, body: PatchBody,
                           _identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    automation = _automation_or_404(automation_id)
    row = await _row(db, automation_id)
    if body.mode is not None:
        if body.mode not in automation.modes:
            raise HTTPException(status_code=422, detail=f"invalid mode {body.mode!r}")
        row.mode = body.mode
    if body.config is not None:
        try:
            row.config = validate_config(automation.config_schema, body.config)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    if body.schedule is not None:
        try:
            parse_schedule(body.schedule)  # validate; reject a bad cron/window
        except ScheduleError as e:
            raise HTTPException(status_code=422, detail=str(e))
        row.schedule = body.schedule or None
    if body.enabled is not None:
        row.enabled = body.enabled
    await db.flush()
    # React to the change (best-effort; failure surfaces as an event, not a 500).
    ctx = AutomationContext(automation_id)
    try:
        await automation.on_state_change(enabled=row.enabled, mode=row.mode,
                                         config=row.config or {}, ctx=ctx)
    except Exception:
        await ctx.emit("error", "on_state_change failed", kind="error")
    return {"id": automation_id, "enabled": row.enabled, "mode": row.mode,
            "config": row.config or {}}


@router.get("/cicd-doctor/findings")
async def cicd_doctor_findings(status: str = "open", repo: str | None = None,
                               _identity: str = Depends(current_identity),
                               db=Depends(get_db)) -> list[dict]:
    """CI/CD Doctor findings, newest first. Reads the DB directly — the loop's
    state is all persisted, so no orchestrator round-trip is needed."""
    from server.db.models import CiFinding
    _automation_or_404("cicd-doctor")
    stmt = select(CiFinding).order_by(CiFinding.last_seen_at.desc()).limit(200)
    if status != "all":
        stmt = stmt.where(CiFinding.status == status)
    if repo is not None:
        stmt = stmt.where(CiFinding.repo == repo)
    rows = (await db.execute(stmt)).scalars().all()
    return [{"id": f.id, "repo": f.repo, "kind": f.kind, "title": f.title,
             "status": f.status, "detail": f.detail or {},
             "issueNumber": f.issue_number,
             "firstSeenAt": f.first_seen_at.isoformat() if f.first_seen_at else None,
             "lastSeenAt": f.last_seen_at.isoformat() if f.last_seen_at else None}
            for f in rows]


@router.get("/backlog-runner/groups")
async def backlog_runner_groups(_identity: str = Depends(current_identity),
                                db=Depends(get_db)) -> dict:
    """The current backlog grouping plan, priority order. Reads the DB directly
    — the background optimizer persists every generation, so no orchestrator
    round-trip is needed. Read-only: the runner consumes groups on its own."""
    from server.automations.backlog_grouping import group_view
    from server.db.models import BacklogGroup
    _automation_or_404("backlog-runner")
    rows = (await db.execute(
        select(BacklogGroup).order_by(BacklogGroup.rank))).scalars().all()
    return {
        "generation": rows[0].generation if rows else None,
        "createdAt": rows[0].created_at if rows else None,
        "groups": [group_view(r) for r in rows],
    }


@router.get("/{automation_id}/actionable")
async def list_automation_actionable(automation_id: str, request: Request,
                                     _identity: str = Depends(current_identity)) -> list[dict]:
    """Detected items an automation can act on now, for the card's action buttons."""
    _automation_or_404(automation_id)
    orch = request.app.state.sessions
    return await orch.automation_actionable(automation_id)


@router.post("/{automation_id}/trigger")
async def trigger_automation_action(automation_id: str, body: TriggerBody, request: Request,
                                    _identity: str = Depends(current_identity)) -> dict:
    """Fire one manual action on an actionable item; returns the TriggerResult."""
    _automation_or_404(automation_id)
    orch = request.app.state.sessions
    result = await orch.automation_trigger(automation_id, body.item_id, body.kind)
    if result.get("status") == "no_provider":
        raise HTTPException(status_code=404, detail=result.get("message", "no action provider"))
    if result.get("status") == "invalid_kind":
        raise HTTPException(status_code=422, detail=result.get("message", "invalid kind"))
    return result


@router.get("/pr-doctor/consolidation")
async def pr_doctor_consolidation(request: Request,
                                  _identity: str = Depends(current_identity)) -> dict:
    """Open PRs (per repo) + suggested consolidation clusters for the picker."""
    _automation_or_404("pr-doctor")
    orch = request.app.state.sessions
    return await orch.consolidation_candidates()


@router.post("/pr-doctor/consolidate")
async def pr_doctor_consolidate(body: ConsolidateBody, request: Request,
                                _identity: str = Depends(current_identity)) -> dict:
    """Spawn one session to consolidate the selected PRs. Semantic rejections
    (<2 PRs, closed PR) come back as graceful TriggerResults, not 4xx — only a
    malformed body is a 422 (pydantic)."""
    _automation_or_404("pr-doctor")
    orch = request.app.state.sessions
    return await orch.automation_consolidate(body.repo, body.numbers)
