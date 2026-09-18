"""Automation registry API — list/read/configure code-defined automations."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from server.audit import record_audit
from server.auth.middleware import current_identity
from server.automations.base import (
    AutomationStatus,
    operator_triggered,
    retired_config_keys_of,
    validate_config,
    work_class_of,
)
from server.automations.context import AutomationContext
from server.automations.engine_policy import policy_for
from server.automations.registry import registry
from server.automations.schedule import PRESETS, ScheduleError, next_runs, parse_schedule
from server.db import get_db
from server.db.models import (
    INACTIVE_SESSION_STATUSES,
    Automation as AutomationRow,
    Session as DbSession,
    WorkItem,
)
from server.github.repo_canon import canonicalize_repo_list
from server.pm.holds import HoldPermissionError, actor_class, actor_for_request, clear_hold
from server.pm.refs import split_ref

log = logging.getLogger(__name__)

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


class DispatchBody(BaseModel):
    refs: list[str]


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
    # orchestrator is unreachable, retain unknown rather than inventing idle.
    statuses: dict = {}
    actionable_counts: dict = {}
    orch = getattr(request.app.state, "sessions", None)
    if orch is not None:
        try:
            overview = await orch.automation_overview()
            statuses = overview.get("statuses") or {}
            actionable_counts = overview.get("actionableCounts") or {}
        except Exception:
            statuses = {}
    out = []
    for a in registry.all():
        row = await _row(db, a.id)
        status = dict(statuses.get(a.id) or AutomationStatus(
            "Runtime telemetry unavailable", level="unknown").to_dict())
        active_sessions = int(active_counts.get(a.id, 0))
        actionable_count = actionable_counts.get(a.id)
        # A previous scheduling/capacity wait no longer constrains a disabled
        # loop. Disabling never resolves an observed human decision, even when
        # its producer has no action provider or live session (e.g. a proposal).
        if (not row.enabled or row.mode == "off") and status.get("waitingReason") != "human":
            status.pop("waitingReason", None)
        oversight = registry.oversight_for(a.id)
        out.append({
            "id": a.id, "name": a.name, "description": a.description,
            "enabled": row.enabled, "mode": row.mode, "modes": list(a.modes),
            # Phase 3 (spec §9/§10): the runner's per-product rows ride INSIDE
            # ``status`` (``AutomationStatus.to_dict``) — a list for an automation
            # whose status carries per-product data, ``None`` for one that has no
            # concept of products (e.g. PR Doctor). Published once, so the browser
            # cannot read a stale sibling copy of the same fact (R3-8).
            "status": status,
            "actionableCount": actionable_count,
            "oversight": oversight.to_dict() if oversight is not None else None,
            # The schema INCLUDING the engine fields this origin's audited
            # ceiling admits (none for a claude_only origin). Same source the
            # PATCH validates against, so the browser can never render a control
            # the server would reject.
            "configSchema": [f.to_dict() for f in registry.schema_for(a.id)],
            "enginePolicy": policy_for(a).value,
            # No control loop: the card renders its config (an engine ceiling
            # still applies) but no enable toggle, because there is nothing to
            # enable. Published rather than re-derived in the browser so the
            # rule lives only in ``automations.base.operator_triggered``.
            "operatorTriggered": operator_triggered(a),
            # "development" | "maintenance": whether this origin's sessions are
            # new work or housekeeping the sessions list keeps in the background.
            "workClass": work_class_of(a),
            "config": row.config or {},
            "schedule": row.schedule or None,
            "lastActiveAt": row.last_active_at.isoformat() if row.last_active_at else None,
            "activeSessions": active_sessions,
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


async def _canonicalize_enabled_repos(config: dict | None) -> dict | None:
    """*config* with ``enabled_repos`` rewritten to GitHub's canonical names.

    The write path is where a rename alias has to be stopped. GitHub forwards a
    renamed repository forever, so a slug entered (or stored) before a rename
    keeps resolving — it just becomes a SECOND string key for one repo, and
    pr-doctor keys its jobs on ``"owner/repo#N"``. Two keys meant two concurrent
    sessions on the same pull request, on the same branch.

    Fails open: :func:`canonicalize_repo_list` returns the list untouched when the
    lookup cannot be made, so an operator's save is never blocked by a GitHub blip.
    """
    repos = (config or {}).get("enabled_repos")
    if not isinstance(repos, list) or not repos:
        return config
    # ``_run_gh`` is a blocking subprocess; keep it off the event loop.
    canonical = await asyncio.to_thread(canonicalize_repo_list, repos)
    if canonical != repos:
        log.info("automations.enabled_repos.canonicalized %s -> %s", repos, canonical)
    return {**(config or {}), "enabled_repos": canonical}


@router.patch("/{automation_id}")
async def patch_automation(automation_id: str, body: PatchBody,
                           _identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    automation = _automation_or_404(automation_id)
    row = await _row(db, automation_id)
    if operator_triggered(automation) and (body.enabled is not None
                                           or body.mode is not None
                                           or body.schedule):
        # An origin with no control loop has nothing to enable, run in a mode, or
        # schedule; accepting the write would persist a state no reader consults
        # and show the operator a switch that does not switch anything. Its
        # config (including the engine ceiling's fields) is still patchable.
        raise HTTPException(
            status_code=422,
            detail=f"{automation_id} has no control loop: it spawns only when "
                   "triggered, so it cannot be enabled, moded or scheduled")
    if body.mode is not None:
        if body.mode not in automation.modes:
            raise HTTPException(status_code=422, detail=f"invalid mode {body.mode!r}")
        row.mode = body.mode
    if body.config is not None:
        try:
            # Validated against the ENGINE-AWARE schema: an origin with no Codex
            # acceptance evidence offers no `engine` field, so `{"engine": ...}`
            # lands here as an unknown key and 422s. That is the enforcement —
            # not a special case downstream (Phase 7, Task 7.1). Keys the origin
            # RETIRED are dropped instead of 422'd, so a client echoing back the
            # config it was served before a field was removed still saves.
            row.config = validate_config(registry.schema_for(automation_id), body.config,
                                         retired=retired_config_keys_of(automation))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        row.config = await _canonicalize_enabled_repos(row.config)
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


@router.get("/pr-doctor/jobs/{job_id}")
async def pr_doctor_job_status(job_id: str, request: Request,
                               _identity: str = Depends(current_identity)) -> dict:
    """Live phase of an async pr-doctor trigger (issue #668). The trigger returns
    a job id immediately; the UI polls this to show cloning → assessing →
    spawning → ready and, on failure, which phase failed. 404 once the job is
    gone (evicted or lost to a daemon restart)."""
    _automation_or_404("pr-doctor")
    orch = request.app.state.sessions
    job = await orch.pr_doctor_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


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


@router.get("/backlog-runner/candidates")
async def backlog_runner_candidates(request: Request,
                                    _identity: str = Depends(current_identity)) -> dict:
    """The runner's current candidate pool (the ranked priority queue, unclaimed)
    for manual dispatch."""
    _automation_or_404("backlog-runner")
    orch = request.app.state.sessions
    candidates = await orch.backlog_candidates()
    return {"candidates": candidates}


@router.post("/backlog-runner/dispatch")
async def backlog_runner_dispatch(body: DispatchBody, request: Request,
                                  identity: str = Depends(current_identity),
                                  db=Depends(get_db)) -> dict:
    """Immediately claim + spawn a session for the given issue refs.

    Implement now from the roadmap dialog: before the spawn, release any
    owner_action hold on the target issue(s) in the same request (spec
    2026-09-18 §7, RELEASE_SIGNALS "implement"). Mirrors the Retry endpoint's
    actor resolution, its `decision`-hold refusal, and its audit shape
    (``server/api/pm.py``'s ``release_work_item_hold``)."""
    _automation_or_404("backlog-runner")
    if not body.refs:
        raise HTTPException(status_code=400, detail="refs must be non-empty")
    actor = actor_for_request(request)
    for ref in body.refs:
        parts = split_ref(ref)
        if parts is None:
            continue
        repo, number = parts
        wi = (await db.execute(select(WorkItem).where(
            WorkItem.repo == repo, WorkItem.number == number,
            WorkItem.kind == "issue"))).scalar_one_or_none()
        if wi is None or wi.hold_kind is None:
            continue
        if wi.hold_kind == "decision":
            raise HTTPException(status_code=422, detail={
                "error": "bad_hold",
                "message": "a decision hold is resolved through its proposal, not released"})
        released = wi.hold_kind
        try:
            clear_hold(wi, actor=actor)
        except HoldPermissionError as e:
            raise HTTPException(status_code=422, detail={
                "error": "hold_not_permitted", "message": str(e)}) from e
        await db.flush()
        await record_audit(db=db, type="pm.hold.release", identity=identity, detail={
            "workItemId": wi.id, "kind": released, "actorClass": actor_class(actor),
            "signal": "implement"}, via="api")
    # Commit (rather than leaving this to `get_db`'s post-route commit) BEFORE
    # calling into the orchestrator: it opens its own DB session to claim
    # ownership, and SQLite serializes writers — an open write transaction here
    # would "database is locked" against that claim.
    await db.commit()
    orch = request.app.state.sessions
    return await orch.backlog_runner_dispatch(body.refs)
