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
import logging
import re
from typing import Any, TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ccswitch import accounts as _accounts
from ccswitch import gitrepos
from server.auth.middleware import current_identity
from server.sessions import adopt as _adopt
from server.audit import record_audit
from server.config import get_settings
from server.db import get_db
from server.sessions.orchestrator import BranchSwitchError
from server.sessions.selection import AllAccountsBusy

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


class RenameSessionRequest(BaseModel):
    name: str


class AdoptRequest(BaseModel):
    claudeSessionId: str


_UNSET = object()


def _record_to_dict(record, repo: Any = _UNSET) -> dict[str, Any]:
    """Convert an in-memory SessionRecord to the wire-format dict.

    ``repo`` lets callers inject a value read live from the session's working
    directory (see :func:`_resolve_repo`); when omitted, the stored
    ``record.repo`` is used as a fallback.
    """
    return {
        "id": record.id,
        "name": record.name,
        "accountName": record.account,
        "repo": record.repo if repo is _UNSET else repo,
        "status": record.status,
        "nameIsAuto": getattr(record, "name_is_auto", True),
        "detached": record.detached,
        "adoptedInPlace": getattr(record, "adopted_in_place", False),
        "subAgents": getattr(record, "sub_agents", None) or [],
        "createdAt": record.created_at.isoformat() if record.created_at else None,
        "updatedAt": None,
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
        "lastActivity": c.last_activity.isoformat() if c.last_activity else None,
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
    # Read each session's repo live from its working dir, all in parallel.
    repos = await asyncio.gather(*(_resolve_repo(r) for r in records))
    return [_record_to_dict(r, repo=repo) for r, repo in zip(records, repos)]


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
        earliest = exc.earliest_reset
        reset_iso = earliest.isoformat() if earliest is not None else None
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
    _identity: str = Depends(current_identity),
) -> dict:
    """Return read-only transcript events for a session (active or archived)."""
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

    events = await orch.transcript_events(session_id)
    wire_events = [redact_event(e.to_wire()) for e in events]
    return {"events": wire_events}
