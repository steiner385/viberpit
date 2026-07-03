"""Accounts REST API router (T037).

Routes:
  GET    /api/accounts                  → list accounts with live usage (no tokens)
  POST   /api/accounts/{name}/refresh   → refresh one account's usage
  DELETE /api/accounts/{name}           → remove account (204)
  PATCH  /api/accounts/{name}           → set/clear nickname
  POST   /api/accounts/{name}/reauth         → start re-authentication (202)
  GET    /api/accounts/{name}/reauth         → get reauth status
  POST   /api/accounts/{name}/reauth/cancel  → cancel a running reauth

All routes require a verified identity via ``current_identity``.
Response NEVER includes token/credential material (FR-023).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from ccswitch.accounts import list_accounts, remove_account, set_nickname
from ccswitch import paths
from ccswitch.account_names import assert_valid_account_name, InvalidAccountName
from ccswitch.config import load_config
from ccswitch.selector import rank_accounts
from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.reauth import ReauthBusy, ReauthNoKey
from server.time_iso import iso_utc

if TYPE_CHECKING:
    from server.sessions.facade import InProcessOrchestratorAdapter

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

# A single account's usage fetch must never stall or 500 the whole accounts list.
# The daemon serves usage from the poller cache (cheap); this ceiling only trips
# when the daemon control loop is wedged/slow — in which case that one account
# degrades to "unknown" (None) rather than blocking the dashboard or bubbling a
# raw ReadTimeout into an unhandled 500 (the GET /api/accounts wedge, incident
# 2026-07-02). Overridable for tests / tuning.
USAGE_FETCH_TIMEOUT = float(os.environ.get("CCSWITCH_USAGE_FETCH_TIMEOUT", "8.0"))


async def _safe_usage(orch, name: str, timeout: float):
    """Fetch one account's usage, degrading to None on timeout or any error.

    Isolates a single slow/failing account so it can neither stall the other
    accounts' fetches nor turn GET /api/accounts into a 500. ``usage=None`` is
    already the wire contract for "unknown/failed fetch" (_usage_to_dict).
    """
    try:
        return await asyncio.wait_for(orch.usage(name), timeout)
    except asyncio.TimeoutError:
        log.warning("usage fetch for %s timed out after %.1fs — degrading to None",
                    name, timeout)
        return None
    except Exception:  # noqa: BLE001 — one account must never fail the whole list
        log.warning("usage fetch for %s failed — degrading to None", name, exc_info=True)
        return None


async def _gather_usages(orch, names: list[str], timeout: float) -> dict[str, Any]:
    """Fetch usage for many accounts concurrently, each isolated (None on failure).

    Concurrent (not serial) so total latency is the slowest single account, not
    the sum — and one hung account no longer blocks the rest behind it.
    """
    results = await asyncio.gather(*(_safe_usage(orch, n, timeout) for n in names))
    return dict(zip(names, results))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _usage_to_dict(usage) -> dict[str, Any] | None:
    """Convert a ccswitch.oauth.Usage dataclass to the wire-format dict.

    Returns None if usage is None (unknown/failed fetch).
    Never includes token material.
    """
    if usage is None:
        return None
    return {
        "session": {
            "pct": usage.session_pct,
            "resetsAt": iso_utc(usage.session_resets_at),
        },
        "weekly": {
            "pct": usage.weekly_pct,
            "resetsAt": iso_utc(usage.weekly_resets_at),
        },
    }


def _account_response(
    name: str,
    status_str: str,
    usage,
    nickname: str | None = None,
    live_sessions: dict[str, int] | None = None,
    rank: int | None = None,
    rank_reason: str | None = None,
) -> dict[str, Any]:
    """Build the account wire format — never includes tokens."""
    return {
        "name": name,
        "status": status_str,
        "usage": _usage_to_dict(usage),
        "nickname": nickname,
        "liveSessions": live_sessions or {"active": 0, "idle": 0},
        "rank": rank,
        "rankReason": rank_reason,
    }


def _get_orchestrator(request: Request) -> "InProcessOrchestratorAdapter":
    return request.app.state.sessions


def _get_reauth(request: Request):
    return request.app.state.reauth


def _check_name_and_slot(name: str) -> None:
    """Assert the account name is valid and the slot exists.

    Raises HTTPException 404 if the name is invalid (path traversal, hidden
    file, bad chars) OR if no credential slot exists for the name.
    """
    try:
        assert_valid_account_name(name)
    except InvalidAccountName:
        raise HTTPException(status_code=404, detail=f"Account '{name}' not found")

    if not paths.slot_path(name).exists():
        raise HTTPException(status_code=404, detail=f"Account '{name}' not found")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
async def get_accounts(
    request: Request,
    _identity: str = Depends(current_identity),
) -> list[dict]:
    """Return all accounts with live usage, live session counts, and next-pick rank.

    Never includes token or credential material.
    """
    orch = _get_orchestrator(request)
    accounts_info = list_accounts()

    # Fetch live usage for every account concurrently, each isolated: a single
    # wedged/slow account degrades to None instead of serially stalling the list
    # or bubbling a raw ReadTimeout into a 500 (poller dedupes concurrent fetches).
    usages = await _gather_usages(
        orch, [acct["name"] for acct in accounts_info], USAGE_FETCH_TIMEOUT)

    # Live session counts (display) + weighted load (ranking input).
    counts = await orch.live_session_counts()
    load = await orch.live_session_load()

    # Rank for display: ignore transient soft reservations so ranks stay stable
    # (a just-picked account would otherwise vanish for reservation_ttl_sec).
    ranked, _earliest = rank_accounts(
        usages, reserved=frozenset(), cfg=load_config(),
        now=datetime.now(timezone.utc), load=load,
    )
    rank_by_name = {r.name: (r.rank, r.reason) for r in ranked}

    result = []
    for acct in accounts_info:
        name = acct["name"]
        rank, reason = rank_by_name.get(name, (None, None))
        result.append(_account_response(
            name,
            acct.get("status", "unknown"),
            usages[name],
            acct.get("nickname"),
            live_sessions=counts.get(name) or {"active": 0, "idle": 0},
            rank=rank,
            rank_reason=reason,
        ))
    return result


@router.post("/{name}/refresh")
async def refresh_account(
    name: str,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Refresh and return fresh usage for a single account.

    Never includes token material.
    """
    _check_name_and_slot(name)

    orch = _get_orchestrator(request)

    # Force a fresh fetch (bypasses cache by going through the poller lock).
    # Bounded + isolated: a wedged daemon degrades this to None rather than 500.
    usage = await _safe_usage(orch, name, USAGE_FETCH_TIMEOUT)

    # Determine status and nickname from the ledger
    accounts_info = list_accounts()
    status_str = "unknown"
    nickname = None
    for acct in accounts_info:
        if acct["name"] == name:
            status_str = acct.get("status", "unknown")
            nickname = acct.get("nickname")
            break

    counts = await orch.live_session_counts()
    return _account_response(
        name, status_str, usage, nickname,
        live_sessions=counts.get(name) or {"active": 0, "idle": 0},
    )


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    name: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Remove an account slot and its config directory. Returns 204."""
    _check_name_and_slot(name)

    remove_account(name)

    await record_audit(
        db=db,
        type="account.remove",
        identity=identity,
        detail={"account": name},
    )


class NicknameBody(BaseModel):
    nickname: str | None = None


@router.patch("/{name}")
async def set_account_nickname(
    name: str,
    body: NicknameBody,
    _identity: str = Depends(current_identity),
) -> dict:
    """Set or clear a display nickname for an account.

    A blank or whitespace-only nickname clears the existing nickname.
    Returns 404 if the account name is invalid or slot does not exist.
    """
    _check_name_and_slot(name)

    cleaned = (body.nickname or "").strip() or None
    set_nickname(name, cleaned)

    accounts_info = list_accounts()
    status_str = "unknown"
    stored_nickname = None
    for acct in accounts_info:
        if acct["name"] == name:
            status_str = acct.get("status", "unknown")
            stored_nickname = acct.get("nickname")
            break

    return _account_response(name, status_str, None, stored_nickname)


# ---------------------------------------------------------------------------
# Add-account routes
#
# Adding an account is the same interactive browser-OAuth login as reauth, just
# with the opposite precondition: the slot must NOT already exist. It reuses the
# global ReauthManager so an add and a reauth can never run at once (there is a
# single reauth Chrome/DISPLAY on the host). The slot does not exist until the
# login succeeds, so add status/cancel must NOT require the slot to exist.
# ---------------------------------------------------------------------------


class AddAccountBody(BaseModel):
    name: str


@router.post("/add", status_code=status.HTTP_202_ACCEPTED)
async def add_account_start(
    body: AddAccountBody,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Start an interactive login that creates a new account slot.

    Returns 202 + state dict on success.
    Returns 400 if the requested name is malformed (bad chars, path traversal).
    Returns 409 if an account with that name already exists, or if another
        interactive login (add or reauth) is already running.
    Returns 503 if no encryption key is configured.
    """
    name = body.name
    try:
        assert_valid_account_name(name)
    except InvalidAccountName:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_name",
                "name": name,
                "message": (
                    "Account name must be a short identifier — letters, numbers, "
                    ". _ - only (no @ or spaces). It's not an email; add a friendly "
                    "nickname afterwards."
                ),
            },
        )

    if paths.slot_path(name).exists():
        raise HTTPException(
            status_code=409,
            detail={"error": "account_exists", "name": name,
                    "message": f"Account '{name}' already exists"},
        )

    reauth = _get_reauth(request)
    try:
        state = await reauth.start(name, identity)
    except ReauthBusy as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "reauth_busy",
                "holdingIdentity": exc.holding_identity,
                "startedAt": exc.started_at,
            },
        )
    except ReauthNoKey:
        raise HTTPException(
            status_code=503,
            detail={"error": "no_encryption_key"},
        )

    await record_audit(
        db=db,
        type="account.add.start",
        identity=identity,
        detail={"account": name},
    )

    return state.to_dict()


@router.get("/add")
async def add_account_status(
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Return the current interactive-login state for an in-flight add.

    The global ReauthManager state is shared with reauth; the name is irrelevant
    (one login at a time). Does not require a slot to exist.
    """
    reauth = _get_reauth(request)
    return (await reauth.status("")).to_dict()


@router.post("/add/cancel")
async def add_account_cancel(
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Cancel a running add. Returns the resulting state dict."""
    reauth = _get_reauth(request)
    pre_cancel_state = await reauth.status("")
    state = await reauth.cancel("")

    if pre_cancel_state.state != "idle":
        await record_audit(
            db=db,
            type="account.add.cancel",
            identity=identity,
            detail={},
        )

    return state.to_dict()


# ---------------------------------------------------------------------------
# Reauth routes
# ---------------------------------------------------------------------------


@router.post("/{name}/reauth", status_code=status.HTTP_202_ACCEPTED)
async def reauth_start(
    name: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
    mode: str = "login",
) -> dict:
    """Start a re-authentication flow for the named account.

    Query params:
      mode: "login" (default, browser OAuth flow) or "setup_token" (PTY-driven flow).

    Returns 202 + state dict on success.
    Returns 409 if another reauth is already running.
    Returns 503 if no encryption key is configured.
    Returns 404 if the account name is invalid or the slot does not exist.
    """
    _check_name_and_slot(name)

    reauth = _get_reauth(request)
    try:
        state = await reauth.start(name, identity, mode=mode)
    except ReauthBusy as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "reauth_busy",
                "holdingIdentity": exc.holding_identity,
                "startedAt": exc.started_at,
            },
        )
    except ReauthNoKey:
        raise HTTPException(
            status_code=503,
            detail={"error": "no_encryption_key"},
        )

    await record_audit(
        db=db,
        type="account.reauth.start",
        identity=identity,
        detail={"account": name},
    )

    return state.to_dict()


@router.get("/{name}/reauth")
async def reauth_status(
    name: str,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Return the current reauth state for the named account.

    Returns 404 if the account name is invalid or the slot does not exist.
    """
    _check_name_and_slot(name)

    reauth = _get_reauth(request)
    # Scoped: a reauth in flight for a *different* account reads as idle here, so
    # one account's terminal success can't freeze reauth on another (see Bug 1).
    state = await reauth.status(name, scoped=True)
    return state.to_dict()


@router.post("/{name}/reauth/cancel")
async def reauth_cancel(
    name: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Cancel a running reauth for the named account.

    Returns 404 if the account name is invalid or the slot does not exist.
    """
    _check_name_and_slot(name)

    reauth = _get_reauth(request)
    pre_cancel_state = await reauth.status(name)
    state = await reauth.cancel(name)

    if pre_cancel_state.state != "idle":
        await record_audit(
            db=db,
            type="account.reauth.cancel",
            identity=identity,
            detail={"account": name},
        )

    return state.to_dict()


class ReauthCodeBody(BaseModel):
    code: str


@router.post("/{name}/reauth/code")
async def reauth_submit_code(
    name: str,
    body: ReauthCodeBody,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Submit the OAuth code for the setup-token flow.

    Called when the user pastes the authorization code from the oauth URL.
    If the manager is not in ``awaiting_code`` state this is a no-op.

    Returns 404 if the account name is invalid or the slot does not exist.
    """
    _check_name_and_slot(name)

    reauth = _get_reauth(request)
    state = await reauth.submit_code(name, body.code)
    return state.to_dict()
