"""Shared in-memory record type for a managed ccswitch session.

Kept separate from the DB model (server.db.models.Session) so that rotation,
usage, and WS layers can import this without pulling in SQLAlchemy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from server.time_iso import iso_utc

# Sentinel identity for internal/background callers (rehydrate, dispatch, eager
# re-launch) that act with no authenticated user. Single source of truth so the
# magic string isn't duplicated across orchestrator/remote/driver — and so the
# "is this a real user?" check (which gates re-threading identity onto a resumed
# driver) has one definition. A driver built under this identity registers
# AskUserQuestion/permission answers under it, so a real user attaching later must
# upgrade it (see SessionOrchestrator.ensure_driver) or their answers are rejected.
SYSTEM_IDENTITY = "system"


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
    origin: str | None = None        # automation_id that spawned this session (None = human-created)
    origin_ref: str | None = None    # automation's reference key (e.g. "owner/repo#123")
    detached: bool = False           # True for rehydrated sessions with no live driver
    status: str = "idle"             # idle / in_progress / condensing / waiting_input / ready_review / done / waiting_external / limited / ended
    name_is_auto: bool = True        # False once the user manually renames the session
    permission_mode: str = "interactive"
    created_at: datetime | None = None
    updated_at: datetime | None = None   # generic row-write time; mirrors DB Session.updated_at (archivedAt)
    last_turn_at: datetime | None = None  # last turn's response; drives the "active … ago" label (see ACTIVITY_STATUSES)
    adopted_in_place: bool = False   # resumed in original cwd, no worktree (adopted session)
    sub_agents: list | None = None   # [{agentId,parentAgentId,agentType,description,status}], refreshed at status changes
    model: str | None = None         # persisted model alias/id; None = account default
    account_pinned: bool = False      # user pinned the account (skips usage-ceiling rotation)
    active_goal: str | None = None    # active /goal condition; None = no goal
    usage_unverified: bool = False    # transient: account picked without a usage read (optimistic)
    authored_branches: list[str] = field(default_factory=list)  # feature branches → multi-PR badges
    # Last EXACT context-window occupancy from the SDK (the `/context` data), refreshed
    # each turn. Persisted + replayed on attach so a reloaded/idle session shows real
    # remaining context instead of the per-call approximation — which divides by a window
    # guessed from the model string and reads 0% for a 1M session whose API-stripped model
    # id ("claude-opus-4-8", no `[1m]`) looks like a 200k window. None until a turn reports it.
    context_used_tokens: int | None = None
    context_max_tokens: int | None = None

    @property
    def updated_at_iso(self) -> str | None:
        """The 'active … ago' wire timestamp: the last turn's response, falling back to
        creation. NOT the raw `updated_at` row-write column (which any write bumps → the
        '0s ago' pin). Single source for the dashboard freshness label — used by to_dict,
        the HTTP serializer (api/sessions.py), and the live WS status event (orchestrator)."""
        return iso_utc(self.last_turn_at or self.created_at)

    @property
    def rev_at(self) -> str | None:
        """Monotonic per-session ordering token for client poll↔WS reconciliation: the
        row-write time (`updated_at`, bumped on EVERY status persist), serialized. Distinct
        from `updated_at_iso` (freshness = last_turn_at). The client keeps whichever of the
        poll and the live WS patch carries the newer `rev_at`, so a stale in-flight poll
        cannot overwrite a fresher live status."""
        return iso_utc(self.updated_at)

    def to_dict(self) -> dict:
        # `updatedAt` is the "active … ago" / last-activity time, matching the HTTP
        # serializer (server/api/sessions.py `_record_to_dict`): the last turn's response,
        # falling back to creation — NOT the raw `updated_at` row-write column (which any
        # write bumps → the "0s ago" pin). `lastTurnAt` below carries the raw turn time so
        # `from_dict` restores it faithfully over RPC.
        return {
            "id": self.id, "name": self.name, "account": self.account,
            "claudeSessionId": self.claude_session_id, "cols": self.cols, "rows": self.rows,
            "cwd": self.cwd, "repo": self.repo, "branch": self.branch, "detached": self.detached,
            "origin": self.origin, "origin_ref": self.origin_ref,
            "status": self.status, "nameIsAuto": self.name_is_auto,
            "permissionMode": self.permission_mode,
            "createdAt": iso_utc(self.created_at),
            "updatedAt": self.updated_at_iso,
            "lastTurnAt": iso_utc(self.last_turn_at),
            "revAt": self.rev_at,
            "adoptedInPlace": self.adopted_in_place,
            "model": self.model,
            "accountPinned": self.account_pinned,
            "activeGoal": self.active_goal,
            "usageUnverified": self.usage_unverified,
            "authoredBranches": list(self.authored_branches),
            "contextUsedTokens": self.context_used_tokens,
            "contextMaxTokens": self.context_max_tokens,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SessionRecord":
        from datetime import datetime
        created = d.get("createdAt")
        last_turn = d.get("lastTurnAt")
        rev = d.get("revAt")
        return cls(
            id=d["id"], name=d["name"], account=d["account"],
            claude_session_id=d["claudeSessionId"], cols=d["cols"], rows=d["rows"],
            cwd=d["cwd"], repo=d.get("repo"), branch=d.get("branch"), detached=d.get("detached", False),
            origin=d.get("origin"), origin_ref=d.get("origin_ref"),
            status=d.get("status", "idle"), name_is_auto=d.get("nameIsAuto", True),
            permission_mode=d.get("permissionMode", "interactive"),
            created_at=datetime.fromisoformat(created) if created else None,
            last_turn_at=datetime.fromisoformat(last_turn) if last_turn else None,
            updated_at=datetime.fromisoformat(rev) if rev else None,
            adopted_in_place=d.get("adoptedInPlace", False),
            model=d.get("model"),
            account_pinned=d.get("accountPinned", False),
            active_goal=d.get("activeGoal"),
            usage_unverified=d.get("usageUnverified", False),
            authored_branches=list(d.get("authoredBranches") or []),
            context_used_tokens=d.get("contextUsedTokens"),
            context_max_tokens=d.get("contextMaxTokens"),
        )
