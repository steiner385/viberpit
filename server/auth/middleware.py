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

import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request, WebSocket, status
from itsdangerous import BadSignature, TimestampSigner
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from server.auth.identity import LOOPBACK_PEERS, verify_identity
from server.config import get_settings
from server.db.models import AppSession

if TYPE_CHECKING:
    from server.assistant.token import AssistantClaims

# Paths that bypass identity enforcement (unauthenticated probes).
_EXEMPT_PATHS = frozenset({"/healthz"})

# Cookie name
_COOKIE_NAME = "ccswitch_session"
# Session lifetime
_SESSION_TTL = timedelta(hours=24)


# Per-process random fallback so we NEVER sign with a hardcoded secret. Cookies
# minted under this secret simply don't survive a restart (acceptable: identity is
# re-verified from the header on every request, so the cookie is bookkeeping only).
_PROCESS_SECRET = secrets.token_urlsafe(32)
_warned_fallback = False


def _signing_secret() -> str | bytes:
    settings = get_settings()
    if settings.session_secret:
        return settings.session_secret
    if settings.master_key:
        return settings.master_key
    # Fall back to the envelope master key (env or keyring) if configured.
    try:
        from ccswitch.crypto import master_key as _envelope_key  # noqa: PLC0415

        k = _envelope_key()
        if k:
            return k
    except Exception:
        pass
    global _warned_fallback
    if not _warned_fallback:
        import logging
        logging.getLogger(__name__).warning(
            "No CCSWITCH_SESSION_SECRET/MASTER_KEY configured; signing session "
            "cookies with an ephemeral per-process secret (cookies reset on restart)."
        )
        _warned_fallback = True
    return _PROCESS_SECRET


def _get_signer() -> TimestampSigner:
    return TimestampSigner(_signing_secret())


def _sign_session_id(session_id: str) -> str:
    return _get_signer().sign(session_id).decode()


def _unsign_session_id(signed: str) -> str | None:
    """Verify and extract session_id; None on any error."""
    try:
        # max_age=None — we rely on DB expiry, not cookie age
        return _get_signer().unsign(signed).decode()
    except (BadSignature, Exception):
        return None




def _assistant_claims(request: Request) -> "AssistantClaims | None":
    """Claims from a `Bearer ccas1:…` header, or None. Loopback peers only: the token is
    minted for the daemon on THIS host and must never be usable from the tailnet."""
    from server.assistant.token import PREFIX, verify_token  # noqa: PLC0415
    from server.config import get_settings  # noqa: PLC0415
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer " + PREFIX):
        return None
    client = request.client
    if client is None or client.host not in LOOPBACK_PEERS:
        return None
    return verify_token(auth[len("Bearer "):], max_age_s=get_settings().assistant_idle_s * 4)


class IdentityMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that enforces identity on every request.

    - Resolves identity via Tailscale headers.
    - Gets-or-creates a server-side AppSession; issues/renews the signed cookie.
    - Attaches request.state.identity for downstream use.
    - Returns 401 JSON on failure (HTTP); for WS upgrades the middleware cannot
      close the socket directly — the `require_ws_identity` dependency handles that.
    """

    async def dispatch(self, request: Request, call_next):
        # Unauthenticated liveness/schema probe (used by auto-deploy health-check).
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        claims = _assistant_claims(request)
        if claims is not None:
            from server.assistant.policy import route_allowed  # noqa: PLC0415
            if not route_allowed(request.method, request.url.path):
                return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"error": {
                    "code": "ASSISTANT_ROUTE_FORBIDDEN",
                    "message": "the assistant may not call this route"}})
            request.state.identity = claims.identity
            request.state.via = "assistant"
            request.state.assistant_session_id = claims.session_id
            request.state.app_session_id = None      # no cookie session for a bearer call
            return await call_next(request)

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

        # Set/renew cookie on response.
        # secure=True by default — set CCSWITCH_DEV_INSECURE_COOKIE=1 only for
        # plain-HTTP local dev or tests that inspect cookie attributes.
        session_id = getattr(request.state, "app_session_id", None)
        if session_id:
            signed = _sign_session_id(session_id)
            _secure = os.environ.get("CCSWITCH_DEV_INSECURE_COOKIE") != "1"
            response.set_cookie(
                _COOKIE_NAME,
                signed,
                httponly=True,
                samesite="strict",
                secure=_secure,
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

    # No usable cookie: reuse this identity's newest live session rather than
    # inserting a row. Every cookie-less caller (API polls, WS upgrades) previously
    # minted one — 45k rows/day for a single identity, 2.2M rows total.
    # Scoped to `identity`, which the Tailscale middleware has already verified, so
    # this only ever returns a row the same principal owns.
    reusable = (await db.execute(
        select(AppSession)
        .where(
            AppSession.identity == identity,
            AppSession.revoked_at.is_(None),
            AppSession.expires_at > now,
        )
        .order_by(AppSession.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if reusable is not None:
        return reusable

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
# Session pruning (janitor)
# ---------------------------------------------------------------------------

# How far past expiry (or revocation) before we delete a row. A short grace
# window (7 days) keeps the table tiny while leaving room for post-mortem
# inspection of recent auth events. Long-expired stock (the 42k rows observed
# in prod) is reclaimed on the first pass. (#830)
_PRUNE_GRACE = timedelta(days=7)


async def prune_app_sessions(db: AsyncSession) -> int:
    """Delete app_sessions rows that expired or were revoked more than _PRUNE_GRACE ago.

    Returns the number of rows deleted. Safe to call concurrently — DELETE WHERE
    is atomic; duplicated deletes are harmless no-ops.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - _PRUNE_GRACE
    result = await db.execute(
        delete(AppSession).where(
            (AppSession.expires_at < cutoff) | (AppSession.revoked_at < cutoff)
        )
    )
    await db.flush()
    return int(result.rowcount)  # type: ignore[attr-defined]


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
