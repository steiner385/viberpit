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

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request, WebSocket, status
from itsdangerous import BadSignature, TimestampSigner
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from server.auth.identity import host_is_loopback, verify_identity
from server.config import get_settings
from server.db.models import AppSession

if TYPE_CHECKING:
    from server.assistant.token import AssistantClaims, UserClaims

log = logging.getLogger(__name__)

# Fire-and-forget background tasks (e.g. the ccas2 last_used_at bump below) must be
# referenced somewhere or the event loop is free to garbage-collect them mid-run —
# this set plus a done-callback is the standard asyncio idiom to keep them alive
# without making the request wait on them.
_bg_tasks: set[asyncio.Task] = set()


def _spawn_bg(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)

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
    if client is None or not host_is_loopback(client.host):
        return None
    return verify_token(auth[len("Bearer "):], max_age_s=get_settings().assistant_idle_s * 4)


async def _bump_last_used_at(token_id: str) -> None:
    """Best-effort `AssistantToken.last_used_at` bump. Failure is logged, never
    surfaced — the request this token authenticated has already been decided by the
    time this runs, so a DB hiccup here must not turn a successful call into one."""
    try:
        from server.db import db_session as _bump_db_session  # noqa: PLC0415
        from server.db.models import AssistantToken  # noqa: PLC0415
        async with _bump_db_session() as db:
            await db.execute(
                update(AssistantToken).where(AssistantToken.id == token_id)
                .values(last_used_at=datetime.now(timezone.utc))
            )
            await db.commit()
    except Exception:
        log.debug("assistant external token last_used_at bump failed", exc_info=True)


async def _validate_user_token(token: str) -> "UserClaims | None":
    """Verify a `ccas2:…` token's signature AND its DB row (revocation, per-token
    expiry) — the full check both the HTTP dispatch below and the WS upgrade
    (`require_ws_identity`, Task 9.7) need, since `BaseHTTPMiddleware` never runs on a
    WebSocket upgrade and so cannot share `_dispatch_user_token` directly. Bumps
    `last_used_at` (fire-and-forget) on success; returns None on any failure without
    distinguishing why — callers that need a specific 401 message check
    `settings.assistant_external_tokens` themselves before calling this.
    """
    settings = get_settings()
    if not settings.assistant_external_tokens:
        return None

    from server.assistant.token import verify_user_token  # noqa: PLC0415
    claims: "UserClaims | None" = verify_user_token(
        token, max_age_s=settings.assistant_token_max_days * 86400)
    if claims is None:
        return None

    from server.db import db_session as _token_db_session  # noqa: PLC0415
    from server.db.models import AssistantToken  # noqa: PLC0415
    # Naive UTC: assistant_tokens.expires_at is DateTime(timezone=True), which round-
    # trips AWARE on Postgres but NAIVE on SQLite (SQLite has no real datetime type —
    # see server/db/clock.py's docstring on the same asymmetry). Comparing two aware
    # values or two naive values both work; comparing one of each raises TypeError, so
    # both sides are normalized to naive UTC before the comparison.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    async with _token_db_session() as db:
        row = (await db.execute(
            select(AssistantToken).where(AssistantToken.id == claims.token_id)
        )).scalar_one_or_none()
    valid = False
    if row is not None and row.revoked_at is None:
        expires_at = row.expires_at
        if expires_at.tzinfo is not None:
            expires_at = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
        valid = expires_at > now and hmac.compare_digest(row.token_hash, token_hash)
    if not valid:
        return None
    _spawn_bg(_bump_last_used_at(claims.token_id))
    return claims


async def _dispatch_user_token(request: Request, call_next, token: str):
    """Handle a `Bearer ccas2:…` request end to end (spec v2 §5/§7, Task 9.5).

    Unlike `ccas1:`, this token is accepted from any peer — it is meant for an
    external client reaching the edge over the tailnet, not just the local daemon —
    so the signature plus the DB row (revocation, per-token expiry, scope) is the
    whole trust boundary rather than a loopback check. Always returns a Response:
    the caller only reaches this function once the `ccas2:` prefix is confirmed, and
    a token that fails here has nothing else to fall back on (`verify_identity` can
    only ever fail it too), so failure paths terminate the request rather than
    falling through.
    """
    settings = get_settings()
    if not settings.assistant_external_tokens:
        return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"error": {
            "code": "UNAUTHORIZED", "message": "external assistant tokens are disabled"}})

    claims = await _validate_user_token(token)
    if claims is None:
        return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"error": {
            "code": "UNAUTHORIZED", "message": "invalid or expired token"}})

    from server.assistant.policy import route_allowed  # noqa: PLC0415
    # `operator=False` unconditionally: an external ccas2 token may never reach the
    # operator tier even when AppSettings.assistant_operator_tools is on for the
    # in-process assistant (spec v2 §7) — a `read` scope is further limited to GET.
    if not (route_allowed(request.method, request.url.path, operator=False)
            and (claims.scope == "act" or request.method.upper() == "GET")):
        return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"error": {
            "code": "ASSISTANT_ROUTE_FORBIDDEN",
            "message": "this token may not call this route"}})

    request.state.identity = claims.identity
    request.state.via = "mcp-external"
    request.state.assistant_scope = claims.scope
    request.state.app_session_id = None
    return await call_next(request)


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
            from server.assistant.policy import operator_tools_enabled, route_allowed  # noqa: PLC0415
            from server.db import db_session as _assistant_db_session  # noqa: PLC0415
            operator = False
            try:
                async with _assistant_db_session() as _db:
                    operator = await operator_tools_enabled(_db)
            except Exception:
                # DB unavailable: fail closed on the privileged operator tier rather
                # than fail the request — the ordinary assistant allowlist below is
                # unaffected (it needs no DB read at all).
                operator = False
            if not route_allowed(request.method, request.url.path, operator=operator):
                return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"error": {
                    "code": "ASSISTANT_ROUTE_FORBIDDEN",
                    "message": "the assistant may not call this route"}})
            request.state.identity = claims.identity
            request.state.via = "assistant"
            request.state.assistant_session_id = claims.session_id
            request.state.app_session_id = None      # no cookie session for a bearer call
            return await call_next(request)

        from server.assistant.token import USER_PREFIX  # noqa: PLC0415
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer " + USER_PREFIX):
            return await _dispatch_user_token(
                request, call_next, auth_header[len("Bearer "):])

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
    """FastAPI WebSocket dependency — closes the socket with 1008 if no identity.

    Also accepts a `Bearer ccas2:…` external assistant token (spec v2 Task 9.7,
    `ccswitch ask`): `BaseHTTPMiddleware` never runs on a WebSocket upgrade, so this
    dependency is the ONLY enforcement point for that path and must redo the same
    signature+DB check `_dispatch_user_token` does for HTTP. A `read`-scope token is
    refused here rather than admitted and gated per-message — unlike HTTP routes,
    `/ws` has no per-message-type allowlist, and every message type this socket
    accepts once attached (prompt, permissionResponse, answerQuestion) mutates.
    `websocket.state.via = "cli"` records how this connection authenticated so the
    message loop can audit permission answers `via="cli"` instead of the default
    (human-in-the-browser) `None`.
    """
    from server.assistant.token import USER_PREFIX  # noqa: PLC0415
    auth_header = websocket.headers.get("authorization", "")
    if auth_header.startswith("Bearer " + USER_PREFIX):
        claims = await _validate_user_token(auth_header[len("Bearer "):])
        if claims is None or claims.scope != "act":
            await websocket.close(code=1008)
            raise HTTPException(status_code=403,
                                 detail="WebSocket: invalid, expired, or read-scope token")
        websocket.state.via = "cli"
        return claims.identity

    identity = verify_identity(websocket)  # WebSocket also has .headers
    if identity is None:
        await websocket.close(code=1008)
        raise HTTPException(status_code=403, detail="WebSocket: no valid identity")
    return identity
