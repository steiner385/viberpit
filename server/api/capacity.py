"""GET /api/capacity — the ceiling stack, one readout (spec 2026-09-19 §5)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from server.auth.middleware import current_identity

router = APIRouter(prefix="/api/capacity", tags=["capacity"])


@router.get("")
async def get_capacity(request: Request, _identity: str = Depends(current_identity)) -> dict:
    orch = getattr(request.app.state, "sessions", None)
    if orch is None or not hasattr(orch, "capacity_readout"):
        raise HTTPException(status_code=503, detail="capacity readout unavailable")
    return await orch.capacity_readout()
