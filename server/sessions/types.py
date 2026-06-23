"""Shared in-memory record type for a managed ccswitch session.

Kept separate from the DB model (server.db.models.Session) so that rotation,
usage, and WS layers can import this without pulling in SQLAlchemy.
"""
from __future__ import annotations

from dataclasses import dataclass
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
    repo: str | None = None          # GitHub nameWithOwner this session works on (None = scratch)
    branch: str | None = None        # per-session worktree branch (joins to PR headRefName)
    detached: bool = False           # True for rehydrated sessions with no live driver
    status: str = "idle"             # idle / in_progress / waiting_input / ready_review / done / limited / ended
    name_is_auto: bool = True        # False once the user manually renames the session
    permission_mode: str = "interactive"
    created_at: datetime | None = None
    updated_at: datetime | None = None   # last activity; mirrors DB Session.updated_at, drives the "active … ago" label
    adopted_in_place: bool = False   # resumed in original cwd, no worktree (adopted session)
    sub_agents: list | None = None   # [{agentId,parentAgentId,agentType,description,status}], refreshed at status changes
    model: str | None = None         # persisted model alias/id; None = account default
    account_pinned: bool = False      # user pinned the account (skips usage-ceiling rotation)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "account": self.account,
            "claudeSessionId": self.claude_session_id, "cols": self.cols, "rows": self.rows,
            "cwd": self.cwd, "repo": self.repo, "branch": self.branch, "detached": self.detached,
            "status": self.status, "nameIsAuto": self.name_is_auto,
            "permissionMode": self.permission_mode,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
            "adoptedInPlace": self.adopted_in_place,
            "model": self.model,
            "accountPinned": self.account_pinned,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SessionRecord":
        from datetime import datetime
        created = d.get("createdAt")
        updated = d.get("updatedAt")
        return cls(
            id=d["id"], name=d["name"], account=d["account"],
            claude_session_id=d["claudeSessionId"], cols=d["cols"], rows=d["rows"],
            cwd=d["cwd"], repo=d.get("repo"), branch=d.get("branch"), detached=d.get("detached", False),
            status=d.get("status", "idle"), name_is_auto=d.get("nameIsAuto", True),
            permission_mode=d.get("permissionMode", "interactive"),
            created_at=datetime.fromisoformat(created) if created else None,
            updated_at=datetime.fromisoformat(updated) if updated else None,
            adopted_in_place=d.get("adoptedInPlace", False),
            model=d.get("model"),
            account_pinned=d.get("accountPinned", False),
        )
