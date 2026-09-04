"""Settings API — the single app-settings row (permission defaults, etc.)."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from server.auth.middleware import current_identity
from server.config import get_settings
from server.db import get_db
from server.db.models import AppSettings, DEFAULT_MAX_ACTIVE_SESSIONS
from server.hostname import self_host_name
from server.sessions.types import VALID_SESSION_MODES
from server.version import running_git_sha

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Membership lives in server.sessions.types.VALID_SESSION_MODES (the single source
# of truth for "what modes exist"); this tuple only fixes the client's display order,
# since a frozenset cannot carry order. A pytest parity test reads
# web/src/session/permissionModes.ts and asserts equality, so adding a mode on either
# side without the other fails CI (S20).
ACCEPTED_PERMISSION_MODES: tuple[str, ...] = (
    "interactive", "auto", "allowlist", "acceptEdits", "full-auto", "bypassPermissions",
)
assert frozenset(ACCEPTED_PERMISSION_MODES) == VALID_SESSION_MODES, \
    "settings: ACCEPTED_PERMISSION_MODES must list exactly VALID_SESSION_MODES"
_VALID_MODES = VALID_SESSION_MODES
_VALID_TOOL_DETAIL = frozenset({"auto", "all-closed", "all-open"})
# Sanity ceiling for the archived-session cap — guards against fat-fingered values.
_MAX_ARCHIVED_CEILING = 100_000

# Read-only env flags shown on Settings › About (spec S30). An ALLOWLIST: a key
# not named here is never sent, so secrets and allowlists cannot leak by accident.
# (config attribute, env var name, one-sentence description)
_MANAGED: tuple[tuple[str, str, str], ...] = (
    ("allow_unauthenticated", "CCSWITCH_ALLOW_UNAUTHENTICATED", "Admit clients with no Tailscale identity as a guest"),
    ("enable_adoption", "CCSWITCH_ENABLE_ADOPTION", "Allow adopting sessions started outside ccswitch"),
    ("enable_copilot", "CCSWITCH_ENABLE_COPILOT", "Enable GitHub Copilot accounts"),
    ("enable_xvfb", "CCSWITCH_ENABLE_XVFB", "Virtual displays for browser-using sessions"),
    ("self_model_switch_enabled", "CCSWITCH_SELF_MODEL_SWITCH_ENABLED", "Sessions may request a model change"),
    ("self_model_switch_cap", "CCSWITCH_SELF_MODEL_SWITCH_CAP", "Auto-applied model switches per session before parking for approval"),
    ("gh_reconcile_enabled", "CCSWITCH_GH_RECONCILE_ENABLED", "GitHub Projects reconciler may write back"),
    ("log_level", "CCSWITCH_LOG_LEVEL", "Server log level"),
    ("client_log_level", "CCSWITCH_CLIENT_LOG_LEVEL", "Browser log level"),
)


def _server_blocks() -> dict[str, object]:
    cfg = get_settings()
    # Route through the app's identity sources of truth rather than re-reading env
    # here: server.hostname.self_host_name is what the deploy controller and
    # orchestrator use to name this process, and server.version.running_git_sha is
    # what /healthz reports — About must agree with both (constitution VI).
    sha = running_git_sha()
    return {
        "acceptedPermissionModes": list(ACCEPTED_PERMISSION_MODES),
        "effectiveDefaults": {
            "repoRoot": cfg.repo_root,
            "archiveHome": cfg.archive_home,
            "modelApprovalAction": cfg.model_approval_action,
            "modelApprovalDenylist": cfg.model_approval_denylist,
            "maxActiveSessions": DEFAULT_MAX_ACTIVE_SESSIONS,
        },
        "managed": [
            {"key": env, "value": getattr(cfg, attr), "description": desc}
            for attr, env, desc in _MANAGED
        ],
        "about": {
            # running_git_sha() degrades to the "unknown" sentinel with no .git dir;
            # the API contract is gitSha: string | null, so that maps to None.
            "gitSha": None if sha == "unknown" else sha,
            "hostName": self_host_name(),
        },
    }


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
    # App-wide auto-archive toggle. Omitted (None) → unchanged; true/false sets it.
    # Exposed to the UI as a plain bool (the storage-level NULL default surfaces as true).
    autoArchiveEnabled: bool | None = None
    # Global model-approval policy. Uses model_fields_set so explicit null clears to
    # env fallback. action ∈ VALID_ACTIONS or null; denylist is a free-text comma list.
    modelApprovalAction: str | None = None
    modelApprovalDenylist: str | None = None


async def _migrate_archive_home(orch, db, old_home: str | None, new_home: str | None) -> tuple[int, int, int]:
    """Move every archived session dir from old_home to new_home on the daemon host.

    Returns (moved, failures, total). The caller decides whether to persist the
    new path — a partial move is a 409, not a warning, because a DB row pointing
    at a directory the files are not in is worse than the old path (S16).
    """
    from server.db.models import Session as DbSession

    archived_ids: list[str] = list((await db.execute(
        select(DbSession.id).where(DbSession.status == "archived")
    )).scalars().all())
    if not archived_ids:
        return (0, 0, 0)

    result = await orch.migrate_archive_home(old_home, new_home, archived_ids)
    moved, failures = int(result.get("moved", 0)), int(result.get("failures", 0))
    if failures:
        log.warning("archive_home migrate: %d/%d moves failed", failures, len(archived_ids))
    else:
        log.info("archive_home migrate: moved %d/%d sessions %s → %s",
                 moved, len(archived_ids), old_home, new_home)
    return (moved, failures, len(archived_ids))


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
        # NULL (unset) surfaces as enabled — the client sees a plain bool.
        "autoArchiveEnabled": s.auto_archive_enabled is not False,
        "modelApprovalAction": s.model_approval_action,
        "modelApprovalDenylist": s.model_approval_denylist,
        **_server_blocks(),
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
    if body.defaultPermissionMode is not None:
        if body.defaultPermissionMode not in _VALID_MODES:
            raise HTTPException(status_code=422, detail="invalid defaultPermissionMode")
        row.default_permission_mode = body.defaultPermissionMode
    if body.thinkingCollapsed is not None:
        row.thinking_collapsed = body.thinkingCollapsed
    if body.repoRoot is not None:
        row.repo_root = body.repoRoot
    if body.defaultToolDetail is not None:
        if body.defaultToolDetail not in _VALID_TOOL_DETAIL:
            raise HTTPException(status_code=422, detail="invalid defaultToolDetail")
        row.default_tool_detail = body.defaultToolDetail
    if body.autoArchiveEnabled is not None:
        row.auto_archive_enabled = body.autoArchiveEnabled
    if body.maxArchivedSessions is not None:
        # <=0 clears to unlimited; otherwise clamp to a sane ceiling.
        row.max_archived_sessions = (
            min(body.maxArchivedSessions, _MAX_ARCHIVED_CEILING)
            if body.maxArchivedSessions >= 1 else None
        )
    if body.archiveHome is not None:
        new_home = body.archiveHome.strip() or None
        if new_home != row.archive_home:
            moved, failed, total = await _migrate_archive_home(
                request.app.state.sessions, db, row.archive_home, new_home)
            if failed:
                raise HTTPException(status_code=409, detail={
                    "code": "archive_move_failed", "moved": moved, "failed": failed, "total": total,
                })
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
                    asyncio.create_task(orch.maybe_start_queued())
            except AttributeError:
                # Orchestrator not yet attached (e.g. during tests with minimal app).
                log.debug("settings.put: orchestrator unavailable for queue drain")
    from server.sessions.model_policy import VALID_ACTIONS
    if "modelApprovalAction" in body.model_fields_set:
        if body.modelApprovalAction is not None and body.modelApprovalAction not in VALID_ACTIONS:
            raise HTTPException(status_code=422, detail="invalid modelApprovalAction")
        row.model_approval_action = body.modelApprovalAction
    if "modelApprovalDenylist" in body.model_fields_set:
        row.model_approval_denylist = body.modelApprovalDenylist
    await db.flush()
    return _to_dict(row)
