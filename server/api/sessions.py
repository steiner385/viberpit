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

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server.auth.middleware import current_identity
from server.audit import record_audit
from server.db import get_db
from server.sessions.orchestrator import SessionOrchestrator, BranchSwitchError
from server.sessions.selection import AllAccountsBusy

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    name: str | None = None
    account: str | None = None
    initialPrompt: str | None = None
    cwd: str | None = None
    branch: str | None = None


class RenameSessionRequest(BaseModel):
    name: str


def _record_to_dict(record) -> dict[str, Any]:
    """Convert an in-memory SessionRecord to the wire-format dict."""
    return {
        "id": record.id,
        "name": record.name,
        "accountName": record.account,
        "status": "working",  # in-memory sessions are always working
        "createdAt": None,    # available from DB; omit for simplicity in-memory
        "updatedAt": None,
    }


def _get_orchestrator(request: Request) -> SessionOrchestrator:
    return request.app.state.orchestrator


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
async def list_sessions(
    request: Request,
    _identity: str = Depends(current_identity),
) -> list[dict]:
    """Return all in-memory session records."""
    orch: SessionOrchestrator = _get_orchestrator(request)
    records = orch.list_sessions()
    return [_record_to_dict(r) for r in records]


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
    orch: SessionOrchestrator = _get_orchestrator(request)
    name = body.name or "New Session"

    try:
        record = await orch.create_session(
            name=name,
            account=body.account,
            initial_prompt=body.initialPrompt,
            cwd=body.cwd,
            branch=body.branch,
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
        content=_record_to_dict(record),
    )


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Return a single session record, or 404 if unknown."""
    orch: SessionOrchestrator = _get_orchestrator(request)
    record = orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    return _record_to_dict(record)


@router.patch("/{session_id}")
async def rename_session(
    session_id: str,
    body: RenameSessionRequest,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Rename a session. Returns 404 if unknown."""
    orch: SessionOrchestrator = _get_orchestrator(request)
    record = orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    await orch.rename_session(session_id, body.name)
    # Re-fetch (rename_session updates in place)
    updated = orch.get_session(session_id)
    return _record_to_dict(updated or record)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def close_session(
    session_id: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
):
    """Close/terminate a session. Returns 404 if unknown."""
    orch: SessionOrchestrator = _get_orchestrator(request)
    record = orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    await orch.close_session(session_id)

    # Audit: session.close
    await record_audit(
        db=db,
        type="session.close",
        identity=identity,
        detail={"session_id": session_id},
    )
