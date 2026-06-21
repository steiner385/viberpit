"""SQLAlchemy 2.x async declarative models for ccswitch-web (T026/T027).

App store: Session, AppSession, AuditEvent.
Secrets and tokens are never stored here.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


def _new_uuid() -> str:
    return str(uuid.uuid4())


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
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    permission_mode: Mapped[str] = mapped_column(String, nullable=False, default="interactive")
    # True while the name is auto-derived (Claude title / first prompt); a manual
    # rename pins this False so auto-naming never overwrites a user-chosen name.
    name_is_auto: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    tool_allowlist: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
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


class AppSettings(Base):
    """Single-row application settings (singleton id='singleton')."""

    __tablename__ = "app_settings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default="singleton")
    default_permission_mode: Mapped[str] = mapped_column(String, nullable=False, default="interactive")
    thinking_collapsed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    repo_root: Mapped[str | None] = mapped_column(String, nullable=True)
