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
from server.sessions.prompt_refiner import refine_prompt

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dispatch", tags=["dispatch"])


class AssessRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=8000)


class AssessResponse(BaseModel):
    model: str | None = None          # None = assessor returned nothing
    refined_prompt: str | None = None  # None = refiner returned nothing


def _pick_helper_config_dir() -> str | None:
    """Return the config_dir of the first live Anthropic account, or None.

    Helper calls (assess_model, refine_prompt) need a valid CLAUDE_CONFIG_DIR
    to authenticate with the SDK. The server process has no per-account env set,
    so we pick the first Anthropic account whose ledger status is not
    ``needs-reauth``. Fail-open: if no account is available the callers degrade
    to None safely.
    """
    try:
        from ccswitch import accounts as _accts
        from ccswitch import ledger as _ledger
        from ccswitch import paths as _paths
        with _ledger.locked_ledger() as led:
            for name, provider in _accts.all_accounts():
                if provider != "anthropic":
                    continue
                if _ledger.get_status(led, name) == "needs-reauth":
                    continue
                return str(_paths.account_cfg_dir(name))
    except Exception:
        pass
    return None


@router.post("/assess", response_model=AssessResponse)
async def assess_dispatch(
    body: AssessRequest,
    _identity=Depends(current_identity),
) -> AssessResponse:
    """Assess model tier and refine prompt in parallel. Fail-open on either."""
    config_dir = _pick_helper_config_dir()
    model, refined = await asyncio.gather(
        assess_model(body.prompt, _config_dir=config_dir),
        refine_prompt(body.prompt, _config_dir=config_dir),
        return_exceptions=False,
    )
    return AssessResponse(model=model, refined_prompt=refined)
