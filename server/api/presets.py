"""Session Preset CRUD API (issue #597).

Routes:
  GET    /api/presets           → list all presets ordered by name
  POST   /api/presets           → create a preset
  GET    /api/presets/{id}      → get one preset
  PATCH  /api/presets/{id}      → partial update
  DELETE /api/presets/{id}      → delete (204)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import SessionPreset, _utcnow

router = APIRouter(prefix="/api/presets", tags=["presets"])


def _preset_to_dict(p: SessionPreset) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "repo": p.repo,
        "account": p.account,
        "model": p.model,
        "permissionMode": p.permission_mode,
        "initialPrompt": p.initial_prompt,
        "branchStrategy": p.branch_strategy,
        "permissionProfileId": p.permission_profile_id,
        "createdAt": p.created_at.isoformat() if p.created_at else None,
        "updatedAt": p.updated_at.isoformat() if p.updated_at else None,
    }


class PresetCreateBody(BaseModel):
    name: str
    repo: str | None = None
    account: str | None = None
    model: str | None = None
    permissionMode: str | None = None
    initialPrompt: str | None = None
    branchStrategy: str | None = None
    permissionProfileId: str | None = None


class PresetPatchBody(BaseModel):
    name: str | None = None
    repo: str | None = None
    account: str | None = None
    model: str | None = None
    permissionMode: str | None = None
    initialPrompt: str | None = None
    branchStrategy: str | None = None
    permissionProfileId: str | None = None


@router.get("")
async def list_presets(
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> list[dict]:
    """Return all session presets ordered by name."""
    rows = (await db.execute(select(SessionPreset).order_by(SessionPreset.name))).scalars().all()
    return [_preset_to_dict(p) for p in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_preset(
    body: PresetCreateBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Create a named session preset. Returns 409 if name already exists."""
    existing = (await db.execute(
        select(SessionPreset).where(SessionPreset.name == body.name)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "name_conflict", "message": f"Preset '{body.name}' already exists"},
        )

    preset = SessionPreset(
        name=body.name,
        repo=body.repo,
        account=body.account,
        model=body.model,
        permission_mode=body.permissionMode,
        initial_prompt=body.initialPrompt,
        branch_strategy=body.branchStrategy,
        permission_profile_id=body.permissionProfileId,
    )
    db.add(preset)
    await db.flush()
    await record_audit(db=db, type="preset.create", identity=identity, detail={"name": body.name})
    return _preset_to_dict(preset)


@router.get("/{preset_id}")
async def get_preset(
    preset_id: str,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Return a single preset by id."""
    preset = (await db.execute(
        select(SessionPreset).where(SessionPreset.id == preset_id)
    )).scalar_one_or_none()
    if not preset:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    return _preset_to_dict(preset)


@router.patch("/{preset_id}")
async def update_preset(
    preset_id: str,
    body: PresetPatchBody,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Partial update. Only fields present in the request body are changed."""
    preset = (await db.execute(
        select(SessionPreset).where(SessionPreset.id == preset_id)
    )).scalar_one_or_none()
    if not preset:
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    fields = body.model_fields_set
    if "name" in fields and body.name:
        # Check uniqueness if name is changing
        if body.name != preset.name:
            clash = (await db.execute(
                select(SessionPreset).where(SessionPreset.name == body.name)
            )).scalar_one_or_none()
            if clash:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"error": "name_conflict", "message": f"Preset '{body.name}' already exists"},
                )
        preset.name = body.name
    if "repo" in fields:
        preset.repo = body.repo
    if "account" in fields:
        preset.account = body.account
    if "model" in fields:
        preset.model = body.model
    if "permissionMode" in fields:
        preset.permission_mode = body.permissionMode
    if "initialPrompt" in fields:
        preset.initial_prompt = body.initialPrompt
    if "branchStrategy" in fields:
        preset.branch_strategy = body.branchStrategy
    if "permissionProfileId" in fields:
        preset.permission_profile_id = body.permissionProfileId
    preset.updated_at = _utcnow()

    await db.flush()
    await record_audit(db=db, type="preset.update", identity=identity, detail={"id": preset_id})
    return _preset_to_dict(preset)


@router.delete("/{preset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_preset(
    preset_id: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> None:
    """Delete a preset. Returns 204."""
    preset = (await db.execute(
        select(SessionPreset).where(SessionPreset.id == preset_id)
    )).scalar_one_or_none()
    if not preset:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    await db.delete(preset)
    await record_audit(db=db, type="preset.delete", identity=identity, detail={"id": preset_id})
