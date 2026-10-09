"""Settings API — the single app-settings row (permission defaults, etc.)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from viberpit import brand
from server.auth.middleware import current_identity
from server.config import get_settings
from server.db import get_db
from server.db.models import AppSettings
from server.hostname import self_host_name
from server.sessions.global_cap import GlobalCap, drain_if_raised, read_global_cap
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
    ("allow_unauthenticated", "VIBERPIT_ALLOW_UNAUTHENTICATED", "Admit clients with no Tailscale identity as a guest"),
    ("enable_adoption", "VIBERPIT_ENABLE_ADOPTION", f"Allow adopting sessions started outside {brand.NAME}"),
    ("enable_copilot", "VIBERPIT_ENABLE_COPILOT", "Enable GitHub Copilot accounts"),
    ("enable_xvfb", "VIBERPIT_ENABLE_XVFB", "Virtual displays for browser-using sessions"),
    ("self_model_switch_enabled", "VIBERPIT_SELF_MODEL_SWITCH_ENABLED", "Sessions may request a model change"),
    ("self_model_switch_cap", "VIBERPIT_SELF_MODEL_SWITCH_CAP", "Auto-applied model switches per session before parking for approval"),
    ("session_tools_enabled", "VIBERPIT_SESSION_TOOLS_ENABLED", f"Coding sessions get the {brand.NAME} MCP tools (session_context, report_feedback)"),
    ("mcp_status_enabled", "VIBERPIT_MCP_STATUS_ENABLED", "Sessions report MCP server connection status; Settings › MCP can test servers"),
    ("mcp_scope_enabled", "VIBERPIT_MCP_SCOPE_ENABLED", "Per-repo MCP server scope and live session toggles"),
    ("session_feedback_cap", "VIBERPIT_SESSION_FEEDBACK_CAP", "Feedback reports one session may file before further ones are capped"),
    ("error_sink_enabled", "VIBERPIT_ERROR_SINK_ENABLED", "Capture WARNING+ log records into error signatures for Error Triage"),
    ("gh_reconcile_enabled", "VIBERPIT_GH_RECONCILE_ENABLED", "GitHub Projects reconciler may write back"),
    ("capacity_governor_enabled", "VIBERPIT_CAPACITY_GOVERNOR_ENABLED", "Capacity governor may apply its plan (act mode)"),
    ("test_slot_cap", "VIBERPIT_TEST_SLOT_CAP", "Full test-suite runs one host may carry at once"),
    ("child_session_cap", "VIBERPIT_CHILD_SESSION_CAP", "Live child sessions one parent session may hold"),
    ("stt_max_concurrent", "VIBERPIT_STT_MAX_CONCURRENT", "Concurrent server-side dictation streams"),
    ("log_level", "VIBERPIT_LOG_LEVEL", "Server log level"),
    ("client_log_level", "VIBERPIT_CLIENT_LOG_LEVEL", "Browser log level"),
)


def _server_blocks(cap: GlobalCap) -> dict[str, object]:
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
            "globalCap": cap.to_wire(),
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
    # Attention ceiling. Omitted → unchanged; explicit null → cleared (no attention
    # ceiling, the physical ceiling is in charge); >=1 → that ceiling; <1 → 422.
    maxActiveSessions: int | None = None
    # App-wide auto-archive toggle. Omitted (None) → unchanged; true/false sets it.
    # Exposed to the UI as a plain bool (the storage-level NULL default surfaces as true).
    autoArchiveEnabled: bool | None = None
    # Global model-approval policy. Uses model_fields_set so explicit null clears to
    # env fallback. action ∈ VALID_ACTIONS or null; denylist is a free-text comma list.
    modelApprovalAction: str | None = None
    modelApprovalDenylist: str | None = None
    # Assistant launch (spec §4.11). Uses model_fields_set like the model-approval
    # pair above: explicit null clears back to the VIBERPIT_ASSISTANT_ENABLED /
    # VIBERPIT_ASSISTANT_MODEL env fallback, omitted leaves the stored value alone.
    assistantEnabled: bool | None = None
    assistantModel: str | None = None
    # Operator tier (v2 spec B1). Same three-state contract; null clears back to
    # VIBERPIT_ASSISTANT_OPERATOR_TOOLS, which is false.
    assistantOperatorTools: bool | None = None


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


async def _get_or_create(db) -> AppSettings:
    row = (await db.execute(select(AppSettings).where(AppSettings.id == "singleton"))).scalar_one_or_none()
    if row is None:
        row = AppSettings(id="singleton")
        db.add(row)
        await db.flush()
    return row


def _to_dict(s: AppSettings, cap: GlobalCap) -> dict:
    return {
        "defaultPermissionMode": s.default_permission_mode,
        "thinkingCollapsed": s.thinking_collapsed,
        "repoRoot": s.repo_root,
        "defaultToolDetail": s.default_tool_detail or "auto",
        "maxArchivedSessions": s.max_archived_sessions,
        "archiveHome": s.archive_home,
        # The ATTENTION ceiling (spec 2026-09-20 D3); the effective cap is
        # effectiveDefaults.globalCap.effective.
        "maxActiveSessions": s.max_active_sessions,
        # NULL (unset) surfaces as enabled — the client sees a plain bool.
        "autoArchiveEnabled": s.auto_archive_enabled is not False,
        "modelApprovalAction": s.model_approval_action,
        "modelApprovalDenylist": s.model_approval_denylist,
        "assistantEnabled": s.assistant_enabled,
        "assistantModel": s.assistant_model,
        "assistantOperatorTools": s.assistant_operator_tools,
        **_server_blocks(cap),
    }


@router.get("")
async def get_settings_row(_identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    row = await _get_or_create(db)
    return _to_dict(row, await read_global_cap(db))


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
    from server.sessions.model_policy import VALID_ACTIONS
    if "modelApprovalAction" in body.model_fields_set:
        if body.modelApprovalAction is not None and body.modelApprovalAction not in VALID_ACTIONS:
            raise HTTPException(status_code=422, detail="invalid modelApprovalAction")
        row.model_approval_action = body.modelApprovalAction
    if "modelApprovalDenylist" in body.model_fields_set:
        row.model_approval_denylist = body.modelApprovalDenylist
    if "assistantEnabled" in body.model_fields_set:
        row.assistant_enabled = body.assistantEnabled
    if "assistantModel" in body.model_fields_set:
        row.assistant_model = body.assistantModel
    if "assistantOperatorTools" in body.model_fields_set:
        row.assistant_operator_tools = body.assistantOperatorTools
    # LAST, after every other field above has had its chance to raise: this
    # block commits (not merely flushes), which is this handler's one point of
    # no return. `get_db`'s deferred commit made the whole PUT atomic before
    # M5's fix — an explicit early commit here is required so the drain's
    # background task (which opens its OWN db session) reads the new cap
    # rather than the pre-commit one, but committing BEFORE a sibling field's
    # validation (e.g. modelApprovalAction) would persist the cap and fire the
    # drain moments before an unrelated 422 aborted the rest of the request.
    # Keeping this block last means every other field's HTTPException has
    # already fired by the time anything is committed.
    if "maxActiveSessions" in body.model_fields_set:
        if body.maxActiveSessions is not None and body.maxActiveSessions < 1:
            raise HTTPException(status_code=422, detail="maxActiveSessions must be >= 1 or null")
        before = await read_global_cap(db)
        row.max_active_sessions = body.maxActiveSessions
        await db.flush()
        await db.commit()
        # The EFFECTIVE cap rose (not merely the typed number): fill the new slots.
        drain_if_raised(getattr(request.app.state, "sessions", None), before, await read_global_cap(db))
    await db.flush()
    return _to_dict(row, await read_global_cap(db))
