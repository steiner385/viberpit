"""SQLAlchemy 2.x async declarative models for ccswitch-web (T026/T027).

App store: Session, AppSession, AuditEvent.
Secrets and tokens are never stored here.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer,
                        String, UniqueConstraint, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    """Timezone-aware UTC now (avoids the deprecated naive datetime.utcnow)."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# Session.status values that mean the session is no longer live: 'ended' (the
# process is gone) and 'archived' (cwd moved out, kept for review). Everything
# else — idle / in_progress / limited / etc — counts as an active session.
# Single source of truth: import this instead of re-inlining the string set.
INACTIVE_SESSION_STATUSES: frozenset[str] = frozenset({"ended", "archived"})

# Statuses that consume no RUNTIME capacity slot: a terminal row (ended/archived),
# a "queued" row (waiting FOR a slot — it holds none), and "complete".
#
# `complete` is here because the work is genuinely finished — the agent emitted the
# completion marker — and `_persist_status` releases the `claude` process on that
# transition, so the session is not holding a live driver either. Counting finished
# work against the cap made it starve queued work: on 2026-08-23 seven finished
# sessions held a cap of seven while ZERO sessions were mid-turn, and 15 queued jobs
# could never start. Six of the seven were permanently exempt from auto-archive
# (`manual-unarchive`), so the deadlock could not self-heal — an archive opt-out had
# silently become a permanent capacity leak.
#
# `done` is deliberately NOT here. It does NOT mean "the session finished": see
# server/sessions/status.derive_status — `done` is returned for ANY ended turn
# (turns_completed >= 1 with no turn active), i.e. after every ordinary prompt. A
# `done` session is a LIVE session between turns: it keeps its warm driver for the
# next prompt, so it holds a real slot and must be counted. `idle` is excluded for
# the same reason — alive and awaiting input.
#
# INVARIANT: a session holding a live driver must not be capacity-exempt. Resuming a
# `complete` session (ensure_driver) therefore transitions it back to `done`, so it
# re-enters the count instead of running as an uncapped ghost.
#
# _ACTIVE_STATUSES in orchestrator.py is a separate allow-list counter with its own
# consumers (/info, deploy drain) and is intentionally not unified with this set.
CAPACITY_EXEMPT_STATUSES: frozenset[str] = INACTIVE_SESSION_STATUSES | {"queued", "complete", "pending_approval"}

# Session.archived_by values written by an AUTOMATIC archive (vs a manual "user"
# archive). Only these are eligible for auto-unarchive by the Archive Audit — a human's
# manual archive is a deliberate decision the audit never second-guesses. NULL/unknown
# provenance is excluded too (an `.in_(...)` predicate never matches NULL). Single source
# of truth: import this instead of re-inlining the ("autopilot", "backlog-runner") set.
AUTO_ARCHIVE_PROVENANCE: frozenset[str] = frozenset({"autopilot", "backlog-runner", "completion"})

# Global cap on concurrently active sessions when AppSettings.max_active_sessions
# is NULL. See docs/superpowers/specs/2026-07-21-max-active-sessions-queue-design.md.
DEFAULT_MAX_ACTIVE_SESSIONS = 5


class Session(Base):
    """An active (or ended) native-agent session managed by ccswitch-web."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    account_name: Mapped[str] = mapped_column(String, nullable=False)
    # Native executable and its conversation/thread identifier. Both stay nullable
    # at the SQL layer so runtime reconciliation can add them to populated SQLite
    # and Postgres databases; init_db backfills every legacy row before use.
    engine: Mapped[str | None] = mapped_column(String, nullable=True)
    engine_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Engine-native reasoning choice. None means the selected model's default.
    reasoning_effort: Mapped[str | None] = mapped_column(String, nullable=True)
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
    # The verbatim first prompt the session was created with (human sessions defer it to
    # first WS attach; automation sessions send it immediately) — persisted so completion
    # judgment / archive audit need not read transcript files. Nullable so init_db
    # backfills on the dev SQLite DB without a migration.
    initial_prompt: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Persisted model for this session (alias like "opus"/"sonnet" or a pinned id
    # like "claude-haiku-4-5"). NULL = account/binary default. Nullable so init_db
    # backfills on the dev SQLite DB without a migration.
    model: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Model-approval gate. When a gated model (see model_policy) is requested, the
    # requested model is parked here pending human approval — for a launch-time hold
    # the row's status is "pending_approval" (capacity-exempt, no driver); for a
    # running session it stays live on its current `model` while `pending_model`
    # holds the requested switch. All nullable so init_db backfills without a hand
    # migration on both dialects.
    pending_model: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    pending_model_requested_by: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    pending_model_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    # Why the requester wants this model — free text from a session's own
    # <!-- ccswitch:model=… reason="…" --> marker. Shown on the approval card.
    pending_model_reason: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Active /goal condition for this session (the built-in Claude Code goal feature).
    # NULL = no active goal. Nullable so init_db backfills on dev SQLite without a migration.
    active_goal: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # LLM-inferred session goal (the middle tier of effective_goal): a one-line statement
    # of what the session is driving toward, refreshed by the goal-inference sweep.
    # NULL until first inferred. Nullable so init_db backfills without a migration.
    inferred_goal: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Per-session autopilot opt-out. NULL/False = governed by autopilot; True = pinned to
    # manual. Nullable so init_db backfills on dev SQLite without a migration.
    autopilot_paused: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    # Sticky auto-archive opt-out. NULL = eligible for auto-archive; a reason string
    # ("manual-unarchive") = a human unarchived this session, so autopilot and the
    # backlog-runner must NEVER auto-archive it again (only archiving is blocked — other
    # autopilot help continues). Nullable so init_db backfills without a migration.
    auto_archive_opt_out_reason: Mapped[str | None] = mapped_column(
        String, nullable=True, default=None)
    # Durable record that a session resolved it will NOT open a PR — either it
    # finished without one ("not-needed") or cancelled the PR work ("cancelled").
    # None = unresolved (default). Reason token may carry ": detail" for audit.
    # Source distinguishes the deterministic marker from the probabilistic classifier.
    pr_resolution: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    pr_resolution_source: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Provenance of the LAST archive: "user" | "autopilot" | "backlog-runner". Lets
    # unarchive detect that an AUTO archive was just reversed (→ a learning heuristic).
    # Cleared on unarchive. Nullable so init_db backfills without a migration.
    archived_by: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # UTC time this session was archived (set in archive_session). NULL for rows
    # archived before this column existed and for never-archived sessions. Nullable
    # so init_db backfills it on existing rows without a migration.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    # Absolute archive ROOT the cwd was moved into, captured at archive time. unarchive()
    # must resolve through this and never through the *current* archive home: archive_home
    # is a placement setting (which disk), while being archived is a lifecycle fact about
    # the session. Fusing the two meant that moving CCSWITCH_ARCHIVE_HOME to another disk
    # retroactively pointed every past archive at a path it had never been written to, and
    # unarchive silently restored an empty tree. NULL = archived before this column existed
    # (resolve via the legacy fallback) or never archived. Nullable so init_db backfills
    # without a migration.
    archive_root: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Fleet host that ran this session, stamped at creation by the dispatcher. NULL
    # for sessions created before the fleet existed. Never inferred at read time:
    # the host a session ran on is a fact about that session, not about current
    # config — the same mistake archive_root exists to prevent.
    host: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Snapshot of the decision features at AUTO-archive time ({repo, goal, goal_source,
    # pr_state, reason}); copied into an `archive_reversed` autopilot event on unarchive so
    # the auto-archive algorithm can learn which sessions it wrongly archived. none_as_null
    # so clearing via None persists SQL NULL. Nullable so init_db backfills without a migration.
    archive_context: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None)
    # True once the user manually pinned the account: usage-ceiling rotation skips
    # the session (it goes 'limited' at the ceiling instead). Hard 401/429 recovery
    # still rotates and clears the pin. Nullable for the same backfill reason.
    account_pinned: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    # Why the session is parked + the earliest known usage reset (naive UTC), written
    # by _persist_status alongside `needs_attention`/`limited` and cleared on any other
    # status. Lets the dashboard say "waiting for capacity · retries 12:10" instead of
    # a bare "Turn failed". Nullable so reconcile ALTER-ADDs them on both dialects.
    paused_reason: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    paused_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    # Provenance: the automation id that spawned this session (NULL = human-created),
    # and what it was spawned for (e.g. "owner/name#412"). Nullable so init_db backfills
    # on the dev SQLite DB without a migration. See server/automations/.
    origin: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    origin_ref: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Provenance (assist): the automation id that has acted on this HUMAN-created
    # session (e.g. "autopilot" after its first successful nudge/approval). Distinct
    # from `origin` (spawned-by). NULL = never assisted. Nullable for init_db backfill.
    assisted_by: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
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
    # Last-known slash-command catalog (JSON list of wire dicts), cached whenever the
    # session is live. Replayed on attach so a DETACHED session — whose lazy-view path
    # never resumes a driver (the source of get_server_info) — still populates the
    # composer's command palette instead of an empty autocomplete. NULL until the
    # session has been live once. Nullable so init_db backfills on dev SQLite w/o a migration.
    command_catalog: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    # Session kind. "assistant" = the AI-drawer meta-assistant (spec
    # docs/superpowers/specs/2026-09-04-ai-assistant-design.md §4.1): hidden from the
    # session list, capacity-exempt, tool-restricted. NULL = a normal coding session.
    # Nullable so init_db backfills on both dialects without a migration.
    kind: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Identity that owns an assistant thread (the idempotency key for
    # POST /api/assistant/session). NULL for coding sessions. Nullable for backfill.
    owner_identity: Mapped[str | None] = mapped_column(String, nullable=True, default=None)


class AppSession(Base):
    """Server-side login/identity session (signed cookie → row lookup)."""

    __tablename__ = "app_sessions"
    # Composite partial index for the auth-reuse hot path: every cookie-less request
    # (API polls, WS upgrades) runs SELECT … WHERE identity=? AND revoked_at IS NULL
    # AND expires_at>? ORDER BY created_at DESC LIMIT 1.  The old single-column
    # ix_app_sessions_identity had near-zero selectivity (42k rows, ~4 identities),
    # so Postgres seq-scanned on every call.  This index descends straight to the one
    # live row without touching revoked/expired history. (#830)
    # NOTE: on a live Postgres DB this MUST be created with CREATE INDEX CONCURRENTLY
    # (see migration 0010_app_sessions_composite_index) — it cannot self-apply.
    __table_args__ = (
        Index(
            "ix_app_sessions_identity_created_at",
            "identity",
            text("created_at DESC"),
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    # Verified Tailscale identity string (e.g. user@example.com).
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
    # Actor channel: "assistant" when the request carried the assistant bearer token
    # (the AI drawer acting on the user's behalf); NULL = the user directly.
    via: Mapped[str | None] = mapped_column(String, nullable=True, default=None)


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


class MergeQueueObservation(Base):
    """One row per PR per poll snapshot for the merge-train widget (M-Track-10).

    All non-key columns are NULLABLE so init_db backfills them on existing dev
    DBs without a migration. The store groups rows by captured_at to reconstruct
    Observation objects for the pure derivation in merge_train.py.
    """

    __tablename__ = "merge_queue_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # repo/captured_at are always written (never null); the columns stay
    # nullable=True so init_db backfills existing dev DBs without a migration, but the
    # Python attributes are typed non-optional since the store never reads a null there.
    # pr_number IS nullable in earnest: a NULL row is the empty-queue sentinel —
    # it pins an observation's captured_at while contributing no queue entry.
    repo: Mapped[str] = mapped_column(String, nullable=True, index=True)
    # BigInteger: epoch seconds. The live max is already 83% of the int32
    # ceiling (2038-01-19) — Integer would hard-fail inserts on Postgres well
    # before then.
    captured_at: Mapped[int] = mapped_column(BigInteger, nullable=True, index=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str | None] = mapped_column(String, nullable=True)
    enqueued_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    batch_head_oid: Mapped[str | None] = mapped_column(String, nullable=True)
    group_checks: Mapped[str | None] = mapped_column(String, nullable=True)
    checks_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checks_done: Mapped[int | None] = mapped_column(Integer, nullable=True)
    eta_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)


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
    # Quiet-hours window for notifications (#602), "HH:MM" local. Both NULL = no
    # quiet hours. A window with end <= start wraps midnight. Rules flagged
    # ``critical`` bypass it. Nullable so init_db backfills without a migration.
    quiet_hours_start: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    quiet_hours_end: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Cap on retained archived sessions: when the archived count exceeds this, the
    # oldest are permanently purged (on archive + a periodic sweep). NULL = unlimited
    # (the default). Nullable so init_db backfills it without a migration.
    max_archived_sessions: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # Override directory for archived session working directories.
    # None = use CCSWITCH_ARCHIVE_HOME env var or $CCSWITCH_HOME/archive.
    # Nullable so init_db backfills it without a migration.
    archive_home: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Global cap on concurrently active sessions; overflow spawns queue.
    # NULL = DEFAULT_MAX_ACTIVE_SESSIONS. Nullable so init_db backfills it
    # without a migration.
    max_active_sessions: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # App-wide kill switch for completion auto-archive. NULL/True = auto-archive
    # completed sessions (the default); False disables it globally, so a completed
    # session lingers exactly like a per-session manual opt-out. Nullable (three-state:
    # NULL means "unset → default enabled") so init_db backfills it without a migration.
    auto_archive_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    # Per-install model-approval action: "require_approval" | "block" | "allow" | NULL
    # (NULL = fall back to CCSWITCH_MODEL_APPROVAL_ACTION). See
    # server/sessions/model_policy.VALID_ACTIONS — the one definition of the set.
    # Both nullable so init_db backfills them without a migration on both dialects.
    model_approval_action: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    model_approval_denylist: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # AI assistant (spec 2026-09-04-ai-assistant-design §4.11). NULL = fall through to
    # CCSWITCH_ASSISTANT_ENABLED / CCSWITCH_ASSISTANT_MODEL. Nullable for backfill.
    assistant_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    assistant_model: Mapped[str | None] = mapped_column(String, nullable=True, default=None)


class SessionQueue(Base):
    """Ordering + deferred spawn parameters for status='queued' sessions.

    One row per queued session; deleted when the session starts, is cancelled,
    or fails to start. `params` holds the create_session kwargs that the
    sessions row does not (cols, rows, attachment_ids, identity, requested
    branch/cwd/account) — worktree and account rotation are decided at DEQUEUE.

    Losing this row is NOT fatal to the session (#1100): a `queued` sessions row
    with no entry here is reconciled by
    ``SessionOrchestrator._reconcile_orphan_queued``, which rebuilds a params blob
    from the sessions row via ``_spawn_kwargs_from_row`` and re-enqueues whenever the
    row still proves its turn one never ran. Only the fields listed above are
    genuinely lost. Every path that deletes an entry must persist the session's new
    status FIRST, or a crash in between manufactures an orphan that replay cannot
    tell apart from a legitimately stranded one.
    """

    __tablename__ = "session_queue"

    session_id: Mapped[str] = mapped_column(
        String, ForeignKey("sessions.id"), primary_key=True)
    # default=0 exists only for the SQLite column-add reconcile path (schema
    # coverage guard); enqueue() always assigns the real back-of-queue value.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class NotificationChannel(Base):
    """A user-defined delivery channel for notifications (#602). v1 kind is
    'webhook' — a POST to a user-supplied URL (covers ntfy/Slack/Discord).
    Server-side delivery avoids the prod insecure-context constraint entirely."""

    __tablename__ = "notification_channel"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="webhook")
    url: Mapped[str] = mapped_column(String, nullable=False)
    # Optional ntfy/Slack-style headers (e.g. {"Title": "...", "Priority": "high"}).
    headers: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # timezone=True because these default to _utcnow(), which is AWARE. asyncpg rejects
    # an aware datetime bound to `timestamp without time zone` — on SELECT comparisons as
    # well as INSERT. The reverse (naive value into a tz-aware column) is accepted and
    # treated as UTC, so widening is strictly safe: it removes failures and adds none.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class NotificationRule(Base):
    """One routing rule (#602): match an event class + optional scope → an action.

    Rules are evaluated in ascending ``priority`` (then created_at); the FIRST
    enabled rule that matches decides the action. event_class '*' matches any."""

    __tablename__ = "notification_rule"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # needs_input | session_ended | automation_error | automation_warn | pr_broke | pr_merged | *
    event_class: Mapped[str] = mapped_column(String, nullable=False, default="*")
    scope_repo: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    scope_account: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    scope_automation: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    action: Mapped[str] = mapped_column(String, nullable=False, default="push")  # push|channel|ignore
    channel_id: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # When true this rule's delivery ignores quiet hours (critical bypass).
    critical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class Automation(Base):
    """Registry state for one code-defined automation. Identity (name, schema,
    behavior) lives in server/automations/; this row stores only mutable state."""

    __tablename__ = "automation"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # stable slug
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mode: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    # Optional user-configured cadence (#603): {activeHours?, cron?}. NULL = the
    # code-defined interval/trigger with no gating. Parsed by server/automations/
    # schedule.py; the runner skips a tick outside the active-hours window.
    # none_as_null so clearing via None persists SQL NULL. Nullable so init_db
    # backfills it on the dev SQLite DB without a migration.
    schedule: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None)
    # Heartbeat the automation bumps while alive; lets the UI flag a stalled loop.
    # timezone=True: _bump_automation_active() (orchestrator.py) writes
    # datetime.now(timezone.utc), an AWARE value — a naive column silently fails
    # on Postgres (asyncpg rejects tz-aware datetimes into a naive column), and
    # that write sits inside a bare `except Exception: log.debug(...)`, so the
    # heartbeat would just stop with no error surfaced.
    last_active_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None)
    # Watermark for cron-scheduled recurring-session dispatch (#603): the most
    # recent cron minute already fired (or a baseline set on first sight). The
    # scheduler only fires strictly-later minutes, so this persists the "already
    # ran" guard across daemon restarts and prevents a double-fire. NULL until the
    # scheduler first evaluates the row. Nullable so init_db backfills it on the
    # dev SQLite DB without a migration.
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class AutomationEvent(Base):
    """One row in an automation's append-only, capped activity feed."""

    __tablename__ = "automation_event"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    automation_id: Mapped[str] = mapped_column(
        String, ForeignKey("automation.id"), nullable=False, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)
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
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class BacklogGroup(Base):
    """One planned unit of Backlog Runner work: an ordered group of related
    issue refs the background grouping optimizer decided belong in one PR.

    Rows are written generation-at-a-time by the grouper (a refresh REPLACES
    the previous generation wholesale) and consumed read-only by the runner's
    selection path and the automations API. ``refs`` is ordered, primary
    first; ``rank`` is the group's priority within its generation (0 = next).
    ``consumed_at`` is stamped when the runner claims the group, purely for
    the UI — correctness never depends on it (claimed issues drop out of the
    live candidate pool, so a consumed group can't be re-selected).

    Every non-key column is NULLABLE (the SQLite reconcile rule — see
    MergedPr's docstring); timestamps are ISO-8601 TEXT for direct JSON use.
    """

    __tablename__ = "backlog_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generation: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    repo: Mapped[str | None] = mapped_column(String, nullable=True)
    refs: Mapped[list | None] = mapped_column(JSON, nullable=True)
    titles: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    rationale: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str | None] = mapped_column(String, nullable=True)
    consumed_at: Mapped[str | None] = mapped_column(String, nullable=True)


class CiRun(Base):
    """One completed GitHub Actions workflow run (CI/CD Doctor sensing).
    id is the GitHub run id (globally unique). All non-PK columns nullable so
    init_db backfills the dev SQLite DB without a migration."""

    __tablename__ = "ci_run"

    # GitHub run/job ids are int64 (e.g. 32407697884) — Integer (32-bit) only
    # "worked" here because SQLite is dynamically typed; Postgres rejects
    # any value above 2^31-1 with "integer out of range".
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    workflow_path: Mapped[str | None] = mapped_column(String, nullable=True)
    event: Mapped[str | None] = mapped_column(String, nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    head_branch: Mapped[str | None] = mapped_column(String, nullable=True)
    conclusion: Mapped[str | None] = mapped_column(String, nullable=True)
    created_ts: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)


class CiJob(Base):
    """One job (== status-check context) inside a CiRun. sha/branch/created_ts
    are denormalized from the run so the analyzers query a single table."""

    __tablename__ = "ci_job"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # GitHub job id
    run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    repo: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    conclusion: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    head_branch: Mapped[str | None] = mapped_column(String, nullable=True)
    created_ts: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)


class CiTestResult(Base):
    """One test-case outcome parsed from a run's JUnit artifact."""

    __tablename__ = "ci_test_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    suite: Mapped[str | None] = mapped_column(String, nullable=True)
    test_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)  # passed/failed/error/skipped
    time_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Parsed failure/error message (truncated) — surfaced in flaky-test findings.
    message: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    head_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    head_branch: Mapped[str | None] = mapped_column(String, nullable=True)
    created_ts: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)

    # Backstop for the ingest-side dedupe above. NULLs compare as distinct in SQL, so
    # rows with a NULL suite/test_id are NOT covered — ingest-side dedupe is the
    # primary guard, this catches concurrent-tick races. Created on the live DB by
    # scripts/ccswitch-db-compact.py (create_all does not add constraints to an
    # existing table).
    #
    # Migration-order warning: when the Postgres migration (sub-project 1b) adds
    # this same unique constraint, it MUST run AFTER scripts/ccswitch-db-compact.py
    # has deduped the table. Creating uq_ci_test_result_run_suite_test on the
    # un-compacted table fails outright — ~430k existing duplicate rows violate it.
    #
    # (repo, created_ts) composite: every doctor tick scans "this repo, this
    # window" (cicd_doctor.flake_counts_stmt). The separate single-column indexes
    # above cannot serve that pair, so Postgres chose a Parallel Seq Scan over the
    # whole 26.4M-row table — 65s per tick per repo on 2026-08-29. Like the unique
    # constraint above, create_all does NOT add indexes to an existing table and
    # deploys do not run `alembic upgrade`, so on a live DB this must be created by
    # hand (CREATE INDEX CONCURRENTLY — it takes minutes and must not hold a lock).
    __table_args__ = (
        UniqueConstraint("run_id", "suite", "test_id",
                         name="uq_ci_test_result_run_suite_test"),
        Index("ix_ci_test_result_repo_created_ts", "repo", "created_ts"),
    )


class CiTestDaily(Base):
    """Per-(repo, test, day) outcome counts, rolled up from CiTestResult.

    Flaky-test detection needs the RATE at which a test fails, not each individual
    passing row. Raw passed/skipped rows older than raw_retention_days are folded in
    here and then dropped (CicdDoctorLoop._rollup_passed); failed rows stay raw for
    retention_days because findings quote their message.

    Every non-key column is NULLABLE — the SQLite reconcile rule (see
    test_schema_reconcile_coverage): init_db can only backfill nullable columns.
    """

    __tablename__ = "ci_test_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    test_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # 'YYYY-MM-DD' UTC. A string, not a Date. Bucketed in SQL by the fold
    # (cicd_doctor.rollup_day_expr, per-dialect) — pinned to the Python reference
    # datetime.fromtimestamp(ts, utc).strftime('%Y-%m-%d') by an oracle test, so
    # the value is identical on SQLite and Postgres (#833).
    day: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    passed: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    # Reserved: no writer currently populates this. Failed rows are kept raw
    # (never folded into CiTestDaily) for the full retention window because
    # findings quote their message — see the class docstring.
    failed: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    skipped: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)

    __table_args__ = (
        UniqueConstraint("repo", "test_id", "day", name="uq_ci_test_daily"),
    )


class CiFinding(Base):
    """A persistent CI/CD Doctor finding: fingerprint-deduped, lifecycle
    open -> resolved. issue_number links the filed ci-doctor GitHub issue."""

    __tablename__ = "ci_finding"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    repo: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    kind: Mapped[str | None] = mapped_column(String, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True, default="open")
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # True once the linked issue is confirmed closed. NULL/False means it is still
    # open on GitHub — either an active finding, or a resolve whose close failed and
    # must be retried. Kept alongside issue_number so a recurrence can reopen it.
    issue_closed: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    first_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=_utcnow)
    # timezone=True to match last_seen_at directly above: both are written with
    # _utcnow() (AWARE) from cicd_doctor.py and resolved_at is also compared
    # against an aware cutoff (`resolved_at < finding_cutoff`) during retention
    # pruning. A naive column here silently breaks both the write and the
    # comparison on Postgres — keep the two columns consistent.
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None)


# ---------------------------------------------------------------------------
# Phase-3 plotroom tables: merged PR timeline, group runs/failures, deploy cfg
# ---------------------------------------------------------------------------


class MergedPr(Base):
    """One row per merged PR, carrying waypoint timestamps for the pipeline
    deploy-chain and lead-time metrics (SPEC §2 — PRTIMELINE CAPTURE).

    Timestamps are ISO-8601 TEXT strings (not epoch ints) because they feed
    directly into the JSON SSE payload without conversion — unlike the epoch-int
    approach in MergeQueueObservation which models internal polling state.

    Every non-key column is NULLABLE (the SQLite reconcile rule — see
    test_schema_reconcile_coverage). merged_at is the insertion trigger and is
    ALWAYS written by the timeline store (writer-side invariant), but stays
    nullable at the schema level so reconcile can add it to an existing DB.
    Composite PK (repo, number). Upsert uses COALESCE(excluded.X, X)
    first-value-wins semantics (caller's job).
    """

    __tablename__ = "merged_prs"

    repo: Mapped[str] = mapped_column(String, primary_key=True)
    number: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    # Writer-side invariant: always set on insert (the merge observation).
    merged_at: Mapped[str | None] = mapped_column(String, nullable=True)
    merge_commit_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str | None] = mapped_column(String, nullable=True)
    first_green_at: Mapped[str | None] = mapped_column(String, nullable=True)
    enqueued_at: Mapped[str | None] = mapped_column(String, nullable=True)
    qa_live_at: Mapped[str | None] = mapped_column(String, nullable=True)
    prod_live_at: Mapped[str | None] = mapped_column(String, nullable=True)


class GroupRun(Base):
    """One completed merge-group CI run whose checks all concluded SUCCESS|SKIPPED|NEUTRAL.

    Supplies duration samples for the MERGEETASIM p50/p90 ETA analytic (SPEC §1).
    completed_at is unique per repo — one canonical record per run completion.
    """

    __tablename__ = "group_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    duration_secs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ISO-8601 TEXT — see MergedPr docstring for rationale vs. epoch int.
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("repo", "completed_at", name="uq_group_runs_repo_completed_at"),
    )


class GroupFailure(Base):
    """One failed/timed-out check within a merge-group run (SPEC §1 — eject probability).

    Deduped on (repo, group_sha, check_name): INSERT OR IGNORE semantics (caller's job).
    conclusion: FAILURE | TIMED_OUT | STARTUP_FAILURE.
    """

    __tablename__ = "group_failures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    check_name: Mapped[str | None] = mapped_column(String, nullable=True)
    group_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    # ISO-8601 TEXT — see MergedPr docstring for rationale vs. epoch int.
    observed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    conclusion: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "repo", "group_sha", "check_name",
            name="uq_group_failures_repo_sha_check",
        ),
    )


class RepoDeployConfig(Base):
    """Per-repo deploy-environment configuration (SPEC §3).

    environments: JSON text — list of {name, healthUrl, auto, shaKey?}.
    enabled gates whether deploy health polling runs for this repo.
    """

    __tablename__ = "repo_deploy_config"

    repo: Mapped[str] = mapped_column(String, primary_key=True)
    environments: Mapped[str | None] = mapped_column(String, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


# ---------------------------------------------------------------------------
# Power-user feature tables: SessionPreset, PromptTemplate, PermissionProfile
# ---------------------------------------------------------------------------


class SessionPreset(Base):
    """A named bundle of session launch settings (account, model, repo, etc.)
    that can be selected in the DispatchBar to pre-fill all fields at once."""

    __tablename__ = "session_presets"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    repo: Mapped[str | None] = mapped_column(String, nullable=True)
    account: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    permission_mode: Mapped[str | None] = mapped_column(String, nullable=True)
    initial_prompt: Mapped[str | None] = mapped_column(String, nullable=True)
    branch_strategy: Mapped[str | None] = mapped_column(String, nullable=True)
    permission_profile_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)

    __table_args__ = (
        UniqueConstraint("name", name="uq_session_presets_name"),
    )


class PromptTemplate(Base):
    """A named, server-stored prompt template with {{variable}} placeholders,
    insertable from the DispatchBar or in-session composer."""

    __tablename__ = "prompt_templates"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str | None] = mapped_column(String, nullable=True)
    variables: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)

    __table_args__ = (
        UniqueConstraint("name", name="uq_prompt_templates_name"),
    )


class PermissionProfile(Base):
    """A named, ordered list of tool patterns (e.g. 'Bash(git *)', 'Read')
    applied at session spawn to set the allowlist in one click."""

    __tablename__ = "permission_profiles"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    mode: Mapped[str | None] = mapped_column(String, nullable=True)
    patterns: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)

    __table_args__ = (
        UniqueConstraint("name", name="uq_permission_profiles_name"),
    )


class Host(Base):
    """A session-executing host in the fleet.

    One row per daemon the edge may dispatch to. ``endpoint`` uses the same grammar
    ``server.ipc.paths.parse_endpoint`` accepts (``unix:///path`` or
    ``https://host:port``), so a single-host deployment is just a one-row fleet.

    The probe-written columns are all nullable: a host that has never been probed
    reports UNKNOWN, not false. Ranking treats unknown health as NOT dispatchable —
    assuming an unprobed host is up is how you dispatch into one that has been down
    since boot.
    """

    __tablename__ = "hosts"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    endpoint: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Operator-set relative preference; multiplies the computed score.
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # Hard cap on concurrent sessions; NULL = uncapped.
    max_sessions: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # --- operator-set upgrade config (NULL = in-app upgrade not configured) ---
    # ssh destination ([user@]host, validated against server.deploy.steps.SSH_HOST_RE)
    # and the absolute checkout path on that host (SSH_REPO_RE). Both feed argv of a
    # sudo-capable ssh command, so the API validates them with the SAME patterns the
    # deploy controller uses for CCSWITCH_DAEMON_SSH_HOST/_REPO.
    ssh_host: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    repo_path: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    # --- probe-written state (nullable = "not yet known") ---
    healthy: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    sha: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    protocol: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    load_avg: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    mem_available_mb: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    active_sessions: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # Fresh `git rev-parse --short HEAD` of the daemon's checkout (reported as
    # /info.checkoutSha). Differs from `sha` (the PROCESS's start-time HEAD) after an
    # edge-only deploy updated the checkout without a restart. Nullable: reconcile adds it.
    checkout_sha: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Last-seen `/info.engines` — per-engine availability as this runner advertised it
    # (see server/sessions/host_engines.py for the shape and the one parser). NULL is
    # "never probed, or probed a daemon that predates the advertisement", which the
    # parser resolves to the legacy Claude-only default for one upgrade window. Stored
    # rather than re-probed at dispatch time so ranking can FILTER candidates without an
    # RPC per host; the chosen host is still re-checked live before a non-Claude launch.
    engines: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None)


class DeployStateRow(Base):
    """Single-row (id='singleton') record of the fleet's deployed version (spec §1).

    Written by the deploy controller once the edge is healthy on a target; read by the
    fleet probe's version-skew gate and by ``server.deploy.runner_sync`` at runner boot.
    Every column but the PK is nullable so init_db reconciles it without a migration.
    """

    __tablename__ = "deploy_state"

    id: Mapped[str] = mapped_column(String, primary_key=True, default="singleton")
    deployed_sha: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    deployed_short: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Full sha of the last deploy whose diff was daemon-changing: a runner PROCESS older
    # than this is stale even if its checkout is current.
    daemon_sha: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    deployed_by: Mapped[str | None] = mapped_column(String, nullable=True, default=None)


class DaemonLease(Base):
    """Fleet-wide single-holder leases; today only ``name='primary'`` (#852).

    ``CCSWITCH_DAEMON_ROLE`` is per-host env config — nothing structural stops two
    hosts both being configured ``primary``, and two primaries double-drive every
    fleet-global automation with no detection (the 2026-08-30 incident shape). This
    row turns that runbook invariant into a checked one: a primary daemon must hold
    the lease to finish ``start()``, renews it on a heartbeat, and a second primary
    fails its boot loudly instead of silently double-driving.

    ``fence`` increments on every (re-)acquisition; renew/release are conditional on
    it, so a daemon whose lease was taken over (fenced) cannot resurrect it by
    heartbeating with the stale fence. Holder identity is the HOST name — same-host
    re-acquisition is always allowed (systemd guarantees one daemon per host), so a
    crash restart of the real primary never waits out the TTL.
    """

    __tablename__ = "daemon_leases"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    # Writer-side invariants: always set by acquire; defaults exist only so the
    # SQLite reconcile path could synthesize them (see test_schema_reconcile_coverage).
    holder: Mapped[str] = mapped_column(String, nullable=False, default="")
    fence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow)
    renewed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow)


class HostHeartbeat(Base):
    """Fenced per-host daemon heartbeat (spec 002 FR-101).

    Written only by the named host (fenced renew in host_heartbeat.beat) or by the
    primary bumping the fence to declare the host dead (host_heartbeat.declare_dead).
    Liveness is DERIVED from beat_at — never stored (FR-102)."""

    __tablename__ = "host_heartbeats"

    host: Mapped[str] = mapped_column(String, primary_key=True)
    beat_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    fence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    epoch: Mapped[str | None] = mapped_column(String, nullable=True)


# ---------------------------------------------------------------------------
# AI-native SDLC artifact-chain spine (#795 Phase 1 / #796)
#
# An Initiative is one unit of work as it moves through the lifecycle
# intake → design → plan → build → deploy → maintain → done, carrying an
# ordered chain of InitiativeArtifacts (its intent, spec, and plan documents).
#
# Deliberately NOT modelled here: per-initiative state-machine counters
# (retry/loop-guard/"gave up"). Those reuse the existing AutomationTargetState
# table keyed by the initiative slug — there is no separate counter table, so
# the one durable state store the automations already share is the single
# source of truth (constitution VI).
#
# Every non-key column is nullable so init_db()'s _reconcile_columns can
# ALTER-ADD it to an existing DB on any dialect (the deploy path runs reconcile,
# never `alembic upgrade`). slug/repo/title/stage/status/kind/repo_path are
# writer-side invariants (always set on insert) even though the schema permits
# NULL — the same pattern as MergedPr / BacklogGroup.
# ---------------------------------------------------------------------------


# Ordered lifecycle stages an Initiative moves through. Single source of truth:
# import this instead of re-inlining the sequence (constitution VI).
INITIATIVE_STAGES: tuple[str, ...] = (
    "intake", "design", "plan", "build", "deploy", "maintain", "done")

# Coarse status an Initiative can rest in, orthogonal to its stage.
INITIATIVE_STATUSES: tuple[str, ...] = (
    "proposed", "active", "awaiting_human", "blocked", "done")

# The three artifact kinds that make up an initiative's document chain, in order.
INITIATIVE_ARTIFACT_KINDS: tuple[str, ...] = ("intent", "spec", "plan")


class Initiative(Base):
    """One unit of work tracked across the AI-native SDLC lifecycle.

    ``slug`` is the stable, human-readable identity (unique) and the key under
    which per-initiative state-machine counters live in AutomationTargetState.
    ``stage`` advances through INITIATIVE_STAGES; ``status`` is the orthogonal
    rest-state in INITIATIVE_STATUSES. ``origin`` records what spawned the
    initiative (e.g. "backlog-runner") and ``origin_ref`` an optional pointer
    back to it (e.g. an issue ref).
    """

    __tablename__ = "initiatives"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    # Writer-side invariant: always set. Nullable + unique so reconcile can
    # ALTER-ADD it; a unique index over a nullable column allows many NULLs on
    # both SQLite and Postgres, which never occur in practice (slug is required).
    slug: Mapped[str | None] = mapped_column(String, nullable=True, unique=True, index=True)
    repo: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    # Lifecycle position; default is the first INITIATIVE_STAGES entry.
    stage: Mapped[str | None] = mapped_column(String, nullable=True, default="intake")
    # Rest-state; default is the first INITIATIVE_STATUSES entry.
    status: Mapped[str | None] = mapped_column(String, nullable=True, default="proposed")
    origin: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    origin_ref: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # --- spec 003 overlay (epic pointer for GitHub Projects v2 mirror) ---
    epic_repo: Mapped[str | None] = mapped_column(String, nullable=True)
    epic_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # --- PM product layer (phase 1 groundwork; design 2026-08-31) ---
    # Owning project; nullable so pre-PM initiatives keep working unlinked.
    project_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("projects.id"), nullable=True, index=True)
    # RICE-lite score inputs (spec §8: score = reach×impact×confidence/effort).
    reach: Mapped[int | None] = mapped_column(Integer, nullable=True)
    impact: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effort: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # timezone=True to match the newer tables: both default to _utcnow() (AWARE),
    # which asyncpg rejects into a naive `timestamp without time zone` column.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class InitiativeArtifact(Base):
    """One document in an Initiative's artifact chain (intent → spec → plan).

    ``repo_path`` is where the artifact lives in the repo; ``commit_sha`` /
    ``pr_number`` / ``pr_url`` are stamped once it is committed and proposed;
    ``session_id`` / ``model`` record which ccswitch session (and which model)
    authored it. All are nullable — an intent may exist before any commit.
    """

    __tablename__ = "initiative_artifacts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    initiative_id: Mapped[str] = mapped_column(
        String, ForeignKey("initiatives.id"), nullable=True, index=True)
    # intent | spec | plan (INITIATIVE_ARTIFACT_KINDS).
    kind: Mapped[str | None] = mapped_column(String, nullable=True)
    repo_path: Mapped[str | None] = mapped_column(String, nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    pr_url: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    model: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)


# ---------------------------------------------------------------------------
# PM product layer (design: docs/superpowers/specs/2026-08-31-pm-product-layer-design.md)
#
# Truth model is layered: GitHub owns work items (WorkItem is a disposable
# mirror), repo markdown owns feature definitions (Feature is an index row,
# like InitiativeArtifact), and these tables own ONLY what has no other home:
# relationships, the classification/score overlay, and AI triage proposals.
# Every non-key column is nullable so _reconcile_columns can ALTER-ADD it
# (same rule as the SDLC block above).
# ---------------------------------------------------------------------------

# Onboarding wizard states, in order (spec §5). Single source of truth.
PROJECT_ONBOARDING_STATES: tuple[str, ...] = (
    "registered", "syncing", "map_proposed", "map_merged", "classifying", "live")

# Typed project→project relationship kinds (spec §3).
PROJECT_EDGE_KINDS: tuple[str, ...] = ("depends_on", "deploys_via", "integrates_with")


class Project(Base):
    """One product under PM management. ``slug`` is the stable identity;
    repos attach via ProjectRepo (the primary repo hosts the feature docs)."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    # Writer-side invariant: always set (nullable for reconcile, like Initiative.slug).
    slug: Mapped[str | None] = mapped_column(String, nullable=True, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True, default="active")
    portfolio_tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # First PROJECT_ONBOARDING_STATES entry; advanced by sync/onboarding flows.
    onboarding_state: Mapped[str | None] = mapped_column(
        String, nullable=True, default="registered")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class ProjectRepo(Base):
    """A repo attached to a project, carrying that repo's sync state.

    ``backfill_cursor``/``backfill_done`` drive resumable history import;
    ``watermark`` is the incremental-sync high-water mark and advances ONLY
    after a fully successful pass (spec §4). ``sync_requested_at`` is the
    sync-now signal set by the API and cleared by the loop."""

    __tablename__ = "project_repos"
    __table_args__ = (UniqueConstraint("project_id", "repo", name="uq_project_repo"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=True, index=True)
    repo: Mapped[str | None] = mapped_column(String, nullable=True)  # "owner/name"
    is_primary: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    backfill_cursor: Mapped[str | None] = mapped_column(String, nullable=True)
    # Which phase the cursor belongs to: "issues" then "prs" (backfill order).
    backfill_phase: Mapped[str | None] = mapped_column(String, nullable=True, default="issues")
    backfill_done: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)


class ProjectEdge(Base):
    """Typed project→project relationship (spec §3): depends_on | deploys_via |
    integrates_with, with an optional free-text note."""

    __tablename__ = "project_edges"
    __table_args__ = (
        UniqueConstraint("src_project_id", "dst_project_id", "kind", name="uq_project_edge"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    src_project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=True, index=True)
    dst_project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=True, index=True)
    kind: Mapped[str | None] = mapped_column(String, nullable=True)  # PROJECT_EDGE_KINDS
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)


# Feature lifecycle (spec §3, "feature doc convention").
FEATURE_LIFECYCLE_STATES: tuple[str, ...] = (
    "proposed", "discovery", "delivering", "live", "sunset")

# Feature→feature relationship kinds (cross-project allowed).
FEATURE_LINK_KINDS: tuple[str, ...] = ("powered_by", "integrates_with")

WORK_ITEM_KINDS: tuple[str, ...] = ("issue", "pr")

# Approved-overlay triage states on WorkItem (proposals live in TriageProposal).
WORK_ITEM_TRIAGE_STATES: tuple[str, ...] = (
    "unclassified", "proposed", "classified", "needs_human")

TRIAGE_PROPOSAL_STATUSES: tuple[str, ...] = ("pending", "accepted", "rejected")


class Feature(Base):
    """Index row for one ``docs/product/features/<slug>.md`` in the project's
    primary repo. Truth is the merged markdown: ``commit_sha`` is stamped by
    sync once the doc is on the default branch (NULL while only proposed) —
    the InitiativeArtifact pointer pattern. ``orphaned`` marks a doc that
    disappeared from the repo (row kept so FeatureLinks never dangle)."""

    __tablename__ = "features"
    __table_args__ = (UniqueConstraint("project_id", "slug", name="uq_feature_slug"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.id"), nullable=True, index=True)
    slug: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str | None] = mapped_column(String, nullable=True)  # FEATURE_LIFECYCLE_STATES
    summary: Mapped[str | None] = mapped_column(String, nullable=True)
    repo_path: Mapped[str | None] = mapped_column(String, nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    pr_url: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    orphaned: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class FeatureLink(Base):
    """Feature→feature edge (spec §3): powered_by | integrates_with."""

    __tablename__ = "feature_links"
    __table_args__ = (
        UniqueConstraint("src_feature_id", "dst_feature_id", "kind", name="uq_feature_link"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    src_feature_id: Mapped[str] = mapped_column(
        String, ForeignKey("features.id"), nullable=True, index=True)
    dst_feature_id: Mapped[str] = mapped_column(
        String, ForeignKey("features.id"), nullable=True, index=True)
    kind: Mapped[str | None] = mapped_column(String, nullable=True)  # FEATURE_LINK_KINDS
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)


class WorkItem(Base):
    """Read-only mirror of one GitHub issue or PR, plus the ccswitch-owned
    overlay. Mirror columns are disposable (rebuildable by re-sync); overlay
    columns (feature_id, scores, triage_state) are the approved truth ccswitch
    owns. Keyed by (repo, number, kind) so a dropped-and-resynced mirror keeps
    the same identity."""

    __tablename__ = "work_items"
    __table_args__ = (
        UniqueConstraint("repo", "number", "kind", name="uq_work_item_identity"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    # --- mirror (GitHub truth) ---
    repo: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str | None] = mapped_column(String, nullable=True)  # WORK_ITEM_KINDS
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    body: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str | None] = mapped_column(String, nullable=True)  # open|closed|merged
    labels: Mapped[list | None] = mapped_column(JSON, nullable=True)
    author: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    # GitHub GraphQL node id (e.g. "I_kwDO..."), used by the reconciler for
    # addProjectV2ItemById content ids and addBlockedBy mutations (spec 003 Phase B).
    gh_node_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # PR→issue closing references (list of ints), from closingIssuesReferences.
    linked_issue_numbers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # GitHub assignee logins; an assigned item is never queue-eligible (spec 003 §7a, C-5).
    assignees: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # --- spec 003 overlay (epic hierarchy + projects v2 mirror) ---
    parent_work_item_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("work_items.id"), nullable=True, index=True)
    is_epic: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    gh_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    gh_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True)
    gh_closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # --- overlay (ccswitch truth) ---
    feature_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("features.id"), nullable=True, index=True)
    reach: Mapped[int | None] = mapped_column(Integer, nullable=True)
    impact: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effort: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    triage_state: Mapped[str | None] = mapped_column(
        String, nullable=True, default="unclassified", index=True)
    classified_by: Mapped[str | None] = mapped_column(String, nullable=True)
    classified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


# ---------------------------------------------------------------------------
# spec 003 — Phase A: GitHub Projects v2 read-only mirror
# ---------------------------------------------------------------------------

DEP_SOURCES = ("native", "prose", "order")


class GhProject(Base):
    """Read-only mirror of one GitHub Projects v2 board (one per product — spec 003 D3)."""

    __tablename__ = "gh_projects"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    project_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("projects.id"), nullable=True, index=True)
    gh_project_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gh_node_id: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    field_cache: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class GhProjectItem(Base):
    """Read-only mirror of one Projects v2 item; substrate of the global queue (spec 003 §5)."""

    __tablename__ = "gh_project_items"
    __table_args__ = (
        UniqueConstraint("gh_project_id", "gh_item_node_id", name="uq_gh_project_item"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    gh_project_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("gh_projects.id"), nullable=True, index=True)
    work_item_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("work_items.id"), nullable=True, index=True)
    gh_item_node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    stage: Mapped[str | None] = mapped_column(String, nullable=True)
    gh_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class WorkItemDep(Base):
    """One observed dependency edge; ``source`` records provenance (spec 003 §5)."""

    __tablename__ = "work_item_deps"
    __table_args__ = (
        UniqueConstraint("src_ref", "dst_ref", "source", name="uq_work_item_dep"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    src_work_item_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("work_items.id"), nullable=True, index=True)
    dst_work_item_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("work_items.id"), nullable=True, index=True)
    src_ref: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    dst_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True)  # DEP_SOURCES
    active: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class GhWriteJournal(Base):
    """Write-ahead journal for reconciler mutations (spec 003 §6). Written from Phase B on;
    the schema lands with the rest of the mirror so migration 0018 is the complete record."""

    __tablename__ = "gh_write_journal"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    target_node_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    field: Mapped[str | None] = mapped_column(String, nullable=True)
    old_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Indexed: every planned mutation is looked up by key each cycle (parked? failed
    # before? already dry-run journaled?), so the journal is read far more than written.
    mutation_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    initiated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    # Groups journal rows written by the same reconciler pass (spec 003 Phase B),
    # so a batch's mutations can be queried/retried/audited together.
    batch_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    applied: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class TriageProposal(Base):
    """One AI classification proposal awaiting the human gate (spec §3).
    ``proposed_feature_id`` is NULL for a ``no_fit`` answer, which instead
    carries ``no_fit_suggestion`` (the suggested new-feature name). Accepting
    copies values onto the WorkItem overlay; this row remains as audit."""

    __tablename__ = "triage_proposals"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    work_item_id: Mapped[str] = mapped_column(
        String, ForeignKey("work_items.id"), nullable=True, index=True)
    proposed_feature_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("features.id"), nullable=True)
    no_fit_suggestion: Mapped[str | None] = mapped_column(String, nullable=True)
    reach: Mapped[int | None] = mapped_column(Integer, nullable=True)
    impact: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effort: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rationale: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence_level: Mapped[str | None] = mapped_column(String, nullable=True)  # low|medium|high
    status: Mapped[str | None] = mapped_column(
        String, nullable=True, default="pending", index=True)  # TRIAGE_PROPOSAL_STATUSES
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)


class InitiativeRepo(Base):
    """Initiative↔repo M2M — phase-1 groundwork for cross-project initiatives
    (spec §3). ``Initiative.repo`` remains the primary repo until phase 4
    folds it in; rows here are additive."""

    __tablename__ = "initiative_repos"
    __table_args__ = (UniqueConstraint("initiative_id", "repo", name="uq_initiative_repo"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    initiative_id: Mapped[str] = mapped_column(
        String, ForeignKey("initiatives.id"), nullable=True, index=True)
    repo: Mapped[str | None] = mapped_column(String, nullable=True)
    is_primary: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)


class InitiativeFeature(Base):
    """Which features an initiative creates/changes (spec §3)."""

    __tablename__ = "initiative_features"
    __table_args__ = (
        UniqueConstraint("initiative_id", "feature_id", name="uq_initiative_feature"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    initiative_id: Mapped[str] = mapped_column(
        String, ForeignKey("initiatives.id"), nullable=True, index=True)
    feature_id: Mapped[str] = mapped_column(
        String, ForeignKey("features.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
