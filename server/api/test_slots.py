"""Verification-phase test-run slot API (#822).

Any session may call this around its own full-suite `pytest server/tests/`
run to cooperate with the per-host cap documented in
.agents/instructions/core.md ("Dev / testing"): acquire before running the
suite, poll status while queued, release when done (in a shell `trap ... EXIT`
so a crash still frees the slot on the DAEMON side once the session's process
tree exits and nothing else releases it — see that doc for the exact recipe).

Fail-open by design: ANY exception from server.sessions.test_run_slots is
caught here and turned into "you may proceed" (acquire → granted=True,
status → "unknown", release → best-effort). A session must never be blocked
from running tests by this control plane being unavailable.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from server.auth.middleware import current_identity
from server.config import get_settings
from server.sessions import test_run_slots

router = APIRouter(prefix="/api/test-slots", tags=["test-slots"])
logger = logging.getLogger(__name__)


class AcquireBody(BaseModel):
    sessionId: str
    host: str


class ReleaseBody(BaseModel):
    sessionId: str


@router.post("/acquire")
async def acquire_test_slot(
    body: AcquireBody, _identity: str = Depends(current_identity)
) -> dict:
    cap = get_settings().test_slot_cap
    try:
        granted, ahead = await test_run_slots.acquire(body.sessionId, body.host, cap=cap)
    except Exception:
        logger.exception(
            "test-slot acquire failed for session=%s host=%s — failing open",
            body.sessionId, body.host)
        return {"granted": True, "ahead": 0, "cap": cap, "degraded": True}
    return {"granted": granted, "ahead": ahead, "cap": cap, "degraded": False}


@router.post("/release")
async def release_test_slot(
    body: ReleaseBody, _identity: str = Depends(current_identity)
) -> dict:
    try:
        await test_run_slots.release(body.sessionId)
    except Exception:
        logger.exception("test-slot release failed for session=%s", body.sessionId)
    return {"released": True}


@router.get("/status")
async def test_slot_status(
    sessionId: str, host: str, _identity: str = Depends(current_identity)
) -> dict:
    cap = get_settings().test_slot_cap
    try:
        state, ahead = await test_run_slots.status(sessionId, host)
    except Exception:
        logger.exception(
            "test-slot status failed for session=%s host=%s — failing open",
            sessionId, host)
        return {"status": "unknown", "ahead": 0, "cap": cap, "degraded": True}
    return {"status": state, "ahead": ahead, "cap": cap, "degraded": False}
