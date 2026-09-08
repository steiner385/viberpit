"""Shared in-memory record type for a managed ccswitch session.

Kept separate from the DB model (server.db.models.Session) so that rotation,
usage, and WS layers can import this without pulling in SQLAlchemy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from server.sessions.engine import AgentEngine, parse_agent_engine
from server.sessions.usage_stall import is_usage_stalled
from server.time_iso import iso_utc

# Sentinel identity for internal/background callers (rehydrate, dispatch, eager
# re-launch) that act with no authenticated user. Single source of truth so the
# magic string isn't duplicated across orchestrator/remote/driver — and so the
# "is this a real user?" check (which gates re-threading identity onto a resumed
# driver) has one definition. A driver built under this identity registers
# AskUserQuestion/permission answers under it, so a real user attaching later must
# upgrade it (see SessionOrchestrator.ensure_driver) or their answers are rejected.
SYSTEM_IDENTITY = "system"

# Original pre-restart status is normalized to "idle" by rehydrate(); these are the
# subset that represents genuinely dropped WORK (vs awaiting-human waiting_input /
# waiting_question / ready_review, which normalize identically but must NOT be nudged).
RESUME_ELIGIBLE_STATUSES = frozenset({"in_progress", "working", "condensing"})

# The full ccswitch session permission-mode vocabulary — the single source of
# truth for "what modes exist". The first four are ccswitch-native (gated by
# PermissionRegistry.request); the last two share the Claude Agent SDK's own
# vocabulary and pass through to its permission engine (see
# server.sessions.permissions._SDK_PASSTHROUGH_MODES / to_sdk_permission_mode).
# Defined here (a dependency-light module) so API validators and the SDLC
# governance tier resolver reference ONE list instead of drifting copies
# (constitution VI: single source of truth / no dead code).
VALID_SESSION_MODES = frozenset({
    "interactive", "auto", "allowlist", "full-auto",
    "acceptEdits", "bypassPermissions",
})

ASSISTANT_KIND = "assistant"


@dataclass
class SessionRecord:
    """In-memory view of a running session — mirrors the DB row but stays process-local."""

    id: str                  # UUID, also the session key in SessionOrchestrator/ClaudeAgentDriver
    name: str                # user-visible label
    account: str             # current account name (updated on rotation)
    engine: AgentEngine      # native executable owning the conversation
    engine_session_id: str   # native id passed to resume on rotation/recovery
    cols: int
    rows: int
    cwd: str
    # Fields with defaults so existing create_session constructions keep compiling
    repo: str | None = None          # GitHub nameWithOwner this session works on (None = scratch)
    branch: str | None = None        # per-session worktree branch (joins to PR headRefName)
    origin: str | None = None        # automation_id that spawned this session (None = human-created)
    origin_ref: str | None = None    # automation's reference key (e.g. "owner/repo#123")
    assisted_by: str | None = None   # automation_id that has acted on this session (e.g. "autopilot")
    detached: bool = False           # no live driver — DERIVED on read (orchestrator._sync_detached), never hand-set
    status: str = "idle"             # idle / in_progress / condensing / waiting_input / ready_review / done / waiting_external / limited / ended
    name_is_auto: bool = True        # False once the user manually renames the session
    permission_mode: str = "interactive"
    created_at: datetime | None = None
    updated_at: datetime | None = None   # generic row-write time; mirrors DB Session.updated_at (archivedAt)
    last_turn_at: datetime | None = None  # last turn's response; drives the "active … ago" label (see ACTIVITY_STATUSES)
    adopted_in_place: bool = False   # resumed in original cwd, no worktree (adopted session)
    sub_agents: list | None = None   # [{agentId,parentAgentId,agentType,description,status}], refreshed at status changes
    model: str | None = None         # persisted model alias/id; None = account default
    reasoning_effort: str | None = None  # engine-native effort; None = model default
    # Requested model held pending human approval while status == "pending_approval"
    # (the launch gate parks a gated tier here rather than spawning it — see
    # SessionOrchestrator._hold_for_approval). Mirrors DB Session.pending_model; None
    # once approved/rejected. Distinct from `model` (which stays None until approval).
    pending_model: str | None = None
    pending_model_reason: str | None = None
    # Identity that requested `pending_model`; "session:self" means the session asked
    # for itself (vs. a human requesting on its behalf). Mirrors DB
    # Session.pending_model_requested_by.
    pending_model_requested_by: str | None = None
    # transient: a "switch after this turn" model change has been handed to the SDK
    # (set_model without an interrupt) but the in-flight turn is still on the old model —
    # the binary applies it on the NEXT turn. Cleared when the turn ends (see
    # SessionOrchestrator._settle_model_pending). Never a DB column: a daemon restart
    # drops the turn anyway and resume re-applies `model` before any new turn.
    model_pending: bool = False
    account_pinned: bool = False      # user pinned the account (skips usage-ceiling rotation)
    active_goal: str | None = None    # active /goal condition; None = no goal
    inferred_goal: str | None = None  # LLM-inferred goal (middle effective_goal tier)
    autopilot_paused: bool = False    # per-session autopilot opt-out (True = pinned to manual)
    kind: str | None = None           # "assistant" for the AI-drawer session; None = coding session
    owner_identity: str | None = None # identity owning an assistant thread (idempotency key)
    # Monotonic time of the last prompt or `activity` WS frame. In-memory only — drives
    # the assistant idle reap; never a DB column (a restart resets it, which is fine:
    # the reap re-arms from the next interaction).
    last_activity_at: float | None = None
    # Sticky auto-archive opt-out reason. None = eligible for auto-archive; "manual-unarchive"
    # = a human unarchived this session, so it is never auto-archived again (see orchestrator).
    auto_archive_opt_out_reason: str | None = None
    # Durable record that a session resolved it will NOT open a PR — either it finished
    # without one ("not-needed") or cancelled the PR work ("cancelled"). None = unresolved.
    # Reason token may carry ": detail" for audit. Source distinguishes the deterministic
    # marker from the probabilistic classifier. None until explicitly set.
    pr_resolution: str | None = None
    pr_resolution_source: str | None = None
    # Fleet host that runs this session, mirroring DB Session.host (stamped at
    # creation by the dispatcher; None for pre-fleet rows and primary-local spawns).
    # Ownership input for host-partitioned recovery (#852): a daemon must not
    # rotate/sweep a session whose driver lives on another live fleet host.
    host: str | None = None
    # "native" | "virtual" | None — where this session's GUI renders (spec
    # 2026-09-06-native-display-routing). Mirrors DB Session.display_mode; None on
    # pre-feature rows and reads as virtual everywhere.
    display_mode: str | None = None
    # Wall-clock time the user last steered this session (prompt/answer/permission/typing ping).
    # Drives autopilot human-deference + the cooldown badge. In-memory/process-local ONLY: NOT in
    # to_dict/from_dict, NOT a DB column — a 120s window is meaningless across a restart.
    last_user_interaction_at: datetime | None = None
    usage_unverified: bool = False    # transient: account picked without a usage read (optimistic)
    # transient (issue #434): set by rehydrate() when this session's mid-turn status
    # (working/in_progress/condensing) was normalized to idle on daemon boot — i.e. a
    # daemon restart dropped its in-flight turn. Lets the dashboard label it "interrupted
    # by daemon restart — re-send to resume" instead of a generic idle/"Turn failed" card.
    # Cleared on the next turn (see SessionOrchestrator._persist_status). Never persisted.
    interrupted_by_restart: bool = False
    # transient companion to interrupted_by_restart: the ORIGINAL pre-restart status
    # (before rehydrate normalized it to "idle"). Drives resume-eligibility only — a
    # session is resumable iff it was flagged AND this is in RESUME_ELIGIBLE_STATUSES.
    # Never persisted; cleared with the flag on the next turn.
    interrupted_from_status: str | None = None
    # Why the session is parked (a _pause_session reason token, or
    # "pinned_account_limited" for the `limited` overlay) and the earliest known
    # usage reset (naive UTC). Persisted so a poll/reload keeps the cause; cleared by
    # _persist_status on any non-stalled status. See server/sessions/usage_stall.py.
    paused_reason: str | None = None
    paused_until: datetime | None = None
    authored_branches: list[str] = field(default_factory=list)  # feature branches → multi-PR badges
    # Last EXACT context-window occupancy from the SDK (the `/context` data), refreshed
    # each turn. Persisted + replayed on attach so a reloaded/idle session shows real
    # remaining context instead of the per-call approximation — which divides by a window
    # guessed from the model string and reads 0% for a 1M session whose API-stripped model
    # id ("claude-opus-4-8", no `[1m]`) looks like a 200k window. None until a turn reports it.
    context_used_tokens: int | None = None
    context_max_tokens: int | None = None
    # Last-known slash-command catalog (list of wire dicts from build_catalog),
    # cached whenever the session is live. Persisted + replayed on attach so a
    # DETACHED session (no live driver — the lazy-view path never resumes one)
    # can still populate the composer's command palette instead of showing an
    # empty autocomplete. None until the session has been live once.
    command_catalog: list | None = None

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
            "engine": self.engine.value,
            "engineSessionId": self.engine_session_id,
            # Temporary daemon→edge rolling-upgrade alias. Daemons deploy before
            # the edge, so the old edge still needs this key while the new edge
            # already understands ``engineSessionId``. Browser serializers never
            # expose it, and it is intentionally null for non-Claude engines.
            "claudeSessionId": (
                self.engine_session_id
                if self.engine is AgentEngine.CLAUDE_CODE
                else None
            ),
            "cols": self.cols, "rows": self.rows,
            "cwd": self.cwd, "repo": self.repo, "branch": self.branch, "detached": self.detached,
            "origin": self.origin, "origin_ref": self.origin_ref,
            "assistedBy": self.assisted_by,
            "status": self.status, "nameIsAuto": self.name_is_auto,
            "permissionMode": self.permission_mode,
            "createdAt": iso_utc(self.created_at),
            "updatedAt": self.updated_at_iso,
            "lastTurnAt": iso_utc(self.last_turn_at),
            "lastUserInteractionAt": iso_utc(self.last_user_interaction_at),
            "revAt": self.rev_at,
            "adoptedInPlace": self.adopted_in_place,
            "model": self.model,
            "reasoningEffort": self.reasoning_effort,
            "pendingModel": self.pending_model,
            "pendingModelReason": self.pending_model_reason,
            "pendingModelRequestedBy": self.pending_model_requested_by,
            "modelPending": self.model_pending,
            "accountPinned": self.account_pinned,
            "activeGoal": self.active_goal,
            "inferredGoal": self.inferred_goal,
            "autopilotPaused": self.autopilot_paused,
            "autoArchiveOptOut": self.auto_archive_opt_out_reason,
            "prResolution": self.pr_resolution,
            "prResolutionSource": self.pr_resolution_source,
            "host": self.host,
            "displayMode": self.display_mode,
            "usageUnverified": self.usage_unverified,
            "interruptedByRestart": self.interrupted_by_restart,
            "interruptedFromStatus": self.interrupted_from_status,
            "resumable": (self.interrupted_by_restart
                          and self.interrupted_from_status in RESUME_ELIGIBLE_STATUSES),
            "pausedReason": self.paused_reason,
            "pausedUntil": iso_utc(self.paused_until),
            "usageStalled": is_usage_stalled(self.status, self.paused_reason),
            "authoredBranches": list(self.authored_branches),
            "contextUsedTokens": self.context_used_tokens,
            "contextMaxTokens": self.context_max_tokens,
            "commandCatalog": self.command_catalog,
            # Carried over RPC so a fleet mirror of an assistant session is still
            # recognised by is_assistant() on the edge (list filtering, sweeps).
            "kind": self.kind,
            "ownerIdentity": self.owner_identity,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SessionRecord":
        from datetime import datetime
        created = d.get("createdAt")
        last_turn = d.get("lastTurnAt")
        rev = d.get("revAt")
        return cls(
            id=d["id"], name=d["name"], account=d["account"],
            # Only an omitted field identifies an old daemon. An explicit null is
            # malformed current wire data and must not be relabelled as Claude.
            engine=parse_agent_engine(
                d["engine"] if "engine" in d else AgentEngine.CLAUDE_CODE),
            # ``claudeSessionId`` is accepted only at this daemon→edge rolling-
            # upgrade boundary. Browser payloads and all internal models use the
            # canonical engine-neutral key.
            engine_session_id=((d.get("engineSessionId")
                                if "engineSessionId" in d
                                else d.get("claudeSessionId")) or ""),
            cols=d["cols"], rows=d["rows"],
            cwd=d["cwd"], repo=d.get("repo"), branch=d.get("branch"), detached=d.get("detached", False),
            origin=d.get("origin"), origin_ref=d.get("origin_ref"),
            assisted_by=d.get("assistedBy"),
            status=d.get("status", "idle"), name_is_auto=d.get("nameIsAuto", True),
            permission_mode=d.get("permissionMode", "interactive"),
            created_at=datetime.fromisoformat(created) if created else None,
            last_turn_at=datetime.fromisoformat(last_turn) if last_turn else None,
            last_user_interaction_at=(datetime.fromisoformat(d["lastUserInteractionAt"])
                                      if d.get("lastUserInteractionAt") else None),
            updated_at=datetime.fromisoformat(rev) if rev else None,
            adopted_in_place=d.get("adoptedInPlace", False),
            model=d.get("model"),
            reasoning_effort=d.get("reasoningEffort"),
            pending_model=d.get("pendingModel"),
            pending_model_reason=d.get("pendingModelReason"),
            pending_model_requested_by=d.get("pendingModelRequestedBy"),
            model_pending=bool(d.get("modelPending", False)),
            account_pinned=d.get("accountPinned", False),
            active_goal=d.get("activeGoal"),
            inferred_goal=d.get("inferredGoal"),
            autopilot_paused=bool(d.get("autopilotPaused", False)),
            auto_archive_opt_out_reason=d.get("autoArchiveOptOut"),
            pr_resolution=d.get("prResolution"),
            pr_resolution_source=d.get("prResolutionSource"),
            host=d.get("host"),
            display_mode=d.get("displayMode"),
            usage_unverified=d.get("usageUnverified", False),
            interrupted_by_restart=d.get("interruptedByRestart", False),
            interrupted_from_status=d.get("interruptedFromStatus"),
            paused_reason=d.get("pausedReason"),
            paused_until=(datetime.fromisoformat(d["pausedUntil"])
                          if d.get("pausedUntil") else None),
            authored_branches=list(d.get("authoredBranches") or []),
            context_used_tokens=d.get("contextUsedTokens"),
            context_max_tokens=d.get("contextMaxTokens"),
            command_catalog=d.get("commandCatalog"),
            kind=d.get("kind"),
            owner_identity=d.get("ownerIdentity"),
        )


def is_assistant_kind(kind: str | None) -> bool:
    """The ONE comparison against ``ASSISTANT_KIND``, for the paths that hold a bare
    kind STRING rather than a record (``create_session``'s parameter, the queue params).
    Keeps the literal from being open-coded a second time (constitution VI)."""
    return kind == ASSISTANT_KIND


def is_assistant(rec) -> bool:
    """The ONE predicate for 'is this the AI-drawer session' — every loop that
    iterates sessions (autopilot, titles, summaries, suggestions, goal inference,
    auto-archive, capacity) routes through it. Duck-typed so fleet mirrors and DB rows
    qualify too."""
    return is_assistant_kind(getattr(rec, "kind", None))
