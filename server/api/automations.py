"""Automation registry API — list/read/configure code-defined automations."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from server.auth.middleware import current_identity
from server.automations.base import validate_config
from server.automations.context import AutomationContext
from server.automations.registry import registry
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


class PrDoctorFixBody(BaseModel):
    repo: str
    number: int
    kind: str


class BacklogImplementBody(BaseModel):
    primary: str


# The failure kinds the propose surface can act on (matches the pure engine's
# recovery modes; ``blocked`` is notify-only and never a one-tap action).
_PR_DOCTOR_FIX_KINDS = {"out_of_date", "failing_ci", "merge_conflict"}


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
async def list_automations(_identity: str = Depends(current_identity), db=Depends(get_db)) -> list[dict]:
    # Count live (non-ended/-archived) spawned sessions per automation in one grouped
    # query, so the list can show "N active" without a follow-up call per card.
    active_counts = dict((await db.execute(
        select(DbSession.origin, func.count())
        .where(DbSession.origin.is_not(None),
               DbSession.status.notin_(INACTIVE_SESSION_STATUSES))
        .group_by(DbSession.origin))).all())
    out = []
    for a in registry.all():
        row = await _row(db, a.id)
        out.append({
            "id": a.id, "name": a.name, "description": a.description,
            "enabled": row.enabled, "mode": row.mode, "modes": list(a.modes),
            "status": a.status(ctx=None).to_dict(),
            "configSchema": [f.to_dict() for f in a.config_schema],
            "config": row.config or {},
            "lastActiveAt": row.last_active_at.isoformat() if row.last_active_at else None,
            "activeSessions": int(active_counts.get(a.id, 0)),
        })
    return out


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


@router.get("/pr-doctor/actionable")
async def pr_doctor_actionable(request: Request,
                               _identity: str = Depends(current_identity)) -> list[dict]:
    """PRs with a real recovery mode, for the PR Doctor card's one-tap actions."""
    _automation_or_404("pr-doctor")
    orch = request.app.state.sessions
    return await orch.pr_doctor_actionable()


@router.post("/pr-doctor/fix")
async def pr_doctor_fix(body: PrDoctorFixBody, request: Request,
                        _identity: str = Depends(current_identity)) -> dict:
    """Trigger a one-tap PR Doctor recovery action for an actionable PR."""
    _automation_or_404("pr-doctor")
    if body.kind not in _PR_DOCTOR_FIX_KINDS:
        raise HTTPException(status_code=422, detail=f"invalid kind {body.kind!r}")
    orch = request.app.state.sessions
    status = await orch.pr_doctor_fix(body.repo, body.number, body.kind)
    return {"status": status}


@router.post("/backlog-runner/implement")
async def backlog_runner_implement(body: BacklogImplementBody, request: Request,
                                   _identity: str = Depends(current_identity)) -> dict:
    """Propose-tap: claim (if needed) + spawn the runner batch for *primary*.

    404 when *primary* is not a current candidate; otherwise returns the spawn
    status (``spawned`` / ``exists`` / ``claim_failed`` / ``no_account``)."""
    _automation_or_404("backlog-runner")
    orch = request.app.state.sessions
    status = await orch.backlog_runner_implement(body.primary)
    if status == "unknown":
        raise HTTPException(status_code=404, detail="unknown primary or not a current candidate")
    return {"status": status}
