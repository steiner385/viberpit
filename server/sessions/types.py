"""Shared in-memory record type for a managed ccswitch session.

Kept separate from the DB model (server.db.models.Session) so that rotation,
usage, and WS layers can import this without pulling in SQLAlchemy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SessionRecord:
    """In-memory view of a running session — mirrors the DB row but stays process-local."""

    id: str                  # UUID, also the session key in SessionOrchestrator/SessionDriver
    name: str                # user-visible label
    account: str             # current account name (updated on rotation)
    claude_session_id: str   # value passed to --resume on rotation
    cols: int
    rows: int
    cwd: str
    # Fields with defaults so existing create_session constructions keep compiling
    detached: bool = False           # True for rehydrated sessions with no live driver
    status: str = "idle"             # working / idle / limited / ended
    permission_mode: str = "interactive"
    created_at: datetime | None = None
