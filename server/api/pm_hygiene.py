"""Needs-you burden API (design 2026-10-14 §2): the needs-you trend and the owner
recommendations the needs-you hygiene loop leaves for ``cautious`` products.

Applying a recommendation runs the SAME executor the loop runs
(``needs_you_hygiene.apply_action``), as the person who asked, via ``api``."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from server.audit import record_audit
from server.auth.middleware import current_identity
from server.automations.needs_you_hygiene import apply_action
from server.db import get_db
from server.db.models import (OWNER_RECOMMENDATION_STATUSES, AttentionSample,
                              OwnerRecommendation, Project)
from server.pm.hygiene import HygieneAction
from server.time_iso import iso_utc

router = APIRouter(prefix="/api/pm", tags=["pm"])

#: Recommendations per apply/dismiss call (the confirmation lists every one).
RECOMMENDATION_BATCH_MAX = 50


async def _project(db, slug: str) -> Project:
    p = (await db.execute(select(Project).where(Project.slug == slug))).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": f"Project '{slug}' not found"})
    return p


@router.get("/attention/history")
async def attention_history(product: str, days: int = Query(30, ge=1, le=365),
                            _identity: str = Depends(current_identity),
                            db=Depends(get_db)) -> dict:
    """The product's hourly needs-you samples over the last *days*, oldest first:
    the badge's count (``needsYou``) and its waiting counters at each sample."""
    p = await _project(db, product)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(select(AttentionSample).where(
        AttentionSample.project_id == p.id, AttentionSample.sampled_at >= since)
        .order_by(AttentionSample.sampled_at))).scalars().all()
    return {"product": product, "samples": [
        {"at": iso_utc(r.sampled_at), "needsYou": r.needs_you, "waiting": r.waiting or {}}
        for r in rows]}


def recommendation_to_wire(r: OwnerRecommendation, slug: str | None) -> dict:
    return {"id": r.id, "product": slug, "rule": r.rule, "ref": r.target_ref,
            "workItemId": r.work_item_id, "action": r.action, "detail": r.detail or {},
            "evidence": r.evidence, "status": r.status, "createdAt": iso_utc(r.created_at),
            "resolvedAt": iso_utc(r.resolved_at), "resolvedBy": r.resolved_by}


async def project_slugs(db) -> dict[str, str]:
    return {pid: slug or pid for pid, slug in (await db.execute(
        select(Project.id, Project.slug))).all()}


@router.get("/recommendations")
async def list_recommendations(product: str | None = None, status: str = "pending",
                               _identity: str = Depends(current_identity),
                               db=Depends(get_db)) -> dict:
    """Owner recommendations, newest first — ``pending`` by default; one product or all."""
    if status not in OWNER_RECOMMENDATION_STATUSES:
        raise HTTPException(status_code=422, detail={
            "error": "bad_status",
            "message": f"status must be one of {list(OWNER_RECOMMENDATION_STATUSES)}"})
    stmt = select(OwnerRecommendation).where(OwnerRecommendation.status == status)
    if product is not None:
        stmt = stmt.where(OwnerRecommendation.project_id == (await _project(db, product)).id)
    rows = (await db.execute(stmt.order_by(OwnerRecommendation.created_at.desc()))).scalars().all()
    slugs = await project_slugs(db)
    return {"recommendations": [recommendation_to_wire(r, slugs.get(r.project_id)) for r in rows]}


class RecommendationApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(min_length=1, max_length=RECOMMENDATION_BATCH_MAX)
    confirm: bool = False


class RecommendationDismissBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(min_length=1, max_length=RECOMMENDATION_BATCH_MAX)


async def recommendations_by_id(db, ids: list[str]) -> dict[str, OwnerRecommendation]:
    rows = (await db.execute(select(OwnerRecommendation).where(
        OwnerRecommendation.id.in_(ids)))).scalars().all()
    return {r.id: r for r in rows}


def refused(rid: str, row: OwnerRecommendation | None) -> dict | None:
    """The per-id refusal for a recommendation that is missing or no longer pending."""
    if row is None:
        return {"id": rid, "ok": False, "step": "not_found", "error": "no such recommendation"}
    if row.status != "pending":
        return {"id": rid, "ok": False, "step": "not_pending",
                "error": f"this recommendation is {row.status}"}
    return None


async def apply_pending(db, row: OwnerRecommendation, *, product: str, seams,
                        identity: str) -> dict:
    """Apply ONE pending recommendation through the needs-you hygiene executor, as
    *identity* via ``api``, and mark it ``applied`` or ``failed`` (committed). THE
    apply both ``POST /recommendations/apply`` and the owner digest
    (``POST /digest/apply``) run. The caller has committed its reads first: the
    executor's writes go through their own sessions."""
    action = HygieneAction(rule=row.rule, product=product, ref=row.target_ref,
                           work_item_id=row.work_item_id, action=row.action,
                           detail=dict(row.detail or {}), evidence=row.evidence or "")
    result = await apply_action(action, seams=seams, identity=identity, actor=identity,
                                via="api")
    ok = bool(result.get("ok"))
    row.status = "applied" if ok else "failed"
    row.resolved_at, row.resolved_by = datetime.now(timezone.utc), identity
    await db.commit()
    return {"id": row.id, "ok": ok, "step": result.get("step"), "error": result.get("error")}


@router.post("/recommendations/apply")
async def apply_recommendations(body: RecommendationApplyBody, request: Request,
                                identity: str = Depends(current_identity),
                                db=Depends(get_db)) -> dict:
    """Apply pending recommendations — each through the needs-you hygiene executor,
    as the caller, audited ``pm.hygiene.<rule>`` via ``api`` — and mark each
    ``applied`` or ``failed``. ``confirm: true`` is required: each one writes to
    GitHub (a close, a board move, a hold release or an edge prune)."""
    if not body.confirm:
        raise HTTPException(status_code=422, detail={
            "error": "confirm_required",
            "message": "applying writes to GitHub; resend with confirm: true"})
    ids = list(dict.fromkeys(body.ids))
    rows = await recommendations_by_id(db, ids)
    slugs = await project_slugs(db)
    # Read first, write after: the executor's writes go through their own sessions.
    await db.commit()
    results: list[dict] = []
    for rid in ids:
        row = rows.get(rid)
        refusal = refused(rid, row)
        if refusal is not None:
            results.append(refusal)
            continue
        assert row is not None
        results.append(await apply_pending(
            db, row, product=slugs.get(row.project_id, row.project_id),
            seams=request.app.state.sessions, identity=identity))
    return {"results": results, "applied": sum(1 for r in results if r["ok"]),
            "failed": sum(1 for r in results if not r["ok"])}


@router.post("/recommendations/dismiss")
async def dismiss_recommendations(body: RecommendationDismissBody,
                                  identity: str = Depends(current_identity),
                                  db=Depends(get_db)) -> dict:
    """Dismiss pending recommendations: nothing is written to GitHub, and the loop
    never re-recommends a dismissed finding (``needs_you_hygiene.upsert_recommendation``)."""
    ids = list(dict.fromkeys(body.ids))
    rows = await recommendations_by_id(db, ids)
    results: list[dict] = []
    for rid in ids:
        row = rows.get(rid)
        refusal = refused(rid, row)
        if refusal is not None:
            results.append(refusal)
            continue
        assert row is not None
        row.status, row.resolved_at, row.resolved_by = (
            "dismissed", datetime.now(timezone.utc), identity)
        await record_audit(db=db, type="pm.hygiene.dismiss", identity=identity,
                           detail={"id": rid, "rule": row.rule, "ref": row.target_ref,
                                   "action": row.action})
        results.append({"id": rid, "ok": True, "step": None, "error": None})
    return {"results": results, "dismissed": sum(1 for r in results if r["ok"])}
