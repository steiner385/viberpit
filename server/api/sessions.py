"""Sessions REST API router (T036).

Routes:
  GET  /api/sessions               → list sessions
  POST /api/sessions               → create session (201) or 409 if all-busy
  GET  /api/sessions/{id}          → get single session (404 if unknown)
  PATCH /api/sessions/{id}         → rename session (200)
  DELETE /api/sessions/{id}        → close session (204)

All routes require a verified identity via the ``current_identity`` dependency.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
from typing import Any, Literal, TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from ccswitch import accounts as _accounts
from ccswitch import gitrepos
from server.auth.middleware import current_identity
from server.sessions.search import parse_terms, session_matches
from server.audit import record_audit
from server.config import get_settings
from server.db import db_session, get_db
from server.sessions.orchestrator import (
    ArchiveMissingError,
    BranchSwitchError,
    InsufficientStorageError,
    ModelBlockedError,
)
from server.ipc.client import DaemonError, DaemonUnavailable
from server.sessions.facade import call_with_deadline
from server.sessions.permissions import resolve_permission_mode
from server.sessions.selection import AllAccountsBusy
from server.sessions.fleet_router import UnknownHost, NoHostAvailable
from server.time_iso import iso_utc

if TYPE_CHECKING:
    from server.sessions.facade import InProcessOrchestratorAdapter

# Repo nameWithOwner shape: owner/name — each segment alphanumeric + . _ -
# No leading/trailing dots, no consecutive dots (blocks path-traversal via ../foo).
_REPO_SEGMENT = r"(?!\.{2,})[A-Za-z0-9][A-Za-z0-9._-]*"
_REPO_RE = re.compile(rf"^{_REPO_SEGMENT}/{_REPO_SEGMENT}$")

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _require_adoption_enabled() -> None:
    """Sunset gate: adoption is off unless CCSWITCH_ENABLE_ADOPTION is set."""
    if not get_settings().enable_adoption:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    name: str | None = None
    account: str | None = None
    initialPrompt: str | None = None
    repo: str | None = None  # nameWithOwner, e.g. "owner/name"
    branch: str | None = None
    permissionMode: str | None = None
    attachmentIds: list[str] | None = None
    # Model alias/id to start the session on (e.g. "opus", "fable"). None / "default"
    # → the account default; the value is applied to the driver once it starts.
    model: str | None = None
    # Optional runner preference. None → auto-rank; a name → that host first, still
    # failing over. Forwarded to FleetOrchestrator.create_session as prefer=.
    host: str | None = None


class RenameSessionRequest(BaseModel):
    name: str


class AutopilotPauseRequest(BaseModel):
    paused: bool


class QueueMoveRequest(BaseModel):
    op: Literal["up", "down", "top", "position"]
    position: int | None = None


class AdoptRequest(BaseModel):
    claudeSessionId: str


class SendPromptRequest(BaseModel):
    text: str


class ModelApprovalBody(BaseModel):
    decision: Literal["approve", "deny"]


class FixPrRequest(BaseModel):
    number: int


_UNSET = object()


def _record_to_dict(
    record, repo: Any = _UNSET, suggestions: list[str] | None = None,
    prs: list | None = None, summary: str = "", defer_seconds: int | None = None,
    queue_position: int | None = None, queue_stall_reason: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert an in-memory SessionRecord to the wire-format dict.

    ``repo`` lets callers inject a value read live from the session's working
    directory (see :func:`_resolve_repo`); when omitted, the stored
    ``record.repo`` is used as a fallback.

    ``suggestions`` holds the latest cached next-prompt suggestions for the
    session (from ``orch.latest_suggestions``). Defaults to [] when omitted
    (detached sessions or none yet generated).

    ``prs`` is the list of cached PullRequestInfo for the session's authored
    branches.  Serialised to a list of dicts on the wire.

    ``summary`` is the one-line status summary for the session (from
    ``orch.latest_summary``). Defaults to "" when omitted or unavailable.

    ``queue_stall_reason`` is set (only on queued records) when the queue is
    non-empty and cannot drain — see :func:`_compute_queue_stall_reason`.
    ``None`` for every non-queued record and for a queued record whose queue
    is not actually stalled. Purely additive: a client that has never heard of
    this field sees nothing new to worry about.
    """
    from datetime import datetime, timedelta, timezone
    from server.sessions.goals import effective_goal
    goal_text, goal_source = effective_goal(record)
    cooldown_until = None
    _last = getattr(record, "last_user_interaction_at", None)
    if defer_seconds and _last is not None and not getattr(record, "autopilot_paused", False):
        if _last.tzinfo is not None:                      # aware after an RPC round trip (daemon mode)
            _last = _last.astimezone(timezone.utc).replace(tzinfo=None)
        _until = _last + timedelta(seconds=defer_seconds)
        if _until > datetime.utcnow():
            cooldown_until = iso_utc(_until)
    return {
        "id": record.id,
        "name": record.name,
        "accountName": record.account,
        "repo": record.repo if repo is _UNSET else repo,
        # Fleet host that ran this session. None for pre-fleet sessions and for
        # deployments whose endpoint is not in an operator-managed registry.
        "host": getattr(record, "host", None),
        "origin": getattr(record, "origin", None),
        "originRef": getattr(record, "origin_ref", None),
        "assistedBy": getattr(record, "assisted_by", None),
        "status": record.status,
        "nameIsAuto": getattr(record, "name_is_auto", True),
        "detached": record.detached,
        "adoptedInPlace": getattr(record, "adopted_in_place", False),
        "subAgents": getattr(record, "sub_agents", None) or [],
        "createdAt": iso_utc(record.created_at),
        # Last-turn-response time for the "active … ago" label (NOT record.updated_at,
        # which any row write bumps — that pinned every card to "0s ago"). Fall back to
        # the creation time so a session with no turn yet reads "active just now" rather
        # than null (which the client would parse as epoch 0 → "20627d ago"). iso_utc
        # tags the naive UTC column with a +00:00 offset so the browser doesn't reparse
        # it as local time (which lands it in the future → clamped back to "0s ago").
        "updatedAt": record.updated_at_iso,
        "revAt": record.rev_at,
        "accountPinned": getattr(record, "account_pinned", False),
        "model": getattr(record, "model", None),
        # Approval-hold model: set when the model-approval gate parks a requested
        # model pending admin sign-off (status="pending_approval" or a mid-session
        # hold). Null once approved/denied or for non-gated sessions.
        "pendingModel": getattr(record, "pending_model", None),
        # True while a "switch after this turn" model change waits for the in-flight
        # turn to end (transient, never persisted) — the picker shows it as pending.
        "modelPending": getattr(record, "model_pending", False),
        "latestSuggestions": list(suggestions or []),
        "activeGoal": getattr(record, "active_goal", None),
        "inferredGoal": getattr(record, "inferred_goal", None),
        # The reckoned goal autopilot drives toward, resolved through the single
        # effective_goal() helper (explicit /goal wins, else the title fallback).
        "effectiveGoal": goal_text,
        "goalSource": goal_source,
        "autopilotPaused": getattr(record, "autopilot_paused", False),
        "autopilotCooldownUntil": cooldown_until,
        "autoArchiveOptOut": getattr(record, "auto_archive_opt_out_reason", None),
        "prs": [p.to_dict() for p in (prs or [])],
        "usageUnverified": getattr(record, "usage_unverified", False),
        "interruptedByRestart": getattr(record, "interrupted_by_restart", False),
        "summary": summary or "",
        "queuePosition": queue_position,
        "queueStallReason": queue_stall_reason,
    }


async def _resolve_repo(record) -> str | None:
    """Read the repo indicator live from the session's working dir.

    Runs git in ``record.cwd`` (off the event loop) so the sessions screen shows
    what's actually checked out — a worktree resolves to its clone's origin, and
    a scratch dir with no git remote reports None ("Scratch"). The live read is
    preferred whenever it yields something, since it reflects the working dir's
    actual current state (e.g. after a branch switch).

    Falls back to the stored ``record.repo`` whenever the live read yields
    nothing — not only when ``cwd`` is empty, but also when ``cwd`` is set and
    the live read returns None/empty or raises. This matters because the
    session daemon can run on a different host from this (edge) process: ``cwd``
    then points to a real path on the daemon's filesystem that doesn't exist
    here, the live read silently returns None, and without this fallback every
    session would render as "Scratch" even though the DB has the correct repo.
    This is strictly safer on a single host too — there, an unreadable cwd
    means a deleted or broken worktree, and the stored repo is still better
    information than "Scratch". A genuine scratch session (a real directory
    with no git remote) still resolves to None here, because in that case
    ``record.repo`` is itself None — no sentinel is invented.
    """
    cwd = getattr(record, "cwd", None)
    if not cwd:
        return record.repo
    try:
        live = await asyncio.to_thread(gitrepos.repo_at_path, cwd)
    except Exception:
        return record.repo
    return live if live else record.repo


async def _session_is_archived(orch, session_id: str) -> bool:
    """Whether ``session_id`` currently appears in the archived list.

    Used to reconcile a lost/timed-out archive RPC: if the daemon completed the
    archive but its response was lost, the session shows up here. A daemon that is
    genuinely unreachable re-raises ``DaemonUnavailable`` (surfacing the real 503)
    rather than being mistaken for "not archived".
    """
    archived = await orch.list_archived()
    return any(a.get("id") == session_id for a in archived)


# Per-session enrichment calls (suggestions, prs, summary) are daemon RPCs with no
# inherent timeout — one wedged session would otherwise hold up the whole response.
# Gate each RPC individually so a single bad session degrades to an empty field instead
# of stalling the list, even when the daemon is under memory pressure or mid-restart.
#
# The deadline is pushed INTO each RPC (call_with_deadline → DaemonClient.call → httpx),
# never raced from outside with asyncio.wait_for. wait_for CANCELS the request, and a
# CancelledError thrown into httpcore orphans the socket and strands its pool slot ACTIVE
# forever — 63 cancellable RPCs per page load wedged the edge twice in two days
# (issue #734, spike 2026-08-23-edge-socket-leak.md).
#
# WHAT THIS DOES *NOT* BOUND: httpx's read timeout is per-read — the gap between bytes —
# not a whole-response budget. A daemon that STALLS is cut off after this many seconds; a
# daemon that drips a response slowly can exceed it, so total /api/sessions latency is no
# longer strictly wall-clock bounded the way the old outer wait_for made it. That is
# inherent to the fix: any outer bound reintroduces the cancellation being removed. If a
# whole-response budget is ever needed it has to come from the daemon side (an RPC that
# answers or gives up), not from an asyncio timer on the edge.
ENRICH_FETCH_TIMEOUT = float(os.environ.get("CCSWITCH_ENRICH_FETCH_TIMEOUT", "3.0"))


async def _enrich_batch(orch, records, viewing: bool, timeout: float):
    """Fetch every session's enrichment in three batched RPCs instead of three per session.

    Returns (repos, suggestions, prs, summaries), each positionally aligned to *records*.

    A session id absent from a reply degrades to that field's empty value, and a whole
    batch that fails degrades only its own field — the other two still render (#742).

    Each RPC's deadline is pushed IN via call_with_deadline. Do NOT wrap these in
    asyncio.wait_for: it cancels the request, stranding the connection's pool slot and
    wedging the edge (#734).
    """
    ids = [r.id for r in records]
    if not ids:
        return (), (), (), ()

    async def _batch(name, **extra):
        """One batched RPC; any failure degrades to empty."""
        fn = getattr(orch, name, None)
        if fn is None:
            return {}
        try:
            return await call_with_deadline(fn, ids, timeout=timeout, **extra) or {}
        except Exception:
            log.warning("list_sessions: %s failed for %d sessions", name, len(ids),
                        exc_info=True)
            return {}

    repos, sugg_map, pr_map, sum_map = await asyncio.gather(
        asyncio.gather(*(_resolve_repo(r) for r in records), return_exceptions=True),
        _batch("latest_suggestions_many"),
        _batch("prs_for_many"),
        _batch("latest_summary_many", viewing=viewing),
    )
    resolved_repos = [
        repo if not isinstance(repo, BaseException) else getattr(r, "repo", None)
        for r, repo in zip(records, repos)
    ]
    return (
        resolved_repos,
        [sugg_map.get(r.id) or [] for r in records],
        [pr_map.get(r.id) or [] for r in records],
        [sum_map.get(r.id) or "" for r in records],
    )


async def _autopilot_defer_seconds() -> int | None:
    """Human-deference window (seconds) for the cooldown badge, or None when autopilot is
    disabled/off (→ no badge). Reads the autopilot Automation row directly: the edge process owns
    DB access, and the orchestrator's `_autopilot_read_config` is not exposed on the edge adapter.
    Mirrors the row-read pattern in server/api/automations.py."""
    from sqlalchemy import select
    from server.automations.autopilot import Autopilot
    from server.automations.base import validate_config
    from server.db.models import Automation
    try:
        async with db_session() as db:
            row = (await db.execute(
                select(Automation).where(Automation.id == "autopilot"))).scalar_one_or_none()
        if row is None or not row.enabled or row.mode == "off":
            return None
        cfg = validate_config(Autopilot.config_schema, dict(row.config or {}))
        return int(cfg.get("human_defer_seconds", 120))
    except Exception:
        return None


async def _max_active_sessions_setting() -> int:
    """Effective global active-session cap (``AppSettings.max_active_sessions``,
    falling back to ``DEFAULT_MAX_ACTIVE_SESSIONS`` when unset).

    Mirrors ``SessionOrchestrator._max_active_sessions`` — that method is
    orchestrator-private and not exposed on either adapter (in-process or
    remote), so this reads the row directly instead, the same pattern already
    used by :func:`_autopilot_defer_seconds` in this module. One query for the
    whole list response, not per session.
    """
    from sqlalchemy import select
    from server.db.models import AppSettings, DEFAULT_MAX_ACTIVE_SESSIONS
    async with db_session() as db:
        row = (await db.execute(select(AppSettings))).scalar_one_or_none()
    val = getattr(row, "max_active_sessions", None) if row else None
    return max(1, val if val is not None else DEFAULT_MAX_ACTIVE_SESSIONS)


async def _compute_queue_stall_reason(records: list) -> dict[str, Any] | None:
    """Why the queue can't drain right now, or ``None`` when nothing is wrong.

    The 2026-08-22 deadlock: 15 queued sessions, capacity fully occupied by
    finished sessions that were exempt from auto-archive, and the UI showed
    nothing but a list. Finding the cause took a DB query, a source read, and
    a py-spy dump of a production process — this makes that same fact visible
    on the endpoint that already renders the queue.

    Capacity-occupancy is read straight off ``CAPACITY_EXEMPT_STATUSES`` (Task
    2's source of truth) applied to the same ``records`` the list endpoint
    already fetched — not re-derived, not a second query. Only the numeric cap
    itself requires a DB read (see :func:`_max_active_sessions_setting`).

    Scope is deliberately narrow: this only names the capacity-full cause.
    An empty queue, or a queue with room to start, reports nothing — there is
    nothing to explain.
    """
    from server.db.models import CAPACITY_EXEMPT_STATUSES
    queued_count = sum(1 for r in records if r.status == "queued")
    if queued_count == 0:
        return None
    cap_occupying = sum(1 for r in records if r.status not in CAPACITY_EXEMPT_STATUSES)
    cap = await _max_active_sessions_setting()
    if cap_occupying < cap:
        return None
    return {
        "code": "capacity_full",
        "message": (
            f"{cap_occupying} of {cap} active-session slot(s) are occupied, so "
            f"{queued_count} queued session(s) cannot start. End or archive an "
            "occupying session to free a slot."
        ),
        "capOccupying": cap_occupying,
        "cap": cap,
        "queuedCount": queued_count,
    }


def _get_orchestrator(request: Request) -> "InProcessOrchestratorAdapter":
    return request.app.state.sessions


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
async def list_sessions(
    request: Request,
    _identity: str = Depends(current_identity),
) -> list[dict]:
    """Return all in-memory session records."""
    orch = _get_orchestrator(request)
    records = await orch.list_sessions()
    viewing = request.query_params.get("viewing") in ("1", "true", "yes")
    # Enrich the whole page in three batched RPCs (one per field) rather than three per
    # session: 21 sessions cost 3 daemon round trips, not 63 (#742). Each is bounded by
    # ENRICH_FETCH_TIMEOUT, pushed INTO the RPC — never raced with wait_for (#734).
    repos, suggs, prs, summaries = await _enrich_batch(
        orch, records, viewing, ENRICH_FETCH_TIMEOUT)
    defer_seconds = await _autopilot_defer_seconds()
    positions: dict[str, int] = {}
    _positions_fn = getattr(orch, "session_queue_positions", None)
    if _positions_fn is not None:
        try:
            _pos_res = _positions_fn()
            if inspect.isawaitable(_pos_res):
                _pos_res = await _pos_res
            positions = _pos_res or {}
        except Exception:
            log.warning("list_sessions: session_queue_positions fetch failed", exc_info=True)
    stall_reason = await _compute_queue_stall_reason(records)
    return [
        _record_to_dict(
            r, repo=repo, suggestions=s, prs=pr_list, summary=summary,
            defer_seconds=defer_seconds, queue_position=positions.get(r.id),
            queue_stall_reason=stall_reason if r.status == "queued" else None)
        for r, repo, s, pr_list, summary in zip(records, repos, suggs, prs, summaries)
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_session(
    body: CreateSessionRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Create a new Claude session on the most-headroom account.

    Returns 409 with earliestReset if all accounts are at ceiling.
    """
    orch = _get_orchestrator(request)
    # When the user supplies no name, the session is auto-named (Claude title / first
    # prompt) and stays auto until a manual rename. A supplied name is pinned manual.
    name_is_auto = not (body.name and body.name.strip())
    name = body.name or "New Session"

    # Validate repo format (nameWithOwner) before using it in URLs or filesystem paths.
    if body.repo is not None and not _REPO_RE.match(body.repo):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "code": "invalid_repo",
                    "message": (
                        f"repo must be 'owner/name' with only alphanumeric, '.', '_', '-' characters; "
                        f"got {body.repo!r}"
                    ),
                },
            },
        )

    # Validate account name — must be well-formed before any filesystem access,
    # then must exist in the local credential store.
    _ACCOUNT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
    if body.account is not None:
        if not _ACCOUNT_RE.match(body.account):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": {
                        "code": "unknown_account",
                        "message": f"account {body.account!r} not found",
                    },
                },
            )
        # Check account existence in a provider-aware way: anthropic accounts have a
        # slot file; copilot accounts have a token directory.
        known_accounts = {name for name, _ in _accounts.all_accounts()}
        if body.account not in known_accounts:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": {
                        "code": "unknown_account",
                        "message": f"account {body.account!r} not found",
                    },
                },
            )

    # repo (nameWithOwner) is resolved into a ccswitch-owned clone path — cloning
    # on first use — by the orchestrator itself (server/sessions/orchestrator.py
    # create_session), NOT here.  gitrepos.ensure_clone owns the on-disk clone
    # registry, and in a dual-host deployment the orchestrator may be running in
    # the session daemon on a different host than this edge process: cloning
    # here would put the checkout on the wrong host's filesystem (a live
    # production bug — see server/sessions/remote.py and server/api/repos.py for
    # why ensure_clone is daemon-owned). Passing repo= through and leaving cwd
    # unset lets create_session resolve it wherever it actually runs.

    # Permission mode: explicit → pinned-account default → global Settings default
    # → interactive. One resolver shared with adopt_session (see permissions.py) so
    # no create path can drift back to a hardcoded "interactive".
    _resolved_permission_mode = await resolve_permission_mode(
        db, explicit=body.permissionMode, account=body.account)
    # Model: the pinned account's own default is the only fallback (only when the
    # account is explicitly pinned); "account default" otherwise.
    _resolved_model = body.model
    if body.account and not _resolved_model:
        from ccswitch import ledger as _ledger
        with _ledger.locked_ledger() as _led:
            _resolved_model = _ledger.get_account_defaults(_led, body.account).get("defaultModel")

    try:
        record = await orch.create_session(
            prefer=getattr(body, "host", None),
            name=name,
            account=body.account,
            initial_prompt=body.initialPrompt,
            cwd=None,
            branch=body.branch,
            repo=body.repo,
            identity=identity,
            permission_mode=_resolved_permission_mode or "interactive",
            attachment_ids=body.attachmentIds,
            name_is_auto=name_is_auto,
            model=_resolved_model,
        )
    except UnknownHost as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": {"code": "unknown_host", "message": str(exc)}},
        )
    except NoHostAvailable as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": {"code": "no_host_available", "message": str(exc)}},
        )
    except (gitrepos.CloneError, gitrepos.GhError) as exc:
        # repo→clone resolution now happens inside orch.create_session (on
        # whichever process actually owns the clone registry — the daemon in
        # dual-host mode). The error contract is unchanged: still a 502 with
        # code "clone_failed". In-process this is the real exception raised
        # by gitrepos.ensure_clone; over the daemon RPC boundary it is the
        # same type, reconstructed by RemoteOrchestrator.create_session from
        # the DaemonError envelope (see server/sessions/remote.py).
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "error": {
                    "code": "clone_failed",
                    "message": str(exc),
                },
            },
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "code": "invalid_cwd",
                    "message": str(exc),
                },
            },
        )
    except BranchSwitchError as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "branch_switch_failed",
                    "message": str(exc),
                },
            },
        )
    except ModelBlockedError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="model blocked by policy")
    except AllAccountsBusy as exc:
        if exc.kind == "no_usable_accounts":
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "error": {
                        "code": "accounts_need_reauth",
                        "message": "No usable accounts — one or more need re-authentication",
                    },
                },
            )
        reset_iso = iso_utc(exc.earliest_reset)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "all_accounts_limited",
                    "message": "All accounts are at or above the usage ceiling",
                },
                "earliestReset": reset_iso,
            },
        )

    # Audit: session.create
    await record_audit(
        db=db,
        type="session.create",
        identity=identity,
        detail={"session_id": record.id, "account": record.account, "name": name},
    )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=_record_to_dict(record, repo=await _resolve_repo(record)),
    )


# NOTE: like /archived below, these static routes MUST precede the dynamic
# "/{session_id}" route or FastAPI captures "adoptable"/"adopt" as a session_id.
@router.get("/adoptable")
async def list_adoptable(
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Discover live claude sessions running outside ccswitch.

    Returns ALL session candidates (mapped, unmapped, already-tracked), each
    flagged with a skipReason where applicable — the dashboard renders adoptable
    ones with a force-takeover action and groups the rest as "not adoptable".
    """
    _require_adoption_enabled()
    orch = _get_orchestrator(request)
    # Discovery runs on the daemon host (its /proc, its account map), not this edge
    # process — see orchestrator.list_adoptable_candidates (#709 finding 4).
    return {"candidates": await orch.list_adoptable_candidates()}


@router.post("/adopt", status_code=status.HTTP_201_CREATED)
async def adopt_session_route(
    body: AdoptRequest,
    request: Request,
    identity: str = Depends(current_identity),
) -> dict:
    """Force-take-over an untracked live session: terminate it, resume in place.

    409 when the session is no longer adoptable (process gone, unmapped, or
    already tracked) — an expected outcome given processes churn fast, so the
    dashboard should re-scan rather than treat it as a hard error.
    """
    _require_adoption_enabled()
    orch = _get_orchestrator(request)
    try:
        rec, outcome = await orch.adopt_session(body.claudeSessionId, identity=identity)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="session no longer adoptable",
        )
    return {"session": _record_to_dict(rec), **outcome}


# NOTE: this static route MUST be declared before the dynamic "/{session_id}" route
# below, or FastAPI matches "/archived" against "/{session_id}" (session_id="archived")
# and returns 404 — which silently broke the entire archived-sessions feature.
@router.get("/archived")
async def list_archived_sessions(
    request: Request,
    _identity: str = Depends(current_identity),
) -> list[dict]:
    """Return all archived session metadata."""
    orch = _get_orchestrator(request)
    return await orch.list_archived()


async def _search_ids(items, fetch_text, terms) -> list[str]:
    """items: list[(id, name)]; fetch_text: async (id)->str (transcript text, "" if
    none). Returns matching ids. Blank `terms` skips the fetches entirely.

    `fetch_text` resolves the transcript path AND reads it inside the orchestrator
    (transcript_search_text — daemon RPC in remote mode, in-process call otherwise),
    never in this edge process. Path strings resolved by the daemon are only valid
    on the daemon's own host, so reading them here would silently return "" once the
    daemon and edge are split across hosts — this is exactly that split.
    """
    async def _matches(sid: str, name: str) -> bool:
        text = ""
        if terms:
            # One session whose transcript text can't be fetched (dangling account
            # name, daemon hiccup) degrades to name-only matching; it must not
            # fail the whole search — the UI silently drops a failed filter.
            try:
                text = await fetch_text(sid)
            except Exception:
                log.warning("session.search transcript fetch failed id=%s", sid, exc_info=True)
                text = ""
        return session_matches(terms, name, text)

    flags = await asyncio.gather(*(_matches(i, n) for i, n in items))
    return [i for (i, _), ok in zip(items, flags) if ok]


@router.get("/search")
async def search_sessions(
    request: Request,
    q: str = "",
    scope: Literal["live", "archived"] = "live",
    _identity: str = Depends(current_identity),
) -> dict:
    """Return session IDs whose name or transcript matches every term in `q`.

    `scope=live` (default) searches active sessions; `scope=archived` searches
    archived rows. Registered before GET /{session_id} so FastAPI does not match
    `search` as an id.
    """
    orch = _get_orchestrator(request)
    terms = parse_terms(q)
    if scope == "archived":
        rows = await orch.list_archived()
        items = [(r["id"], r["name"]) for r in rows]
        ids = await _search_ids(items, orch.transcript_search_text, terms)
    else:
        records = await orch.list_sessions()
        items = [(r.id, r.name) for r in records]
        ids = await _search_ids(items, orch.transcript_search_text, terms)
    return {"ids": ids}


# ---------------------------------------------------------------------------
# Batch operations on archived sessions
#
# These static routes MUST be declared before the dynamic "/{session_id}"
# routes below, or "batch" is matched as a session_id.
# ---------------------------------------------------------------------------


class BatchSessionIdsRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1, max_length=200)


@router.post("/batch/unarchive")
async def batch_unarchive_sessions(
    body: BatchSessionIdsRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Unarchive multiple sessions. Best-effort: returns per-id succeeded/failed."""
    orch = _get_orchestrator(request)
    succeeded: list[str] = []
    failed: list[dict] = []
    for session_id in dict.fromkeys(body.ids):  # dedupe, preserve order
        try:
            record = await orch.unarchive(session_id)
        except Exception as exc:  # noqa: BLE001 — best-effort batch must not abort
            log.warning("batch unarchive failed for %s: %s", session_id, exc)
            failed.append({"id": session_id, "error": str(exc)})
            continue
        if record is None:
            failed.append({"id": session_id, "error": "not found"})
            continue
        succeeded.append(session_id)
        await record_audit(
            db=db,
            type="session.unarchive",
            identity=identity,
            detail={"session_id": session_id},
        )
    return {"succeeded": succeeded, "failed": failed}


@router.post("/batch/permanent-delete")
async def batch_permanent_delete_sessions(
    body: BatchSessionIdsRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Permanently delete multiple sessions. Best-effort: returns per-id succeeded/failed."""
    orch = _get_orchestrator(request)
    succeeded: list[str] = []
    failed: list[dict] = []
    for session_id in dict.fromkeys(body.ids):  # dedupe, preserve order
        try:
            deleted = await orch.permanent_delete(session_id)
        except Exception as exc:  # noqa: BLE001 — best-effort batch must not abort
            log.warning("batch permanent-delete failed for %s: %s", session_id, exc)
            failed.append({"id": session_id, "error": str(exc)})
            continue
        if not deleted:
            failed.append({"id": session_id, "error": "not found"})
            continue
        succeeded.append(session_id)
        await record_audit(
            db=db,
            type="session.permanent_delete",
            identity=identity,
            detail={"session_id": session_id},
        )
    return {"succeeded": succeeded, "failed": failed}


@router.post("/batch/archive")
async def batch_archive_sessions(
    body: BatchSessionIdsRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Archive multiple live sessions. Best-effort: returns per-id succeeded/failed."""
    orch = _get_orchestrator(request)
    succeeded: list[str] = []
    failed: list[dict] = []
    for session_id in dict.fromkeys(body.ids):  # dedupe, preserve order
        try:
            await orch.archive_session(session_id)
        except DaemonUnavailable:
            # The daemon may have completed the archive but lost its response.
            if not await _session_is_archived(orch, session_id):
                log.warning("batch archive failed for %s: DaemonUnavailable", session_id)
                failed.append({"id": session_id, "error": "daemon unavailable"})
                continue
        except Exception as exc:  # noqa: BLE001 — best-effort batch must not abort
            log.warning("batch archive failed for %s: %s", session_id, exc)
            failed.append({"id": session_id, "error": str(exc)})
            continue
        succeeded.append(session_id)
        await record_audit(
            db=db,
            type="session.archive",
            identity=identity,
            detail={"session_id": session_id},
        )
    return {"succeeded": succeeded, "failed": failed}


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Return a single session record, or 404 if unknown."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    queue_position: int | None = None
    _positions_fn = getattr(orch, "session_queue_positions", None)
    if _positions_fn is not None:
        try:
            _pos_res = _positions_fn()
            if inspect.isawaitable(_pos_res):
                _pos_res = await _pos_res
            queue_position = (_pos_res or {}).get(session_id)
        except Exception:
            log.warning("get_session: session_queue_positions fetch failed", exc_info=True)
    return _record_to_dict(record, repo=await _resolve_repo(record), queue_position=queue_position)


@router.patch("/{session_id}")
async def rename_session(
    session_id: str,
    body: RenameSessionRequest,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Rename a session. Returns 404 if unknown."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    await orch.rename_session(session_id, body.name)
    # Re-fetch (rename_session updates in place)
    updated = await orch.get_session(session_id)
    final = updated or record
    return _record_to_dict(final, repo=await _resolve_repo(final))


@router.post("/{session_id}/autopilot")
async def set_autopilot_paused(
    session_id: str,
    body: AutopilotPauseRequest,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Pin a session to manual (paused=true) or return it to autopilot governance.
    Returns the updated session record, or 404 if unknown."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    await orch.set_autopilot_paused(session_id, body.paused)
    updated = await orch.get_session(session_id)
    final = updated or record
    return _record_to_dict(final, repo=await _resolve_repo(final))


@router.post("/{session_id}/queue-move")
async def queue_move_session(
    session_id: str,
    body: QueueMoveRequest,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Reorder a queued session. Returns 404 if the session is not queued or unknown."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    ok = await orch.session_queue_move(session_id, body.op, body.position)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_queued", "message": f"Session {session_id!r} is not in the queue"},
        )
    return {"ok": True}


@router.delete("/{session_id}/queue", status_code=status.HTTP_204_NO_CONTENT)
async def queue_cancel_session(
    session_id: str,
    request: Request,
    _identity: str = Depends(current_identity),
):
    """Remove a queued session from the queue and mark it ended. Returns 404 if not queued."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    ok = await orch.session_queue_cancel(session_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_queued", "message": f"Session {session_id!r} is not in the queue"},
        )


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def close_session(
    session_id: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Archive a session (status flip; worktree stays in place, DB row kept).

    Returns 404 if unknown. Disk is reclaimed later, when the
    max_archived_sessions cap purges the row.
    """
    from server.db.models import INACTIVE_SESSION_STATUSES, Session as _DbSession

    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        # Orphaned-session fallback (#898): the owning daemon has lost the in-memory
        # record (process exited, daemon restarted, worktree removed) but the DB row
        # may still exist. A fleet primary loads ALL sessions into _records, but a
        # worker's get_session only sees its own. Verify via DB before returning 404
        # so orphaned sessions don't become permanently undeletable through the UI.
        _db_row = await db.get(_DbSession, session_id)
        if _db_row is None or _db_row.status in INACTIVE_SESSION_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
            )
        # Session is known to DB but not to the owning daemon (orphaned). Proceed:
        # archive_session handles a missing _records entry gracefully (DB-only update).
    try:
        await orch.archive_session(session_id)
    except DaemonUnavailable:
        # The daemon may have completed the archive but lost/timed out its response
        # (it was briefly wedged or restarting). Reconcile against real state rather
        # than surface a spurious 503 for an op that actually took effect: if the
        # session now appears in the archived list, the archive succeeded.
        if not await _session_is_archived(orch, session_id):
            raise

    # Audit: session.archive
    await record_audit(
        db=db,
        type="session.archive",
        identity=identity,
        detail={"session_id": session_id},
    )


@router.post("/{session_id}/unarchive")
async def unarchive_session(
    session_id: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Restore an archived session to idle. Returns 404 if not found or not archived."""
    orch = _get_orchestrator(request)
    try:
        record = await orch.unarchive(session_id)
    except InsufficientStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail={"code": "insufficient_storage", "message": str(exc)},
        )
    except ArchiveMissingError as exc:
        # 410: the session row is valid, but the archived bytes are not where the row
        # says they are. Surfacing this beats silently restoring an empty worktree.
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"code": "archive_missing", "message": str(exc)},
        )
    except DaemonError as exc:
        if exc.err_type == "InsufficientStorageError":
            raise HTTPException(
                status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                detail={"code": "insufficient_storage", "message": exc.message},
            )
        if exc.err_type == "ArchiveMissingError":
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={"code": "archive_missing", "message": exc.message},
            )
        raise
    except DaemonUnavailable:
        # As with archive: the daemon may have restored the session but lost/timed out
        # its response. Reconcile — if the session is live again, the unarchive took
        # effect; return it instead of a spurious 503.
        record = await orch.get_session(session_id)
        if record is None:
            raise
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Archived session {session_id!r} not found"},
        )

    # Audit: session.unarchive
    await record_audit(
        db=db,
        type="session.unarchive",
        identity=identity,
        detail={"session_id": session_id},
    )
    return _record_to_dict(record, repo=await _resolve_repo(record))


@router.post("/{session_id}/prompt")
async def send_session_prompt(
    session_id: str,
    body: SendPromptRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Send a prompt to a session from the dashboard (quick-resume).

    Lazily RESUMES a detached (idle/"Ready") session first — the same ensure_driver
    path the WebSocket prompt flow uses — so one tap from the list gets a stalled
    session moving. 404 if unknown; 409 if the session exists but can't be resumed."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    # Resume the session if it has no live driver (detached). ensure_driver is
    # idempotent for already-live sessions and returns None only if the session
    # can't be brought live.
    driver = await orch.ensure_driver(session_id, identity)
    if driver is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "resume_failed", "message": "Session could not be resumed."},
        )
    await orch.send_prompt(session_id, body.text)
    await record_audit(db, "session.prompt", identity, {"sessionId": session_id})
    updated = await orch.get_session(session_id) or record
    return _record_to_dict(updated, repo=await _resolve_repo(updated))


@router.post("/{session_id}/suggest")
async def generate_session_suggestions(
    session_id: str,
    request: Request,
    identity: str = Depends(current_identity),
) -> dict:
    """Generate fresh next-prompt suggestions for a session via the LLM, on demand.

    Backs the sparkle (✨) action: tapping it ALWAYS asks the model for a new
    suggestion rather than replaying a cached value or a hard-coded fallback (those
    live only in the dropdown). Resume-less and best-effort — never resumes the
    session merely to suggest, and returns an empty list rather than erroring when
    the model yields nothing. 404 only when the session itself is unknown."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    suggestions = await orch.generate_suggestions_now(session_id)
    return {"suggestions": list(suggestions or [])}


@router.post("/{session_id}/fix-pr")
async def fix_session_pr(
    session_id: str,
    body: FixPrRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Manual 'Fix PR #N' tap (propose mode): resume the owning session if
    detached and inject the same branch-aware fix prompt the auto path uses.

    Routes through orch.request_pr_fix so the manual and autonomous fixes are
    one implementation. The auto-mode loop guard does NOT apply here — a human
    tap is always allowed."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    driver = await orch.ensure_driver(session_id, identity)
    if driver is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "resume_failed", "message": "Session could not be resumed."},
        )
    result = await orch.request_pr_fix(session_id, body.number)
    if result != "sent":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": result, "message": f"Cannot fix PR #{body.number}: {result}"},
        )
    await record_audit(db, "session.fix_pr", identity,
                       {"sessionId": session_id, "number": body.number})
    updated = await orch.get_session(session_id) or record
    return _record_to_dict(updated, repo=await _resolve_repo(updated))


@router.delete("/{session_id}/permanent", status_code=status.HTTP_204_NO_CONTENT)
async def permanent_delete_session(
    session_id: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Permanently delete a session: remove archive folder and DB row. 404 if unknown."""
    orch = _get_orchestrator(request)
    deleted = await orch.permanent_delete(session_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )

    # Audit: session.permanent_delete
    await record_audit(
        db=db,
        type="session.permanent_delete",
        identity=identity,
        detail={"session_id": session_id},
    )


@router.get("/{session_id}/transcript")
async def get_session_transcript(
    session_id: str,
    request: Request,
    before: int | None = None,
    limit: int | None = None,
    _identity: str = Depends(current_identity),
) -> dict:
    """Read-only transcript events, always with earliestTurn/totalTurns metadata.

    - no params → the FULL transcript, sub-agent threads included (no silent cap)
    - `limit` alone → the last `limit` turns
    - `before` → a turn-ordinal window for lazy scrollback paging
    """
    from server.db import db_session as _db_session
    from server.db.models import Session as _DbSession
    from server.audit import redact_event
    from sqlalchemy import select as sa_select

    orch = _get_orchestrator(request)

    # Verify the session exists (in _records for active, or DB for archived).
    record = await orch.get_session(session_id)
    if record is None:
        async with _db_session() as _db:
            row = (await _db.execute(
                sa_select(_DbSession).where(_DbSession.id == session_id)
            )).scalars().first()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
            )

    # In-process orchestrator yields event objects (.to_wire()); the remote/daemon
    # orchestrator returns already-serialised wire dicts. Handle both (#172).
    def _wire(e):
        return redact_event(e.to_wire() if hasattr(e, "to_wire") else e)

    if before is not None and limit is None:
        limit = 20  # classic scrollback page size when only a cursor is given
    window = await orch.transcript_window(session_id, before=before, limit=limit)
    if window is None:
        return {"events": [], "earliestTurn": 0, "totalTurns": 0}
    return {
        "events": [_wire(e) for e in window.events],
        "earliestTurn": window.earliest_turn,
        "totalTurns": window.total_turns,
    }


@router.get("/{session_id}/export.md")
async def export_session_markdown(
    session_id: str,
    request: Request,
    _identity: str = Depends(current_identity),
):
    """Download session transcript as a human-readable Markdown file."""
    from server.audit import redact_event
    from server.sessions.export import transcript_markdown, safe_filename
    from server.sessions.orchestrator import TRANSCRIPT_EXPORT_CEILING_S
    orch = _get_orchestrator(request)
    path = await orch.transcript_path(session_id)
    if not path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "not_found", "message": "No transcript"})
    record = await orch.get_session(session_id)
    title = (getattr(record, "name", None) or getattr(record, "title", None) or session_id) if record else session_id
    # Full windowed replay (limit=None): the export must never be silently
    # truncated to a trailing slice of the session (B1).
    #
    # A download is not the session-open critical path, so it gets the larger export
    # ceiling instead of the interactive one. And because transcript_path() above
    # already proved a transcript EXISTS, a None here can only mean the replay timed
    # out or failed — surface that as 504 rather than a 200 with an empty document,
    # which would look to the user like a successful export of an empty session.
    window = await orch.transcript_window(session_id, timeout_s=TRANSCRIPT_EXPORT_CEILING_S)
    if window is None:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"code": "transcript_timeout",
                    "message": "Transcript replay timed out; export not generated"},
        )
    events = window.events
    # In-process orchestrator yields event objects (.to_wire()); the remote/daemon
    # orchestrator returns already-serialised wire dicts. Handle both (#172).
    md = transcript_markdown(
        [redact_event(e.to_wire() if hasattr(e, "to_wire") else e) for e in events],
        title=str(title),
    )
    fname = safe_filename(f"{title}-{session_id}") + ".md"
    return Response(content=md, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/{session_id}/export.jsonl")
async def export_session_jsonl(
    session_id: str,
    request: Request,
    _identity: str = Depends(current_identity),
):
    """Download session transcript as a raw unredacted JSONL file.

    This endpoint streams the SDK's on-disk transcript verbatim — the user's own
    data over an already-authenticated connection (deliberate exception to the
    redaction rule).

    Note: archived sessions whose path only resolves via the DB-glob fallback may
    return 404 here, because transcript_path() relies on in-memory driver state for
    those sessions.  The Markdown export (/export.md) still works for them via
    transcript_events().  This is an accepted limitation.
    """
    from server.sessions.export import safe_filename
    orch = _get_orchestrator(request)
    path = await orch.transcript_path(session_id)
    if not path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "not_found", "message": "No transcript"})

    def _iter():
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    fname = safe_filename(f"{session_id}") + ".jsonl"
    return StreamingResponse(_iter(), media_type="application/x-ndjson",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.post("/{session_id}/model-approval")
async def model_approval(
    session_id: str,
    body: ModelApprovalBody,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Approve or deny a parked model-approval hold for a session.

    Returns the updated session record (200), 404 if unknown, 409 if no
    pending approval to resolve. Bad ``decision`` values yield 422 from the
    ``Literal`` constraint.
    """
    orch = _get_orchestrator(request)
    try:
        rec = await orch.resolve_model_approval(session_id, body.decision, identity=identity)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")
    except ValueError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no pending model approval")
    return _record_to_dict(rec)
