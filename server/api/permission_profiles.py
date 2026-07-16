"""Permission Profile CRUD API (issue #600).

A permission profile is a named, ordered list of tool patterns (e.g.
'Bash(git *)', 'Read') applied at session spawn to set the tool allowlist.

Routes:
  GET    /api/permission-profiles        → list all
  POST   /api/permission-profiles        → create
  GET    /api/permission-profiles/{id}   → get one
  PATCH  /api/permission-profiles/{id}   → partial update
  DELETE /api/permission-profiles/{id}   → delete (204)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import PermissionProfile, _utcnow

router = APIRouter(prefix="/api/permission-profiles", tags=["permission-profiles"])

_VALID_MODES = frozenset({"interactive", "auto", "allowlist", "full-auto"})


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


def _validate_mode(mode: str | None) -> None:
    if mode is not None and mode not in _VALID_MODES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_mode",
                "message": f"mode must be one of: {', '.join(sorted(_VALID_MODES))}",
            },
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
        select(PermissionProfile).order_by(PermissionProfile.name)
    )).scalars().all()
    return [_profile_to_dict(p) for p in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_profile(
    body: ProfileCreateBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Create a named permission profile. Returns 409 if name already exists."""
    _validate_mode(body.mode)

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
    return _profile_to_dict(profile)


@router.patch("/{profile_id}")
async def update_profile(
    profile_id: str,
    body: ProfilePatchBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Partial update. Only fields present in the request body are changed."""
    profile = (await db.execute(
        select(PermissionProfile).where(PermissionProfile.id == profile_id)
    )).scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    fields = body.model_fields_set
    if "name" in fields and body.name:
        if body.name != profile.name:
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
    await db.delete(profile)
    await record_audit(
        db=db, type="permission_profile.delete",
        identity=identity, detail={"id": profile_id},
    )
