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
import contextlib
import logging
import os
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ccswitch.accounts import (list_accounts, provider_of, remove_account,
                               set_account_enabled, set_allow_extra_usage, set_nickname,
                               set_weekly_reserve)
from ccswitch import account_registry, paths
from ccswitch.account_names import assert_valid_account_name, InvalidAccountName
from ccswitch.account_registry import UnknownAccountError
from ccswitch.config import load_config
from ccswitch.selector import rank_accounts
from server.audit import record_audit
from server.codex.auth_client import edge_authority_client
from server.codex.authorization import push_authorization
from server.codex.auth_types import (
    AuthorityError,
    AuthorityErrorCode,
    AuthorityUnavailable,
)
from server.auth.middleware import current_identity
from server.db import get_db
from server.ipc.client import DaemonTimeout
from server.reauth import ReauthBusy, ReauthNoKey
from server.sessions.engine import AgentEngine
from server.sessions.facade import call_with_deadline
from server.time_iso import iso_utc

if TYPE_CHECKING:
    from server.sessions.facade import InProcessOrchestratorAdapter

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

# Env keys that must never be overridable (constitution II: strict isolation)
_BLOCKED_ENV_KEYS = frozenset({"CLAUDE_CONFIG_DIR", "CREDENTIALS_DIR", "HOME"})


def _validate_env_overrides(overrides: dict | None) -> None:
    if not overrides:
        return
    bad = [k for k in overrides if any(k.startswith(b) for b in _BLOCKED_ENV_KEYS)]
    if bad:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "disallowed_env_key",
                "keys": bad,
                "message": "These environment variable keys are not allowed in account overrides",
            },
        )

# A single account's usage fetch must never stall or 500 the whole accounts list.
# The daemon serves usage from the poller cache (cheap); this ceiling only trips
# when the daemon control loop is wedged/slow — in which case that one account
# degrades to "unknown" (None) rather than blocking the dashboard or bubbling a
# raw ReadTimeout into an unhandled 500 (the GET /api/accounts wedge, incident
# 2026-07-02). Overridable for tests / tuning.
USAGE_FETCH_TIMEOUT = float(os.environ.get("CCSWITCH_USAGE_FETCH_TIMEOUT", "3.0"))


def _local_cached_usage(name: str):
    """Usage from the SHARED ledger, read locally. None when stale or absent.

    Usage is cached poller output that lives in ledger.json. On the master host
    that file is local, so this is a file read; asking the daemon for it instead
    is an RPC into a process whose account store may be a network mount. Measured
    2026-08-22: that RPC took 1.03-3.68s per account against a 3.0s ceiling, so
    every account intermittently degraded to "unavailable" in the UI.

    Returns None (never raises) when the entry is missing or older than
    usage_cache_ttl_sec, so the caller falls back to the daemon and correctness
    is preserved — this is a latency optimisation, not a new source of truth.
    """
    from datetime import datetime, timezone

    from ccswitch import ledger
    from ccswitch.config import load_config

    with ledger.locked_ledger() as led:
        # Returns a ccswitch.quota.QuotaSnapshot (or None) — the SAME type
        # orch.usage() yields. _safe_usage's callers convert with _usage_to_dict
        # downstream, so returning a raw dict here would break the accounts page.
        return ledger.cached_usage(led, name, load_config(), datetime.now(timezone.utc))


async def _safe_usage(orch, name: str, timeout: float):
    """Fetch one account's usage, degrading to None on timeout or any error.

    Tries the local shared ledger first (cheap), then the daemon RPC. Isolates a
    single slow/failing account so it can neither stall the other accounts'
    fetches nor turn GET /api/accounts into a 500. ``usage=None`` is already the
    wire contract for "unknown/failed fetch" (_usage_to_dict).
    """
    try:
        local = _local_cached_usage(name)
        if local is not None:
            return local
    except Exception:  # noqa: BLE001 — a bad ledger must fall back, never 500
        log.warning("local usage read for %s failed — falling back to the daemon",
                    name, exc_info=True)

    try:
        # The deadline is pushed INTO the RPC (call_with_deadline), never raced from
        # outside: asyncio.wait_for CANCELS an in-flight httpx request, which orphans
        # the socket and strands its pool slot ACTIVE forever (issue #734). It also
        # collapses the old nested pair of deadlines (this one and remote's
        # USAGE_RPC_TIMEOUT) into a single, unambiguous one.
        return await call_with_deadline(orch.usage, name, timeout=timeout)
    except (asyncio.TimeoutError, DaemonTimeout):
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
    """Convert a ``ccswitch.quota.QuotaSnapshot`` to the wire-format dict.

    ``windows`` is a LIST, not two named dimensions: a provider reports the
    allowances it actually meters, so an Anthropic account sends session+weekly
    and a Copilot account sends its single premium-request window (it previously
    sent the same number twice under two labels). The browser renders whatever
    arrives.

    Returns None if usage is None (unknown/failed fetch). Never includes token
    material.
    """
    if usage is None:
        return None
    return {
        "provider": usage.provider,
        "fetchedAt": iso_utc(usage.fetched_at),
        # "available" | "unknown" | "auth_error" — how much the windows can be
        # trusted. A failed read is expressed HERE, never as a fabricated window.
        "availability": usage.availability,
        "windows": [
            {
                "key": w.key,
                "label": w.label,
                "pct": w.used_pct,
                "resetsAt": iso_utc(w.resets_at),
                "durationSeconds": w.duration_seconds,
                "scope": w.scope,
                "appliesToModels": list(w.applies_to_models),
            }
            for w in usage.windows
        ],
        # Whether the PROVIDER has extra usage enabled — not the local opt-in
        # (that is the account-level ``allowExtraUsage``). The UI warns when the
        # opt-in is on but this is false, since credits then cannot be spent.
        "extraUsageEnabled": usage.extra_usage_enabled,
    }


def _account_response(
    name: str,
    status_str: str,
    usage,
    nickname: str | None = None,
    live_sessions: dict[str, int] | None = None,
    rank: int | None = None,
    rank_reason: str | None = None,
    weekly_reserve_pct: float | None = None,
    account_defaults: dict | None = None,  # NEW
    enabled: bool = True,
    allow_extra_usage: bool = False,
    codex: dict | None = None,
) -> dict[str, Any]:
    """Build the account wire format — never includes tokens."""
    entry = None
    try:
        entry = account_registry.entry(name)
        provider = entry.provider
    except (UnknownAccountError, OSError):
        # Not in the account manifest. Say so rather than defaulting to
        # "anthropic": the browser gates provider-specific actions (Copilot
        # re-login vs Anthropic reauth) on this field, and a wrong value offers
        # the wrong recovery.
        provider = "unknown"
    return {
        "name": name,
        "status": status_str,
        "usage": _usage_to_dict(usage),
        "nickname": nickname,
        "provider": provider,
        # Identity facts straight off the manifest. `engine` is nullable there and
        # means "the engine registry's default" (claude_code) — resolved here so
        # the browser never has to know that rule. The two auth-mode fields tell
        # the UI which recovery to offer without a second provider switch:
        # `chatgpt_device` renders a device-code panel, `claude_oauth` a browser
        # flow. `credentialAuthority` names WHO holds the credential; it is never
        # one (see account_registry._SECRET_KEY_ALLOWLIST).
        "engine": (
            None if entry is None
            else (entry.engine or AgentEngine.CLAUDE_CODE.value)
        ),
        "userAuthMode": None if entry is None else entry.user_auth_mode,
        "runnerAuthMode": None if entry is None else entry.runner_auth_mode,
        "credentialAuthority": (
            None if entry is None else entry.credential_authority
        ),
        # Present only for OpenAI accounts, and never carrying a native account id
        # or a token: {"authority": <health>, "quota": <windows>,
        # "activeRunners": int, "degradedReason": str|None}.
        "codex": codex,
        "liveSessions": live_sessions or {"active": 0, "idle": 0},
        "rank": rank,
        "rankReason": rank_reason,
        "weeklyReservePct": weekly_reserve_pct,
        "allowExtraUsage": allow_extra_usage,
        "accountDefaults": account_defaults or {"defaultModel": None, "defaultPermissionMode": None, "modelApprovalAction": None, "envOverrides": None},
        "enabled": enabled,
    }


def _account_response_from_info(acct: dict, usage=None, **kw) -> dict[str, Any]:
    """Build the wire response from one ``list_accounts()`` entry.

    Single source of truth for the ledger-backed fields (reserve, defaults,
    enabled, extra-usage opt-in): every route that echoes an account back goes
    through here, so adding a field is one edit rather than four call sites that
    silently drift apart.
    """
    return _account_response(
        acct["name"],
        acct.get("status", "unknown"),
        usage,
        acct.get("nickname"),
        weekly_reserve_pct=acct.get("weeklyReservePct"),
        account_defaults=acct.get("accountDefaults"),
        enabled=acct.get("enabled", True),
        allow_extra_usage=bool(acct.get("allowExtraUsage")),
        **kw,
    )


async def _codex_details(request: Request, names: list[str]) -> dict[str, dict]:
    """Per-account Codex facts for the accounts list, or ``{}`` when irrelevant.

    Fetched ONCE per request and only when an OpenAI account actually exists, so a
    deployment with none pays nothing — not an authority round trip, not a daemon
    RPC. The authority health is a single call shared by every OpenAI account
    (it describes the service, not the account); quota and runner counts are
    per-account and gathered concurrently.

    Every failure degrades to a partial answer. The accounts page is the surface
    an operator uses to FIX a broken authority, so it must render when the
    authority is down.
    """
    openai_names = [n for n in names if _provider_or_none(n) == account_registry.PROVIDER_OPENAI]
    if not openai_names:
        return {}
    client = edge_authority_client()
    health = None
    if client is not None:
        try:
            health = (await client.health()).to_wire()
        except Exception:  # noqa: BLE001 -- a dead authority must not 500 the page
            log.warning("codex authority health probe failed", exc_info=True)

    orch = _get_orchestrator(request)

    async def _one(account: str) -> tuple[str, dict]:
        try:
            status = await orch.codex_account_status(account)
        except Exception:  # noqa: BLE001 -- one account must not fail the list
            log.warning("codex status for %s failed", account, exc_info=True)
            status = {}
        return account, {
            "authority": health,
            "quota": status.get("quota"),
            "activeRunners": status.get("activeRunners", 0),
            "degradedReason": status.get("degradedReason"),
        }

    return dict(await asyncio.gather(*(_one(n) for n in openai_names)))


def _get_orchestrator(request: Request) -> "InProcessOrchestratorAdapter":
    return request.app.state.sessions


def _get_reauth(request: Request):
    return request.app.state.reauth


def _check_name_and_slot(name: str) -> None:
    """Assert the account name is valid and a credential exists for it.

    Raises HTTPException 404 if the name is invalid (path traversal, hidden
    file, bad chars) OR if no credential artefact exists for the name.

    "Artefact" is provider-specific, and OpenAI has none on this host *by design*:
    its credentials live in the credential authority's private state directory,
    which the edge cannot (and must not) read. So an OpenAI account is proven to
    exist by its manifest entry instead — the manifest is the declared identity
    record precisely so that "does this account exist?" stops being a filesystem
    guess.
    """
    try:
        assert_valid_account_name(name)
    except InvalidAccountName:
        raise HTTPException(status_code=404, detail=f"Account '{name}' not found")

    if paths.slot_path(name).exists() or paths.copilot_token_dir(name).exists():
        return
    if _provider_or_none(name) == account_registry.PROVIDER_OPENAI:
        return
    raise HTTPException(status_code=404, detail=f"Account '{name}' not found")


def _provider_or_none(name: str) -> str | None:
    """The manifest's provider for *name*, or None when it is not registered."""
    try:
        return provider_of(name)
    except (UnknownAccountError, OSError):
        return None


def _require_authority():
    """The edge's authority client, or a typed 503.

    503 rather than 500: "this deployment has no ChatGPT credential authority" is
    a configuration state an operator can fix, not a bug, and the browser renders
    it as "unavailable" rather than "something went wrong".
    """
    client = edge_authority_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "codex_authority_unconfigured",
                "message": (
                    "No ChatGPT credential authority is configured on this host, "
                    "so OpenAI accounts cannot be managed here."
                ),
            },
        )
    return client


def _authority_http_error(exc: AuthorityError) -> HTTPException:
    """Map a typed authority refusal onto the status code that matches it.

    Preserving the code is the point: the browser gates its recovery UI on it (a
    ``login_required`` offers re-auth, an ``account_disabled`` offers enable), and
    flattening everything to 500 would make every one of those look like an outage.
    """
    status_by_code = {
        AuthorityErrorCode.UNKNOWN_ACCOUNT: 404,
        AuthorityErrorCode.ACCOUNT_DISABLED: 409,
        AuthorityErrorCode.LOGIN_BUSY: 409,
        AuthorityErrorCode.LOGIN_REQUIRED: 409,
        AuthorityErrorCode.FORBIDDEN: 403,
        AuthorityErrorCode.BAD_REQUEST: 400,
        AuthorityErrorCode.REVISION_ROLLBACK: 409,
        AuthorityErrorCode.SYNC_EXPIRED: 503,
        AuthorityErrorCode.NOT_AUTHORITY: 503,
    }
    return HTTPException(
        status_code=status_by_code.get(exc.code, 502),
        detail={"error": exc.code.value, "message": exc.detail or exc.code.value},
    )


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

    # Build per-account weekly reserve map for display ranking.
    reserves = {a["name"]: a["weeklyReservePct"] for a in accounts_info
                if a.get("weeklyReservePct") is not None}
    extra_allowed = frozenset(a["name"] for a in accounts_info
                              if a.get("allowExtraUsage"))

    # Rank for display: ignore transient soft reservations so ranks stay stable
    # (a just-picked account would otherwise vanish for reservation_ttl_sec).
    # Exclude disabled accounts like the selection path does — otherwise a
    # disabled account can be labelled "#1 next" while every selectable
    # account's rank is shifted by the phantom entry.
    disabled = frozenset(
        a["name"] for a in accounts_info if not a.get("enabled", True))
    ranked, _earliest = rank_accounts(
        usages, reserved=frozenset(), cfg=load_config(),
        now=datetime.now(timezone.utc), load=load,
        reserves=reserves, exclude=disabled,
        extra_usage_allowed=extra_allowed,
    )
    rank_by_name = {r.name: (r.rank, r.reason) for r in ranked}
    codex_by_name = await _codex_details(
        request, [acct["name"] for acct in accounts_info])

    result = []
    for acct in accounts_info:
        name = acct["name"]
        rank, reason = rank_by_name.get(name, (None, None))
        result.append(_account_response_from_info(
            acct,
            usages[name],
            live_sessions=counts.get(name) or {"active": 0, "idle": 0},
            rank=rank,
            rank_reason=reason,
            codex=codex_by_name.get(name),
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

    # Ledger-backed fields (status, nickname, reserve, defaults, toggles).
    acct = next((a for a in list_accounts() if a["name"] == name), None)

    counts = await orch.live_session_counts()
    live = counts.get(name) or {"active": 0, "idle": 0}
    if acct is None:
        return _account_response(name, "unknown", usage, live_sessions=live)
    return _account_response_from_info(acct, usage, live_sessions=live)


async def _revoke_openai_credentials(request: Request, name: str, *, action: str) -> None:
    """Kill an OpenAI account's credentials everywhere, before anything else.

    This is the *safe ordering* the plan requires, and the order is the whole
    point. Revoke at the authority first (which bumps the generation, so every
    envelope already in circulation is dead), then fan invalidation out to every
    runner (which drops cached envelopes and stops the account's app-servers).
    Only then may the caller write the manifest.

    The mirror image — write "disabled" first, then revoke — leaves a window in
    which ccswitch says the account is off while runners still hold live tokens,
    and if the revoke then fails the window never closes.

    A partial failure raises 502 with ``degraded: true``. It is deliberately NOT
    swallowed: "some host may still be able to use this account" must not be
    reported to an operator as a completed disable.
    """
    client = _require_authority()
    try:
        await client.revoke(name)
    except AuthorityError as exc:
        if exc.code is not AuthorityErrorCode.UNKNOWN_ACCOUNT:
            raise _authority_http_error(exc) from None
    except AuthorityUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "authority_unreachable", "action": action,
                    "message": str(exc)},
        ) from None

    orch = _get_orchestrator(request)
    try:
        result = await orch.codex_invalidate_credentials(name)
    except Exception as exc:  # noqa: BLE001 -- a fan-out fault is a degraded state
        raise HTTPException(
            status_code=502,
            detail={"error": "invalidation_failed", "action": action,
                    "degraded": True, "retryable": True, "message": str(exc)},
        ) from None
    failed = (result or {}).get("failed") or {}
    if failed:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "invalidation_partial",
                "action": action,
                "degraded": True,
                "retryable": True,
                "hosts": sorted(failed),
                "message": (
                    f"Credentials were revoked, but {len(failed)} host(s) could "
                    f"not be told to drop them. Retry before treating this "
                    f"account as {action}d."
                ),
            },
        )


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    name: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Remove an account slot and its config directory. Returns 204."""
    _check_name_and_slot(name)

    if _provider_or_none(name) == account_registry.PROVIDER_OPENAI:
        await _revoke_openai_credentials(request, name, action="remove")
        with contextlib.suppress(Exception):
            await _require_authority().forget(name)

    remove_account(name)

    if _provider_or_none(name) is None:
        # The manifest entry is gone, so the projection must lose it too —
        # otherwise the authority keeps a record for an account that no longer
        # exists and would still answer for it until the lease lapsed.
        client = edge_authority_client()
        if client is not None:
            await _sync_authority(client)

    await record_audit(
        db=db,
        type="account.remove",
        identity=identity,
        detail={"account": name},
    )


class AccountPatchBody(BaseModel):
    nickname: str | None = None
    weeklyReservePct: float | None = Field(default=None, ge=1.0, le=100.0)
    allowExtraUsage: bool | None = None
    defaultModel: str | None = None
    defaultPermissionMode: str | None = None
    envOverrides: dict | None = None
    modelApprovalAction: str | None = None


@router.patch("/{name}")
async def update_account(
    name: str,
    body: AccountPatchBody,
    _identity: str = Depends(current_identity),
) -> dict:
    """Update per-account settings: nickname, weekly usage reserve, extra-usage
    opt-in, and/or launch defaults.

    Fields absent from the body are left untouched; an explicit ``null`` clears.
    A blank or whitespace-only nickname clears the existing nickname.
    Returns 404 if the account name is invalid or slot does not exist.
    """
    _check_name_and_slot(name)

    if "nickname" in body.model_fields_set:
        set_nickname(name, (body.nickname or "").strip() or None)
    if "weeklyReservePct" in body.model_fields_set:
        set_weekly_reserve(name, body.weeklyReservePct)
    if "modelApprovalAction" in body.model_fields_set and body.modelApprovalAction is not None:
        from server.sessions.model_policy import VALID_ACTIONS
        if body.modelApprovalAction not in VALID_ACTIONS:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_model_approval_action",
                    "value": body.modelApprovalAction,
                    "allowed": sorted(VALID_ACTIONS),
                    "message": f"modelApprovalAction must be one of: {', '.join(sorted(VALID_ACTIONS))}",
                },
            )
    if "allowExtraUsage" in body.model_fields_set:
        # Explicit null and false both mean "off" — there is no third state.
        set_allow_extra_usage(name, bool(body.allowExtraUsage))
    if any(f in body.model_fields_set for f in ("defaultModel", "defaultPermissionMode", "envOverrides", "modelApprovalAction")):
        _validate_env_overrides(body.envOverrides)
        from ccswitch.accounts import set_account_defaults
        set_account_defaults(
            name,
            default_model=body.defaultModel if "defaultModel" in body.model_fields_set else None,
            clear_model="defaultModel" in body.model_fields_set and body.defaultModel is None,
            default_permission_mode=body.defaultPermissionMode if "defaultPermissionMode" in body.model_fields_set else None,
            clear_permission_mode="defaultPermissionMode" in body.model_fields_set and body.defaultPermissionMode is None,
            model_approval_action=body.modelApprovalAction if "modelApprovalAction" in body.model_fields_set else None,
            clear_model_approval_action="modelApprovalAction" in body.model_fields_set and body.modelApprovalAction is None,
            env_overrides=body.envOverrides if "envOverrides" in body.model_fields_set else None,
            clear_env_overrides="envOverrides" in body.model_fields_set and body.envOverrides is None,
        )

    for acct in list_accounts():
        if acct["name"] == name:
            return _account_response_from_info(acct)
    return _account_response(name, "unknown", None, None)


class AccountEnabledBody(BaseModel):
    enabled: bool


@router.patch("/{name}/enabled")
async def set_enabled(
    name: str,
    body: AccountEnabledBody,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Enable or disable an account without deleting it.

    A disabled account is blocked everywhere — it is never auto-selected, never a
    rotation target, and cannot be manually picked — but its credentials stay
    intact so the toggle is fully reversible. Returns the updated account.
    Returns 404 if the account name is invalid or its slot does not exist.

    For an OpenAI account the two directions are NOT symmetric, because a
    credential lives outside this host:

    * **Disable** must revoke and fan out invalidation BEFORE writing the flag, so
      the moment ccswitch reports "disabled" no runner can still use it.
    * **Enable** writes the flag first, then syncs the authority. If the sync
      fails the account is enabled in ccswitch but not yet issuable — the
      authority simply has not been told — which is the safe direction to fail.
    """
    _check_name_and_slot(name)

    is_openai = _provider_or_none(name) == account_registry.PROVIDER_OPENAI
    if is_openai and not body.enabled:
        await _revoke_openai_credentials(request, name, action="disable")

    set_account_enabled(name, body.enabled)

    if is_openai:
        await _sync_authority(_require_authority())

    await record_audit(
        db=db,
        type="account.enabled" if body.enabled else "account.disabled",
        identity=identity,
        detail={"account": name, "enabled": body.enabled},
    )

    for acct in list_accounts():
        if acct["name"] == name:
            return _account_response_from_info(acct)
    return _account_response(name, "unknown", None, None, enabled=body.enabled)


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
# Copilot device-flow add-account routes
#
# Unlike the browser-OAuth add flow (which uses ReauthManager), Copilot
# accounts are added via GitHub device-flow (no browser required).  The flow
# is managed by CopilotAuthManager inside the orchestrator.  The slot is a
# token directory at paths.copilot_token_dir(name); it must NOT already exist
# when starting.  Status/cancel do NOT require the slot to exist.
# ---------------------------------------------------------------------------


@router.post("/add/copilot", status_code=status.HTTP_202_ACCEPTED)
async def copilot_add_start(
    body: AddAccountBody,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Start a GitHub device-flow that creates a new Copilot account slot.

    Returns 202 + state dict (state, userCode, verificationUri …) on success.
    Returns 400 if the requested name is malformed.
    Returns 409 if a Copilot account with that name already exists.
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

    if paths.copilot_token_dir(name).exists():
        raise HTTPException(
            status_code=409,
            detail={
                "error": "account_exists",
                "name": name,
                "message": f"Copilot account '{name}' already exists",
            },
        )

    orch = _get_orchestrator(request)
    state = await orch.copilot_add_start(name)

    await record_audit(
        db=db,
        type="account.add.copilot.start",
        identity=identity,
        detail={"account": name},
    )

    return state


@router.get("/add/copilot")
async def copilot_add_status(
    name: str,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Return the current device-flow state for an in-flight Copilot add.

    Does not require the slot to exist.
    """
    orch = _get_orchestrator(request)
    return await orch.copilot_add_status(name)


class AddCancelBody(BaseModel):
    """Body for cancelling a device-flow add. Shared by Copilot and OpenAI.

    One model rather than one per provider: both flows are cancelled by naming the
    account, and a `CopilotAddCancelBody` sitting in an OpenAI route would read as
    a copy-paste slip rather than as deliberate reuse.
    """

    name: str


@router.post("/add/copilot/cancel")
async def copilot_add_cancel(
    body: AddCancelBody,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Cancel a running Copilot device-flow add.  Returns the resulting state dict."""
    name = body.name
    orch = _get_orchestrator(request)
    state = await orch.copilot_add_cancel(name)

    await record_audit(
        db=db,
        type="account.add.copilot.cancel",
        identity=identity,
        detail={"account": name},
    )

    return state


# ---------------------------------------------------------------------------
# OpenAI (Codex) device-flow add-account routes
#
# Shaped exactly like the Copilot device flow above — same four state words, same
# {state,userCode,verificationUri,error} wire fields — so the browser has ONE
# device-flow component rather than one per provider. What differs is where the
# credential lands: not in a slot on this host, but inside the credential
# authority's private state directory, which the edge never reads.
#
# The manifest entry is written only AFTER the login succeeds. Writing it first
# would advertise an account that cannot start a session, and — worse — would let
# the authorization sync mark it enabled before it had credentials.
# ---------------------------------------------------------------------------


def _reject_invalid_name(name: str) -> None:
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
        ) from None


async def _sync_authority(client) -> None:
    """Push the authorization projection, best-effort.

    Best-effort because the caller has already made the *safe* half of its change
    (see the ordering note on each route). A failed sync leaves the authority on
    its previous projection, which expires on its own lease — it can only ever
    become more restrictive, never less.
    """
    try:
        await push_authorization(client)
    except (AuthorityError, AuthorityUnavailable):
        log.warning("codex authorization sync failed after an account change",
                    exc_info=True)


@router.post("/add/openai", status_code=status.HTTP_202_ACCEPTED)
async def openai_add_start(
    body: AddAccountBody,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Start a ChatGPT device-flow login that creates a new OpenAI account.

    Returns 202 + state dict (state, userCode, verificationUri …).
    Returns 400 for a malformed name, 409 if the name is taken (by ANY provider —
    names are globally unique), 503 if no credential authority is configured.
    """
    name = body.name
    _reject_invalid_name(name)
    if account_registry.has_account(name) or paths.slot_path(name).exists():
        raise HTTPException(
            status_code=409,
            detail={"error": "account_exists", "name": name,
                    "message": f"Account '{name}' already exists"},
        )
    client = _require_authority()

    # Register FIRST, disabled. The authority refuses to log in an account it has
    # never been told about, so there is a chicken-and-egg here; `enabled=False`
    # resolves it safely, because issuance (unlike login) does require enabled.
    # A login that never completes therefore leaves a disabled, credential-less
    # entry the operator can delete — never a usable account.
    account_registry.register_account(
        name,
        account_registry.PROVIDER_OPENAI,
        engine=AgentEngine.CODEX.value,
    )
    account_registry.set_enabled(name, False)
    await _sync_authority(client)

    try:
        state = await client.login_start(name)
    except AuthorityError as exc:
        raise _authority_http_error(exc) from None
    except AuthorityUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "authority_unreachable", "message": str(exc)},
        ) from None

    await record_audit(
        db=db,
        type="account.add.openai.start",
        identity=identity,
        detail={"account": name},
    )
    return state.to_wire()


@router.get("/add/openai")
async def openai_add_status(
    name: str,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Poll an in-flight OpenAI device login.

    On the transition to ``authorized`` this is what enables the account and
    re-syncs the authority — the login runs inside the authority process, so the
    edge has no other moment at which to learn it succeeded.
    """
    client = _require_authority()
    try:
        state = await client.login_status(name)
    except AuthorityError as exc:
        raise _authority_http_error(exc) from None
    except AuthorityUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "authority_unreachable", "message": str(exc)},
        ) from None
    if state.state == "authorized" and account_registry.has_account(name):
        entry = account_registry.entry(name)
        if not entry.enabled:
            set_account_enabled(name, True)
            await _sync_authority(client)
    return state.to_wire()


@router.post("/add/openai/cancel")
async def openai_add_cancel(
    body: AddCancelBody,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Cancel a running OpenAI device login and drop the placeholder entry.

    The entry is removed because it was created by ``/add/openai`` purely to make
    the login addressable: leaving a disabled, credential-less account behind
    after an explicit cancel would clutter the picker with something that can
    never run.
    """
    name = body.name
    client = _require_authority()
    try:
        state = await client.login_cancel(name)
    except AuthorityError as exc:
        raise _authority_http_error(exc) from None
    except AuthorityUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "authority_unreachable", "message": str(exc)},
        ) from None

    with contextlib.suppress(Exception):
        await client.forget(name)
    with contextlib.suppress(account_registry.AccountRegistryError, OSError):
        account_registry.forget_account(name)
    await _sync_authority(client)

    await record_audit(
        db=db,
        type="account.add.openai.cancel",
        identity=identity,
        detail={"account": name},
    )
    return state.to_wire()


# ---------------------------------------------------------------------------
# Reauth routes
# ---------------------------------------------------------------------------


@router.post("/{name}/reauth", status_code=status.HTTP_202_ACCEPTED)
async def reauth_start(
    name: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Start a re-authentication flow for the named (existing) account.

    Always the in-process PKCE/setup-token exchange (issue #1130) — no browser,
    no virtual display, no Chrome profile copy. The account's slot already
    exists, so this only needs to mint a fresh long-lived setup-token; the
    browser/VNC login (mode="login" on ReauthManager) is reserved for
    /accounts/add, which still needs it to bootstrap a brand-new slot's full
    OAuth credential — setup_token_oauth.exchange() yields a bare access token,
    with no refresh_token, so it cannot create a new account by itself.

    Returns 202 + state dict on success.
    Returns 409 if another reauth is already running.
    Returns 503 if no encryption key is configured.
    Returns 404 if the account name is invalid or the slot does not exist.

    An OpenAI account re-authenticates by re-running its device flow at the
    credential authority — there is no browser OAuth and no local slot to
    re-encrypt — so it is dispatched to that path rather than to ReauthManager.
    """
    _check_name_and_slot(name)

    if _provider_or_none(name) == account_registry.PROVIDER_OPENAI:
        client = _require_authority()
        try:
            state = await client.login_start(name)
        except AuthorityError as exc:
            raise _authority_http_error(exc) from None
        except AuthorityUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"error": "authority_unreachable", "message": str(exc)},
            ) from None
        await record_audit(
            db=db,
            type="account.reauth.start",
            identity=identity,
            detail={"account": name, "provider": account_registry.PROVIDER_OPENAI},
        )
        return state.to_wire()

    reauth = _get_reauth(request)
    try:
        state = await reauth.start(name, identity, mode="setup_token")
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

    if _provider_or_none(name) == account_registry.PROVIDER_OPENAI:
        try:
            return (await _require_authority().login_status(name)).to_wire()
        except AuthorityError as exc:
            raise _authority_http_error(exc) from None
        except AuthorityUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"error": "authority_unreachable", "message": str(exc)},
            ) from None

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

    if _provider_or_none(name) == account_registry.PROVIDER_OPENAI:
        try:
            state = await _require_authority().login_cancel(name)
        except AuthorityError as exc:
            raise _authority_http_error(exc) from None
        except AuthorityUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"error": "authority_unreachable", "message": str(exc)},
            ) from None
        await record_audit(
            db=db,
            type="account.reauth.cancel",
            identity=identity,
            detail={"account": name, "provider": account_registry.PROVIDER_OPENAI},
        )
        return state.to_wire()

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
