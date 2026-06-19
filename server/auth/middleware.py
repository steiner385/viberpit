"""Identity middleware + FastAPI dependency (T029).

Every HTTP route and WebSocket upgrade requires a verified Tailscale identity.
On success the identity is:
  1. Attached to request.state.identity
  2. Backed by a server-side AppSession (signed HttpOnly cookie, 24h absolute expiry)

HTTP 401 is returned when identity cannot be verified.
WebSocket connections are closed with code 1008 (Policy Violation) when identity
cannot be verified.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, WebSocket, status
from itsdangerous import BadSignature, TimestampSigner
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.websockets import WebSocketState

from server.auth.identity import verify_identity
from server.config import get_settings
from server.db import get_db
from server.db.models import AppSession

# Cookie name
_COOKIE_NAME = "ccswitch_session"
# Session lifetime
_SESSION_TTL = timedelta(hours=24)


def _get_signer() -> TimestampSigner:
    settings = get_settings()
    secret = settings.master_key or "ccswitch-dev-secret-change-me"
    return TimestampSigner(secret)


def _sign_session_id(session_id: str) -> str:
    return _get_signer().sign(session_id).decode()


def _unsign_session_id(signed: str) -> str | None:
    """Verify and extract session_id; None on any error."""
    try:
        # max_age=None — we rely on DB expiry, not cookie age
        return _get_signer().unsign(signed).decode()
    except (BadSignature, Exception):
        return None


class IdentityMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that enforces identity on every request.

    - Resolves identity via Tailscale headers.
    - Gets-or-creates a server-side AppSession; issues/renews the signed cookie.
    - Attaches request.state.identity for downstream use.
    - Returns 401 JSON on failure (HTTP); for WS upgrades the middleware cannot
      close the socket directly — the `require_ws_identity` dependency handles that.
    """

    async def dispatch(self, request: Request, call_next):
        # Skip internal paths that don't need auth (none currently, but future-proof)
        identity = verify_identity(request)
        if identity is None:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"error": {"code": "UNAUTHORIZED", "message": "No valid identity"}},
            )

        request.state.identity = identity

        # Get-or-create AppSession via cookie
        # We handle DB session manually here since we're in middleware (no DI)
        try:
            from server.db import _session_factory  # noqa: PLC0415
            if _session_factory is not None:
                async with _session_factory() as db_session:
                    app_session = await _resolve_or_create_app_session(
                        request, db_session, identity
                    )
                    await db_session.commit()
                    request.state.app_session_id = app_session.id
        except Exception:
            # DB unavailable: still allow the request through (identity is verified)
            request.state.app_session_id = None

        response = await call_next(request)

        # Set/renew cookie on response
        session_id = getattr(request.state, "app_session_id", None)
        if session_id:
            signed = _sign_session_id(session_id)
            response.set_cookie(
                _COOKIE_NAME,
                signed,
                httponly=True,
                samesite="strict",
                secure=False,  # flip to True when behind HTTPS/tailscale serve
                max_age=int(_SESSION_TTL.total_seconds()),
            )

        return response


async def _resolve_or_create_app_session(
    request: Request, db: AsyncSession, identity: str
) -> AppSession:
    """Look up an existing valid AppSession from the signed cookie, or create one."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC for DB

    # Try to resolve from cookie
    signed = request.cookies.get(_COOKIE_NAME)
    if signed:
        session_id = _unsign_session_id(signed)
        if session_id:
            result = await db.execute(
                select(AppSession).where(
                    AppSession.id == session_id,
                    AppSession.identity == identity,
                    AppSession.revoked_at.is_(None),
                    AppSession.expires_at > now,
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                return existing

    # Create new
    new_session = AppSession(
        id=str(uuid.uuid4()),
        identity=identity,
        created_at=now,
        expires_at=now + _SESSION_TTL,
        revoked_at=None,
    )
    db.add(new_session)
    return new_session


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def current_identity(request: Request) -> str:
    """FastAPI dependency — returns the verified identity, already attached by middleware."""
    identity = getattr(request.state, "identity", None)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "No valid identity"},
        )
    return identity


async def require_ws_identity(websocket: WebSocket) -> str:
    """FastAPI WebSocket dependency — closes the socket with 1008 if no identity."""
    identity = verify_identity(websocket)  # WebSocket also has .headers
    if identity is None:
        await websocket.close(code=1008)
        raise HTTPException(status_code=403, detail="WebSocket: no valid identity")
    return identity
