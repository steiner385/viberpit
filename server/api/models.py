"""GET /api/models — the curated model list for the session model selector.

Single source of truth lives in server.sessions.model_catalog; this route just
exposes it so the web has no hardcoded TS model list.
"""
from __future__ import annotations

from fastapi import APIRouter

from server.sessions import model_catalog

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("")
async def get_models(refresh: bool = False) -> list[dict]:
    """Return the curated model list as [{"id", "label"}]. No auth-sensitive data.

    Pass ``?refresh=1`` to re-probe the claude binary (e.g. after an upgrade)
    without restarting the server.
    """
    if refresh:
        model_catalog.refresh()
    return model_catalog.catalog()
