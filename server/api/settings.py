"""Settings API — the single app-settings row (permission defaults, etc.)."""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select

from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import AppSettings, DEFAULT_MAX_ACTIVE_SESSIONS

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

_VALID_MODES = {"interactive", "auto", "allowlist", "full-auto"}
_VALID_TOOL_DETAIL = {"auto", "all-closed", "all-open"}
# Sanity ceiling for the archived-session cap — guards against fat-fingered values.
_MAX_ARCHIVED_CEILING = 100_000


class SettingsBody(BaseModel):
    defaultPermissionMode: str | None = None
    thinkingCollapsed: bool | None = None
    repoRoot: str | None = None
    defaultToolDetail: str | None = None
    # Max retained archived sessions. Omitted (None) → unchanged; >=1 → that cap;
    # <=0 → cleared to unlimited. So the UI sends 0 to mean "unlimited", never null.
    maxArchivedSessions: int | None = None
    # Archive home path. None → unchanged; "" → clear to default; path → set.
    archiveHome: str | None = None
    # Max concurrently active sessions. Omitted (None) → unchanged; >=1 → that cap;
    # <=0 → clamped to 1 (minimum). UI shows "5 (default)" as placeholder when null.
    maxActiveSessions: int | None = None


async def _migrate_archive_home(db, old_home: str | None, new_home: str | None) -> None:
    """Move all archived session directories from old_home to new_home.

    Resolves both paths using the same fallback logic as _effective_archive_home()
    in the orchestrator (DB value → CCSWITCH_ARCHIVE_HOME env → default).
    Runs the filesystem moves off the event loop; failures are logged but do not
    abort the settings update so the DB row is always written.
    """
    from ccswitch.paths import archive_dir
    from server.db.models import Session as DbSession

    old_dir = Path(old_home) if old_home else archive_dir()
    new_dir = Path(new_home) if new_home else archive_dir()
    if old_dir == new_dir:
        return

    archived_ids: list[str] = list((await db.execute(
        select(DbSession.id).where(DbSession.status == "archived")
    )).scalars().all())

    if not archived_ids:
        return

    def _move_all() -> int:
        new_dir.mkdir(parents=True, exist_ok=True)
        failures = 0
        for sid in archived_ids:
            src = old_dir / sid
            if src.exists():
                dst = new_dir / sid
                if not dst.exists():
                    try:
                        shutil.move(str(src), str(dst))
                    except Exception:
                        log.exception("archive_home migrate: %s → %s failed", src, dst)
                        failures += 1
        return failures

    failures = await asyncio.to_thread(_move_all)
    if failures:
        log.warning("archive_home migrate: %d/%d moves failed", failures, len(archived_ids))
    else:
        log.info("archive_home migrate: moved %d sessions %s → %s",
                 len(archived_ids), old_dir, new_dir)


def _cap_raised(old_cap: int | None, new_cap: int) -> bool:
    """Check if cap_raised: compare against effective defaults.

    NULL max_active_sessions means DEFAULT_MAX_ACTIVE_SESSIONS (5), so setting
    a first explicit value only counts as a raise when it exceeds the default.
    """
    effective_old = old_cap if old_cap is not None else DEFAULT_MAX_ACTIVE_SESSIONS
    return new_cap > effective_old


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
        "defaultToolDetail": s.default_tool_detail or "auto",
        "maxArchivedSessions": s.max_archived_sessions,
        "archiveHome": s.archive_home,
        "maxActiveSessions": s.max_active_sessions,
    }


@router.get("")
async def get_settings_row(_identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    return _to_dict(await _get_or_create(db))


@router.put("")
async def put_settings_row(
    request: Request,
    body: SettingsBody,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    row = await _get_or_create(db)
    if body.defaultPermissionMode is not None and body.defaultPermissionMode in _VALID_MODES:
        row.default_permission_mode = body.defaultPermissionMode
    if body.thinkingCollapsed is not None:
        row.thinking_collapsed = body.thinkingCollapsed
    if body.repoRoot is not None:
        row.repo_root = body.repoRoot
    if body.defaultToolDetail is not None and body.defaultToolDetail in _VALID_TOOL_DETAIL:
        row.default_tool_detail = body.defaultToolDetail
    if body.maxArchivedSessions is not None:
        # <=0 clears to unlimited; otherwise clamp to a sane ceiling.
        row.max_archived_sessions = (
            min(body.maxArchivedSessions, _MAX_ARCHIVED_CEILING)
            if body.maxArchivedSessions >= 1 else None
        )
    if body.archiveHome is not None:
        new_home = body.archiveHome.strip() or None
        if new_home != row.archive_home:
            await _migrate_archive_home(db, row.archive_home, new_home)
        row.archive_home = new_home
    if body.maxActiveSessions is not None:
        old_cap = row.max_active_sessions
        new_cap = max(1, body.maxActiveSessions)
        row.max_active_sessions = new_cap
        # When the cap increases (or was previously unlimited and is now set),
        # fire a drain so newly-available slots are filled from the queue.
        cap_raised = _cap_raised(old_cap, new_cap)
        if cap_raised:
            try:
                orch = request.app.state.sessions
                if hasattr(orch, "maybe_start_queued"):
                    # Remote proxy path — Task 6 will register this RPC.
                    asyncio.create_task(orch.maybe_start_queued())
                elif hasattr(orch, "_maybe_start_queued"):
                    asyncio.create_task(orch._maybe_start_queued())
            except AttributeError:
                # Orchestrator not yet attached (e.g. during tests with minimal app).
                log.debug("settings.put: orchestrator unavailable for queue drain")
    await db.flush()
    return _to_dict(row)
