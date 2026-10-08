"""Dispatch-bar assistants — best-effort Haiku helpers the UI calls while the
user is still typing. Every route here is fail-open: a helper that returns
nothing (or blows up) produces a partial/empty 200, never an error.

POST /api/dispatch/assess — model-tier + prompt-refinement assistant.
Fires two parallel Haiku calls (assess_model + refine_prompt) and returns
their results. Prompt refinement additionally falls back to a native Codex
thread (``refine_prompt_codex``, #1288) when no Anthropic account is
available — a fleet configured with OpenAI-only accounts previously got no
refinement at all, since ``refine_prompt``/``pick_helper_config_dir`` only
ever look for an Anthropic account. Model-tier assessment stays Claude-only:
model *tiers* (haiku/sonnet/opus) are an Anthropic-specific concept with no
Codex analogue.

POST /api/dispatch/repo-check — "wrong repository" check. Judges the draft
prompt against each repository's recent session titles
(``server.sessions.repo_match``) and names a different repository only on a
high-confidence match, so the UI can offer a switch before dispatch.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from server.auth.middleware import current_identity
from server.automations.model_pick import assess_model
from server.sessions.account_auth import account_store_io
from server.sessions.helper_call import pick_helper_config_dir, pick_helper_openai_account
from server.sessions.prompt_refiner import refine_prompt
from server.sessions.repo_match import recent_repo_vocabulary, suggest_repo

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
    config_dir = await account_store_io(pick_helper_config_dir, default=None)
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


class RepoCheckRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=8000)
    #: The repository currently selected in the dispatch bar (nameWithOwner), or
    #: None when nothing is selected yet.
    repo: str | None = None


class RepoCheckResponse(BaseModel):
    suggested_repo: str | None = None  # None = the prompt fits the selected repo (or no verdict)
    reason: str | None = None          # one sentence from the router; None whenever suggested_repo is


@router.post("/repo-check", response_model=RepoCheckResponse)
async def repo_check_dispatch(
    body: RepoCheckRequest,
    _identity=Depends(current_identity),
) -> RepoCheckResponse:
    """Does the draft prompt look like work for a different repository? Fail-open."""
    try:
        vocab = await recent_repo_vocabulary()
        config_dir = await account_store_io(pick_helper_config_dir, default=None)
        suggestion = await suggest_repo(body.prompt, body.repo, vocab, _config_dir=config_dir)
    except Exception:
        # Both helpers are fail-open already; this is the belt to their braces so
        # a dispatch-bar hint can never turn into a 500.
        log.warning("repo_check_dispatch: helper failed", exc_info=True)
        suggestion = None
    if suggestion is None:
        return RepoCheckResponse()
    return RepoCheckResponse(suggested_repo=suggestion.repo, reason=suggestion.reason)
