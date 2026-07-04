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
import re
from typing import Any, Literal, TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from ccswitch import accounts as _accounts
from ccswitch import gitrepos
from server.auth.middleware import current_identity
from server.sessions import adopt as _adopt
from server.sessions.search import transcript_text, parse_terms, session_matches
from server.audit import record_audit
from server.config import get_settings
from server.db import get_db
from server.sessions.orchestrator import BranchSwitchError
from server.sessions.selection import AllAccountsBusy
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


class RenameSessionRequest(BaseModel):
    name: str


class AutopilotPauseRequest(BaseModel):
    paused: bool


class AdoptRequest(BaseModel):
    claudeSessionId: str


class SendPromptRequest(BaseModel):
    text: str


class FixPrRequest(BaseModel):
    number: int


_UNSET = object()


def _record_to_dict(
    record, repo: Any = _UNSET, suggestions: list[str] | None = None,
    prs: list | None = None, summary: str = "",
) -> dict[str, Any]:
    """Convert an in-memory SessionRecord to the wire-format dict.

    ``repo`` lets callers inject a value read live from the session's working
    directory (see :func:`_resolve_repo`); when omitted, the stored
    ``record.repo`` is used as a fallback.

    ``suggestions`` holds the latest cached next-prompt suggestions for the
    session (from ``orch.latest_suggestions``). Defaults to [] when omitted
    (detached sessions or none yet generated).

    ``prs`` is the list of cached PullRequestInfo for the session's authored
    branches (see :func:`_resolve_prs`).  Serialised to a list of dicts on the wire.

    ``summary`` is the one-line status summary for the session (from
    ``orch.latest_summary``). Defaults to "" when omitted or unavailable.
    """
    from server.sessions.goals import effective_goal
    goal_text, goal_source = effective_goal(record)
    return {
        "id": record.id,
        "name": record.name,
        "accountName": record.account,
        "repo": record.repo if repo is _UNSET else repo,
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
        "latestSuggestions": list(suggestions or []),
        "activeGoal": getattr(record, "active_goal", None),
        "inferredGoal": getattr(record, "inferred_goal", None),
        # The reckoned goal autopilot drives toward, resolved through the single
        # effective_goal() helper (explicit /goal wins, else the title fallback).
        "effectiveGoal": goal_text,
        "goalSource": goal_source,
        "autopilotPaused": getattr(record, "autopilot_paused", False),
        "autoArchiveOptOut": getattr(record, "auto_archive_opt_out_reason", None),
        "prs": [p.to_dict() for p in (prs or [])],
        "usageUnverified": getattr(record, "usage_unverified", False),
        "interruptedByRestart": getattr(record, "interrupted_by_restart", False),
        "summary": summary or "",
    }


def _candidate_to_dict(c) -> dict[str, Any]:
    """Convert an AdoptionCandidate to the wire-format dict for the dashboard."""
    return {
        "pid": c.pid,
        "account": c.account,
        "cwd": c.cwd,
        "claudeSessionId": c.claude_session_id,
        "name": c.name,
        "mapped": c.mapped,
        "tracked": c.tracked,
        "skipReason": c.skip_reason,
        "lastActivity": iso_utc(c.last_activity),
        "pidAlive": c.pid_alive,
    }


async def _resolve_repo(record) -> str | None:
    """Read the repo indicator live from the session's working dir.

    Runs git in ``record.cwd`` (off the event loop) so the sessions screen shows
    what's actually checked out — a worktree resolves to its clone's origin, and
    a scratch dir with no git remote reports None ("Scratch"). Falls back to the
    stored ``record.repo`` only when the session has no working directory.
    """
    cwd = getattr(record, "cwd", None)
    if not cwd:
        return record.repo
    return await asyncio.to_thread(gitrepos.repo_at_path, cwd)


async def _resolve_prs(orch, record):
    """Resolve a record's PR list via ``orch.prs_for``, sync (in-process) or async (remote).

    Returns ``list[PullRequestInfo]``; an orchestrator with no ``prs_for`` (older shim)
    yields ``[]`` rather than raising.
    """
    fn = getattr(orch, "prs_for", None)
    if fn is None:
        return []
    res = fn(record.id)
    if inspect.isawaitable(res):
        res = await res
    return res or []


async def _resolve_summary(orch, record, viewing: bool) -> str:
    """Resolve a record's one-line status summary via ``orch.latest_summary``.

    Returns "" for an orchestrator shim without ``latest_summary`` (never raises),
    mirroring the defensiveness of :func:`_resolve_prs`.
    """
    fn = getattr(orch, "latest_summary", None)
    if fn is None:
        return ""
    res = fn(record.id, viewing=viewing)
    if inspect.isawaitable(res):
        res = await res
    return res or ""


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
    # Read each session's repo (live, from its cwd), cached suggestions, PR list, and
    # status summary in parallel.
    repos = await asyncio.gather(*(_resolve_repo(r) for r in records))
    suggs = await asyncio.gather(*(orch.latest_suggestions(r.id) for r in records))
    prs = await asyncio.gather(*(_resolve_prs(orch, r) for r in records))
    summaries = await asyncio.gather(*(_resolve_summary(orch, r, viewing) for r in records))
    return [
        _record_to_dict(r, repo=repo, suggestions=s, prs=pr_list, summary=summary)
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
        from ccswitch import paths as _paths
        if not _paths.slot_path(body.account).exists():
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": {
                        "code": "unknown_account",
                        "message": f"account {body.account!r} not found",
                    },
                },
            )

    # Resolve the repo (nameWithOwner) into a ccswitch-owned clone path, cloning
    # on first use.  The user's real ~/GitHub checkouts are never touched — all
    # clone + worktree activity lives under <repo_root>/.ccswitch/.
    cwd: str | None = None
    if body.repo:
        settings = get_settings()
        url = f"https://github.com/{body.repo}.git"
        try:
            cwd = await asyncio.to_thread(
                gitrepos.ensure_clone, settings.repo_root, body.repo, url
            )
        except (gitrepos.CloneError, gitrepos.GhError) as exc:
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content={
                    "error": {
                        "code": "clone_failed",
                        "message": str(exc),
                    },
                },
            )

    try:
        record = await orch.create_session(
            name=name,
            account=body.account,
            initial_prompt=body.initialPrompt,
            cwd=cwd,
            branch=body.branch,
            repo=body.repo,
            identity=identity,
            permission_mode=body.permissionMode or "interactive",
            attachment_ids=body.attachmentIds,
            name_is_auto=name_is_auto,
            model=body.model,
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
    known = [a["name"] for a in _accounts.list_accounts()]
    tracked = {r.claude_session_id for r in await orch.list_sessions() if r.claude_session_id}
    cands = await asyncio.to_thread(_adopt.discover_candidates, known, tracked)
    return {"candidates": [_candidate_to_dict(c) for c in cands]}


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


async def _search_ids(items, resolve, terms) -> list[str]:
    """items: list[(id, name)]; resolve: async (id)->path|None. Returns matching ids.

    The transcript file is read here in the edge process (see note below) for each
    candidate; blank `terms` skips the reads entirely.
    """
    async def _matches(sid: str, name: str) -> bool:
        text = ""
        if terms:
            path = await resolve(sid)
            if path:
                # Read happens in the edge process (not the daemon). Safe because the
                # daemon shares the host filesystem over a local Unix socket. If the
                # daemon is ever split to another host, move this behind a daemon RPC.
                text = await asyncio.to_thread(transcript_text, path)
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
        ids = await _search_ids(items, orch._resolve_transcript_path, terms)
    else:
        records = await orch.list_sessions()
        items = [(r.id, r.name) for r in records]
        ids = await _search_ids(items, orch.transcript_path, terms)
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
    return _record_to_dict(record, repo=await _resolve_repo(record))


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


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def close_session(
    session_id: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Archive a session (moves cwd to archive, keeps DB row). Returns 404 if unknown."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    await orch.archive_session(session_id)

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
    record = await orch.unarchive(session_id)
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
    limit: int = 20,
    _identity: str = Depends(current_identity),
) -> dict:
    """Read-only transcript events. With `before`, returns a turn-ordinal window
    plus earliestTurn/totalTurns for lazy scrollback paging."""
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

    if before is None:
        events = await orch.transcript_events(session_id)
        return {"events": [_wire(e) for e in events]}

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
    orch = _get_orchestrator(request)
    path = await orch.transcript_path(session_id)
    if not path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "not_found", "message": "No transcript"})
    record = await orch.get_session(session_id)
    title = (getattr(record, "name", None) or getattr(record, "title", None) or session_id) if record else session_id
    events = await orch.transcript_events(session_id)
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
