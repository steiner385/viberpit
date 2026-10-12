"""Permission Profile CRUD API (issue #600).

A permission profile is a named, ordered list of tool patterns (e.g.
'Bash(npm test:*)', 'Edit(src/**)') chosen at session launch. A session's launch
profile is enforced like a session-scope always-allow rule set
(``server/permission_rules/gate.py``), so a pattern the rule safety check refuses
cannot be saved, and only a person in the UI may write patterns.

Routes:
  GET    /api/permission-profiles        → list all
  POST   /api/permission-profiles        → create
  GET    /api/permission-profiles/{id}   → get one
  PATCH  /api/permission-profiles/{id}   → partial update
  DELETE /api/permission-profiles/{id}   → delete (204)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select

from server.audit import record_audit
from server.auth.identity import human_gate_via
from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import PermissionProfile, _utcnow
from server.permission_rules.safety import assess
from server.permission_rules.store import PROFILE_PREFIX
from server.sessions.session_actions import ActionError, require_human
from server.sessions.types import VALID_SESSION_MODES

router = APIRouter(prefix="/api/permission-profiles", tags=["permission-profiles"])

# Canonical mode vocabulary lives in server.sessions.types (single source of
# truth); a profile may set any real session mode.
_VALID_MODES = VALID_SESSION_MODES


def _profile_to_dict(p: PermissionProfile) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "mode": p.mode,
        "patterns": p.patterns or [],
        "createdAt": p.created_at.isoformat() if p.created_at else None,
        "updatedAt": p.updated_at.isoformat() if p.updated_at else None,
    }


def _managed_profile_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": "managed_profile",
                "message": "Always-allow rule sets are managed from the rules API"},
    )


def _validate_mode(mode: str | None) -> None:
    if mode is not None and mode not in _VALID_MODES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_mode",
                "message": f"mode must be one of: {', '.join(sorted(_VALID_MODES))}",
            },
        )


def _validate_patterns(patterns: list[str] | None, identity: str, request: Request) -> None:
    """Launch-profile patterns are enforced, so they meet the always-allow rule bar: written
    by a person (403) and none refused by the safety check (400). A warning-level pattern
    is allowed; choosing to save it in a named profile is the confirmation."""
    if not patterns:
        return
    try:
        # A local process asserting the operator's identity is not the UI (#2980).
        require_human(identity, human_gate_via(request))
    except ActionError as exc:
        raise HTTPException(status_code=exc.http,
                            detail={"error": exc.code, "message": exc.message}) from None
    reasons = [f"{p}: {'; '.join(a.reasons)}"
               for p in patterns if (a := assess(p, "session")).level == "refuse"]
    if reasons:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "pattern_refused",
                    "message": "Some patterns are too broad or unsafe to allow.",
                    "reasons": reasons},
        )


class ProfileCreateBody(BaseModel):
    name: str
    description: str | None = None
    mode: str | None = None
    patterns: list[str] | None = None


class ProfilePatchBody(BaseModel):
    name: str | None = None
    description: str | None = None
    mode: str | None = None
    patterns: list[str] | None = None


@router.get("")
async def list_profiles(
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> list[dict]:
    """Return all permission profiles ordered by name."""
    rows = (await db.execute(
        select(PermissionProfile).where(PermissionProfile.scope.is_(None))
        .order_by(PermissionProfile.name)
    )).scalars().all()
    return [_profile_to_dict(p) for p in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_profile(
    body: ProfileCreateBody,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Create a named permission profile. Returns 409 if name already exists."""
    _validate_mode(body.mode)
    _validate_patterns(body.patterns, identity, request)
    if body.name.startswith(PROFILE_PREFIX):
        raise _managed_profile_error()

    existing = (await db.execute(
        select(PermissionProfile).where(PermissionProfile.name == body.name)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "name_conflict", "message": f"Profile '{body.name}' already exists"},
        )

    profile = PermissionProfile(
        name=body.name,
        description=body.description,
        mode=body.mode,
        patterns=body.patterns,
    )
    db.add(profile)
    await db.flush()
    await record_audit(
        db=db, type="permission_profile.create",
        identity=identity, detail={"name": body.name},
    )
    return _profile_to_dict(profile)


@router.get("/{profile_id}")
async def get_profile(
    profile_id: str,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Return a single permission profile by id."""
    profile = (await db.execute(
        select(PermissionProfile).where(PermissionProfile.id == profile_id)
    )).scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    if profile.scope is not None:
        raise _managed_profile_error()
    return _profile_to_dict(profile)


@router.patch("/{profile_id}")
async def update_profile(
    profile_id: str,
    body: ProfilePatchBody,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Partial update. Only fields present in the request body are changed."""
    profile = (await db.execute(
        select(PermissionProfile).where(PermissionProfile.id == profile_id)
    )).scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    if profile.scope is not None:
        raise _managed_profile_error()

    fields = body.model_fields_set
    if "name" in fields and body.name:
        if body.name != profile.name:
            if body.name.startswith(PROFILE_PREFIX):
                raise _managed_profile_error()
            clash = (await db.execute(
                select(PermissionProfile).where(PermissionProfile.name == body.name)
            )).scalar_one_or_none()
            if clash:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"error": "name_conflict", "message": f"Profile '{body.name}' already exists"},
                )
        profile.name = body.name
    if "description" in fields:
        profile.description = body.description
    if "mode" in fields:
        _validate_mode(body.mode)
        profile.mode = body.mode
    if "patterns" in fields:
        _validate_patterns(body.patterns, identity, request)
        profile.patterns = body.patterns
    profile.updated_at = _utcnow()

    await db.flush()
    await record_audit(
        db=db, type="permission_profile.update",
        identity=identity, detail={"id": profile_id},
    )
    return _profile_to_dict(profile)


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(
    profile_id: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> None:
    """Delete a permission profile. Returns 204."""
    profile = (await db.execute(
        select(PermissionProfile).where(PermissionProfile.id == profile_id)
    )).scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    if profile.scope is not None:
        raise _managed_profile_error()
    await db.delete(profile)
    await record_audit(
        db=db, type="permission_profile.delete",
        identity=identity, detail={"id": profile_id},
    )
