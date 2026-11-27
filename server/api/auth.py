"""Auth REST API router (T038).

Routes:
  GET  /api/auth/me      → current caller identity and authMode
  POST /api/auth/logout  → revoke the current AppSession (204)

Passkeys (#1410, server/auth/webauthn.py) — 404 unless VIBERPIT_WEBAUTHN_ENABLED:
  POST   /api/auth/webauthn/register/begin|complete  → enrol a passkey (FR-G12.8)
  POST   /api/auth/webauthn/login/begin|complete     → sign in; no identity needed
  GET    /api/auth/credentials                       → the caller's passkeys
  DELETE /api/auth/credentials/{id}                  → revoke one + its sessions

Every other route requires a verified identity via ``current_identity``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import update

from server.api.ratelimit import passkey_login_limiter
from server.audit import record_audit
from server.auth import webauthn
from server.auth.identity import human_sender
from server.auth.middleware import (DISPLAY_COOKIE_NAME, current_identity, _unsign_session_id,
                                    set_session_cookie,
                                    clear_session_cookies, read_session_cookie)
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
    """Return the current caller's verified identity, scope and auth mode.

    A display (kiosk) session reports ``scope: "display"`` and an identity of
    ``display:<label>`` — never the operator who minted the link (#1408).
    """
    scope = getattr(request.state, "scope", None) or "full"
    body: dict = {
        "identity": identity,
        "scope": scope,
        # How THIS request authenticated, as the middleware recorded it (#1410):
        # tailscale | passkey | display | bearer.
        "authMode": getattr(request.state, "auth_method", None) or "tailscale",
    }
    if scope == "display":
        # The GET routes this session may call. The client gates its own queries on it
        # (#3544) so a wall never polls an endpoint that can only 403.
        from server.assistant.policy import display_read_patterns  # noqa: PLC0415
        body["allowedReads"] = display_read_patterns()
    return body


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Revoke the current AppSession.

    Marks the server-side AppSession row as revoked and clears the cookie.
    """
    if getattr(request.state, "scope", None) == "display":
        # Ends the display session itself; the token stays valid for the next exchange.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.execute(
            update(AppSession)
            .where(AppSession.id == request.state.app_session_id,
                   AppSession.scope == "display", AppSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        response = JSONResponse(status_code=status.HTTP_204_NO_CONTENT, content=None)
        response.delete_cookie(DISPLAY_COOKIE_NAME)
        return response

    # Resolve the AppSession id from the signed cookie
    signed = read_session_cookie(request)
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
    clear_session_cookies(response)
    return response


# ---------------------------------------------------------------------------
# Passkeys (#1410)
# ---------------------------------------------------------------------------


def _require_passkeys() -> None:
    """While passkeys are off none of their routes exist (FR-G12.7: 404, not 403)."""
    from server.config import get_settings  # noqa: PLC0415
    if not get_settings().webauthn_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def _error(status_code: int, code: str, message: str,
           headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, headers=headers,
                        content={"error": {"code": code, "message": message}})


def _peer(request: Request) -> str:
    return request.client.host if request.client else "<unknown>"


def _login_throttled(request: Request, identity: str | None) -> JSONResponse | None:
    """FR-G12.10: the shared limiter, per peer address and per claimed identity."""
    keys = [f"peer:{_peer(request)}"] + ([f"identity:{identity}"] if identity else [])
    waits = [w for w in (passkey_login_limiter.check(k) for k in keys) if w is not None]
    if not waits:
        return None
    import math  # noqa: PLC0415
    return _error(status.HTTP_429_TOO_MANY_REQUESTS, "PASSKEY_RATE_LIMITED",
                  "too many sign-in attempts; wait and try again",
                  headers={"Retry-After": str(max(1, math.ceil(max(waits))))})


async def _audit_failure(db, request: Request, identity: str | None, reason: str) -> None:
    await record_audit(db, "auth.passkey.fail", identity or "-",
                       {"reason": reason, "peer": _peer(request), "path": request.url.path})
    await db.commit()


async def _enrolling_identity(request: Request) -> str:
    identity = getattr(request.state, "identity", None)
    human = await asyncio.to_thread(human_sender, request)
    if not webauthn.may_enrol(identity=identity,
                              scope=getattr(request.state, "scope", None),
                              auth_method=getattr(request.state, "auth_method", None),
                              human=human):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={
            "code": "PASSKEY_ENROL_FORBIDDEN",
            "message": "sign in over Tailscale (or with a passkey) as yourself to add a passkey"})
    return str(identity)


@router.post("/webauthn/register/begin", dependencies=[Depends(_require_passkeys)])
async def passkey_register_begin(request: Request, db=Depends(get_db),
                                 _identity: str = Depends(current_identity)):
    identity = await _enrolling_identity(request)
    try:
        return await webauthn.begin_registration(db, identity)
    except webauthn.PasskeyError as exc:
        return _error(exc.status, exc.code, str(exc))


@router.post("/webauthn/register/complete", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(_require_passkeys)])
async def passkey_register_complete(request: Request, payload: dict = Body(...),
                                    db=Depends(get_db),
                                    _identity: str = Depends(current_identity)):
    identity = await _enrolling_identity(request)
    try:
        row = await webauthn.complete_registration(
            db, identity, str(payload.get("ceremonyId") or ""),
            payload.get("credential") or {}, payload.get("label"))
    except webauthn.PasskeyError as exc:
        await _audit_failure(db, request, identity, exc.audit)
        return _error(exc.status, exc.code, str(exc))
    await db.flush()
    await record_audit(db, "auth.passkey.register", identity, {"credential": row.id})
    await db.commit()
    return JSONResponse(status_code=status.HTTP_201_CREATED,
                        content=webauthn.credential_summary(row))


@router.post("/webauthn/login/begin", dependencies=[Depends(_require_passkeys)])
async def passkey_login_begin(request: Request, payload: dict | None = Body(default=None),
                              db=Depends(get_db)):
    identity = str((payload or {}).get("identity") or "").strip() or None
    throttled = _login_throttled(request, identity)
    if throttled is not None:
        await _audit_failure(db, request, identity, "rate_limited")
        return throttled
    try:
        return await webauthn.begin_authentication(db, identity)
    except webauthn.PasskeyError as exc:
        return _error(exc.status, exc.code, str(exc))


@router.post("/webauthn/login/complete", dependencies=[Depends(_require_passkeys)])
async def passkey_login_complete(request: Request, payload: dict = Body(...),
                                 db=Depends(get_db)):
    throttled = _login_throttled(request, None)
    if throttled is not None:
        await _audit_failure(db, request, None, "rate_limited")
        return throttled
    credential = payload.get("credential")
    try:
        if not isinstance(credential, dict):
            raise webauthn.PasskeyError(400, "PASSKEY_CEREMONY_INVALID",
                                        "missing credential", audit="assertion_missing")
        row, session = await webauthn.complete_authentication(
            db, str(payload.get("ceremonyId") or ""), credential)
    except webauthn.PasskeyError as exc:
        await db.rollback()
        await _audit_failure(db, request, None, exc.audit)
        return _error(exc.status, exc.code, str(exc))
    await db.flush()
    await record_audit(db, "auth.passkey.login", row.identity,
                       {"credential": row.id, "peer": _peer(request)})
    # Committed before the cookie leaves: the browser's very next request must find it.
    await db.commit()
    response = JSONResponse(content={"identity": row.identity, "authMode": webauthn.PASSKEY})
    set_session_cookie(response, session.id)
    return response


@router.get("/credentials", dependencies=[Depends(_require_passkeys)])
async def passkey_credentials(request: Request, db=Depends(get_db),
                              identity: str = Depends(current_identity)) -> list[dict]:
    if getattr(request.state, "scope", None) == "display":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="display scope")
    return [webauthn.credential_summary(r) for r in await webauthn.live_credentials(db, identity)]


@router.delete("/credentials/{credential_id}", status_code=status.HTTP_204_NO_CONTENT,
               dependencies=[Depends(_require_passkeys)])
async def passkey_revoke(credential_id: str, request: Request, db=Depends(get_db),
                         identity: str = Depends(current_identity)):
    if getattr(request.state, "scope", None) == "display" \
            or getattr(request.state, "auth_method", None) not in ("tailscale", webauthn.PASSKEY):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not allowed")
    if not await webauthn.revoke_credential(db, identity, credential_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such passkey")
    await record_audit(db, "auth.passkey.revoke", identity, {"credential": credential_id})
    await db.commit()
    return JSONResponse(status_code=status.HTTP_204_NO_CONTENT, content=None)
