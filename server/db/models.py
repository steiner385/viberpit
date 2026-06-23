"""SQLAlchemy 2.x async declarative models for ccswitch-web (T026/T027).

App store: Session, AppSession, AuditEvent.
Secrets and tokens are never stored here.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String
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
    # Persisted model for this session (alias like "opus"/"sonnet" or a pinned id
    # like "claude-haiku-4-5"). NULL = account/binary default. Nullable so init_db
    # backfills on the dev SQLite DB without a migration.
    model: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # True once the user manually pinned the account: usage-ceiling rotation skips
    # the session (it goes 'limited' at the ceiling instead). Hard 401/429 recovery
    # still rotates and clears the pin. Nullable for the same backfill reason.
    account_pinned: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


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
