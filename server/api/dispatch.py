"""POST /api/dispatch/assess — model-tier + prompt-refinement assistant.

Fires two parallel Haiku calls (assess_model + refine_prompt) and returns
their results. Both are best-effort: a None from either produces a partial
response so the caller can show whatever succeeded.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from server.auth.middleware import current_identity
from server.automations.model_pick import assess_model
from server.sessions.helper_call import pick_helper_config_dir
from server.sessions.prompt_refiner import refine_prompt

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dispatch", tags=["dispatch"])


class AssessRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=8000)


class AssessResponse(BaseModel):
    model: str | None = None          # None = assessor returned nothing
    refined_prompt: str | None = None  # None = refiner returned nothing


@router.post("/assess", response_model=AssessResponse)
async def assess_dispatch(
    body: AssessRequest,
    _identity=Depends(current_identity),
) -> AssessResponse:
    """Assess model tier and refine prompt in parallel. Fail-open on either."""
    config_dir = pick_helper_config_dir()
    model, refined = await asyncio.gather(
        assess_model(body.prompt, _config_dir=config_dir),
        refine_prompt(body.prompt, _config_dir=config_dir),
        return_exceptions=False,
    )
    return AssessResponse(model=model, refined_prompt=refined)
