"""POST /api/dispatch/assess — model-tier + prompt-refinement assistant.

Fires two parallel Haiku calls (assess_model + refine_prompt) and returns
their results. Both are best-effort: a None from either produces a partial
response so the caller can show whatever succeeded.

Prompt refinement additionally falls back to a native Codex thread
(``refine_prompt_codex``, #1288) when no Anthropic account is available — a
fleet configured with OpenAI-only accounts previously got no refinement at
all, since ``refine_prompt``/``pick_helper_config_dir`` only ever look for an
Anthropic account. Model-tier assessment stays Claude-only: model *tiers*
(haiku/sonnet/opus) are an Anthropic-specific concept with no Codex analogue.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from server.auth.middleware import current_identity
from server.automations.model_pick import assess_model
from server.sessions.helper_call import pick_helper_config_dir, pick_helper_openai_account
from server.sessions.prompt_refiner import refine_prompt

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dispatch", tags=["dispatch"])


class AssessRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=8000)


class AssessResponse(BaseModel):
    model: str | None = None          # None = assessor returned nothing
    refined_prompt: str | None = None  # None = refiner returned nothing
    #: Which provider's account actually produced refined_prompt ("anthropic" /
    #: "openai"), for billing/account provenance. None whenever refined_prompt is.
    refined_by: str | None = None


@router.post("/assess", response_model=AssessResponse)
async def assess_dispatch(
    body: AssessRequest,
    request: Request,
    _identity=Depends(current_identity),
) -> AssessResponse:
    """Assess model tier and refine prompt in parallel. Fail-open on either."""
    config_dir = pick_helper_config_dir()
    model, refined = await asyncio.gather(
        assess_model(body.prompt, _config_dir=config_dir),
        refine_prompt(body.prompt, _config_dir=config_dir),
        return_exceptions=False,
    )
    refined_by = "anthropic" if refined is not None else None
    if refined is None:
        # No Anthropic account (or the Claude call itself failed): explicitly try
        # the OpenAI/Codex path next, never composing Anthropic and OpenAI
        # credentials into the same attempt.
        openai_account = pick_helper_openai_account()
        if openai_account is not None:
            orch = request.app.state.sessions
            try:
                refined = await orch.refine_prompt_codex(openai_account, body.prompt)
            except Exception:
                # Best-effort like every other branch here: a daemon-unreachable or
                # RPC failure degrades to "no refinement" rather than a 500.
                log.warning("assess_dispatch: refine_prompt_codex failed", exc_info=True)
                refined = None
            if refined is not None:
                refined_by = "openai"
    return AssessResponse(model=model, refined_prompt=refined, refined_by=refined_by)
