"""SQLAlchemy 2.x async declarative models for ccswitch-web (T026/T027).

App store: Session, AppSession, AuditEvent.
Secrets and tokens are never stored here.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    """Timezone-aware UTC now (avoids the deprecated naive datetime.utcnow)."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Session(Base):
    """An active (or ended) Claude session managed by ccswitch-web."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    account_name: Mapped[str] = mapped_column(String, nullable=False)
    claude_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # working / idle / limited / ended
    status: Mapped[str] = mapped_column(String, nullable=False, default="idle")
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cwd: Mapped[str] = mapped_column(String, nullable=False)
    # GitHub repo (nameWithOwner, e.g. "owner/name") this session works on, or NULL
    # for a scratch / arbitrary-cwd session. Surfaced in the dashboard sessions list.
    repo: Mapped[str | None] = mapped_column(String, nullable=True)
    # The session's per-session worktree branch (e.g. ccswitch/main-abcd), used
    # to join the session to its GitHub PR by headRefName. NULL for detached/
    # scratch sessions. Nullable so init_db backfills it without a migration.
    branch: Mapped[str | None] = mapped_column(String, nullable=True)
    # Feature branches this session has authored (for multi-PR badges). JSON list,
    # nullable so init_db backfills it on existing rows without a migration.
    authored_branches: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    permission_mode: Mapped[str] = mapped_column(String, nullable=False, default="interactive")
    # True while the name is auto-derived (Claude title / first prompt); a manual
    # rename pins this False so auto-naming never overwrites a user-chosen name.
    name_is_auto: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # True when this session was adopted from an untracked live process and resumed
    # in the original cwd — a documented exception to the worktree-isolation guarantee.
    # Nullable so init_db backfills it on the dev SQLite DB without a migration.
    adopted_in_place: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    tool_allowlist: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Per-sub-agent summary [{agentId, parentAgentId, agentType, description, status}],
    # refreshed from the transcript on status changes. Nullable so init_db backfills
    # it on the dev SQLite DB without a migration.
    sub_agents: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    # Open background tasks (Monitors / backgrounded Bash|Task) snapshotted at deploy
    # drain so the daemon can relaunch them after the restart that reaps them. JSON list
    # of {tool_use_id, kind, name, input, description}; NULL = nothing pending. Nullable
    # so init_db backfills it on the dev SQLite DB without a migration. none_as_null=True
    # so clearing it via None persists SQL NULL — not the JSON literal text 'null', which
    # would defeat the rearm query's `pending_rearm IS NOT NULL` filter.
    pending_rearm: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None)
    # The in-flight foreground prompt snapshotted at deploy drain, persisted ONLY when
    # the turn had produced no durable output yet (so re-sending it after the restart
    # cannot duplicate destructive work). NULL = nothing to re-send. Nullable so init_db
    # backfills it on the dev SQLite DB without a migration.
    pending_prompt: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Persisted model for this session (alias like "opus"/"sonnet" or a pinned id
    # like "claude-haiku-4-5"). NULL = account/binary default. Nullable so init_db
    # backfills on the dev SQLite DB without a migration.
    model: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Active /goal condition for this session (the built-in Claude Code goal feature).
    # NULL = no active goal. Nullable so init_db backfills on dev SQLite without a migration.
    active_goal: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # True once the user manually pinned the account: usage-ceiling rotation skips
    # the session (it goes 'limited' at the ceiling instead). Hard 401/429 recovery
    # still rotates and clears the pin. Nullable for the same backfill reason.
    account_pinned: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    # Provenance: the automation id that spawned this session (NULL = human-created),
    # and what it was spawned for (e.g. "owner/name#412"). Nullable so init_db backfills
    # on the dev SQLite DB without a migration. See server/automations/.
    origin: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    origin_ref: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    # Generic row-write time: bumped on EVERY update (status, sub-agents, branch, name,
    # model, pin…). Used for the archived-sessions list's "archived at" only — NOT the
    # "active … ago" label, which would otherwise reset on every incidental write.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    # Time the most recent turn handed output back to the user (see ACTIVITY_STATUSES).
    # Drives the dashboard's "active … ago" label. Advanced ONLY on a response, never by
    # incidental row writes — so it stays meaningful. Nullable so init_db backfills on the
    # dev SQLite DB without a migration; NULL = no turn yet → label falls back to created_at.
    last_turn_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    # Last EXACT context-window occupancy from the SDK (the `/context` data), refreshed
    # each turn. Replayed on attach so a reloaded/idle session shows real remaining context
    # rather than the per-call approximation (which guesses the window from the model id and
    # reads 0% for a 1M opus session whose API-stripped id lacks the `[1m]` marker). NULL =
    # no turn has reported usage yet. Nullable so init_db backfills on dev SQLite without a migration.
    context_used_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    context_max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)


class AppSession(Base):
    """Server-side login/identity session (signed cookie → row lookup)."""

    __tablename__ = "app_sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    # Verified Tailscale identity string (e.g. user@example.com)
    identity: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditEvent(Base):
    """Write-only security audit log. detail must never contain token material."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    # e.g. auth.ok / auth.deny / account.add / session.create / ...
    type: Mapped[str] = mapped_column(String, nullable=False)
    identity: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class PushSubscription(Base):
    """A browser Web Push subscription. endpoint is the natural unique key."""

    __tablename__ = "push_subscriptions"

    endpoint: Mapped[str] = mapped_column(String, primary_key=True)
    p256dh: Mapped[str] = mapped_column(String, nullable=False)
    auth: Mapped[str] = mapped_column(String, nullable=False)
    # Nullable so init_db backfills existing rows without a migration.
    identity: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str | None] = mapped_column(String, nullable=True)


class Attachment(Base):
    """An image uploaded by a user and attached to a prompt.

    The DB row ``id`` (UUID) is the public API handle (``/api/attachments/{id}``).
    On disk the bytes live at ``attachments_dir()/{sha256}.{ext}`` — content-addressed,
    so re-uploading identical bytes reuses the existing file (dedup). Multiple rows may
    therefore reference the same on-disk file; deletion must be reference-aware.
    """

    __tablename__ = "attachments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    # Bound to a session+turn on send; NULL while still an unbound (orphan) upload.
    session_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    turn_id: Mapped[str | None] = mapped_column(String, nullable=True)
    media_type: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    # sha256 of the bytes — content-addressed on-disk filename + dedup key.
    sha256: Mapped[str] = mapped_column(String, nullable=False, index=True)
    uploaded_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class AppSettings(Base):
    """Single-row application settings (singleton id='singleton')."""

    __tablename__ = "app_settings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default="singleton")
    default_permission_mode: Mapped[str] = mapped_column(String, nullable=False, default="interactive")
    thinking_collapsed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    repo_root: Mapped[str | None] = mapped_column(String, nullable=True)
    default_tool_detail: Mapped[str | None] = mapped_column(String, nullable=True)
    compaction_ewma_ms: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    compaction_samples: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # 'off' | 'propose' | 'auto' — how to react when a session's PR fails CI.
    # Nullable: init_db backfills nullable columns; code defaults NULL → 'propose'.
    pr_autofix_mode: Mapped[str | None] = mapped_column(String, nullable=True)


class Automation(Base):
    """Registry state for one code-defined automation. Identity (name, schema,
    behavior) lives in server/automations/; this row stores only mutable state."""

    __tablename__ = "automation"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # stable slug
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mode: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    # Heartbeat the automation bumps while alive; lets the UI flag a stalled loop.
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)


class AutomationEvent(Base):
    """One row in an automation's append-only, capped activity feed."""

    __tablename__ = "automation_event"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    automation_id: Mapped[str] = mapped_column(
        String, ForeignKey("automation.id"), nullable=False, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow, index=True)
    level: Mapped[str] = mapped_column(String, nullable=False, default="info")  # info/warn/error
    # Queryable event code: detected/attempted/recovered/escalated/gave_up/spawned/skipped_duplicate.
    kind: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    summary: Mapped[str] = mapped_column(String, nullable=False)
    target: Mapped[str | None] = mapped_column(String, nullable=True, default=None)  # display-only
    session_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, default=None)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)


class AutomationTargetState(Base):
    """Durable per-target state (retry counters, loop-guard, 'gave up') keyed by
    (automation_id, target). The automation deletes a row when its target reaches
    terminal rest (e.g. PR merged), so the table does not grow unbounded."""

    __tablename__ = "automation_target_state"

    automation_id: Mapped[str] = mapped_column(
        String, ForeignKey("automation.id"), primary_key=True)
    target: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)
