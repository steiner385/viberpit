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

from ccswitch import brand
from server.auth.identity import client_node_name, host_is_loopback, verify_identity
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

# Cookie name. The rebrand (#1368) flips the EMITTED name while BOTH spellings stay
# readable for one release: the deployment that flips this must not log out a user
# whose tab is already open, and a browser mid-migration carries only the old one.
# Ordered new-first, so a browser that happens to hold both prefers the new cookie —
# they address the same `app_sessions` row, and the legacy one simply ages out of the
# jar within `_SESSION_TTL` because nothing renews it any more.
_COOKIE_NAME = f"{brand.SLUG}_session"
_LEGACY_COOKIE_NAME = f"{brand.LEGACY_SLUG}_session"
_COOKIE_NAMES = (_COOKIE_NAME, _LEGACY_COOKIE_NAME)

# Attribution headers the assistant's loopback client stamps on every call
# (`server/assistant/client.py`). DIAGNOSTICS ONLY — they say how a call was made for
# the benefit of access logs and proxies; authorization comes from the bearer token
# alone, never from a header the caller controls. Emission is single-spelled (the new
# brand); `request_attribution` below is the accept side and takes either.
VIA_HEADER = f"X-{brand.NAME}-Via"
ASSISTANT_SESSION_HEADER = f"X-{brand.NAME}-Assistant-Session"
# Wire prefix of the daemon-minted assistant bearer token (server/assistant/token.py
# re-exports it as PREFIX; defined here because that module imports this one).
ASSISTANT_TOKEN_PREFIX = "ccas1:"
# The pre-rebrand spellings. Not derivable from `brand.LEGACY_SLUG`: the old headers
# used a CamelCase the slug does not carry. Retired with the rest of #1368's compat.
LEGACY_VIA_HEADER = "X-CCSwitch-Via"
LEGACY_ASSISTANT_SESSION_HEADER = "X-CCSwitch-Assistant-Session"

# Session lifetime
_SESSION_TTL = timedelta(hours=24)


def read_session_cookie(request: Request) -> str | None:
    """The signed session cookie under EITHER brand spelling, new first.

    The one place the cookie dual-accept is spelled. Both the middleware and
    `/api/auth/logout` go through it: a logout that only looked for the new name
    would answer 204 while leaving the row live and the legacy cookie in the jar —
    a "signed out" that did not sign anything out.
    """
    for name in _COOKIE_NAMES:
        signed = request.cookies.get(name)
        if signed:
            return signed
    return None


def clear_session_cookies(response) -> None:
    """Drop BOTH spellings — a jar mid-migration may hold either or both."""
    for name in _COOKIE_NAMES:
        response.delete_cookie(name)


def _header_either(headers, new: str, legacy: str) -> str | None:
    """*headers* value under the new brand spelling, else the legacy one, else None."""
    return headers.get(new) or headers.get(legacy) or None


def request_attribution(headers) -> tuple[str | None, str | None]:
    """``(via, assistant_session_id)`` from the attribution headers of *headers*.

    The ONE place the header dual-accept is spelled, for the same reason
    ``brand.mcp_tool_stem`` is the one place the MCP-prefix dual-accept is: a reader
    that grew its own ``headers.get("X-…")`` would miss one spelling for the whole
    compat window and lose the attribution silently. Takes any mapping, so an
    ``httpx.Headers`` (case-insensitive, as Starlette's is) works unchanged.
    """
    return (_header_either(headers, VIA_HEADER, LEGACY_VIA_HEADER),
            _header_either(headers, ASSISTANT_SESSION_HEADER,
                           LEGACY_ASSISTANT_SESSION_HEADER))


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


def _cross_process_signing_secret() -> str | bytes:
    """Key for tokens minted in ONE process and verified in ANOTHER: the daemon-minted,
    edge-verified ``ccas1`` assistant token and ``ccsys1`` internal token.

    ``_signing_secret`` above serves the session cookie — minted and verified by the
    edge alone — and prefers the session secret, an edge-local setting. The daemon
    reads a dedicated env file (runbook: onboarding-a-session-runner) that carries
    only the master key, the ONE secret the runbook requires to match on every host. A daemon with the master key alone and an edge with both
    therefore signed with different keys: every assistant token failed signature at
    the edge and, in LAN-open mode, the call was silently admitted as the ``lan@local``
    guest (2026-09-22 — sessions the drawer created were owned by the guest and the
    real user's every AskUserQuestion answer was refused). Prefer the fleet-shared
    master key; fall back to the cookie chain only when no master key is configured.
    """
    settings = get_settings()
    if settings.master_key:
        return settings.master_key
    return _signing_secret()


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
    from server.assistant.token import verify_token  # noqa: PLC0415
    from server.config import get_settings  # noqa: PLC0415
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer " + ASSISTANT_TOKEN_PREFIX):
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


async def _dispatch_internal_token(request: Request, call_next, token: str):
    """Handle a `Bearer ccsys1:…` request end to end.

    Loopback-only (the token is minted for the daemon on THIS host, same posture as
    `ccas1`) and restricted to the routes it exists for — the primary daemon's own
    automation loops asking their co-located edge to place a new session on the
    best-ranked host, or to certify a harvested one-shot session on the host that
    owns it (server/sessions/internal_dispatch.py), rather than acting only on
    whichever host happens to run the automation loop itself. A wrong route or a
    non-loopback peer gets 403/401 rather than falling through to Tailscale identity
    verification, which this caller has none of.
    """
    from server.auth.identity import host_is_loopback  # noqa: PLC0415
    from server.auth.internal_token import (  # noqa: PLC0415
        ALLOWED_ROUTES, verify_token,
    )
    from server.sessions.types import SYSTEM_IDENTITY  # noqa: PLC0415

    client = request.client
    if client is None or not host_is_loopback(client.host):
        return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"error": {
            "code": "UNAUTHORIZED", "message": "internal token is loopback-only"}})
    if not verify_token(token):
        return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"error": {
            "code": "UNAUTHORIZED", "message": "invalid or expired internal token"}})
    if (request.method, request.url.path) not in ALLOWED_ROUTES:
        return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"error": {
            "code": "INTERNAL_ROUTE_FORBIDDEN",
            "message": "this token may not call this route"}})
    request.state.identity = SYSTEM_IDENTITY
    request.state.via = "internal"
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
        if claims is None and request.headers.get("authorization", "").startswith(
                "Bearer " + ASSISTANT_TOKEN_PREFIX):
            # A PRESENTED assistant token that does not verify (bad signature — e.g.
            # the daemon and edge signing with different keys —, expired, or sent
            # from a non-loopback peer) is refused here. Falling through to the
            # ordinary identity resolution below let it collect the LAN-open guest
            # identity instead: every route open, sessions owned by `lan@local`,
            # and not one log line (2026-09-22). Loud and closed, like ccsys1.
            peer = request.client.host if request.client else "<unknown>"
            log.warning("assistant token refused (invalid, expired, or non-loopback peer %s) "
                        "path=%s", peer, request.url.path)
            return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"error": {
                "code": "ASSISTANT_TOKEN_INVALID",
                "message": "invalid or expired assistant token"}})
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

        from server.auth.internal_token import PREFIX as INTERNAL_PREFIX  # noqa: PLC0415
        if auth_header.startswith("Bearer " + INTERNAL_PREFIX):
            return await _dispatch_internal_token(
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

    # Try to resolve from cookie — under either brand spelling, so the deployment
    # that flips the emitted name (#1368) keeps every open tab signed in.
    signed = read_session_cookie(request)
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


def current_client_node(request: Request) -> str | None:
    """FastAPI dependency — the tailnet node the caller's browser is on, or None.

    Soft: it never rejects a request. Only the session-create route asks for it (an
    extra whois subprocess per create, not per request), so it is a dependency
    rather than middleware state.
    """
    return client_node_name(request)


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
