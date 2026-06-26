"""Pipeline REST API — the full PR population for the board view.

GET /api/prs → every open + recently-merged PR across watched repos, each
joined to its authoring ccswitch session (or session=null). Requires a verified
identity. Reads the in-memory population cache (cheap); no GitHub call per request.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from server.auth.middleware import current_identity

router = APIRouter(prefix="/api/prs", tags=["pipeline"])


@router.get("")
async def list_prs(
    request: Request,
    _identity: str = Depends(current_identity),
) -> list[dict]:
    orch = request.app.state.sessions
    items = await orch.all_prs()
    return [p.to_dict() for p in items]
