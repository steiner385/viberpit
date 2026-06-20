"""Auth REST API router (T038).

Routes:
  GET  /api/auth/me      → current caller identity and authMode
  POST /api/auth/logout  → revoke the current AppSession (204)

All routes require a verified identity via ``current_identity``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select, update

from server.auth.middleware import current_identity, _COOKIE_NAME, _unsign_session_id
from server.db import get_db
from server.db.models import AppSession

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/me")
async def auth_me(
    request: Request,
    identity: str = Depends(current_identity),
) -> dict:
    """Return the current caller's verified identity and auth mode."""
    return {
        "identity": identity,
        "authMode": "tailscale",
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Revoke the current AppSession.

    Marks the server-side AppSession row as revoked and clears the cookie.
    """
    # Resolve the AppSession id from the signed cookie
    signed = request.cookies.get(_COOKIE_NAME)
    if signed:
        session_id = _unsign_session_id(signed)
        if session_id:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            await db.execute(
                update(AppSession)
                .where(
                    AppSession.id == session_id,
                    AppSession.identity == identity,
                    AppSession.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )

    # Return 204 with cookie cleared
    response = JSONResponse(status_code=status.HTTP_204_NO_CONTENT, content=None)
    response.delete_cookie(_COOKIE_NAME)
    return response
