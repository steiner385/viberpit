"""GET /api/capacity — the ceiling stack, one readout (spec 2026-09-19 §5)."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from server.auth.middleware import current_identity
from server.automations.capacity_apply import PLAN_MAX_AGE_S
from server.automations.capacity_store import read_plan

router = APIRouter(prefix="/api/capacity", tags=["capacity"])


@router.get("")
async def get_capacity(request: Request, _identity: str = Depends(current_identity)) -> dict:
    orch = getattr(request.app.state, "sessions", None)
    if orch is None or not hasattr(orch, "capacity_readout"):
        raise HTTPException(status_code=503, detail="capacity readout unavailable")
    return await orch.capacity_readout()


@router.get("/binding")
async def get_capacity_binding(_identity: str = Depends(current_identity)) -> dict:
    """The nav dot's cheap signal: the binding constraint the governor persisted on
    its last tick. One row read — no candidates read, no per-repo ``gh`` snapshot.
    A missing or stale plan (governor off or stalled) reads as ``binding: None``."""
    try:
        plan = await read_plan()
    except Exception:
        plan = None
    binding = None
    if plan and plan.get("computedAt"):
        computed = datetime.fromisoformat(plan["computedAt"])
        if computed.tzinfo is None:
            computed = computed.replace(tzinfo=timezone.utc)
        if abs((datetime.now(timezone.utc) - computed).total_seconds()) <= PLAN_MAX_AGE_S:
            binding = plan.get("binding")
    return {"binding": binding}
