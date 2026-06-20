"""Settings API — the single app-settings row (permission defaults, etc.)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import AppSettings

router = APIRouter(prefix="/api/settings", tags=["settings"])

_VALID_MODES = {"interactive", "auto", "allowlist", "full-auto"}


class SettingsBody(BaseModel):
    defaultPermissionMode: str | None = None
    thinkingCollapsed: bool | None = None
    repoRoot: str | None = None


async def _get_or_create(db) -> AppSettings:
    row = (await db.execute(select(AppSettings).where(AppSettings.id == "singleton"))).scalar_one_or_none()
    if row is None:
        row = AppSettings(id="singleton")
        db.add(row)
        await db.flush()
    return row


def _to_dict(s: AppSettings) -> dict:
    return {
        "defaultPermissionMode": s.default_permission_mode,
        "thinkingCollapsed": s.thinking_collapsed,
        "repoRoot": s.repo_root,
    }


@router.get("")
async def get_settings_row(_identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    return _to_dict(await _get_or_create(db))


@router.put("")
async def put_settings_row(body: SettingsBody, _identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    row = await _get_or_create(db)
    if body.defaultPermissionMode is not None and body.defaultPermissionMode in _VALID_MODES:
        row.default_permission_mode = body.defaultPermissionMode
    if body.thinkingCollapsed is not None:
        row.thinking_collapsed = body.thinkingCollapsed
    if body.repoRoot is not None:
        row.repo_root = body.repoRoot
    await db.flush()
    return _to_dict(row)
