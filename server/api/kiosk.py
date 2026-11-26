"""Kiosk exchange: a display token in, a read-only display session out (#1408).

``POST /api/kiosk/session`` redeems a ``scope="display"`` token (minted by a full
identity at ``POST /api/assistant/tokens``) for an HttpOnly display cookie. The token
arrives in a form BODY — never a query string — and the answer is a 303 to a fixed
page, so it never re-enters a URL, an access log or a Referer. The route authenticates
by that body alone (the middleware exempts it from the identity requirement, not from
the Origin guard). The cookie session's life is clamped to the token's own expiry, and
every request it makes re-checks the token row, so revoking the token ends the display
on its next poll (``server/auth/middleware.py:resolve_display_session``).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select

from server.audit import record_audit
from server.auth.middleware import (DISPLAY_COOKIE_NAME, _sign_session_id,
                                    _validate_user_token)
from server.db import get_db
from server.db.models import AppSession, AssistantToken

router = APIRouter(prefix="/api/kiosk", tags=["kiosk"])

_DISPLAY_SESSION_TTL = timedelta(hours=24)
# The kiosk page every display lands on; `kiosk=1` keeps the client's layout guard on
# until the client derives its mode from the scope.
FIRST_PAGE = "/pipeline?kiosk=1"
_MAX_BODY = 4096


def _refuse(code: str, message: str, http: int) -> JSONResponse:
    return JSONResponse(status_code=http, content={"error": {"code": code, "message": message}})


@router.post("/session")
async def exchange(request: Request, db=Depends(get_db)):
    body = await request.body()
    token = (parse_qs(body[:_MAX_BODY].decode("utf-8", "replace")).get("token") or [""])[0]
    claims = await _validate_user_token(token, display=True) if token else None
    if claims is None:
        await record_audit(db, "kiosk.exchange.deny", "display:unknown", {})
        await db.commit()
        return _refuse("DISPLAY_TOKEN_INVALID",
                       "this display link has expired or been revoked", 401)
    row = (await db.execute(
        select(AssistantToken).where(AssistantToken.id == claims.token_id)
    )).scalar_one()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    token_expires = row.expires_at
    if token_expires.tzinfo is not None:
        token_expires = token_expires.astimezone(timezone.utc).replace(tzinfo=None)
    expires_at = min(now + _DISPLAY_SESSION_TTL, token_expires)
    session = AppSession(id=str(uuid.uuid4()), identity=claims.identity, created_at=now,
                         expires_at=expires_at, revoked_at=None, scope="display",
                         token_id=row.id)
    db.add(session)
    await record_audit(db, "kiosk.exchange", claims.identity,
                       {"tokenId": row.id, "label": row.label, "sessionId": session.id})
    await db.commit()
    response = RedirectResponse(FIRST_PAGE, status_code=303)
    response.set_cookie(
        DISPLAY_COOKIE_NAME, _sign_session_id(session.id), httponly=True, samesite="strict",
        secure=os.environ.get("VIBERPIT_DEV_INSECURE_COOKIE") != "1",
        max_age=int((expires_at - now).total_seconds()))
    response.headers["Referrer-Policy"] = "same-origin"
    return response
