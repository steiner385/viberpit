"""Accounts REST API router (T037).

Routes:
  GET    /api/accounts                  → list accounts with live usage (no tokens)
  POST   /api/accounts/{name}/refresh   → refresh one account's usage
  DELETE /api/accounts/{name}           → remove account (204)
  POST   /api/accounts/{name}/relogin   → trigger host-side re-login (202)

All routes require a verified identity via ``current_identity``.
Response NEVER includes token/credential material (FR-023).
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ccswitch.accounts import list_accounts, remove_account, set_nickname
from ccswitch import paths
from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


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
            "resetsAt": usage.session_resets_at.isoformat() if usage.session_resets_at else None,
        },
        "weekly": {
            "pct": usage.weekly_pct,
            "resetsAt": usage.weekly_resets_at.isoformat() if usage.weekly_resets_at else None,
        },
    }


def _account_response(name: str, status_str: str, usage, nickname: str | None = None) -> dict[str, Any]:
    """Build the account wire format — never includes tokens."""
    return {
        "name": name,
        "status": status_str,
        "usage": _usage_to_dict(usage),
        "nickname": nickname,
    }


def _get_orchestrator(request: Request):
    return request.app.state.orchestrator


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
async def get_accounts(
    request: Request,
    _identity: str = Depends(current_identity),
) -> list[dict]:
    """Return all accounts with live usage — refreshed before returning.

    Never includes token or credential material.
    """
    orch = _get_orchestrator(request)

    # Get base account info from the ledger (status, names)
    accounts_info = list_accounts()

    # Refresh live usage for each account via the poller (deduplicates in-flight)
    result = []
    for acct in accounts_info:
        name = acct["name"]
        acct_status = acct.get("status", "unknown")
        nickname = acct.get("nickname")

        # Fetch fresh usage via the poller (async, deduplicates concurrent fetches)
        usage = await orch.poller.get(name)

        result.append(_account_response(name, acct_status, usage, nickname))

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
    orch = _get_orchestrator(request)

    # Force a fresh fetch (bypasses cache by going through the poller lock)
    usage = await orch.poller.get(name)

    # Determine status and nickname from the ledger
    accounts_info = list_accounts()
    status_str = "unknown"
    nickname = None
    for acct in accounts_info:
        if acct["name"] == name:
            status_str = acct.get("status", "unknown")
            nickname = acct.get("nickname")
            break

    return _account_response(name, status_str, usage, nickname)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    name: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Remove an account slot and its config directory. Returns 204."""
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
    Returns 404 if the account slot does not exist.
    """
    if not paths.slot_path(name).exists():
        raise HTTPException(status_code=404, detail=f"Account '{name}' not found")

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


@router.post("/{name}/relogin", status_code=status.HTTP_202_ACCEPTED)
async def relogin_account(
    name: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Acknowledge a host-side re-login request for an account.

    In v1, registration is host-side (``ccswitch add``); this endpoint
    acknowledges the intent and records an audit event.
    """
    await record_audit(
        db=db,
        type="account.relogin.requested",
        identity=identity,
        detail={"account": name},
    )
    return {"acknowledged": True, "account": name}
