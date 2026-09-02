"""SessionOrchestrator — ties SessionDriver, UsagePoller, and rotation together.

The API and WS layers call this.  One instance lives on app.state.

Usage:
    orch = SessionOrchestrator(sdk_factory=real_sdk_factory)
    await orch.start()
    record = await orch.create_session("My session")
    ...
    await orch.stop()
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import functools
import logging
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from datetime import datetime

    from server.automations.completion_bar import PrFacts
    from server.github.merge_queue import QueueEntry
    from server.github.pr_status import PipelinePr
    from server.pipeline.merge_train import MergeTrainState
    from server.pipeline.track import StageModel
    from server.pm.onboarding import ProjectOnboardingView

from ccswitch import accounts as ccswitch_accounts
from ccswitch import configdir, copilot as _copilot_mod, crypto, gitrepos, worktrees
from ccswitch.accounts import provider_of as _provider_of
from ccswitch.account_names import (
    assert_valid_account_name, validate_account_name, InvalidAccountName,
)
from ccswitch.config import load_config
from ccswitch.configdir import compose
from ccswitch.paths import account_cfg_dir, shared_base
from server.aio import bounded_wait
from server.audit import record_audit
from server.db import db_session
from server.db.models import AUTO_ARCHIVE_PROVENANCE, CAPACITY_EXEMPT_STATUSES, INACTIVE_SESSION_STATUSES, Session as DbSession
from server.sessions import adopt as _adopt
from server.sessions.account_auth import oauth_env_for
from server.sessions import deploy_snapshot as _deploy_snapshot
from server.sessions.archive_move import mountpoint, restore_from_archive
from server.sessions import attachments as _attachments
from server.sessions import dispatch
from server.sessions import events as ev
from server.sessions import primary_lease
from server.sessions import push as push_mod
from server.sessions.commands import catalog as cmd_catalog
from server.sessions.commands import dispatcher as cmd_dispatcher
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry, resolve_permission_mode
from server.sessions.sdk_factory import real_sdk_factory, real_suggestion_factory, real_summary_factory
from server.sessions.replay import replay_events
from server.sessions import status as _status_mod
from server.sessions.suggestions import SuggestionService, last_turn_context
from server.sessions.summaries import SummaryService, should_regenerate
from server.sessions.recovery import (
    RecoveryState, RETRY_AFTER_MAX, QUARANTINE_429, QUARANTINE_401, QUARANTINE_MAX,
    STALL_TIMEOUT, STALL_TIMEOUT_THROTTLED,
)
from server.sessions.selection import AllAccountsBusy, Picked, select_account  # noqa: F401
from server.sessions.background_tasks import scan_open_background_tasks, build_rearm_prompt, OpenBgTask
from server.obs.context import log_event
from server.sessions.title_generator import generate_title
from server.sessions.titles import read_ai_title_from_file, read_user_prompts_from_file
from server.sessions.types import SessionRecord, SYSTEM_IDENTITY
from server.config import env_float
from server.time_iso import iso_utc
from server.usage.poller import UsagePoller
from server.github.pr_status import PullRequestInfo
from server.sessions.pr_fix import build_conflict_fix_prompt, build_pr_fix_prompt

log = logging.getLogger(__name__)


# Statuses that mean a session is mid-turn or awaiting user input — restarting now
# would lose in-flight work, so the deploy drain waits for them to clear. All other
# statuses (idle / done / limited / ended / archived) are restart-safe: resume +
# transcript replay recover them.
_ACTIVE_STATUSES = frozenset({"in_progress", "working", "waiting_input", "condensing"})

# Statuses where the SDK owned an in-flight turn when the deploy snapshot was taken.
# The restart kills that turn (ensure_driver only resumes the transcript), so
# restore_deploy re-issues the snapshotted prompt to continue it (issue #864).
# Deliberately EXCLUDES the awaiting-user statuses (waiting_input / waiting_question /
# ready_review): replaying there would redo work the user was asked to approve —
# rehydrate()'s interrupted_by_restart flag (#434) covers those instead. "working"
# is the legacy pre-crash spelling of an active turn (see rehydrate()).
_REPLAY_STATUSES = _status_mod.MID_TURN_STATUSES | frozenset({"working"})

# Statuses derived purely from the live driver's in-memory registries — a mid-turn
# turn, or a pending permission / question / plan approval (see status.derive_status
# + driver._dash_signals). A daemon restart wipes those registries, so NONE of these
# can ever re-derive after a restart: the persisted row would keep a "needs you"
# badge with no prompt to answer (the permission Future the restart destroyed).
# rehydrate() MUST normalize every one to idle (resumable). Superset of
# _ACTIVE_STATUSES: waiting_question / ready_review don't block the deploy drain but
# strand identically, so they belong here even though they're absent above.
_RESTART_TRANSIENT_STATUSES = _ACTIVE_STATUSES | frozenset({"waiting_question", "ready_review"})

# Message surfaced to the user when auto-recovery gives up on a repeatedly-dropped turn.
_TURN_DROPPED_GIVEUP = ("Auto-recovery gave up after repeated empty turns — the context "
                        "may be full. Send a message to retry, or /compact to free space.")

# Bounded auto-recovery attempts for a session that failed to resume after a
# daemon-affecting deploy. Past this the session is kept visible (flagged gave-up)
# but no longer retried — retrying a worktree-deleted / repo-gone session is futile.
RESUME_RECOVERY_MAX_ATTEMPTS = 3

# Max consecutive completion nudges a single session may receive when it self-declares
# `complete` but the completion bar keeps judging it not-ready (e.g. an open-unarmed PR).
# Each nudge opens a fresh turn; without a cap, an agent that re-signals completion
# without arming its PR would loop forever. After the cap the deterministic archiver
# gives up and leaves the session for a human (the retro audit / autopilot may still act).
COMPLETE_NUDGE_CAP: int = 3

# How long autopilot defers a session after its turn-restart dance began (a model
# swap's interrupt → set_model → replay, or _reassign's cross-driver equivalent).
# The interrupt flips the session through an actionable status, which fires the
# status-change autopilot nudge tick — without the defer, autopilot could inject a
# competing prompt into the very session that is about to replay its own. Time-based
# (vs. cleared-on-turn-start) so a swap with nothing to replay never strands the
# session ungoverned; 60s comfortably covers the interrupt unwind + replay pickup.
TURN_RESTART_DEFER_S: float = 60.0

# Top-tier "needs you" and "crashed/failed" statuses that warrant an outbound push alert.
# Derived from the same vocabulary as server/sessions/status.py — not a parallel list.
PUSH_STATUSES: frozenset[str] = frozenset(
    {"waiting_input", "waiting_question", "ready_review", "needs_attention"}
)


def is_push_worthy(status: str) -> bool:
    return status in PUSH_STATUSES


def _log_bg_exception(task: asyncio.Task) -> None:
    """Log any unhandled exception from a fire-and-forget background task.

    asyncio silently discards task exceptions until GC; this surfaces them at
    ERROR level immediately so the root cause is visible without waiting for the
    ``Task exception was never retrieved`` GC-driven log line.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("background task %r raised unhandled exception", task.get_name(), exc_info=exc)


# TTL for cached issue proposals (seconds).
_ISSUE_TTL_S: float = 300.0

# Backoff after a detached-suggestion generation attempt gives up for lack of
# usable auth (no valid OAuth token and no setup-token). The result is not
# cached as an answer (see _generate_detached_suggestions), so without this a
# 5s dashboard poll (/sessions) would re-spawn generation — and its real
# acquire_valid_token -> refresh_access_token network POST plus sync_back —
# on every single poll for as long as any dashboard is open. This is a narrow
# negative-TTL, not the helper_call circuit breaker: the breaker classifies
# failures of an actual SDK query stream (429/401/5xx/crash) around
# run_helper_call, and this give-up happens *before* any such call is even
# attempted, so it doesn't fit that call shape. Same 60s window as the
# breaker's cooldown for consistency, not because they share state.
_SUGGEST_AUTH_FAIL_BACKOFF_S: float = 60.0

# Rate-limit window for _default_title_fn's give-up WARNING (see below). Same account-keyed
# monotonic-deadline shape as _SUGGEST_AUTH_FAIL_BACKOFF_S above, but scoped to the log line
# only, not to skipping the underlying acquire_valid_token/setup-token lookup on each call:
# unlike detached suggestions (re-spawned on every 5s /sessions poll), title generation is
# already bounded to once per _titling_sweep pass (every _titling_interval, default 15s), so
# there is no equivalent network-call storm to also suppress — only the WARNING volume
# (~240/hr per still-auto-named session on an unauthed account) needed rate-limiting. Same
# 60s window for consistency, not because the two dicts share state.
_TITLE_AUTH_WARN_BACKOFF_S: float = 60.0


def _prepend_proposal(proposal: str | None, base: list[str]) -> list[str]:
    """Prepend *proposal* to *base* iff truthy and not already present."""
    if not proposal:
        return base
    if proposal in base:
        return base
    return [proposal, *base]


def _rethread_identity(driver, identity: str) -> None:
    """Upgrade a live driver's identity to a real attaching user.

    A driver first built by an internal/background path (rehydrate, dispatch,
    eager re-launch) carries ``SYSTEM_IDENTITY``. AskUserQuestion/permission
    answers are registered under ``driver.identity`` and resolved against the
    answering WS identity, so if a real user attaches to a system-built driver and
    its identity isn't upgraded, every answer is rejected and the agent hangs on
    the question forever. Only upgrade FROM the system sentinel TO a real user —
    never downgrade a real identity back to system (an internal call must not clobber
    the attached user)."""
    if identity != SYSTEM_IDENTITY and driver.identity == SYSTEM_IDENTITY:
        driver.identity = identity


class ModelBlockedError(Exception):
    """A model whose approval policy is 'block' was requested at create_session.

    The launch gate refuses it outright — no row, no driver, no capacity slot.
    Carries the requested model on `.model` for the API/automation caller to report.
    """

    def __init__(self, model: str | None):
        super().__init__(f"model {model!r} is blocked by policy")
        self.model = model


class BranchSwitchError(Exception):
    """Raised when a git branch checkout fails."""


class InsufficientStorageError(Exception):
    """Raised when unarchiving cannot proceed due to inode or byte exhaustion."""


class ArchiveMissingError(Exception):
    """Raised when an archived session's directory is absent from its archive root.

    Never fabricate an empty cwd instead: doing so reports success while presenting
    the user an empty worktree, which is indistinguishable from "the work was lost"
    and hides a real misconfiguration (e.g. an archive root that moved disks)."""


# Session-window utilisation ceiling that triggers rotation (100 = hard ceiling per spec)
_ROTATION_CEILING: float = 100.0

# Consecutive poller failures before treating an account as persistently dead (e.g. Copilot
# proxy crashed at quota exhaustion).  1–2 failures are transient; ≥3 warrants rotation.
_UNREACHABLE_ROTATION_THRESHOLD: int = 3

# How often the stuck-session sweep retries rotation for needs_attention sessions.
# _pause_session() is a dead-end: the rotation monitor only fires on degradation
# events, not recovery, so paused sessions need a separate periodic retry.
_STUCK_SESSION_SWEEP_INTERVAL: float = 300.0

# Consecutive mirror-reconcile passes a FOREIGN _records entry may be missing from
# the DB before the primary evicts it (#895-adjacent). A worker deleting a session
# pops its own _records + the shared DB row, but nothing pops the primary's mirror
# copy — so autopilot proposes for the ghost and every insert fails the session_id
# FK (and a dashboard reload re-shows the deleted card). Two strikes so a session
# a just-dispatched worker is mid-creating (row not yet committed) is never dropped
# on a single-tick race.
_MIRROR_EVICT_STRIKES: int = 2

# Periodic re-check of the archived-session cap, so a limit LOWERED via settings takes
# effect even with no new archive event (the on-archive path covers the fresh-archive case).
_ARCHIVE_LIMIT_SWEEP_INTERVAL: float = 300.0

# How many consecutive permanent_delete failures for the SAME archived session before
# the cap-purge gives up on it and skips it in future sweeps (#825). Without this a
# single un-deletable directory (e.g. a stale sshfs handle, or a containment-guard
# rejection) is re-selected as "oldest" forever and disk is never reclaimed from any
# other archived session — observed on rocky as 72 identical failures in 24h.
_ARCHIVE_PURGE_MAX_STRIKES: int = 3

# Recurring-session dispatch (#603): how often the scheduler evaluates cron-
# scheduled Automation rows. Sub-minute so a cron minute fires within a tick of
# its due time; the persisted last_fired_at watermark makes the fire idempotent.
_RECURRING_TICK_INTERVAL: float = 30.0

# Opening a session must not hang. Replay work that exceeds this degrades to "no
# transcript" rather than burning the caller's 30s RPC timeout (RC2). Generous enough
# that a legitimately large transcript still renders.
#
# Scope: this ceiling bounds ONLY the replay_window() call — sub-agent sidecar
# discovery and everything else that happens inside it. resolve_transcript_path()
# runs BEFORE this wait_for and does its own stat/glob (_find_transcript) over the
# account's projects tree — on sshfs that could itself block the loop for seconds,
# and wait_for can't help (it cancels a coroutine, not synchronous work already on
# the loop). As of #826 that resolution is offloaded via asyncio.to_thread, so both
# the path lookup and the replay now run off the event loop.
#
# minimum=0.1: a 0 (or negative) override would make wait_for fire immediately and
# turn EVERY transcript into an empty one — fall back to the default instead.
_TRANSCRIPT_WINDOW_CEILING_S = env_float("CCSWITCH_TRANSCRIPT_WINDOW_CEILING_S", 10.0, minimum=0.1)

# stop() runs inside the app lifespan shutdown; every await in it is bounded so a
# single cancel-ignoring task or wedged driver close can't hang teardown (#846).
# Per-item budgets: reaping a cancelled task should be near-instant; a driver close
# legitimately takes up to ~10s (interrupt flush + subprocess terminate).
_STOP_TASK_TIMEOUT_S = 5.0
_STOP_DRIVER_CLOSE_TIMEOUT_S = 10.0

# The export path (/export.md) replays the FULL transcript with limit=None, so a
# legitimately huge session can exceed the interactive ceiling above. Exporting is a
# deliberate, user-initiated download — the user is already waiting on a file — so it
# gets its own, larger ceiling. Blowing even this is surfaced as a 504 by the caller,
# never as a successful empty document (a silent empty export is worse than a slow one).
TRANSCRIPT_EXPORT_CEILING_S = env_float("CCSWITCH_TRANSCRIPT_EXPORT_CEILING_S", 120.0, minimum=0.1)

# Ceiling on how long ensure_driver waits for an in-flight completion-driver release
# before it gives up and builds the replacement driver anyway. That wait exists to
# avoid racing the old process's session-file flush, but it runs under the
# orchestrator-wide `_ensure_lock` and `SessionDriver.aclose()` is only partly
# bounded (the SDK client's __aexit__ and the account sync-back are not), so an
# unbounded wait would let one stuck teardown wedge every attach in the process.
# minimum=0.1: a 0/negative override would fire the timeout instantly and defeat the
# race protection entirely — fall back to the default instead.
DRIVER_RELEASE_WAIT_S = env_float("CCSWITCH_DRIVER_RELEASE_WAIT_S", 20.0, minimum=0.1)

# ---------------------------------------------------------------------------
# Daemon identity + host-partitioned session ownership (#852)
# ---------------------------------------------------------------------------

# How stale the primary lease's heartbeat may be before another primary daemon may
# take it over (fenced). Renewal cadence is TTL/5. Same-host re-acquisition ignores
# staleness entirely (systemd guarantees one daemon per host), so a crash restart
# of the real primary is never delayed by this.
PRIMARY_LEASE_TTL_S = env_float("CCSWITCH_PRIMARY_LEASE_TTL_S", 300.0, minimum=10.0)

# TTL for the cached enabled-hosts registry snapshot that backs session-ownership
# decisions. Ownership only changes when an operator edits the fleet registry, so
# a short cache is plenty — this bounds DB reads from the event-driven rotation
# monitor, not correctness.
_OWNERSHIP_HOSTS_TTL_S = 60.0


def daemon_role() -> str:
    """Return this daemon's role from ``CCSWITCH_DAEMON_ROLE``.

    ``primary`` (the default) runs every background loop and owns fleet-global
    automation; ``worker`` serves dispatched sessions only (#840). Any other
    value raises: silently falling back to ``primary`` would double-drive the
    fleet on a typo, and silently becoming ``worker`` would quietly stop every
    automation — both are worse than a loud failed boot.
    """
    role = os.environ.get("CCSWITCH_DAEMON_ROLE", "primary").strip().lower()
    if role not in ("primary", "worker"):
        raise ValueError(
            f"CCSWITCH_DAEMON_ROLE={role!r} — expected 'primary' or 'worker'"
        )
    return role


def self_host_name() -> str:
    """This daemon's fleet identity — the ``hosts.name`` the dispatcher stamps
    onto sessions it sends here (``Session.host``).

    ``CCSWITCH_HOST_NAME`` wins when set (operator-managed registries can use any
    name). The fallback is the kernel hostname's first label, which matches how
    auto-registered hosts are named (``_derive_host_name`` takes the first label
    of the tailnet MagicDNS endpoint, and MagicDNS names ARE machine hostnames).
    A worker whose identity doesn't match its registry row rehydrates zero
    sessions after a restart — set the env var explicitly on renamed hosts.
    """
    name = os.environ.get("CCSWITCH_HOST_NAME", "").strip()
    if name:
        return name
    import socket
    return socket.gethostname().split(".")[0]


def local_host_stamp() -> str | None:
    """``Session.host`` for a session THIS daemon creates in-process.

    The dispatcher stamps sessions it routes to a host, but a daemon's own spawns
    (automation loops, a direct create on the primary) never pass through it. On a
    WORKER those landed as host=NULL — which reads as "the primary's" everywhere
    (``_owns_record``, a worker's ``rehydrate`` filter): the primary's rotation and
    stuck-session loops acted on them, the worker dropped them on its next restart,
    and the primary's ``_records`` (the fleet dashboard) never listed them. A worker
    therefore stamps itself at creation. The primary keeps NULL: that IS its
    ownership marker, so single-host installs never see a host value.
    """
    return self_host_name() if daemon_role() == "worker" else None


class SessionOrchestrator:
    """Orchestrates session lifecycle: creation, rotation, termination.

    Args:
        sdk_factory: ``(cwd, config_dir, can_use_tool, mode, resume) -> SDKClientProtocol``
            builds the SDK client backing each SessionDriver.  Defaults to
            ``real_sdk_factory`` (a real ClaudeSDKClient); override in tests with a
            fake-SDK factory.
        work_dir: Default cwd for new sessions.  Defaults to the process cwd.
        poll_interval: Seconds between usage-poller sweeps (default 60).
        usage_fn: Override for the usage-fetch function (testing).
    """

    # Optional listener fired whenever a session's live driver is SWAPPED for a new
    # SessionDriver by a recovery path (see _install_driver / _reassign). The daemon RPC
    # layer registers it to restart the per-session event pump; a recovery swap carries
    # no RPC, so without this the pump keeps forwarding the dead driver and every
    # attached viewer's transcript freezes. None in edge/in-process mode (no daemon pump).
    on_driver_replaced = None   # Callable[[str, SessionDriver], None] | None

    def __init__(
        self,
        sdk_factory=None,
        suggestion_factory=None,
        work_dir: str | None = None,
        poll_interval: float = 60.0,
        usage_fn: Callable[[str], object] | None = None,
        repo_root: str | None = None,
        titling_interval: float = 15.0,
        x11=None,
        copilot=None,
        copilot_auth=None,
        title_fn: Callable | None = None,
        summary_factory=None,
    ) -> None:
        # work_dir param is accepted for back-compat (used in tests to keep
        # the orchestrator anchored to the repo root), but is no longer stored
        # as an instance attribute since sessions now always resolve their own cwd.
        _ = work_dir
        # Root under which per-session worktrees are created (<root>/.ccswitch/...).
        # Defaults to the configured repo_root so worktrees sit next to the repos.
        if repo_root is None:
            try:
                from server.config import get_settings
                repo_root = get_settings().repo_root
            except Exception:
                repo_root = os.path.expanduser("~/GitHub")
        self._repo_root: str = repo_root
        # session_id → WorktreeInfo for sessions launched in an isolated worktree
        self._worktrees: dict[str, worktrees.WorktreeInfo] = {}
        # automation_id → last-known "blocked by schedule" state, so the schedule
        # gate emits one activity event per open→closed transition (#603).
        self._schedule_blocked: dict[str, bool] = {}
        self._sdk_factory = sdk_factory or real_sdk_factory
        self._x11 = x11  # X11Manager | None — per-session virtual displays
        self._copilot = copilot  # CopilotProxyManager | None — per-account translating proxy
        self._copilot_auth = copilot_auth  # CopilotAuthManager | None — device-flow auth manager
        if os.environ.get("CCSWITCH_SUGGESTIONS", "1") == "0":
            self._suggester = None
        else:
            self._suggester = SuggestionService(suggestion_factory or real_suggestion_factory)
        if os.environ.get("CCSWITCH_SUMMARIES", "1") == "0":
            self._summarizer = None
        else:
            self._summarizer = SummaryService(summary_factory or real_summary_factory)
        # Cadence (seconds) of the background titling sweep that adopts Claude's
        # own transcript title into still-auto-named sessions. Constructor arg so
        # tests can drive it fast.
        self._titling_interval: float = titling_interval
        # Fallback title generator: Claude only writes an ``ai-title`` record in
        # the interactive CLI, so SDK-driven sessions get none. When the transcript
        # has no title, summarize the user's own prompts with a one-shot Haiku query.
        # Injectable so tests can stub it without a live SDK call.
        self._title_fn: Callable = title_fn or self._default_title_fn
        self._registry = PermissionRegistry()
        self._drivers: dict[str, SessionDriver] = {}
        # In-flight completion-driver-release tasks (see _persist_status), keyed by
        # session_id. The driver is already out of `_drivers` by the time an entry
        # lands here — a fast reopen (ensure_driver) must await this before building
        # a replacement driver against the same claude_session_id/cwd, otherwise the
        # old process can still be flushing its session file while the new one reads
        # it (the SDK's own subprocess_cli.py warns this can lose the last assistant
        # message). Cleared on completion, success or failure, so a stuck release can
        # never make a session permanently unopenable.
        self._releasing: dict[str, asyncio.Task] = {}
        # Sessions that failed to re-launch after a daemon-affecting deploy:
        # session_id -> attempts so far. Seeded by restore_deploy, drained by the
        # daemon's recovery pass (_recover_resume_failures_once). Deploy-scoped and
        # in-memory by design — a fresh deploy rebuilds it; a crash loses it exactly
        # as the deploy controller loses its own frozen 'failed' list.
        self._resume_recovery: dict[str, int] = {}
        # session_id -> consecutive mirror-reconcile passes its DB row was missing.
        # Drives _reconcile_mirror_once's two-strike eviction of ghosts left behind
        # when a session is deleted on another fleet host (#895-adjacent).
        self._mirror_missing_strikes: dict[str, int] = {}
        # Startup health report (2026-08-29): a failed rehydrate() used to vanish into
        # a log line while the daemon looked healthy with zero sessions. Boot steps
        # record their outcome here; /api/daemon/health exposes it (RPC-bridged in
        # remote mode). In-memory by design — it describes THIS process's boot.
        from datetime import datetime as _dt, timezone as _tz
        self._startup_report: dict = {
            "startedAt": _dt.now(_tz.utc).isoformat(),
            "rehydrated": None, "rehydrateError": None,
            "rearmed": None, "rearmError": None,
        }
        # Detached-session next-prompt suggestions: session_id -> (transcript mtime, items).
        # Generated from the on-disk transcript without resuming the driver (see
        # latest_suggestions / _generate_detached_suggestions). Cached even when empty so
        # the same transcript state is not re-generated on every /sessions poll.
        self._detached_suggestions: dict[str, tuple[float, list[str]]] = {}
        self._suggest_inflight: set[str] = set()
        # Backoff after a give-up-for-no-auth attempt: account -> monotonic deadline before
        # which a re-spawn is skipped. Keyed by account (not session_id): "this account has
        # no usable credential" is a per-account fact, so all sessions sharing an account
        # share one cooldown instead of each hammering acquire_valid_token independently.
        # See _SUGGEST_AUTH_FAIL_BACKOFF_S.
        self._suggest_auth_failed_until: dict[str, float] = {}
        # Backoff for _default_title_fn's give-up WARNING: account -> monotonic deadline
        # before which the log line is suppressed (the underlying lookup still runs every
        # call — only the warning is rate-limited). See _TITLE_AUTH_WARN_BACKOFF_S.
        self._title_auth_failed_until: dict[str, float] = {}
        # Session status summaries: session_id -> (transcript mtime, generated_at_monotonic, text).
        # Generated lazily from the transcript, only while a viewer is present and at most
        # once / 60s (see latest_summary / should_regenerate). Ephemeral — never persisted.
        self._summaries: dict[str, tuple[float, float, str]] = {}
        self._summary_inflight: set[str] = set()
        # Issue-proposal cache: repo → (monotonic_ts, proposal_or_None).
        # Populated lazily by _refresh_issue_proposal; TTL = _ISSUE_TTL_S.
        self._issue_proposals: dict[str, tuple[float, str | None]] = {}
        self._issue_inflight: set[str] = set()
        # Collision-avoidance (US-8): top issue number per repo, and which live session
        # currently "holds" each issue, so two idle/done sessions in the same repo are
        # never pointed at the same backlog issue. Claims are refreshed each
        # _dispatch_proposal call and pruned to current idle/done sessions.
        self._issue_numbers: dict[str, int | None] = {}
        self._proposed_issue: dict[str, int] = {}
        # Review-comment cache: (repo, pr_number) → (monotonic_ts, proposal_or_None).
        # Populated lazily by _refresh_review_proposal; TTL = _ISSUE_TTL_S.
        self._review_proposals: dict[tuple[str, int], tuple[float, str | None]] = {}
        self._review_inflight: set[tuple[str, int]] = set()
        self._last_prompt: dict[str, str] = {}
        # session_id → clock() when its turn-restart dance (model swap / reassign)
        # began; autopilot skips the session for TURN_RESTART_DEFER_S from that mark.
        self._turn_restart_at: dict[str, float] = {}
        # Failure-driven recovery: quarantine + per-session rotation cap. Clock is
        # injectable for deterministic tests; production uses wall-clock time. The
        # recompose-attempt map distinguishes a first 401 (heal in place) from a
        # repeat 401 (creds genuinely bad → rotate away).
        self._clock: Callable[[], float] = time.time
        self._recovery = RecoveryState()
        self._recompose_attempts: dict[str, float] = {}  # session_id → last recompose ts
        self._recompose_ttl: float = 60.0
        # initial_prompt deferred until first WS attach so the client is watching
        # when the turn runs — avoids losing events before any consumer exists.
        self._pending_initial_prompt: dict[str, str] = {}
        # Deferred attachment ids for the initial prompt, sent on first WS attach.
        self._pending_initial_attachment_ids: dict[str, list[str]] = {}
        # Cap-purge poison tracking (#825): consecutive permanent_delete failures per
        # session, and the set that has crossed _ARCHIVE_PURGE_MAX_STRIKES and is now
        # skipped during selection so one un-deletable archive can't wedge the whole
        # cap. In-memory by design — a daemon restart re-tries a session that may since
        # have become deletable (e.g. the sshfs mount recovered).
        self._archive_purge_strikes: dict[str, int] = {}
        self._archive_purge_poisoned: set[str] = set()
        # Cached enabled-hosts registry snapshot for _owns_record (#852):
        # (monotonic_ts, names) — None until the first read. See _enabled_host_names.
        self._ownership_hosts_cache: tuple[float, frozenset[str]] | None = None

        # Build a live-session-aware usage_fn: when an account has a live session
        # the SDK owns its token family, so the poller must not independently
        # refresh.  Pass allow_refresh=False for those accounts so the poller
        # calls sync_back first and returns None on an expired-but-owned token
        # (the SDK will refresh on its next request).
        #
        # A WORKER daemon never refreshes at all (#852): the shared refresh-token
        # files admit exactly one refresher (the primary's poller / token-refresh
        # monitor — two race the token family), but the rotation monitor and
        # _account_is_dead/_account_limited read THIS process's poller cache, so
        # a worker with no poll signal loses proactive ceiling rotation and its
        # crash recovery can't tell a dead account from a healthy one. Read-only
        # polling restores that signal without the refresh race.
        if usage_fn is not None:
            _poller_usage_fn = usage_fn
        else:
            def _poller_usage_fn(name: str):
                return ccswitch_accounts.usage_for(
                    name,
                    allow_refresh=(daemon_role() != "worker"
                                   and not self.has_live_session(name)),
                    path="poller-idle",
                )

        self._poll_interval = poll_interval
        self._poller: UsagePoller = UsagePoller(
            poll_interval=poll_interval,
            usage_fn=_poller_usage_fn,
        )
        from server.github.pr_poller import PrStatusPoller
        from server.github.api_budget import PriorityGate
        # One gate shared by both GitHub pollers so the best-effort ETA fetch in
        # pr_population yields to the user-facing PR-status refresh under a
        # shared rate-limit budget (issue #671).
        self._api_gate = PriorityGate()
        self._pr_poller: PrStatusPoller = PrStatusPoller(api_gate=self._api_gate)
        self._pr_poller.set_pairs_provider(self._pr_pairs)
        self._pr_poller.set_on_change(self._on_pr_change)
        # Per-poll-cycle SWEEP: after every refresh the sweep re-classifies the
        # whole fresh cache so non-CI failure modes (out_of_date/merge_conflict)
        # that never trip a checks-transition still reach _pr_doctor_step.
        self._pr_poller.set_on_cycle(self._pr_doctor_sweep)
        from server.github.pr_population import PrPopulationPoller, _default_merge_queue_query
        from server.pipeline import merge_queue_store

        async def _merge_queue_sink(by_repo: dict, captured_at: int) -> None:
            for repo, entries in by_repo.items():
                await merge_queue_store.record(repo, captured_at, entries)
            await merge_queue_store.prune(older_than_epoch=captured_at - 36 * 3600)

        # _default_merge_queue_query groups watched repos by owner so each org's repos
        # use that org's installation token (works across multiple GitHub owners).
        self._pr_population: PrPopulationPoller = PrPopulationPoller(
            merge_queue_query_fn=_default_merge_queue_query,
            merge_queue_sink=_merge_queue_sink,
            api_gate=self._api_gate,
        )
        self._pr_population.set_repos_provider(self._pr_repos)
        from server.pipeline.deploy_watch import DeployWatcher

        self._deploy_watcher: DeployWatcher = DeployWatcher()
        from server.automations.action_providers import (
            PrDoctorActionProvider, BacklogRunnerActionProvider,
            CicdDoctorActionProvider,
        )
        from server.automations.base import ActionProvider
        self._action_providers: dict[str, ActionProvider] = {
            "pr-doctor": PrDoctorActionProvider(self),
            "backlog-runner": BacklogRunnerActionProvider(self),
            "cicd-doctor": CicdDoctorActionProvider(self),
        }
        # In-memory map: session_id → SessionRecord
        self._records: dict[str, SessionRecord] = {}
        # session_id → full spawn kwargs for a model-gated session parked at
        # 'pending_approval' (see _hold_for_approval). The approve path (Task 10)
        # spawns from these. In-memory only; a restart rebuilds the row-backed view
        # via rehydrate() (attachments excluded — rebuilt from the row if lost).
        self._pending_approvals: dict[str, dict] = {}
        # Sessions whose cwd has vanished — warned once so the titling sweep doesn't
        # spam "Working directory does not exist" every interval (see _generate_fallback_title).
        self._missing_cwd_warned: set[str] = set()
        # Sessions whose background tasks have been re-armed this process (dedup the
        # auto pass against retry-on-attach). Cleared by a fresh process start.
        self._rearmed: set[str] = set()
        # Session-ids currently in a push-worthy state (AUT-02 debounce): push fires once
        # on entry; cleared when the session transitions out so re-entry fires again.
        self._pushed: set[str] = set()
        # Session-ids (any origin) we've scheduled a completion auto-archive for,
        # so repeated ``complete`` status emits schedule the archive exactly once.
        self._auto_archiving: set[str] = set()
        # Per-session count of consecutive not-ready completion nudges (bounded by
        # COMPLETE_NUDGE_CAP); cleared when the session finally archives.
        self._complete_nudge_counts: dict[str, int] = {}
        # (repo, branch) pairs whose PR is currently in a failing-CI state (M2b.2 debounce):
        # push fires once on entry into failing; cleared on recovery so a re-break re-fires.
        self._pushed_pr: set[tuple[str, str]] = set()
        # Auto-fix loop guard + activity feed: the pr-doctor automation's outward
        # seam. Per-target ``attempted`` state lives in automation_target_state
        # (durable; survives restart), keyed by ``f"{repo}#{number}"`` — no parallel
        # in-memory set. See _on_pr_change.
        from server.automations.context import AutomationContext
        self._pr_doctor_ctx = AutomationContext(
            "pr-doctor", create_session=self.create_session)
        # Consolidations whose clone/model/spawn tail is still running in a
        # background task (no DB row yet, so spawn_session's DB de-dup can't see
        # them): the consolidate fast path answers `duplicate` for these. Task
        # refs are held so in-flight tasks aren't GC'd.
        self._consolidation_inflight: set[str] = set()
        self._consolidation_tasks: set[asyncio.Task] = set()
        # Async manual pr-doctor triggers (issue #668): the slow clone→assess→
        # spawn pipeline runs in a background task that advances a job here; the
        # trigger RPC returns the job id immediately and the UI polls its phase.
        from server.automations.pr_doctor_jobs import JobStore
        self._pr_doctor_jobs = JobStore()
        self._pr_doctor_job_tasks: set[asyncio.Task] = set()
        # Injectable seam for the pr-doctor Mechanical (update-branch) executor:
        # a swappable ``gh`` runner so tests can record argv without touching the
        # network. Defaults to the real ``gh`` shell-out.
        self._gh_run = gitrepos._run_gh
        # Backlog Runner control loop. Every I/O seam is now the real Plan-B
        # wiring: select_batch (label-gated gh query → LLM rank → batch), claim /
        # unclaim (in-progress label lifecycle), spawn (clone-resolve + de-duped
        # session), plus in-flight/account/PR-status/config/session-ended probes.
        from server.automations.backlog_runner import BacklogRunnerLoop
        self._backlog_ctx = AutomationContext(
            "backlog-runner", create_session=self.create_session)
        self._backlog_loop = BacklogRunnerLoop(
            self._backlog_ctx,
            select_batch=self._backlog_select_batch,
            claim=self._backlog_claim,
            spawn=self._backlog_spawn,
            count_in_flight=self._backlog_count_in_flight,
            count_candidates=self._backlog_count_candidates,
            healthy_account=self._backlog_healthy_account,
            pr_status_for=self._backlog_pr_status_for,
            pr_doctor_active=self._backlog_pr_doctor_active,
            read_config=self._backlog_read_config,
            clock=time.monotonic,
            release=self._backlog_release,
            needs_human=self._backlog_needs_human,
            session_ended=self._backlog_session_ended,
            reconcile=self._backlog_reconcile,
            close_out=self._backlog_close_out,
            heal_orphans=self._backlog_heal_orphans,
            refresh_groups=self._backlog_refresh_groups,
            credential_alerts=self._backlog_credential_alerts,
        )
        self._backlog_task: asyncio.Task | None = None
        # SDLC Intake control loop: turns pending triggers into proposed intent.md
        # PRs (propose-only). Same injected-seam mold as BacklogRunnerLoop; the
        # trigger source (read_triggers) stays empty until a triage queue (#804) or
        # the /api/sdlc intake path (#802) feeds it, so today each tick is a cheap
        # no-op sweep.
        from server.automations.sdlc_intake import SdlcIntakeLoop
        self._sdlc_intake_ctx = AutomationContext(
            "sdlc-intake", create_session=self.create_session)
        self._sdlc_intake_loop = SdlcIntakeLoop(
            self._sdlc_intake_ctx,
            read_config=self._sdlc_intake_read_config,
            read_triggers=self._sdlc_intake_read_triggers,
            ensure_initiative=self._sdlc_intake_ensure_initiative,
            record_intent=self._sdlc_intake_record_intent,
            spawn=self._sdlc_intake_spawn,
            clock=time.monotonic,
        )
        self._sdlc_intake_task: asyncio.Task | None = None
        # PM Sync control loop (design 2026-08-31 §4): mirrors onboarded repos'
        # issues/PRs into work_items and refreshes the feature index. Same
        # injected-seam mold as the SDLC loops; GitHub I/O rides the gh seam.
        from server.automations.pm_sync import PmSyncLoop
        self._pm_sync_ctx = AutomationContext("pm-sync")
        self._pm_sync_loop = PmSyncLoop(
            self._pm_sync_ctx,
            read_config=self._pm_sync_read_config,
            list_repos=self._pm_sync_list_repos,
            fetch=self._pm_sync_fetch,
            upsert_items=self._pm_sync_upsert_items,
            save_sync_state=self._pm_sync_save_state,
            read_feature_docs=self._pm_sync_read_feature_docs,
            list_feature_rows=self._pm_sync_list_feature_rows,
            apply_feature_plan=self._pm_sync_apply_feature_plan,
            now_utc=self._pm_sync_now_utc,
        )
        self._pm_sync_task: asyncio.Task | None = None
        # PM archaeology onboarding (design 2026-08-31 §5): the map-synthesis and
        # classification sessions ride the SAME spawn seam as the SDLC loops.
        # No polling loop in phase 2 — the /propose-map and /classify API routes
        # invoke these seams directly (phase 3 adds the pm-triage cadence).
        self._pm_onboard_ctx = AutomationContext(
            "pm-onboard", create_session=self.create_session)
        self._pm_classify_ctx = AutomationContext(
            "pm-classify", create_session=self.create_session)
        # SDLC Synthesizer control loop (#800): carries initiatives intent→spec→
        # plan by proposing each stage's artifact PR and enriching artifact index
        # rows with PR metadata (number/url, then merge sha). Same injected-seam
        # mold as the intake loop; every seam is a thin adapter over db_session()
        # + the shared backlog helpers (healthy-account, ensure_clone, PR-poller).
        from server.automations.sdlc_synthesizer import SdlcSynthesizerLoop
        self._sdlc_synth_ctx = AutomationContext(
            "sdlc-synthesizer", create_session=self.create_session)
        self._sdlc_synthesizer_loop = SdlcSynthesizerLoop(
            self._sdlc_synth_ctx,
            list_initiatives=self._sdlc_list_initiatives,
            list_artifacts=self._sdlc_list_artifacts,
            record_artifact=self._sdlc_record_artifact,
            set_status=self._sdlc_set_status,
            spawn=self._sdlc_spawn_synth,
            pr_lookup=self._sdlc_pr_lookup,
            update_artifact=self._sdlc_update_artifact,
            read_config=self._sdlc_synth_read_config,
            clock=time.monotonic,
        )
        self._sdlc_synth_task: asyncio.Task | None = None
        self._recurring_task: asyncio.Task | None = None
        # Autopilot control loop: advances every governed session through its stuck
        # states each tick (also nudged from _persist_status). All seams injected so
        # the loop stays testable; the seam methods live on this orchestrator.
        from server.automations.autopilot import AutopilotLoop
        self._autopilot_ctx = AutomationContext("autopilot")
        self._autopilot_loop = AutopilotLoop(
            self._autopilot_ctx,
            read_config=self._autopilot_read_config,
            list_governed=self._autopilot_list_governed,
            get_context=self._autopilot_get_context,
            apply=self._autopilot_apply,
            decide_fn=self._autopilot_decide,
            clock=time.monotonic,
            archive=self._autopilot_archive,
            compose=self._autopilot_compose,
            refresh_facts=self._autopilot_refresh_pr_facts,
        )
        self._autopilot_task: asyncio.Task | None = None
        # Per-tick fleet snapshot cache: (built_at, (active, archived)). One snapshot
        # serves every governed session's get_context within the TTL, so an N-session
        # tick runs the archived-session query once, not N times.
        self._fleet_snapshot_cache: tuple[float, tuple[list, list]] | None = None
        # CI/CD Doctor control loop: system-level CI health (flaky tests, check
        # graduation, waste). Real seams shell out to gh via asyncio.to_thread.
        from server.automations.cicd_doctor import CicdDoctorLoop
        from server.github import actions_api, ci_issues
        self._cicd_ctx = AutomationContext("cicd-doctor")
        self._cicd_loop = CicdDoctorLoop(
            self._cicd_ctx,
            read_config=self._cicd_read_config,
            list_repos=self._cicd_list_repos,
            fetch_runs=lambda repo, since: asyncio.to_thread(
                actions_api.list_completed_runs, repo, since),
            fetch_jobs=lambda repo, rid: asyncio.to_thread(
                actions_api.list_jobs, repo, rid),
            fetch_junit=lambda repo, rid: asyncio.to_thread(
                actions_api.junit_artifacts, repo, rid),
            fetch_repo_meta=lambda repo: asyncio.to_thread(
                actions_api.repo_ci_meta, repo),
            fetch_workflows=lambda repo, branch: asyncio.to_thread(
                actions_api.workflow_texts, repo, branch),
            file_issue=lambda repo, title, body: asyncio.to_thread(
                ci_issues.create_issue, repo, title, body),
            close_issue=lambda repo, n: asyncio.to_thread(
                ci_issues.close_issue, repo, n),
            reopen_issue=lambda repo, n: asyncio.to_thread(
                ci_issues.reopen_issue, repo, n),
            clock=time.time,  # wall clock: created_ts from GitHub is epoch time
        )
        self._cicd_task: asyncio.Task | None = None
        # Archive Audit control loop: periodic re-audit of recently-archived sessions.
        from server.automations.archive_audit import ArchiveAuditLoop, _real_gh, _real_gh_num
        from server.automations.archive_audit import list_recent_archived_rows
        self._archaudit_ctx = AutomationContext("archive-audit")
        self._archaudit_loop = ArchiveAuditLoop(
            self._archaudit_ctx,
            read_config=self._archaudit_read_config,
            list_rows=lambda since: list_recent_archived_rows(db_session, since),
            gh_fn=_real_gh,
            gh_num_fn=_real_gh_num,
            unarchive_fn=lambda sid: self.unarchive(sid),  # type: ignore[arg-type,return-value]
            clock=time.time,
        )
        self._archaudit_task: asyncio.Task | None = None
        # Fire-and-forget task registry (issue #405). Every detached ``create_task``
        # (persist, rotation, recovery, summary/proposal refresh, backlog nudge …) is
        # routed through ``_spawn`` so ``stop()`` can cancel+await ALL of them. An
        # untracked task that outlives its owning app/test keeps a live reference to
        # ``db_session`` — which resolves the module-global ``_session_factory`` at
        # call time — so a late fire writes into whatever DB is *currently* installed,
        # i.e. the NEXT test's, corrupting it (the CI-only batch-test flake).
        self._bg_tasks: set[asyncio.Task] = set()
        # Background tasks
        self._rotation_task: asyncio.Task | None = None
        self._titling_task: asyncio.Task | None = None
        # Inferred-goal sweep: per-session transcript-turn marker so a session whose
        # transcript hasn't advanced is never re-billed for inference (cost gate).
        self._goal_marker: dict[str, int] = {}
        self._goal_task: asyncio.Task | None = None
        self._stall_task: asyncio.Task | None = None
        self._token_refresh_task: asyncio.Task | None = None
        self._sweep_task: asyncio.Task | None = None
        self._archive_limit_task: asyncio.Task | None = None
        self._janitor_task: asyncio.Task | None = None
        self._pr_branch_task: asyncio.Task | None = None
        # Primary-lease state (#852): the heartbeat task and the fence acquired at
        # start(). None on workers and on an orchestrator that never start()ed.
        self._lease_task: asyncio.Task | None = None
        self._lease_fence: int | None = None
        # Event bus — WS bridge subscribes here for lifecycle events
        self._event_subscribers: list[asyncio.Queue] = []
        # Serialises the check-build-register critical section in ensure_driver
        # so that two concurrent WS attaches to the same detached session cannot
        # each see no driver, each build+start one, and both register → two SDK
        # processes attached to the same Claude conversation (TOCTOU fix).
        self._ensure_lock: asyncio.Lock = asyncio.Lock()
        # Serialises concurrent drain calls so two teardown paths (e.g. close + archive
        # racing) cannot each pop the same queue head and double-start one session.
        self._queue_drain_lock: asyncio.Lock = asyncio.Lock()
        # Graceful-drain state (deploy handshake). In-memory only; a restart clears it.
        self._draining: bool = False
        self._drain_started_at: float = 0.0
        self._drain_timer: asyncio.Task | None = None
        # Set at stop(): the daemon is tearing down (deploy or watchdog restart). While
        # true, a turn dropped by our own driver teardown must NOT run failure recovery
        # or escalate to needs_attention — rehydrate() on the next boot normalizes the
        # row's live status to idle (resumable). Without this, a watchdog restart mid-turn
        # left every in-flight session stranded as needs_attention (incident 2026-07-02).
        self._shutting_down: bool = False

    def _oat_env(self, session_id: str) -> dict:
        """Return auth env for the session's account.

        For copilot accounts, returns the proxy env ({ANTHROPIC_BASE_URL, …}) from
        the CopilotProxyManager.  For standard accounts, returns the oauth-token env.
        """
        rec = self._records.get(session_id)
        if rec is None:
            return {}
        if self._copilot is not None and _provider_of(rec.account) == _copilot_mod.PROVIDER:
            return self._copilot.env_for(rec.account)
        return oauth_env_for(rec.account)

    def _session_factory(self, session_id: str):
        """Return a factory closure injecting this session's DISPLAY and PreCompact hook.

        Forwards ``on_pre_compact`` to the base factory so the real SDK factory can
        register the hook; fake factories accept and ignore it.
        """
        base = self._sdk_factory
        x11 = self._x11
        def _factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
            extra = {**(x11.env_for(session_id) if x11 else {}), **self._oat_env(session_id)}
            if extra:
                return base(cwd, config_dir, can_use_tool, mode, resume,
                            extra_env=extra, on_pre_compact=on_pre_compact)
            return base(cwd, config_dir, can_use_tool, mode, resume, on_pre_compact=on_pre_compact)
        return _factory

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _spawn(self, coro, *, name: str | None = None) -> asyncio.Task:
        """Schedule a fire-and-forget coroutine and track it for teardown (#405).

        Use this for every detached task the orchestrator owns instead of a bare
        ``asyncio.create_task`` whose handle is discarded. Tracked tasks are
        cancelled+awaited by ``stop()``, so none can survive its owning app/test to
        write into a later test's DB. The done-callback discards the handle so a
        completed task is not retained.
        """
        task = asyncio.create_task(coro, name=name)
        # Lazy-init tolerates a partially-constructed orchestrator (some tests build
        # one via ``__new__`` without running ``__init__``). Bind the callback to this
        # exact set so a completed task removes itself even if the attribute is later
        # rebound.
        tasks = getattr(self, "_bg_tasks", None)
        if tasks is None:
            tasks = self._bg_tasks = set()
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        task.add_done_callback(_log_bg_exception)
        return task

    async def start(self) -> None:
        """Start the usage poller, the rotation monitor and the titling sweep.

        A PRIMARY daemon must first win the DB-side primary lease (#852): the role
        is per-host env config, so nothing else stops two hosts both booting as
        primary and double-driving every fleet-global automation. Failure raises —
        a loud failed boot, deliberately unguarded in the daemon lifespan, beats a
        silent second primary. Workers skip the lease entirely.
        """
        if daemon_role() == "primary":
            me = self_host_name()
            fence = await primary_lease.acquire(me, ttl_s=PRIMARY_LEASE_TTL_S)
            if fence is None:
                state = await primary_lease.current()
                held = (f"held by {state.holder!r}, renewed {state.age_s:.0f}s ago"
                        if state else "held")
                raise RuntimeError(
                    f"primary lease {held} — another primary daemon is alive. "
                    f"Exactly one daemon in the fleet may run as primary: set "
                    f"CCSWITCH_DAEMON_ROLE=worker on this host ({me!r}) or stop "
                    f"the other daemon first."
                )
            self._lease_fence = fence
            self._lease_task = asyncio.create_task(
                self._primary_lease_heartbeat(), name="primary-lease-heartbeat"
            )
        await self._poller.start()
        await self._pr_poller.start()
        await self._pr_population.start()
        await self._deploy_watcher.start()
        self._pr_branch_task = asyncio.create_task(
            self._pr_branch_monitor(), name="pr-branch-monitor"
        )
        self._rotation_task = asyncio.create_task(
            self._rotation_monitor(), name="rotation-monitor"
        )
        self._titling_task = asyncio.create_task(
            self._titling_monitor(), name="titling-monitor"
        )
        self._goal_task = asyncio.create_task(
            self._goal_monitor(), name="goal-inference-monitor"
        )
        self._stall_task = asyncio.create_task(self._stall_loop(), name="stall-watchdog")
        self._token_refresh_task = asyncio.create_task(
            self._token_refresh_monitor(), name="token-refresh-monitor"
        )
        # Backlog Runner control loop: one tick per interval (refill + handoff/
        # terminal-watch sweep). A disabled row makes each tick a cheap sweep.
        self._backlog_task = asyncio.create_task(
            self._backlog_tick_loop(interval=60.0), name="backlog-runner-loop"
        )
        # SDLC Intake control loop: one tick per interval (propose intent for each
        # pending trigger). A disabled/absent row makes each tick a cheap no-op.
        self._sdlc_intake_task = asyncio.create_task(
            self._sdlc_intake_tick_loop(interval=60.0), name="sdlc-intake-loop"
        )
        # PM Sync control loop: one tick per interval (backfill or incremental pass
        # per onboarded repo + feature index refresh). Off by default; DB row enables.
        self._pm_sync_task = asyncio.create_task(
            self._pm_sync_tick_loop(interval=60.0), name="pm-sync-loop"
        )
        # SDLC Synthesizer control loop: one tick per interval (propose spec/plan
        # artifacts + enrich PR metadata). A disabled row makes each tick cheap.
        self._sdlc_synth_task = asyncio.create_task(
            self._sdlc_synth_tick_loop(interval=60.0), name="sdlc-synthesizer-loop"
        )
        # Autopilot control loop: one tick per interval plus status-change nudges.
        self._autopilot_task = asyncio.create_task(
            self._autopilot_tick_loop(interval=20.0), name="autopilot-loop"
        )
        # CI/CD Doctor: slow cadence — CI history moves in minutes, not seconds,
        # and each tick costs several REST calls per repo.
        self._cicd_task = asyncio.create_task(
            self._cicd_tick_loop(interval=900.0), name="cicd-doctor-loop"
        )
        # Archive Audit: re-audit recently-archived sessions every 6 hours.
        self._archaudit_task = asyncio.create_task(
            self._archaudit_tick_loop(interval=21600.0), name="archive-audit-loop"
        )
        # Stuck-session sweep: retry rotation for needs_attention sessions every 5 min.
        # The rotation monitor is event-driven (fires on degradation); this covers
        # the recovery side — sessions paused due to all_accounts_busy get a chance
        # to move once a healthy account frees up.
        self._sweep_task = asyncio.create_task(
            self._stuck_session_sweep(), name="stuck-session-sweep"
        )
        # Archived-session cap sweep: re-enforce max_archived_sessions every 5 min so a
        # lowered limit takes effect without waiting for the next archive event.
        self._archive_limit_task = asyncio.create_task(
            self._archive_limit_loop(), name="archive-limit-sweep"
        )
        # Orphan-worktree janitor: fires immediately on startup then every 24 h.
        # Reclaims worktree dirs left behind by spawn crashes, failed archive moves,
        # or SIGKILL mid-teardown — the three orphan sources from issue #591.
        self._janitor_task = asyncio.create_task(
            self._worktree_janitor_loop(interval=86400.0), name="worktree-janitor-loop"
        )
        # Recurring-session scheduler: fire cron-scheduled Automation rows as their
        # due time arrives (#603 dispatch). Alongside the loops above; the watermark
        # column makes each firing idempotent across restarts.
        self._recurring_task = asyncio.create_task(
            self._recurring_tick_loop(interval=_RECURRING_TICK_INTERVAL),
            name="recurring-session-scheduler",
        )

    async def _primary_lease_heartbeat(self) -> None:
        """Renew the primary lease every TTL/5; scream on a fenced takeover (#852).

        A failed renew means another daemon took the lease — i.e. TWO primaries
        are (or were) configured. This loop does not self-demote (stopping live
        automation loops mid-flight has its own failure modes); it makes the
        split-brain impossible to miss: a CRITICAL log every beat until an
        operator fixes the roles. A transient DB error is retried silently — the
        fence means a delayed renew can never resurrect a lease someone took.
        """
        interval = max(PRIMARY_LEASE_TTL_S / 5.0, 2.0)
        while True:
            await asyncio.sleep(interval)
            fence = self._lease_fence
            if fence is None:  # defensive: heartbeat only starts after acquire
                continue
            try:
                ok = await primary_lease.renew(self_host_name(), fence)
            except Exception:
                log.warning("primary-lease: renew failed (DB error) — retrying "
                            "next beat", exc_info=True)
                continue
            if not ok:
                state = await primary_lease.current()
                log.critical(
                    "primary-lease: LOST (fenced takeover by %r) — TWO primary "
                    "daemons are configured. This daemon keeps running but the "
                    "fleet is double-driven until CCSWITCH_DAEMON_ROLE is fixed "
                    "on one host and that daemon restarts.",
                    state.holder if state else "<released>",
                )

    async def _refresh_idle_tokens_once(self) -> None:
        """Proactively refresh idle accounts' access tokens (best-effort).

        Decoupled from the usage poller's backoff so a throttled *usage* endpoint
        can't starve *token* refresh. Skips accounts that are already needs-reauth
        (a dead single-use token can't be refreshed — don't hammer the endpoint)
        and accounts with a live session (the SDK owns that token family and
        ccswitch must not refresh it out from under the running process). Reuses
        ensure_valid_token as the single source of truth; per-account errors are
        swallowed — status transitions stay owned by the usage_for path.
        """
        try:
            accounts = ccswitch_accounts.list_accounts()
        except Exception:
            log.warning("token-refresh: list_accounts failed", exc_info=True)
            return
        for acct in accounts:
            name = acct["name"]
            if not acct.get("enabled", True):
                continue
            if acct.get("status") == "needs-reauth" or self.has_live_session(name):
                continue
            try:
                await asyncio.to_thread(ccswitch_accounts.ensure_valid_token, name)
            except Exception:
                log.debug("token-refresh: ensure_valid_token failed for %s", name, exc_info=True)

    async def _token_refresh_monitor(self) -> None:
        """Run an idle-token refresh immediately (restart recovery), then every
        poll_interval seconds."""
        while True:
            try:
                await self._refresh_idle_tokens_once()
            except Exception:  # pragma: no cover — defensive, _refresh_idle_tokens_once swallows
                log.exception("token-refresh: monitor sweep failed")
            await asyncio.sleep(self._poll_interval)

    async def stop(self) -> None:
        """Stop the titling sweep, rotation monitor and usage poller.

        Every await here is bounded (issue #846): stop() runs inside the app
        lifespan shutdown, so a single task or driver that ignores cancellation
        used to wedge TestClient exits (and systemd stop) indefinitely.
        """
        # Mark teardown FIRST, before any driver is closed below: a turn dropped by
        # our own aclose() must not trip failure recovery → needs_attention. Leave the
        # row for rehydrate() to normalize to idle (resumable) on the next boot.
        self._shutting_down = True
        if self._pr_branch_task is not None:
            self._pr_branch_task.cancel()
            await bounded_wait(self._pr_branch_task, timeout=_STOP_TASK_TIMEOUT_S,
                               phase="stop:pr-branch")
            self._pr_branch_task = None
        # EVERY loop start() creates must appear here. Two were missing — the
        # recurring-session scheduler and the SDLC intake tick — and outlived
        # stop() as orphan tasks, reaped only by the runner's final loop shutdown
        # after the DB engine was disposed (#405's contamination vector; a live
        # suspect in the #846 teardown stalls). test_orchestrator_stop_completeness
        # pins the full set by task name.
        for attr in ("_titling_task", "_goal_task", "_rotation_task", "_stall_task",
                     "_token_refresh_task", "_backlog_task", "_sdlc_intake_task",
                     "_pm_sync_task",
                     "_sdlc_synth_task",
                     "_autopilot_task", "_cicd_task", "_archaudit_task", "_sweep_task",
                     "_archive_limit_task", "_janitor_task", "_recurring_task",
                     "_lease_task", "_drain_timer"):
            # Default None: stop() must be safe on an orchestrator that never
            # start()ed (tests build one via __new__/without start).
            task = getattr(self, attr, None)
            if task is not None:
                task.cancel()
                await bounded_wait(task, timeout=_STOP_TASK_TIMEOUT_S,
                                   phase=f"stop:{attr}")
                setattr(self, attr, None)
        # Cancel+await every tracked fire-and-forget task (#405) so none survives to
        # write into a later app/test's DB. Snapshot first: a task's done-callback
        # mutates ``_bg_tasks`` as it settles.
        pending = [t for t in self._bg_tasks if not t.done()]
        for t in pending:
            t.cancel()
        for t in pending:
            await bounded_wait(t, timeout=_STOP_TASK_TIMEOUT_S,
                               phase=f"stop:bg:{t.get_name()}")
        self._bg_tasks.clear()
        # Close any still-live driver so its stream/pump (which invokes the persist
        # callbacks that write to the DB) cannot outlive teardown. In production the
        # app shutdown closes sessions before stop(); when it doesn't (e.g. the X11
        # manager is absent, as in tests), this is the backstop.
        for sid in list(self._drivers.keys()):
            driver = self._drivers.pop(sid, None)
            if driver is not None:
                # suppress() covers the synchronous aclose() call itself (a stand-in
                # without the method); bounded_wait never raises once awaited.
                with contextlib.suppress(Exception):
                    await bounded_wait(driver.aclose(),
                                       timeout=_STOP_DRIVER_CLOSE_TIMEOUT_S,
                                       phase=f"stop:driver-close:{sid}")
        await bounded_wait(self._poller.stop(), timeout=_STOP_TASK_TIMEOUT_S,
                           phase="stop:usage-poller")
        await bounded_wait(self._pr_poller.stop(), timeout=_STOP_TASK_TIMEOUT_S,
                           phase="stop:pr-poller")
        await bounded_wait(self._pr_population.stop(), timeout=_STOP_TASK_TIMEOUT_S,
                           phase="stop:pr-population")
        await bounded_wait(self._deploy_watcher.stop(), timeout=_STOP_TASK_TIMEOUT_S,
                           phase="stop:deploy-watcher")
        # Hand the primary lease back LAST (#852) — after every automation loop
        # above is genuinely dead — so a planned failover can promote another
        # primary immediately instead of waiting out the TTL. getattr: stop()
        # must be safe on a never-start()ed / __new__-built orchestrator. The
        # fenced release makes releasing after a takeover a harmless no-op.
        fence = getattr(self, "_lease_fence", None)
        if fence is not None:
            self._lease_fence = None
            await bounded_wait(
                primary_lease.release(self_host_name(), fence),
                timeout=_STOP_TASK_TIMEOUT_S, phase="stop:primary-lease-release")

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------

    async def create_session(
        self,
        name: str,
        account: str | None = None,
        initial_prompt: str | None = None,
        cols: int = 80,
        rows: int = 24,
        cwd: str | None = None,
        branch: str | None = None,
        repo: str | None = None,
        identity: str = SYSTEM_IDENTITY,
        permission_mode: str = "interactive",
        attachment_ids: list[str] | None = None,
        name_is_auto: bool = True,
        origin: str | None = None,
        origin_ref: str | None = None,
        model: str | None = None,
    ) -> SessionRecord:
        """Pick an account, start a SessionDriver, persist a DB row, return a SessionRecord.

        Raises:
            AllAccountsBusy: when no account has headroom.
            ValueError: when cwd is given but does not exist.
            BranchSwitchError: when branch checkout fails.
            gitrepos.CloneError / gitrepos.GhError: when repo is given (no cwd)
                and resolving it into a ccswitch-owned clone fails.
        """
        # Resolve cwd: if given, it MUST be an existing directory
        if cwd is not None:
            if not os.path.isdir(cwd):
                raise ValueError(f"cwd does not exist: {cwd}")
        elif repo is not None:
            # No explicit cwd — resolve the repo (nameWithOwner) into a
            # ccswitch-owned clone path, cloning on first use.  This MUST run
            # in this process: gitrepos.ensure_clone owns the on-disk clone
            # registry, and in a dual-host deployment (edge on one host, this
            # orchestrator running in the session daemon on another) only the
            # daemon's filesystem is where the session will actually execute.
            # The edge used to resolve this itself and hand over a `cwd` —
            # cross-host that path doesn't exist here. Mirrors the daemon's
            # other ensure_clone call sites (_backlog_spawn, _pr_doctor_step,
            # _pr_doctor_consolidate_spawn). The user's real ~/GitHub checkouts
            # are never touched — all clone + worktree activity lives under
            # <repo_root>/.ccswitch/.
            url = f"https://github.com/{repo}.git"
            cwd = await asyncio.to_thread(
                gitrepos.ensure_clone, await self._effective_repo_root(), repo, url)

        session_id = str(uuid.uuid4())

        # Model-approval gate. The single chokepoint for BOTH the API and
        # automations (which call this method directly). A gated model is parked
        # here (status 'pending_approval', no driver, no capacity slot) rather than
        # spawned; a blocked one is refused outright. Runs BEFORE the capacity check
        # so a parked session never has to compete for a runtime slot it won't use.
        from server.sessions.model_policy import resolve_model_policy
        async with db_session() as _db:
            _policy = await resolve_model_policy(_db, model=model, account=account)
        if _policy == "block":
            raise ModelBlockedError(model)
        if _policy == "require_approval":
            return await self._hold_for_approval(
                session_id, name=name, account=account, initial_prompt=initial_prompt,
                cols=cols, rows=rows, cwd=cwd, branch=branch, repo=repo,
                identity=identity, permission_mode=permission_mode,
                attachment_ids=attachment_ids, name_is_auto=name_is_auto,
                origin=origin, origin_ref=origin_ref, model=model)

        # Global active-session cap: at/over the cap the session is created as a
        # queued row (real session, no worktree/account/driver yet) and started
        # later by _maybe_start_queued in (priority, enqueued_at) order. Applies
        # to EVERY origin — automations included (their claims simply wait).
        if self._active_session_count() >= await self._max_active_sessions():
            return await self._enqueue_session(
                session_id, name=name, account=account,
                initial_prompt=initial_prompt, cols=cols, rows=rows, cwd=cwd,
                branch=branch, repo=repo, identity=identity,
                permission_mode=permission_mode, attachment_ids=attachment_ids,
                name_is_auto=name_is_auto, origin=origin, origin_ref=origin_ref,
                model=model)
        return await self._spawn_session(
            session_id, name=name, account=account, initial_prompt=initial_prompt,
            cols=cols, rows=rows, cwd=cwd, branch=branch, repo=repo,
            identity=identity, permission_mode=permission_mode,
            attachment_ids=attachment_ids, name_is_auto=name_is_auto,
            origin=origin, origin_ref=origin_ref, model=model)

    def _active_session_count(self) -> int:
        """Sessions consuming a runtime capacity slot (see CAPACITY_EXEMPT_STATUSES)."""
        return sum(1 for r in self._records.values()
                   if r.status not in CAPACITY_EXEMPT_STATUSES)

    async def _max_active_sessions(self) -> int:
        from sqlalchemy import select as sa_select
        from server.db.models import DEFAULT_MAX_ACTIVE_SESSIONS, AppSettings
        async with db_session() as db:
            s = (await db.execute(sa_select(AppSettings))).scalar_one_or_none()
        val = getattr(s, "max_active_sessions", None) if s else None
        return max(1, val if val is not None else DEFAULT_MAX_ACTIVE_SESSIONS)

    async def _enqueue_session(self, session_id: str, *, name, account,
                               initial_prompt, cols, rows, cwd, branch, repo,
                               identity, permission_mode, attachment_ids,
                               name_is_auto, origin, origin_ref, model) -> SessionRecord:
        from datetime import datetime
        from server.sessions import session_queue as sq
        now = datetime.utcnow()
        host = local_host_stamp()
        async with db_session() as db:
            db.add(DbSession(
                id=session_id, name=name, account_name=account or "",
                claude_session_id=None, status="queued", pid=None,
                cwd=cwd or "", repo=repo, branch=None,
                permission_mode=permission_mode, name_is_auto=name_is_auto,
                origin=origin, origin_ref=origin_ref, model=model,
                initial_prompt=initial_prompt, host=host))
        priority = await sq.enqueue(session_id, {
            "name": name, "account": account, "initial_prompt": initial_prompt,
            "cols": cols, "rows": rows, "cwd": cwd, "branch": branch,
            "repo": repo, "identity": identity,
            "permission_mode": permission_mode,
            "attachment_ids": attachment_ids, "name_is_auto": name_is_auto,
            "origin": origin, "origin_ref": origin_ref, "model": model,
        })
        rec = SessionRecord(
            id=session_id, name=name, account=account or "", claude_session_id="",
            cols=cols, rows=rows, cwd=cwd or "", repo=repo, branch=None,
            status="queued", name_is_auto=name_is_auto,
            created_at=now, updated_at=now, origin=origin, origin_ref=origin_ref,
            model=model, host=host)
        self._records[session_id] = rec
        log.info("session.queued id=%s priority=%d origin=%s",
                 session_id, priority, origin)
        return rec

    async def _hold_for_approval(self, session_id: str, *, name, account,
                                 initial_prompt, cols, rows, cwd, branch, repo,
                                 identity, permission_mode, attachment_ids,
                                 name_is_auto, origin, origin_ref, model) -> SessionRecord:
        """Park a model-gated session at 'pending_approval': persist a driverless row
        with the requested model in `pending_model`, no queue entry, no capacity slot.

        Mirrors _enqueue_session's persistence shape (same DbSession/SessionRecord
        kwargs) but status 'pending_approval', model=None, pending_model=<model>, and
        the requested_by/at provenance columns set. The full spawn kwargs are kept in
        self._pending_approvals so the approve path (Task 10) can spawn cleanly; that
        map is in-memory only — a restart rebuilds the row-backed record via rehydrate.
        """
        from datetime import datetime
        now = datetime.utcnow()
        host = local_host_stamp()
        async with db_session() as db:
            db.add(DbSession(
                id=session_id, name=name, account_name=account or "",
                claude_session_id=None, status="pending_approval", pid=None,
                cwd=cwd or "", repo=repo, branch=None,
                permission_mode=permission_mode, name_is_auto=name_is_auto,
                origin=origin, origin_ref=origin_ref,
                model=None, pending_model=model,
                pending_model_requested_by=identity, pending_model_requested_at=now,
                initial_prompt=initial_prompt, host=host))
            await record_audit(db, type="model_approval.requested", identity=identity,
                               detail={"session_id": session_id, "model": model,
                                       "account": account})
        # Keep full spawn kwargs for a clean approve path (attachments included).
        # Restart-durable minus attachments: rebuilt from the row if lost.
        self._pending_approvals[session_id] = dict(
            name=name, account=account, initial_prompt=initial_prompt, cols=cols,
            rows=rows, cwd=cwd, branch=branch, repo=repo, identity=identity,
            permission_mode=permission_mode, attachment_ids=attachment_ids,
            name_is_auto=name_is_auto, origin=origin, origin_ref=origin_ref, model=model)
        rec = SessionRecord(
            id=session_id, name=name, account=account or "", claude_session_id="",
            cols=cols, rows=rows, cwd=cwd or "", repo=repo, branch=None,
            status="pending_approval", name_is_auto=name_is_auto,
            created_at=now, updated_at=now, origin=origin, origin_ref=origin_ref,
            model=None, pending_model=model, host=host)
        self._records[session_id] = rec
        log.info("session.pending_approval id=%s model=%s origin=%s",
                 session_id, model, origin)
        return rec

    async def resolve_model_approval(self, session_id: str, decision: str, *,
                                     identity: str) -> SessionRecord:
        """Approve or deny a parked model selection for BOTH hold kinds.

        LAUNCH-TIME hold (a driverless 'pending_approval' row): approve spawns or
        enqueues the parked model reusing the SAME session_id (delete the placeholder
        row first — _spawn_session/_enqueue_session INSERT their own); deny closes it
        to 'ended'. The in-memory _pending_approvals spawn kwargs are lost on a daemon
        restart, so approve rebuilds them from the persisted row via _rebuild_hold_kwargs.

        MID-SESSION hold (a live session with record.pending_model): approve applies
        driver.set_model(pending_model) and promotes record.model; deny leaves the
        driver and current model untouched. Either resolution fully clears pending
        state on both the record and the row.

        Raises:
            KeyError: unknown session id.
            ValueError: bad decision, or nothing pending to resolve.
        """
        if decision not in ("approve", "deny"):
            raise ValueError(f"bad decision: {decision!r}")
        rec = self._records.get(session_id)
        if rec is None:
            raise KeyError(session_id)

        # Launch-time hold — a not-yet-spawned pending_approval row.
        if rec.status == "pending_approval":
            approved_model = rec.pending_model
            kwargs = self._pending_approvals.pop(session_id, None)
            if kwargs is None:
                kwargs = await self._rebuild_hold_kwargs(session_id)
            async with db_session() as db:
                await record_audit(
                    db, type=f"model_approval.{decision}", identity=identity,
                    detail={"session_id": session_id, "model": approved_model})
            if decision == "deny":
                async with db_session() as db:
                    from sqlalchemy import update as sa_update
                    await db.execute(
                        sa_update(DbSession).where(DbSession.id == session_id).values(
                            status="ended", pending_model=None,
                            pending_model_requested_by=None,
                            pending_model_requested_at=None))
                rec.status = "ended"
                rec.pending_model = None
                log.info("session.model_approval_denied id=%s (launch)", session_id)
                return rec
            # approve: drop the placeholder row + record, then run the normal
            # spawn/enqueue tail with the parked model, reusing the same session_id.
            # _spawn_session/_enqueue_session INSERT their own row, so the placeholder
            # must go first to avoid a duplicate-PK insert. If the spawn then raises
            # (BranchSwitchError from worktree checkout, driver.start() failure, a
            # credential error, …) the session would otherwise vanish completely —
            # deleted row, popped record, popped kwargs. Mirror _maybe_start_queued's
            # durability philosophy: on failure, RESTORE the pending_approval hold so
            # the human can retry the approval, then re-raise so the API surfaces the
            # error. We snapshot the provenance the row needs to be rebuilt first.
            requested_by = None
            requested_at = None
            async with db_session() as db:
                row = await db.get(DbSession, session_id)
                if row is not None:
                    requested_by = row.pending_model_requested_by
                    requested_at = row.pending_model_requested_at
                    await db.delete(row)
            self._records.pop(session_id, None)
            spawn_kwargs = dict(kwargs)
            spawn_kwargs.pop("model", None)
            log.info("session.model_approval_approved id=%s model=%s (launch)",
                     session_id, approved_model)
            try:
                if self._active_session_count() >= await self._max_active_sessions():
                    return await self._enqueue_session(
                        session_id, model=approved_model, **spawn_kwargs)
                return await self._spawn_session(
                    session_id, model=approved_model, **spawn_kwargs)
            except Exception:
                # Spawn/enqueue failed after the placeholder was already deleted.
                # Restore the pending_approval hold (row + in-memory record + the
                # in-memory spawn kwargs) so the approval can be retried instead of
                # the session being silently lost, then re-raise.
                log.warning(
                    "session.model_approval_spawn_failed id=%s model=%s — restoring "
                    "pending_approval hold for retry", session_id, approved_model,
                    exc_info=True)
                await self._restore_hold_after_failed_approve(
                    session_id, model=approved_model, kwargs=kwargs,
                    requested_by=requested_by, requested_at=requested_at)
                raise

        # Mid-session hold — a live session with a parked switch.
        if rec.pending_model:
            pending = rec.pending_model
            async with db_session() as db:
                from sqlalchemy import update as sa_update
                await record_audit(
                    db, type=f"model_approval.{decision}", identity=identity,
                    detail={"session_id": session_id, "model": pending})
                values: dict = {
                    "pending_model": None,
                    "pending_model_requested_by": None,
                    "pending_model_requested_at": None,
                }
                if decision == "approve":
                    values["model"] = pending
                await db.execute(
                    sa_update(DbSession).where(DbSession.id == session_id).values(**values))
            if decision == "approve":
                driver = self._drivers.get(session_id)
                if driver is not None:
                    await driver.set_model(pending)
                rec.model = pending
            rec.pending_model = None
            # Live-push a status frame (unchanged status) so the dashboard reconciles
            # and drops the pending badge — the same lifecycle-frame mechanism the
            # park path in set_model_for_session uses.
            self._publish_event({
                "type": "status", "sessionId": session_id, "status": rec.status,
                "updatedAt": rec.updated_at_iso, "revAt": rec.rev_at})
            log.info("session.model_approval_%s id=%s model=%s (mid-session)",
                     "approved" if decision == "approve" else "denied",
                     session_id, pending)
            return rec

        raise ValueError(f"session {session_id} has no pending model approval")

    async def _rebuild_hold_kwargs(self, session_id: str) -> dict:
        """Reconstruct spawn kwargs from the persisted 'pending_approval' row when the
        in-memory _pending_approvals entry was lost to a daemon restart. Attachment ids
        are NOT persisted on the row, so they default to None (a re-launched approval
        forgoes the original attachments — acceptable vs. failing the approve)."""
        async with db_session() as db:
            row = await db.get(DbSession, session_id)
            if row is None:
                raise KeyError(session_id)
            return dict(
                name=row.name, account=row.account_name or None,
                initial_prompt=row.initial_prompt, cols=80, rows=24,
                cwd=row.cwd or None, branch=row.branch, repo=row.repo,
                identity=row.pending_model_requested_by or SYSTEM_IDENTITY,
                permission_mode=row.permission_mode, attachment_ids=None,
                name_is_auto=row.name_is_auto, origin=row.origin,
                origin_ref=row.origin_ref)

    async def _restore_hold_after_failed_approve(
            self, session_id: str, *, model, kwargs: dict,
            requested_by, requested_at) -> None:
        """Re-park a launch-time approval whose spawn/enqueue raised after the
        placeholder row was already deleted, so the session is never silently lost.

        Re-INSERTs the driverless 'pending_approval' row (model=None,
        pending_model=<model>, provenance restored), re-seeds the in-memory
        SessionRecord, and restores the self._pending_approvals spawn kwargs so a
        second approval can retry cleanly. Mirrors _hold_for_approval's persistence
        shape; deliberately does NOT re-audit (the original 'model_approval.approve'
        stands — this is recovery, not a new decision). Any failure here is swallowed
        so it cannot mask the original spawn exception the caller re-raises."""
        from datetime import datetime
        try:
            async with db_session() as db:
                # The failed spawn may or may not have INSERTed a row before raising;
                # remove any partial row first so the re-INSERT can't collide on PK.
                existing = await db.get(DbSession, session_id)
                if existing is not None:
                    await db.delete(existing)
                await db.flush()
                host = local_host_stamp()
                db.add(DbSession(
                    id=session_id, name=kwargs.get("name"),
                    account_name=kwargs.get("account") or "",
                    claude_session_id=None, status="pending_approval", pid=None,
                    cwd=kwargs.get("cwd") or "", repo=kwargs.get("repo"), branch=None,
                    permission_mode=kwargs.get("permission_mode"),
                    name_is_auto=kwargs.get("name_is_auto"),
                    origin=kwargs.get("origin"), origin_ref=kwargs.get("origin_ref"),
                    model=None, pending_model=model,
                    pending_model_requested_by=requested_by,
                    pending_model_requested_at=requested_at,
                    initial_prompt=kwargs.get("initial_prompt"), host=host))
            restored = dict(kwargs)
            restored["model"] = model
            self._pending_approvals[session_id] = restored
            now = datetime.utcnow()
            self._records[session_id] = SessionRecord(
                id=session_id, name=kwargs.get("name") or session_id,
                account=kwargs.get("account") or "", claude_session_id="",
                cols=kwargs.get("cols", 80), rows=kwargs.get("rows", 24),
                cwd=kwargs.get("cwd") or "", repo=kwargs.get("repo"), branch=None,
                status="pending_approval", name_is_auto=kwargs.get("name_is_auto", True),
                created_at=now, updated_at=now, origin=kwargs.get("origin"),
                origin_ref=kwargs.get("origin_ref"),
                model=None, pending_model=model, host=host)
        except Exception:
            # Never let the recovery path mask the real spawn error.
            log.exception("session.model_approval_hold_restore_failed id=%s",
                          session_id)

    async def _maybe_start_queued(self) -> int:
        """Start queued sessions while capacity allows. Serialised by a lock so
        concurrent teardown paths can't double-start one entry. Returns count
        started.

        Start failure → row leaves the queue, session is marked ``ended`` (not
        ``needs_attention``).  Rationale: a failed-to-start session holds no
        process; leaving it ``needs_attention`` would consume a cap slot forever
        and starve the rest of the queue.  ``ended`` puts the session in
        CAPACITY_EXEMPT_STATUSES so the capacity gate re-opens for the next
        iteration without any bypass.  Existing end-of-life machinery (UI ended
        state, backlog-runner claim release) handles the rest.

        NEVER awaited inline inside a WS-attach coroutine — always scheduled via
        asyncio.create_task so a client-disconnect cancellation cannot interrupt it.
        """
        if self._shutting_down:
            return 0
        from server.sessions import session_queue as sq
        started = 0
        # Guard: an instance created before this attribute was added (mid-session
        # code update without a daemon restart) won't have it in __init__. Lazy-init
        # here so the drain proceeds rather than raising AttributeError in a task.
        if not hasattr(self, "_queue_drain_lock"):
            self._queue_drain_lock = asyncio.Lock()
        async with self._queue_drain_lock:
            while True:
                if self._active_session_count() >= await self._max_active_sessions():
                    break
                entry = await sq.next_entry()
                if entry is None:
                    break
                sid, params = entry.session_id, dict(entry.params)
                await sq.remove(sid)
                # Re-verify the DB row is still 'queued' before spawning.  A
                # concurrent close/archive (or crash-window orphan) may have already
                # changed the status while the entry sat in the queue.  Skipping here
                # heals those stale entries without double-starting the session.
                in_mem = self._records.get(sid)
                # A legitimately queued entry ALWAYS has an in-memory record
                # (enqueue and rehydrate both seed one) — a missing record means
                # a concurrent close/archive won the race; skip, never spawn.
                if in_mem is None or in_mem.status != "queued":
                    log.warning("session_queue.stale_entry id=%s status=%s — skipping",
                                sid, in_mem.status if in_mem is not None else "<gone>")
                    continue
                # Remove the queued pre-record so it doesn't double-count while
                # _spawn_session re-inserts it as an active record.
                self._records.pop(sid, None)
                try:
                    await self._spawn_session(sid, preexisting_row=True, **params)
                    started += 1
                except Exception as exc:
                    # A queued session can lose a race with branch auto-delete on
                    # merge (#836): its PR lands between enqueue and spawn, the
                    # remote deletes the head branch, and create_worktree finds no
                    # ref. That is the pipeline succeeding, not a failure — one
                    # INFO line and an 'obsolete' note instead of a traceback.
                    obsolete_pr = self._queued_spawn_merged_pr(exc, params)
                    if obsolete_pr is not None:
                        log.info(
                            "session_queue.obsolete_branch_merged id=%s branch=%s "
                            "pr=#%d — branch auto-deleted when its PR merged "
                            "before spawn; ending queued session as obsolete",
                            sid, params.get("branch"), obsolete_pr.number)
                    else:
                        log.warning("session_queue.start_failed id=%s", sid,
                                    exc_info=True)
                    resolution = (
                        f"obsolete: PR #{obsolete_pr.number} merged before start"
                        if obsolete_pr is not None else None)
                    # Mark the session ended so it does not occupy a cap slot.
                    # Re-seed a minimal in-memory record if _spawn_session raised
                    # before creating one, then persist the status to the DB row
                    # (which still exists as a 'queued' row — the UPDATE in
                    # _spawn_session was not reached).
                    if sid not in self._records:
                        from datetime import datetime
                        self._records[sid] = SessionRecord(
                            id=sid,
                            name=params.get("name", sid),
                            account=params.get("account") or "",
                            claude_session_id="",
                            cols=params.get("cols", 220),
                            rows=params.get("rows", 50),
                            cwd=params.get("cwd") or "",
                            repo=params.get("repo"),
                            branch=None,
                            status="ended",
                            name_is_auto=params.get("name_is_auto", True),
                            updated_at=datetime.utcnow(),
                            origin=params.get("origin"),
                            origin_ref=params.get("origin_ref"),
                            model=params.get("model"),
                        )
                    else:
                        self._records[sid].status = "ended"
                    if resolution is not None:
                        self._records[sid].pr_resolution = resolution
                        self._records[sid].pr_resolution_source = "queue-drain"
                    # Persist to DB — the row still exists (was 'queued'); update it.
                    values: dict = {"status": "ended"}
                    if resolution is not None:
                        values["pr_resolution"] = resolution
                        values["pr_resolution_source"] = "queue-drain"
                    async with db_session() as db:
                        from sqlalchemy import update as sa_update
                        await db.execute(
                            sa_update(DbSession)
                            .where(DbSession.id == sid)
                            .values(**values)
                        )
        return started

    def _queued_spawn_merged_pr(self, exc: Exception,
                                params: dict) -> "PullRequestInfo | None":
        """The merged PR that makes a queue-spawn failure benign, or None (#836).

        Benign means BOTH: the failure's root cause is an unresolvable branch
        ref (worktrees.BranchRefUnresolvableError — the ref exists nowhere,
        typical of a PR head branch auto-deleted on merge between enqueue and
        spawn), AND the daemon's PR caches show a *merged* PR for the queued
        (repo, branch). Checked against the per-branch poller cache first
        (it retains merged state — that's how _pr_pairs prunes terminal pairs),
        then the population cache (which keeps recently merged PRs). Anything
        else returns None so the caller keeps the real error path: an
        unresolvable ref with no merged PR behind it is a genuine problem.
        """
        if not isinstance(exc, BranchSwitchError) or not isinstance(
                exc.__cause__, worktrees.BranchRefUnresolvableError):
            return None
        repo, branch = params.get("repo"), params.get("branch")
        if not repo or not branch:
            return None
        # Defensive getattr: minimally-constructed orchestrators (some tests
        # build via __new__) may lack the pollers — mirrors _pr_pairs.
        info: PullRequestInfo | None = None
        poller = getattr(self, "_pr_poller", None)
        if poller is not None:
            info = poller.cached(repo, branch)
        if info is None and getattr(self, "_pr_population", None) is not None:
            info = self._pr_population_pr(repo, branch)
        if info is not None and info.state == "merged":
            return info
        return None

    async def _reconcile_orphan_queued(self, candidate_rows=None) -> int:
        """Reconcile 'queued' session rows with no matching session_queue entry.

        The drain loop (server/sessions/session_queue.py) only ever iterates the
        session_queue table, so a 'queued' sessions row that has lost (or never
        got) its queue entry is invisible to it forever: stranded garbage that
        also renders in the UI as a duplicate queue position (two cards both
        numbered 1 — the live incident this fixes).

        Decision: mark orphans 'ended', never re-enqueue. Re-enqueueing needs the
        full _enqueue_session params dict, but per SessionQueue's docstring,
        session_queue.params is the ONLY place cols/rows/identity/attachment_ids
        are persisted — the sessions row itself never carries them (confirmed
        against the live schema and the actual stranded row: `sessions` has no
        such columns). An orphan's queue entry is gone, so those fields cannot be
        reconstructed without guessing at values that could silently corrupt the
        replayed session. This mirrors _maybe_start_queued's start-failure
        handling (see its docstring above): a queued row we cannot honor is
        marked 'ended' rather than left occupying a phantom slot forever.
        'ended' is in CAPACITY_EXEMPT_STATUSES / INACTIVE_SESSION_STATUSES, so it
        drops out of both the capacity count and any future 'queued' query.

        Called from rehydrate(), immediately before the queue-drain-on-rehydrate
        spawn, so a reconciled slate is what that drain sees in the same pass.
        Idempotent: once a row is 'ended' it no longer matches the orphan query,
        so a second call reconciles nothing. Returns the count reconciled.

        ``candidate_rows``: rehydrate() already selects every non-inactive row
        (which includes 'queued') in one query — pass that list in to avoid a
        second full-table SELECT. Standalone callers (tests) omit it and this
        method queries for itself.
        """
        from sqlalchemy import select as sa_select
        from sqlalchemy import update as sa_update
        from server.sessions import session_queue as sq

        if candidate_rows is None:
            async with db_session() as db:
                candidate_rows = (await db.execute(
                    sa_select(DbSession).where(DbSession.status == "queued")
                )).scalars().all()
        rows = [r for r in candidate_rows if r.status == "queued"]
        if not rows:
            return 0
        queued_ids = set((await sq.positions()).keys())
        orphans = [row for row in rows if row.id not in queued_ids]
        for row in orphans:
            async with db_session() as db:
                await db.execute(
                    sa_update(DbSession).where(DbSession.id == row.id)
                    .values(status="ended")
                )
            if row.id in self._records:
                self._records[row.id].status = "ended"
            log.warning(
                "session_queue.orphan_reconciled id=%s action=ended "
                "origin=%s origin_ref=%s enqueued_since=%s",
                row.id, getattr(row, "origin", None),
                getattr(row, "origin_ref", None), getattr(row, "created_at", None))
        return len(orphans)

    async def session_queue_move(self, session_id: str, op: str,
                                 position: int | None = None) -> bool:
        """Reorder a queued session. Returns True on success, False if not found."""
        from server.sessions import session_queue as sq
        return await sq.move(session_id, op, position)

    async def session_queue_positions(self) -> dict[str, int]:
        """Return {session_id: 1-based-position} for all queued sessions."""
        from server.sessions import session_queue as sq
        return await sq.positions()

    async def session_queue_cancel(self, session_id: str) -> bool:
        """Remove a session from the queue and mark it ended. Returns True on success."""
        from server.sessions import session_queue as sq
        if not await sq.remove(session_id):
            return False
        await self._persist_status(session_id, "ended")
        rec = self._records.get(session_id)
        if rec is not None:
            rec.status = "ended"
        return True

    async def maybe_start_queued(self) -> int:
        """Public wrapper for _maybe_start_queued. RPC names must not start with '_'."""
        return await self._maybe_start_queued()

    async def _spawn_session(
        self,
        session_id: str,
        *,
        name: str,
        account: str | None,
        initial_prompt: str | None,
        cols: int,
        rows: int,
        cwd: str | None,
        branch: str | None,
        repo: str | None,
        identity: str,
        permission_mode: str,
        attachment_ids: list[str] | None,
        name_is_auto: bool,
        origin: str | None,
        origin_ref: str | None,
        model: str | None,
        preexisting_row: bool = False,
    ) -> SessionRecord:
        """Start a SessionDriver, persist/update a DB row, return a SessionRecord.

        When preexisting_row=True the session already has a DB row (status='queued')
        from _enqueue_session; this method UPDATEs that row (status→'idle',
        account_name, cwd, branch, model) instead of INSERT-ing a new one.
        Task 4 (_maybe_start_queued) calls this path.
        """
        repo_root = await self._effective_repo_root()

        # Per-session isolation: EVERY repo session runs in its OWN git worktree, never
        # the shared clone or the user's real tree. With a branch, the worktree checks it
        # out (child-branch fallback if it's busy elsewhere); without a branch, it's a
        # detached worktree off HEAD. Either way two sessions on one repo never collide,
        # and the user's real checkout is untouched. (No-repo sessions get a scratch dir
        # below; this branch only applies when cwd is an actual git repo — the ccswitch
        # clone resolved by the API.)
        worktree_info: worktrees.WorktreeInfo | None = None
        if cwd is not None and worktrees.is_git_repo(cwd):
            try:
                worktree_info = await asyncio.to_thread(
                    worktrees.create_worktree, cwd, branch, session_id, repo_root
                )
            except worktrees.WorktreeError as exc:
                raise BranchSwitchError(str(exc)) from exc
            assert worktree_info is not None  # create_worktree returns a value or raises
            cwd = worktree_info.path
            self._worktrees[session_id] = worktree_info

        # Resolve final cwd: an explicit cwd was already validated above; a
        # branch-selected session already has cwd=worktree path.  For a no-repo
        # session (no explicit cwd, no worktree) we create an isolated scratch dir
        # under the ccswitch base so the agent never runs in the server's own
        # checkout.  This is the safety guarantee stated in the New Session dialog:
        # "your real checkouts are never touched."
        if cwd is None:
            scratch_dir = Path(worktrees.worktrees_base(repo_root)).parent / "scratch" / session_id
            scratch_dir.mkdir(parents=True, exist_ok=True)
            cwd = str(scratch_dir)

        # Branch this session's worktree is on (None for detached/scratch) — the
        # join key to its GitHub PR. Surfaced as a PR pill on the dashboard.
        branch_name = worktree_info.branch if worktree_info is not None else None

        usage_unverified = False
        if account is None:
            # Skip accounts currently quarantined for a 429/401 so a fresh session
            # doesn't land on an account we already know is struggling.
            picked = select_account(
                exclude=self._recovery.quarantined(now=self._clock()),
                load=self.live_session_load())
            account = picked.name
            usage_unverified = picked.optimistic
        else:
            # Verify the given account exists (will raise if cred file missing — acceptable)
            pass

        cfg_dir: Path = compose(account)

        # Pre-trust the project directory (now the worktree, if any) so claude
        # won't prompt for trust on startup.
        configdir.trust_project(account, cwd)

        # Allocate a virtual X11 display for this session (if X11Manager wired in).
        if self._x11 is not None:
            try:
                await self._x11.start_display(session_id)
            except Exception:
                log.exception("session.display_start_failed id=%s", session_id)

        # Ensure the Copilot proxy is running for copilot-backed accounts.
        if self._copilot is not None and _provider_of(account) == _copilot_mod.PROVIDER:
            await self._copilot.ensure(account)

        # Pre-seed the in-memory record so _session_factory's _oat_env closure can
        # look up the account (needed for copilot env injection during driver.start()).
        # The model field is patched below once the driver resolves the actual alias.
        from datetime import datetime
        _now = datetime.utcnow()
        # A session promoted from the queue keeps its queue-time created_at (and
        # host stamp); a fresh spawn is born now, on this host (worker) / NULL.
        _prior = self._records.get(session_id)
        _host = getattr(_prior, "host", None) or local_host_stamp()
        _pre_record = SessionRecord(
            id=session_id, name=name, account=account, claude_session_id="",
            cols=cols, rows=rows, cwd=cwd, repo=repo, branch=branch_name,
            status="idle", name_is_auto=name_is_auto,
            created_at=getattr(_prior, "created_at", None) or _now,
            updated_at=_now, usage_unverified=usage_unverified,
            origin=origin, origin_ref=origin_ref, model=None, host=_host,
        )
        self._records[session_id] = _pre_record

        # Start a SessionDriver backed by the configured SDK factory.
        driver = SessionDriver(
            session_id, account, cwd, str(cfg_dir),
            mode=permission_mode, sdk_factory=self._session_factory(session_id),
            registry=self._registry, identity=identity,
            on_session_id=self._persist_claude_session_id,
            on_sync_back=functools.partial(ccswitch_accounts.sync_back, account),
            on_status=self._persist_status,
            on_context_usage=self._persist_context_usage,
            on_record_condense_ms=self._record_condense_ms,
            get_condense_estimate=self._condense_estimate,
            on_recoverable_failure=self._handle_recoverable_failure,
            suggester=self._suggester,
        )
        try:
            await driver.start()
        except Exception:
            # driver.start() failed — remove the pre-seeded record so the session
            # does not appear as a ghost idle card in list_sessions.
            self._records.pop(session_id, None)
            raise
        # Apply the chosen model before the first prompt runs (initial prompt is
        # deferred until a client attaches, so this always lands on turn one). The
        # "default" alias / empty means "account default" → leave the driver as-is.
        model = (model or "").strip() or None
        if model and model != "default":
            await driver.set_model(model)
        else:
            model = None
        self._drivers[session_id] = driver
        # Human-created sessions (origin is None) DEFER the initial prompt until the
        # first WebSocket client attaches. Sending it before any subscriber exists
        # risks the turn's events being lost: control events broadcast to zero
        # subscribers are dropped, and the transcript file may not be flushed yet.
        # Deferring means the client is already watching when the turn runs.
        #
        # Automation-spawned sessions (origin set) have NO human who will ever
        # attach, so a deferred prompt would never be sent and the session would sit
        # idle forever. Those send immediately, below — the driver persists its
        # transcript and fires status callbacks with no live subscriber, and a later
        # human attach replays the turn.
        if initial_prompt and origin is None:
            self._pending_initial_prompt[session_id] = initial_prompt
        if attachment_ids and origin is None:
            self._pending_initial_attachment_ids[session_id] = list(attachment_ids)

        if preexisting_row:
            # UPDATE the existing queued row → idle with resolved account/cwd/branch/model
            async with db_session() as db:
                from sqlalchemy import update as sa_update
                await db.execute(
                    sa_update(DbSession)
                    .where(DbSession.id == session_id)
                    .values(status="idle", account_name=account,
                            cwd=cwd, branch=branch_name, model=model)
                )
        else:
            # Persist DB row (fresh session, not previously queued)
            async with db_session() as db:
                row = DbSession(
                    id=session_id,
                    name=name,
                    account_name=account,
                    claude_session_id=None,
                    status="idle",
                    pid=None,
                    cwd=cwd,
                    repo=repo,
                    branch=branch_name,
                    permission_mode=permission_mode,
                    name_is_auto=name_is_auto,
                    origin=origin,
                    origin_ref=origin_ref,
                    model=model,
                    initial_prompt=initial_prompt,
                    host=_host,
                )
                db.add(row)

        # Patch the model on the pre-seeded record (resolved by driver.set_model above).
        _pre_record.model = model
        record = _pre_record
        log_event(log, logging.INFO, "session.start",
                  session_id=session_id, **{"from": None, "to": "idle", "reason": "create"})
        log.info("session.create id=%s account=%s", session_id, account)
        # Autonomous kick-off for automation-spawned sessions: no human will attach
        # to trigger the deferred prompt, so run turn one now (after the DB row +
        # record exist, so status/claude-session callbacks land on a persisted row).
        if initial_prompt and origin is not None:
            await self.send_prompt(session_id, initial_prompt, attachment_ids)
        return record

    async def close_session(self, session_id: str) -> None:
        """Stop the driver, clean up the session worktree, mark the row ended."""
        # Teardown the virtual X11 display — runs unconditionally so orphaned
        # displays are never leaked even when the driver was never registered.
        if self._x11 is not None:
            try:
                await self._x11.stop_display(session_id)
            except Exception:
                log.exception("session.display_cleanup_failed id=%s", session_id)

        driver = self._drivers.pop(session_id, None)
        self._last_prompt.pop(session_id, None)
        self._turn_restart_at.pop(session_id, None)
        self._pending_initial_prompt.pop(session_id, None)
        if driver is not None:
            await driver.aclose()
        record = self._records.pop(session_id, None)

        # Remove the per-session worktree if we created one (or if the session's
        # cwd is one of our managed worktree paths — recognised even after a
        # server restart). Dirty worktrees are kept; remove_worktree never
        # destroys uncommitted work.
        wt = self._worktrees.pop(session_id, None)
        wt_path = wt.path if wt else (
            record.cwd if record and worktrees.is_managed_path(record.cwd, self._repo_root)
            else None
        )
        if wt_path:
            try:
                removed = await asyncio.to_thread(worktrees.remove_worktree, wt_path)
                log.info("session.worktree id=%s removed=%s path=%s",
                         session_id, removed, wt_path)
            except Exception:
                log.exception("session.worktree_cleanup_failed id=%s path=%s",
                              session_id, wt_path)

        # Remove the no-repo scratch dir if this session used one.  Guard with a
        # strict containment check so we never rm anything outside the scratch base.
        if record and record.cwd:
            scratch_base = Path(worktrees.worktrees_base(self._repo_root)).parent / "scratch"
            try:
                cwd_resolved = Path(record.cwd).resolve()
                scratch_base_resolved = scratch_base.resolve()
                cwd_resolved.relative_to(scratch_base_resolved)  # raises ValueError if not under base
                if cwd_resolved.is_dir():
                    import shutil
                    await asyncio.to_thread(shutil.rmtree, str(cwd_resolved))
                    log.info("session.scratch_cleanup id=%s path=%s", session_id, cwd_resolved)
            except ValueError:
                pass  # Not a scratch dir — nothing to do
            except Exception:
                log.exception("session.scratch_cleanup_failed id=%s", session_id)

        # Remove from the session queue BEFORE persisting ended status so the
        # drain triggered below cannot resurrect a just-closed queued session.
        from server.sessions import session_queue as sq
        await sq.remove(session_id)

        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession)
                .where(DbSession.id == session_id)
                .values(status="ended")
            )
        log.info("session.close id=%s", session_id)

        # Nudge the Backlog Runner when one of its sessions ends so a freed slot
        # refills immediately (and the terminal-watch runs) without waiting for
        # the next interval tick. Best-effort; the loop is non-reentrant so this
        # never overlaps an in-progress tick.
        if record is not None and getattr(record, "origin", None) == "backlog-runner":
            try:
                self._spawn(self._backlog_loop.tick(), name="backlog-runner-nudge")
            except Exception:  # pragma: no cover — scheduling must never break close
                log.debug("backlog_runner.nudge_failed", exc_info=True)

        # Drain the session queue: a closed session frees a slot so the next queued
        # session can start. Tracked via _spawn so stop() can cancel it before it
        # writes into a later test's DB.
        self._spawn(self._maybe_start_queued(), name="queue-drain-on-close")

    async def rename_session(self, session_id: str, name: str) -> None:
        """Update the session name in the DB and in-memory record.

        A manual rename pins ``name_is_auto = False`` so auto-naming never overwrites it.
        """
        if session_id in self._records:
            self._records[session_id].name = name
            self._records[session_id].name_is_auto = False
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession)
                .where(DbSession.id == session_id)
                .values(name=name, name_is_auto=False)
            )

    async def _persist_status(self, session_id: str, status: str) -> None:
        """Driver ``on_status`` callback: persist the derived dashboard status.

        Updates the in-memory record (which the dashboard's 5s poll reads) and the DB
        row. Never downgrades a terminal ``ended`` status. When a turn finishes
        (``done``), opportunistically refresh the auto-name from Claude's title.
        """
        from server.sessions.status import ACTIVITY_STATUSES
        rec = self._records.get(session_id)
        if rec is not None and rec.status == "ended":
            return
        # A "switch after this turn" model change is in force once the turn is over.
        self._settle_model_pending(session_id, status)
        # A turn failure surfacing during teardown is our own shutdown dropping the
        # turn (deploy / watchdog restart / systemctl stop), not a real failure.
        # Persisting needs_attention here brands the session "Turn failed" across
        # the restart; leaving the mid-turn row lets the next boot's rehydrate()
        # normalize it to idle + interruptedByRestart (issue #434). Terminal writes
        # (ended) and every other status still persist during shutdown.
        if status == "needs_attention" and self._shutting_down:
            log.info("persist.skip_needs_attention_during_shutdown session=%s", session_id)
            return
        # Overlay: a turn that finished (`done`) but left the agent parked on an external
        # event — an open background task or an armed self-scheduled wakeup — is "Waiting…",
        # not "Your turn". Computed here (not in the pure `derive_status`) because it needs
        # the transcript, like the `limited`/`ended` overlays. Scan only on `done` so the
        # per-emit cost is paid once per turn-end. `waiting_external` is NOT in
        # ACTIVITY_STATUSES, so parking does NOT bump the "active … ago" clock below — the
        # card shows time since the last real turn, not a reset on every re-arm.
        completed_turn = status == "done"
        if completed_turn:
            from .background_tasks import is_parked, last_turn_awaits_user
            tpath = self.transcript_path(session_id)
            if is_parked(tpath):
                # A parked `done` turn normally reads "Waiting…". But if the turn handed
                # a question/decision back to the user in prose (no structured
                # `pending_question`, which would already win in derive_status), it needs
                # the user — not a timer/process — so surface "Answer needed" instead of
                # masking it as waiting_external.
                status = ("waiting_question" if last_turn_awaits_user(tpath)
                          else "waiting_external")
        # Advance the "active … ago" clock ONLY when the assistant hands output back —
        # a finished or user-paused turn (ACTIVITY_STATUSES). Bumping it on every status
        # (in_progress) or incidental row write is the bug that pinned every card to
        # "0s ago". Naive UTC to match the DB DateTime columns.
        from datetime import datetime
        turn_at = datetime.utcnow() if status in ACTIVITY_STATUSES else None
        driver = self._drivers.get(session_id)
        # getattr (not attribute access): a driver may be absent, or a test/partial
        # driver stub without last_pr_resolution — neither should break status persist.
        resolution = getattr(driver, "last_pr_resolution", None)
        if rec is not None:
            rec.status = status
            rec.updated_at = datetime.utcnow()   # row-write token; bumps on EVERY persist (see rev_at)
            if turn_at is not None:
                rec.last_turn_at = turn_at
            # Capture the pr_resolution token from the driver when a turn completes,
            # but never clobber a pre-existing value (e.g. from a classifier).
            if status == "complete" and resolution and rec.pr_resolution is None:
                rec.pr_resolution = resolution
                rec.pr_resolution_source = "marker"
            # The session is doing something again → the "interrupted by daemon restart,
            # re-send to resume" hint (issue #434) is resolved. Clear it on any non-idle
            # status so the card stops nagging once the user re-sends.
            if status != "idle":
                rec.interrupted_by_restart = False
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            values: dict = {"status": status}
            if turn_at is not None:
                values["last_turn_at"] = turn_at
            # Write pr_resolution to DB when the in-memory record was just set above;
            # rec.pr_resolution == resolution guards idempotency (won't overwrite a
            # pre-existing classifier value that differs from the marker resolution).
            if (status == "complete" and resolution and rec is not None
                    and rec.pr_resolution == resolution):
                values["pr_resolution"] = resolution
                values["pr_resolution_source"] = "marker"
            # Fold the in-memory slash-command catalog (cached during attach by
            # command_catalog(), which must not write from the cancellable attach path)
            # into this driver-loop write so it reaches the DB row for cross-restart
            # replay. Written here rather than on attach because this callback runs in
            # the turn loop, safe from client-disconnect cancellation.
            if rec is not None and rec.command_catalog:
                values["command_catalog"] = rec.command_catalog
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id).values(**values)
            )
        # Refresh the per-sub-agent summary so the dashboard list can nest sub-agents
        # under this session. Best-effort: a failure must never break the session loop.
        # Deferred to a background task — _persist_sub_agents reads the full transcript
        # via a sync glob that can take >1s on machines with many project dirs; awaiting
        # it inline would block the event loop and stall the session status pump.
        self._spawn(self._persist_sub_agents(session_id))
        # Live-push the change to every connected dashboard so the session LIST updates
        # immediately, instead of waiting for the 5s poll (which the browser suspends
        # while the ccswitch tab is backgrounded — the cause of cards getting stuck on
        # "Working…" until the session is opened). Mirrors the "rotated" broadcast; the
        # WS bridge forwards lifecycle frames top-level so the client's status handler
        # (_patchSessionStatus) patches the cached session record.
        self._publish_event({"type": "status", "sessionId": session_id, "status": status,
                             "updatedAt": rec.updated_at_iso if rec is not None else None,
                             "revAt": rec.rev_at if rec is not None else None,
                             "interruptedByRestart": (
                                 rec.interrupted_by_restart if rec is not None else False)})
        await self._maybe_push(session_id, status)
        if completed_turn:
            await self._maybe_apply_auto_name(session_id)
        # A `complete` session still holds a live `claude` process until something
        # releases it — historically only auto-archive did, which has two
        # independent failure modes (autopilot outages; the sticky manual-unarchive
        # opt-out), so a stuck autopilot or an opted-out session could pin capacity
        # forever. Release the process deterministically here instead: completion
        # itself frees the driver/capacity slot, no model call and no autopilot
        # dependency required. Nothing is archived — the row and transcript stay;
        # reopening lazily rebuilds the driver via ensure_driver (resume=
        # claude_session_id).
        #
        # ONLY `complete` — never `done`. `done` is not a finished-work signal: it is
        # what derive_status returns for ANY ended turn (turns_completed >= 1, no turn
        # active), i.e. after every ordinary prompt in every session — `completed_turn`
        # above says exactly that. Releasing on `done` killed the `claude` process at
        # the end of every turn, so the next prompt paid a full resume and any warm
        # in-process state was thrown away. `idle` (never ran) and the parked overlays
        # (waiting_external/waiting_question, computed above) are likewise left alone —
        # those sessions are still alive and awaiting input or an event, not finished.
        #
        # Placed after the live-driver side effects above (dashboard Title push) so an
        # attached client still sees them before the process goes away.
        # Same pop+suppress+aclose idiom as close_session/stop() — but the pop is
        # synchronous (so capacity accounting sees it the instant this call returns)
        # while the aclose() is deferred to its own task, NOT awaited inline: this
        # callback runs as the driver's own on_status hook, i.e. from inside its pump
        # task, and aclose() awaits that same pump task to finish — awaiting it here
        # would be the task waiting on itself. (Identical reasoning to why
        # _maybe_autoarchive_completed schedules the archive instead of awaiting it
        # inline.)
        if status == "complete":
            live_driver = self._drivers.pop(session_id, None)
            if live_driver is not None:
                async def _release(d=live_driver, sid=session_id):
                    try:
                        with contextlib.suppress(Exception):
                            await d.aclose()
                    finally:
                        # Clear only our own entry — never a newer release that
                        # (defensively) replaced this one for the same session_id.
                        # Runs on both success and failure so a stuck/erroring
                        # release can never make the session permanently unopenable
                        # (ensure_driver awaits this entry before rebuilding).
                        if self._releasing.get(sid) is asyncio.current_task():
                            self._releasing.pop(sid, None)
                self._releasing[session_id] = self._spawn(
                    _release(), name="completion-driver-release")
        self._maybe_autoarchive_completed(session_id, status, rec)
        # Nudge autopilot when a session enters an actionable state so a stuck session
        # is advanced immediately, not only on the next interval tick. Best-effort; the
        # loop is non-reentrant so this never overlaps an in-progress tick.
        from server.automations.autopilot_policy import ACTIONABLE_STATUSES
        if status in ACTIONABLE_STATUSES and getattr(self, "_autopilot_loop", None) is not None:
            try:
                self._spawn(self._autopilot_loop.tick(), name="autopilot-nudge")
            except Exception:  # pragma: no cover — scheduling must never break status persist
                log.debug("autopilot.nudge_failed", exc_info=True)
        # When a session reaches ANY status that frees a capacity slot — terminal
        # (ended/archived) via its driver (self-exit, process death, etc.) without an
        # explicit close_session/archive_session call, OR `complete` (the driver was
        # just released a few lines above) — a queued session should get that slot
        # immediately, not wait for some later unrelated event (another close/archive,
        # a rehydrate, a settings change, a manual RPC) to happen to drain it.
        # CAPACITY_EXEMPT_STATUSES is exactly "a slot just freed"; excluding "queued"
        # itself is required — that member means "waiting FOR a slot", not "just freed
        # one", and status here is never actually "queued" (queued rows are written
        # directly at creation/enqueue, not through this driver on_status callback) but
        # excluding it keeps this call site correct even if that ever changes, rather
        # than relying on an invariant elsewhere.
        # Tracked via _spawn so stop() can cancel it; never awaited inline
        # (WS-attach-reentrant).
        if status in CAPACITY_EXEMPT_STATUSES and status != "queued":
            self._spawn(self._maybe_start_queued(), name="queue-drain-on-status")

    def _maybe_autoarchive_completed(self, session_id: str, status: str, rec) -> None:
        """Schedule a completion auto-archive for ANY session that self-declares done.

        When the agent emits the completion marker, ``derive_status`` yields ``complete``
        (distinct from a plain ``done`` turn). Every session — human, backlog-runner,
        pr-doctor, … — that reaches ``complete`` is auto-archived once the completion bar
        confirms the goal is genuinely met: a single universal rule, independent of the
        autopilot mode. A ``complete`` session is not terminal, so without this its
        worktree lingers and it never leaves the dashboard's active list — the driver
        itself is already released deterministically by ``_persist_status`` regardless
        of archiving (see the block just above this method). Scheduled (not awaited)
        because archive may try to pop this session's driver from inside its
        ``on_status`` callback (a harmless no-op if ``_persist_status`` already popped
        it); idempotent via ``_auto_archiving``. The manual-unarchive opt-out is sticky
        and always wins over the completion signal."""
        if status != "complete":
            return
        if rec is None:
            return
        # A human unarchived this session → honor the sticky opt-out and never re-archive it.
        if getattr(rec, "auto_archive_opt_out_reason", None):
            return
        if session_id in self._auto_archiving:
            return
        self._auto_archiving.add(session_id)
        asyncio.create_task(self._auto_archive_completed(session_id),
                            name="completion-autoarchive")

    def _complete_nudge_over_cap(self, session_id: str) -> bool:
        """Count one not-ready completion nudge for *session_id* and report whether it has
        now exceeded ``COMPLETE_NUDGE_CAP`` (so the caller should give up instead of
        nudging again). Lazily creates the counter map so bare/`__new__` test orchs work."""
        counts = getattr(self, "_complete_nudge_counts", None)
        if counts is None:
            counts = self._complete_nudge_counts = {}
        counts[session_id] = counts.get(session_id, 0) + 1
        return counts[session_id] > COMPLETE_NUDGE_CAP

    async def _auto_archive_completed(self, session_id: str) -> None:
        """Archive a completed session (any origin) once the completion bar confirms the
        goal is met; otherwise nudge it toward the remaining work. A failure clears the
        guard so a later ``complete`` emit can retry.

        Readiness gate (the universal rule): run ``met_bar`` for every completed session.
        When it is NOT met — an open-unarmed PR, an armed PR blocked by red CI, or a code
        goal that opened no PR — the session is not done. First *verify before directing*:
        refresh the PR facts once (the cache may lag a just-armed PR) and, for the
        low-confidence "code goal but no PR" case, consult the PR-intent classifier before
        acting. If still not met, nudge with the remedy (which opens a fresh turn, so the
        session leaves ``complete``), bounded by ``COMPLETE_NUDGE_CAP``. When met, archive:
        stop the driver, release the worktree, snapshot decision-time evidence (goal, repo,
        PR state) for the retro archive audit, and let the sweep reclaim the slot.

        Before ANY of that, the open-question hold: a completed session whose final
        hand-back still awaits the user — a prose question left in the marker message,
        or a structured question/permission that raced this task — is not finished for
        the user even when the bar is met. It flips back to ``waiting_question``
        ("Answer needed") instead of archiving or nudging; the guard is cleared so the
        answer's turn can complete cleanly later, and no completion nudge is consumed
        (the cap bounds not-ready nudges — a held session did nothing wrong).

        Before all of that, the app-wide kill switch: when a user has turned
        auto-archive off globally, a completed session is left exactly as a
        per-session manual opt-out would be — no archive, no nudge, no status flip
        (its driver was already released by ``_persist_status``). The guard is
        cleared so re-enabling the setting and a later ``complete`` can retry."""
        if not await self._read_auto_archive_enabled():
            self._auto_archiving.discard(session_id)
            log.info("completion.autoarchive_disabled sid=%s", session_id)
            return
        rec = self._records.get(session_id)
        origin = getattr(rec, "origin", None) if rec is not None else None
        try:
            hold = await self._completion_open_question(session_id)
        except Exception:
            hold = False  # gate is best-effort: never let it block a legitimate archive
            log.debug("completion.question_gate_failed sid=%s", session_id, exc_info=True)
        if hold:
            self._auto_archiving.discard(session_id)
            log.info("completion.question_hold sid=%s", session_id)
            try:
                await self._persist_status(session_id, "waiting_question")
            except Exception:
                log.warning("completion.question_hold_persist_failed sid=%s",
                            session_id, exc_info=True)
            return
        if rec is not None:
            from server.sessions.goals import effective_goal
            from server.automations.completion_bar import met_bar
            goal_text, goal_source = effective_goal(rec)
            pr = self._autopilot_pr_facts(rec)
            initial_prompt = getattr(rec, "initial_prompt", None)
            resolution = getattr(rec, "pr_resolution", None)
            verdict = met_bar(goal_text, goal_source, pr, resolution,
                              instructions=initial_prompt)
            if not verdict.met:
                # Verify-before-directing: the cached PR facts may lag a just-armed PR.
                # Refresh once (a no-op without a poller), then re-judge.
                refreshed = await self._autopilot_refresh_pr_facts(session_id)
                if refreshed is not None:
                    pr = refreshed
                    verdict = met_bar(goal_text, goal_source, pr, resolution,
                                      instructions=initial_prompt)
                # Salvage the low-confidence "code goal but no PR" case: a research/Q&A
                # session that legitimately opens no PR shouldn't be nudged to open one.
                if not verdict.met and verdict.confidence == "low":
                    try:
                        tail = await self._autopilot_transcript_tail(session_id)
                        resolution = await self._autopilot_resolve_pr_intent(
                            rec, goal_text, goal_source, pr, tail)
                        if resolution:
                            verdict = met_bar(goal_text, goal_source, pr, resolution,
                                              instructions=initial_prompt)
                    except Exception:  # pragma: no cover — salvage is best-effort
                        log.debug("completion.intent_salvage_failed sid=%s",
                                  session_id, exc_info=True)
            if not verdict.met:
                # Not the finished state — nudge toward the remaining work. Clearing the
                # guard first lets a later `complete` retry; the nudge opens a fresh turn,
                # moving the session out of `complete`. Cap prevents an endless loop.
                self._auto_archiving.discard(session_id)
                if self._complete_nudge_over_cap(session_id):
                    log.info("completion.autoarchive_giveup sid=%s reason=%s",
                             session_id, verdict.reason)
                    return
                log.info("completion.autoarchive_deferred sid=%s reason=%s",
                         session_id, verdict.reason)
                nudge = (verdict.remedy
                         or "Finish the remaining work before signalling completion.")
                try:
                    # The session is `complete`, so `_persist_status` has ALREADY popped
                    # and released its driver — send_prompt would just return "no_driver"
                    # and the corrective nudge would be silently thrown away, parking the
                    # session at `complete` on a premature marker. Resume it first (which
                    # also moves it out of `complete`, so it stops being capacity-exempt).
                    if await self.ensure_driver(session_id, SYSTEM_IDENTITY) is None:
                        log.warning("completion.nudge_failed sid=%s reason=no_driver",
                                    session_id)
                        return
                    result = await self.send_prompt(session_id, nudge)
                    if result != "sent":
                        log.warning("completion.nudge_failed sid=%s reason=%s",
                                    session_id, result)
                except Exception:
                    log.warning("completion.nudge_failed sid=%s", session_id, exc_info=True)
                return
            context = {
                "repo": rec.repo, "goal": goal_text, "goal_source": goal_source,
                "pr_state": pr.to_prompt() if pr is not None else None,
                "reason": f"{origin or 'session'}: completion marker — goal met",
            }
        else:
            context = {
                "repo": None,
                "goal": None,
                "goal_source": "completion",
                "reason": "completion marker — goal met",
            }
        # Provenance: keep backlog-runner's existing tag; everything else is 'completion'.
        # Both are in AUTO_ARCHIVE_PROVENANCE, so the retro audit + reversal-learning
        # cover completion archives too.
        archived_by = "backlog-runner" if origin == "backlog-runner" else "completion"
        try:
            await self.archive_session(session_id, archived_by=archived_by, context=context)
        except Exception:
            log.warning("completion.autoarchive_failed id=%s", session_id, exc_info=True)
            self._auto_archiving.discard(session_id)
            return
        counts = getattr(self, "_complete_nudge_counts", None)
        if counts is not None:
            counts.pop(session_id, None)
        if origin == "backlog-runner":
            await self._backlog_ctx.emit(
                "info", f"archived completed run {session_id}",
                kind="archived", session_id=session_id)

    async def _completion_open_question(self, session_id: str) -> bool:
        """True when a completed session's final hand-back still awaits the user.

        Two sources, checked cheapest-first: (1) a structured question/permission/
        plan still pending on the driver — free, and covers the register-after-persist
        race; (2) a prose question in the last assistant message, judged by the gated
        LLM classifier (heuristic degrade) AFTER stripping the completion markers —
        the marker trails the message, so it masks a closing "?" from any tail check.
        Raises only to the caller's best-effort catch (reads as 'no question')."""
        driver = getattr(self, "_drivers", {}).get(session_id)
        if driver is not None:
            try:
                sig = driver._dash_signals()
                if sig.pending_question or sig.pending_permission or sig.pending_plan:
                    return True
            except Exception:
                log.debug("completion.question_signals_failed sid=%s",
                          session_id, exc_info=True)
        tpath = self.transcript_path(session_id)
        if not tpath:
            return False
        from .background_tasks import last_assistant_text
        from .completion import strip_markers
        from .open_question import completion_awaits_user
        text = strip_markers(last_assistant_text(tpath))
        if not text:
            return False
        rec = self._records.get(session_id)
        return await completion_awaits_user(text, cwd=getattr(rec, "cwd", None))

    async def _persist_context_usage(self, session_id: str, used_tokens: int, max_tokens: int) -> None:
        """Driver ``on_context_usage`` callback: persist the exact context-window occupancy.

        Mirrors the in-memory record + DB row so the WS bridge can replay a ``contextUsage``
        event on attach. Without this the live reading is lost on reload and the ctx% readout
        falls back to the model-string approximation — which reads 0% for a 1M opus session.
        Best-effort: a missing record/row never breaks the session loop.
        """
        rec = self._records.get(session_id)
        if rec is not None:
            rec.context_used_tokens = used_tokens
            rec.context_max_tokens = max_tokens
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id).values(
                    context_used_tokens=used_tokens, context_max_tokens=max_tokens)
            )

    async def _maybe_push(self, session_id: str, status: str) -> None:
        """Fire an outbound push alert when a session enters a top-tier needs-you status.

        Debounced: fires once per condition while it holds; cleared when the session
        transitions to a non-push-worthy status so re-entry fires again (AUT-02).
        Never raises — failures in push_mod.push are already swallowed by the sink.
        """
        if is_push_worthy(status):
            if session_id not in self._pushed:
                self._pushed.add(session_id)
                rec = self._records.get(session_id)
                name = rec.name if rec else session_id
                await push_mod.notify_event(
                    "needs_input", "ccswitch — needs you", f"{name}: {status}",
                    repo=getattr(rec, "repo", None),
                    account=getattr(rec, "account_name", None),
                    url=f"/session/{session_id}")
        else:
            self._pushed.discard(session_id)

    # ------------------------------------------------------------------
    # Backlog Runner control-loop dependencies (injected into BacklogRunnerLoop)
    # ------------------------------------------------------------------

    async def _backlog_read_config(self) -> tuple[str, dict]:
        """The backlog-runner row's (mode, config) merged onto the schema defaults.

        Mode is ``off`` unless the row is enabled AND carries a mode; an
        absent/invalid row falls back to ``off`` + pure defaults so a bad config
        never spawns.  Single source of truth for the flags the loop reads."""
        from server.automations.backlog_runner import BacklogRunner
        from server.automations.base import validate_config
        from sqlalchemy import select
        from server.db.models import Automation
        enabled, mode, raw = False, None, {}
        try:
            async with db_session() as db:
                row = (await db.execute(select(Automation).where(
                    Automation.id == "backlog-runner"))).scalar_one_or_none()
                if row is not None:
                    enabled = bool(row.enabled)
                    mode = row.mode
                    raw = dict(row.config) if row.config else {}
        except Exception:
            log.debug("backlog_runner.config.load_failed", exc_info=True)
        try:
            config = validate_config(BacklogRunner.config_schema, raw)
        except ValueError:
            config = validate_config(BacklogRunner.config_schema, {})
        effective = mode if (enabled and mode in ("propose", "auto")) else "off"
        return effective, config

    async def _backlog_count_in_flight(self) -> int:
        """Restart-safe in-flight count: active (not-given-up) durable target
        states this automation holds, read fresh from the DB every tick.

        A run occupies a slot from claim until the sweep frees it — merged /
        handed off to PR Doctor / gave-up — which is EXACTLY when the target state
        is deleted (or marked ``gave_up``). Counting active target states makes a
        freed slot recycle immediately.

        This deliberately does NOT count session rows: a spawned session never
        reaches a terminal DB status on its own (``ended`` is only set on daemon
        shutdown), so a finished-but-lingering ``done``/``idle``/``archived``
        session used to keep holding its slot forever and the runner stopped
        spawning new work. Target states are the authoritative slot ledger the
        sweep already maintains.

        A handed_off row is PR Doctor's to carry — it is kept only so the merged close-out can run, and must not occupy a slot."""
        states = await self._backlog_ctx.list_target_states()
        return sum(1 for st in states.values()
                   if not st.get("gave_up") and not st.get("handed_off"))

    async def _backlog_healthy_account(self) -> str | None:
        """A non-quarantined, non-limited account with headroom, or None.

        Reuses the same selection the interactive path uses (``select_account``
        excluding quarantined accounts, load-aware) plus the usage-ceiling guard
        (``_account_limited``). None means: pause spawning until an account frees."""
        try:
            # reserve=False: this is a read-only availability probe. Reserving here
            # would consume the picked account's reservation window without starting
            # a session, starving the real spawn (and interactive creates) when only
            # one account has headroom.
            picked = select_account(
                exclude=self._recovery.quarantined(now=self._clock()),
                load=self.live_session_load(), reserve=False)
        except AllAccountsBusy:
            return None
        if self._account_limited(picked.name):
            return None
        return picked.name

    async def _backlog_pr_status_for(self, target: str) -> PullRequestInfo | None:
        """The PullRequestInfo for a runner target, or None.

        The target key is ``owner/repo#N``. The branch is unknowable at spawn
        time (a fresh session has no branch yet), so the spawn-time snapshot in
        the target state is usually null — resolve branches at SWEEP time from
        the session itself (live record first, then the DB row's
        ``branch``/``authored_branches``) and read the poller cache for each.

        When every cached-branch lookup misses AND the session has ended — the
        exact moment the sweep would charge a failed attempt — fall back to
        asking GitHub which PRs cross-reference the issue. Gating on session
        end keeps the gh call off the per-tick hot path for live targets, and
        never matching by bare PR number keeps an unrelated PR that happens to
        share the issue's number from freeing the slot."""
        repo = target.split("#", 1)[0]
        st = await self._backlog_ctx.get_target_state(target)
        branches: list[str] = []
        if st.get("branch"):
            branches.append(st["branch"])
        sid = st.get("session_id")
        if sid:
            rec = self._records.get(sid)
            if rec is not None:
                if rec.branch:
                    branches.append(rec.branch)
                branches.extend(getattr(rec, "authored_branches", None) or [])
            else:
                from sqlalchemy import select as sa_select
                async with db_session() as db:
                    row = (await db.execute(sa_select(
                        DbSession.branch, DbSession.authored_branches,
                    ).where(DbSession.id == sid))).first()
                if row is not None:
                    if row.branch:
                        branches.append(row.branch)
                    branches.extend(row.authored_branches or [])
        for branch in dict.fromkeys(branches):
            info = self._pr_poller.cached(repo, branch)
            if info is not None:
                return info
        if sid is None or not await self._backlog_session_ended(sid):
            return None
        from server.automations import backlog_github
        return await asyncio.to_thread(
            backlog_github.find_referencing_pr, target, gh_run=self._gh_run)

    async def _pr_doctor_enabled(self) -> bool:
        """True iff the pr-doctor automation row exists and is enabled.

        Single source of truth for the enabled gate: a disabled (or missing) row
        means PR Doctor takes NO autonomous recovery action. Consulted by
        ``_pr_doctor_step`` — the one dispatcher both the per-cycle sweep and the
        ``_on_pr_change`` callback funnel through — and by
        ``_backlog_pr_doctor_active``. A DB error defaults to disabled (fail safe:
        never act when we can't confirm the toggle is on)."""
        from sqlalchemy import select
        from server.db.models import Automation
        try:
            async with db_session() as db:
                row = (await db.execute(select(Automation).where(
                    Automation.id == "pr-doctor"))).scalar_one_or_none()
            return row is not None and bool(row.enabled)
        except Exception:
            log.debug("pr_doctor.enabled_check_failed", exc_info=True)
            return False

    async def _backlog_pr_doctor_active(self, repo: str) -> bool:
        """True when PR Doctor is enabled and covers *repo* (empty scope = all).

        Read from the pr-doctor row so ``should_free_slot`` knows whether a safety
        net exists to carry the PR to auto-merge."""
        from sqlalchemy import select
        from server.db.models import Automation
        try:
            if not await self._pr_doctor_enabled():
                return False
            async with db_session() as db:
                row = (await db.execute(select(Automation).where(
                    Automation.id == "pr-doctor"))).scalar_one_or_none()
            if row is None:
                return False
            scope = (row.config or {}).get("enabled_repos") or []
            return not scope or repo in scope
        except Exception:
            log.debug("backlog_runner.pr_doctor_active_failed", exc_info=True)
            return False

    async def _backlog_select_batch(self, config: dict):
        """Real selection: label-gated ``gh`` candidate query → pop the next
        live group off the precomputed grouping plan (``backlog_groups``).

        No model call here — grouping happens in the background via
        ``_backlog_refresh_groups``; this path only intersects the stored plan
        with the live candidate pool. Returns ``None`` (the loop waits and
        nudges the optimizer) when no plan group is live."""
        from server.automations.backlog_runner import select_batch
        repo_load = await self._backlog_repo_load()
        # Repos whose clone can't fetch (stale git credentials, #670) are paused:
        # branching a worktree off their stale ref would implement against
        # ancient code. Auto-resumes when a later fetch clears the registry.
        paused = {a["repo"] for a in gitrepos.stale_credential_repos()}
        return await select_batch(config, gh_run=self._gh_run, repo_load=repo_load,
                                  paused_repos=paused)

    def _backlog_credential_alerts(self) -> list[dict]:
        """Stale-credential seam for the loop's dashboard notification (#670).

        Returns the current auth-failed-fetch repos straight from the
        (daemon-process) gitrepos registry that ``ensure_clone`` populates."""
        return gitrepos.stale_credential_repos()

    async def _backlog_refresh_groups(self, config: dict) -> int:
        """The grouping-optimizer seam: recompute + persist the group plan.

        Queries the live candidate pool, asks Sonnet (``propose_groups``) to
        partition it into coherent 1..K groups, and REPLACES the stored plan.
        An empty backlog stores an empty plan (cheap, no model call). A model
        failure over a real backlog RAISES instead of storing an empty plan, so
        the loop backs off a full grouping interval and the runner keeps
        waiting rather than losing the plan it has.

        The one-shot SDK call needs a config dir — the DAEMON process has no
        ``CLAUDE_CONFIG_DIR`` — so bind it to a healthy account (same trick the
        old inline ranking used); with no healthy account the SDK default
        applies and a failure is caught by the raise-on-empty contract."""
        from server.automations.backlog_github import query_candidates
        from server.automations.backlog_grouping import propose_groups, store_plan
        candidates = await asyncio.to_thread(query_candidates, config,
                                             gh_run=self._gh_run)
        if not candidates:
            return await store_plan([], [])
        config_dir = None
        acct = await self._backlog_healthy_account()
        if acct is not None:
            from ccswitch import paths
            config_dir = str(paths.account_cfg_dir(acct))
        groups = await propose_groups(candidates, config, _config_dir=config_dir)
        if not groups:
            raise RuntimeError("grouping model returned no plan for a non-empty backlog")
        await store_plan(groups, candidates)
        return len(groups)

    async def _backlog_repo_load(self) -> dict[str, int]:
        """Per-repo pipeline pressure, derived from durable target states — the
        input to the runner's per-repo fairness (``plan_to_batch`` sends a freed
        slot to the repo carrying the fewest).

        This deliberately DIVERGES from ``_backlog_count_in_flight``, which counts
        occupied concurrency slots: a ``handed_off`` run has freed its slot (PR
        Doctor carries the PR now) but is still THIS repo's work, in flight
        downstream until the PR merges. Counting it here is the fix for the
        KinDash-bias amplifier: a repo with a deep backlog that merges fast would
        otherwise drop its counted load to zero the instant it hands off and
        immediately re-win every freed slot ahead of a repo with nothing running.
        Load only reorders repos that both have live plan groups, so counting
        handed_off can never block or starve selection — it only stops the
        fast-handoff repo from monopolising freed slots. Terminal ``gave_up``
        (needs-human) guard rows carry no active work and stay excluded."""
        states = await self._backlog_ctx.list_target_states()
        load: dict[str, int] = {}
        for st in states.values():
            if st.get("gave_up"):
                continue
            repo = st.get("repo")
            if repo:
                load[repo] = load.get(repo, 0) + 1
        return load

    async def _backlog_claim(self, batch) -> bool:
        """Atomically claim every issue in *batch* (add the ``in-progress`` label).

        Posts a brief linking comment on each issue so a human sees who claimed
        it and can trace back to the runner via the ``origin_ref``."""
        from server.automations import backlog_github
        comment = (f"Claimed by ccswitch Backlog Runner ({batch.primary}) — "
                   f"an implementation session is in progress.")
        return await asyncio.to_thread(
            backlog_github.claim_batch, batch, gh_run=self._gh_run, comment=comment)

    async def _backlog_batch_for(self, target: str):
        """Reconstruct the claimed Batch for *target* from its durable state
        (falls back to the primary ref alone)."""
        from server.automations.backlog_selection import Batch
        st = await self._backlog_ctx.get_target_state(target)
        issues = st.get("issues") or [target]
        return Batch(primary=target, issues=list(issues),
                     repo=target.split("#", 1)[0])

    async def _backlog_release(self, target: str) -> None:
        """RELEASE-only path (spawn failure): remove ``in-progress`` and return
        the batch to the pool. Does NOT add ``needs-human`` — a transient spawn
        failure is retried, not escalated. Best-effort, off the event loop."""
        from server.automations import backlog_github
        batch = await self._backlog_batch_for(target)
        await asyncio.to_thread(
            backlog_github.unclaim_batch, batch, gh_run=self._gh_run)

    async def _backlog_needs_human(self, target: str) -> None:
        """GIVE-UP path: add ``needs-human`` and remove ``in-progress`` so a human
        picks the target up. Used only after ``max_attempts`` or a PR-Doctor
        give-up. Best-effort, off the event loop."""
        from server.automations import backlog_github
        batch = await self._backlog_batch_for(target)
        await asyncio.to_thread(
            backlog_github.label_needs_human, batch, gh_run=self._gh_run)

    async def _backlog_close_out(self, target: str, info) -> str:
        """MERGED path: close every issue in the batch with a comment linking
        the merged PR — the PR body may have lacked ``Fixes #N``, and an issue
        left open under ``in-progress``/``needs-human`` is excluded from the
        candidate pools forever. Fails CLOSED on PR↔issue linkage (#642): the
        returned outcome (``"closed"`` / ``"unlinked"`` / ``"retry"``) tells the
        sweep whether the PR really was the batch's implementation, so a
        shared-clone branch misattribution can never close unrelated issues.
        Best-effort, off the event loop."""
        from server.automations import backlog_github
        batch = await self._backlog_batch_for(target)
        return await asyncio.to_thread(
            backlog_github.close_batch_issues, batch, info, gh_run=self._gh_run)

    async def _backlog_reconcile(self, claimed_refs: list[str],
                                 live_refs: set[str]) -> None:
        """Return orphaned claims to the pool (crash-between-claim-and-spawn).

        For each durably-claimed ref with no live runner session (not in
        ``live_refs``), remove the ``in-progress`` label. Best-effort, off the
        event loop."""
        from server.automations import backlog_github
        await asyncio.to_thread(
            backlog_github.reconcile_dangling, list(claimed_refs), set(live_refs),
            gh_run=self._gh_run)

    async def _backlog_heal_orphans(self, config: dict,
                                    tracked: set[str]) -> list[tuple[str, str]]:
        """GitHub-truth orphan healing: diff the open ``in-progress`` labels in
        the enabled repos against the refs the state rows track, and heal the
        rest (close if the referencing PR merged, release the claim if nothing
        carries it). Best-effort, off the event loop; the per-pass lookup cap
        lives in ``heal_orphan_claims``."""
        from server.automations import backlog_github
        repos = list(config.get("enabled_repos") or [])
        if not repos:
            return []
        refs = await asyncio.to_thread(
            backlog_github.query_claimed_refs, repos, gh_run=self._gh_run)
        if not refs:
            return []
        return await asyncio.to_thread(
            backlog_github.heal_orphan_claims, refs, tracked, gh_run=self._gh_run)

    async def _backlog_session_ended(self, session_id: str) -> bool:
        """True once the runner session *session_id* has reached a terminal state.

        Reads the DB row (restart-safe; the in-memory record may be gone) and
        treats any terminal status — ``ended`` OR ``archived`` — as ended, so the
        sweep can charge an attempt / give up for a session that finished (and
        was completion-auto-archived) without a mergeable PR."""
        from sqlalchemy import select as _select
        async with db_session() as db:
            status = (await db.execute(_select(DbSession.status).where(
                DbSession.id == session_id))).scalar_one_or_none()
        return status in INACTIVE_SESSION_STATUSES

    async def _backlog_count_candidates(self) -> int:
        """Count of eligible, unclaimed OPEN issues in configured repos.

        The honest backlog gauge for the Automation card — independent of how many
        runner slots are free (a saturated runner still reports the real depth)."""
        from server.automations.backlog_github import query_candidates
        _mode, config = await self._backlog_read_config()
        try:
            cands = await asyncio.to_thread(query_candidates, config, gh_run=self._gh_run)
        except Exception:  # pragma: no cover — a gh failure must not break the tick
            log.warning("orchestrator._backlog_count_candidates_failed", exc_info=True)
            return 0
        return len(cands)

    async def _backlog_spawn(self, batch, account: str):
        """Spawn a de-duped runner session for *batch*, seeded with the implement
        prompt.

        Resolves the repo's ccswitch-owned clone ``cwd`` off the event loop
        (mirrors PR Doctor's spawn), then spawns via the de-dup-safe context
        spawner (stamps origin/origin_ref, enforces one session per primary, and
        emits ``spawned`` with ``detail.issues``). Returns the SessionRecord."""
        from server.automations.backlog_prompt import build_implement_prompt
        from server.automations.model_pick import resolve_model
        url = f"https://github.com/{batch.repo}.git"
        try:
            default_branch = await asyncio.to_thread(
                lambda: gitrepos.list_remote_branches(batch.repo).get("default") or "master"
            )
        except Exception:
            default_branch = "master"
        cwd = await asyncio.to_thread(
            gitrepos.ensure_clone, await self._effective_repo_root(), batch.repo, url)
        _mode, config = await self._backlog_read_config()
        model, model_source = await resolve_model(config, "\n".join(
            f"{ref}  {batch.titles.get(ref) or '(no title)'}" for ref in batch.issues))
        detail: dict = {"issues": batch.issues}
        if model is not None:
            detail.update(model=model, model_source=model_source)
        return await self._backlog_ctx.spawn_session(
            origin_ref=batch.primary,
            dedupe_key=batch.primary,
            repo=batch.repo,
            cwd=cwd,
            account=account,
            name=f"backlog {batch.primary}",
            initial_prompt=build_implement_prompt(batch, default_branch),
            model=model,
            detail=detail,
        )

    async def backlog_runner_implement_core(self, primary: str) -> str:
        """Propose-tap: claim (if needed) + spawn the batch for *primary*.

        Re-queries the current label-gated candidate set, pins a batch on
        *primary*, claims it (unless already claimed), resolves a healthy account,
        and spawns the de-duped runner session. Returns a status string:
        ``"spawned"`` / ``"unknown"`` (not a current candidate) /
        ``"claim_failed"`` / ``"no_account"``."""
        from server.automations.backlog_github import query_candidates
        from server.automations.backlog_selection import Batch
        _mode, config = await self._backlog_read_config()
        # Query the current candidate set and pin the batch on the requested
        # primary (the tap targets one issue explicitly; no re-ranking needed).
        candidates = await asyncio.to_thread(
            query_candidates, config, gh_run=self._gh_run)
        by_ref = {c["ref"]: c for c in candidates}
        if primary not in by_ref:
            return "unknown"
        batch = Batch(primary=primary, issues=[primary], repo=by_ref[primary]["repo"],
                      titles={primary: t} if (t := by_ref[primary].get("title")) else {})
        st = await self._backlog_ctx.get_target_state(primary)
        already_claimed = bool(st.get("issues"))
        if not already_claimed:
            # RECORDS-BEFORE-LABELS: write the durable claim row BEFORE the gh
            # label write so a crash after the label lands still leaves a
            # recoverable state row (reconciliation sees a claimed ref with no
            # session_id and no proposed flag → dangler → released).
            await self._backlog_ctx.set_target_state(primary, {
                "repo": batch.repo,
                "attempts": st.get("attempts", 0),
                "issues": batch.issues,
                "claimed": True,
            })
            if not await self._backlog_claim(batch):
                # Never actually held the claim → drop the speculative row.
                await self._backlog_ctx.delete_target_state(primary)
                return "claim_failed"
            await self._backlog_ctx.emit("info", f"claimed {primary}",
                                         kind="claimed", target=primary,
                                         detail={"issues": batch.issues})
        elif st.get("proposed"):
            # Tapping a propose-mode pending claim: clear the proposed flag so
            # reconciliation sees a normal claimed row after the session_id is set.
            new_st = dict(st)
            new_st.pop("proposed", None)
            await self._backlog_ctx.set_target_state(primary, new_st)
        account = await self._backlog_healthy_account()
        if account is None:
            return "no_account"
        rec = await self._backlog_spawn(batch, account)
        # A de-dup hit returns the existing row without appending to spawns; treat
        # a matching origin_ref that already existed as "exists".
        await self._backlog_ctx.set_target_state(primary, {
            "session_id": getattr(rec, "id", None),
            "repo": batch.repo,
            "branch": getattr(rec, "branch", None),
            "attempts": st.get("attempts", 0),
            "issues": batch.issues,
            "claimed": True,
        })
        return "spawned"

    async def backlog_candidates(self) -> list[dict]:
        """Current label-gated backlog candidate dicts ({ref, repo, title, url})."""
        from server.automations.backlog_github import query_candidates
        _mode, config = await self._backlog_read_config()
        return await asyncio.to_thread(query_candidates, config, gh_run=self._gh_run)

    async def backlog_runner_dispatch(self, refs: list[str]) -> dict:
        """Manual dispatch: claim + spawn a batch for *refs*.

        *refs[0]* is the primary; all refs are bundled into one PR. Does NOT
        require refs to be in the current candidate set — picker state may lag
        by seconds and blocking an explicit user request is worse than a missing
        title. Returns ``{"status": ..., "session_id": ..., "session_name": ...}``."""
        from server.automations.backlog_github import query_candidates
        from server.automations.backlog_selection import Batch
        if not refs:
            return {"status": "no_refs", "session_id": None, "session_name": None}
        primary = refs[0]
        _mode, config = await self._backlog_read_config()
        candidates = await asyncio.to_thread(
            query_candidates, config, gh_run=self._gh_run)
        by_ref = {c["ref"]: c for c in candidates}
        repo = by_ref.get(primary, {}).get("repo") or primary.split("#")[0]
        titles = {r: by_ref[r]["title"]
                  for r in refs if r in by_ref and by_ref[r].get("title")}
        batch = Batch(primary=primary, issues=list(refs), repo=repo, titles=titles)
        st = await self._backlog_ctx.get_target_state(primary)
        already_claimed = bool(st.get("issues"))
        if not already_claimed:
            await self._backlog_ctx.set_target_state(primary, {
                "repo": batch.repo,
                "attempts": st.get("attempts", 0),
                "issues": batch.issues,
                "claimed": True,
            })
            if not await self._backlog_claim(batch):
                await self._backlog_ctx.delete_target_state(primary)
                return {"status": "claim_failed", "session_id": None,
                        "session_name": None}
            await self._backlog_ctx.emit("info", f"manual dispatch {primary}",
                                         kind="claimed", target=primary,
                                         detail={"issues": batch.issues})
        account = await self._backlog_healthy_account()
        if account is None:
            return {"status": "no_account", "session_id": None,
                    "session_name": None}
        rec = await self._backlog_spawn(batch, account)
        await self._backlog_ctx.set_target_state(primary, {
            "session_id": getattr(rec, "id", None),
            "repo": batch.repo,
            "branch": getattr(rec, "branch", None),
            "attempts": st.get("attempts", 0),
            "issues": batch.issues,
            "claimed": True,
        })
        return {
            "status": "spawned",
            "session_id": getattr(rec, "id", None),
            "session_name": getattr(rec, "name", None),
        }

    async def _backlog_tick_loop(self, interval: float) -> None:
        """Periodic driver for the backlog-runner control loop. Runs a tick every
        *interval* seconds while the automation is enabled; a disabled row makes
        each tick idle-and-sweep (mode=='off') cheaply."""
        while True:
            await self._backlog_tick_once()
            await asyncio.sleep(interval)

    async def _backlog_tick_once(self) -> None:
        """One control-loop tick plus a ``last_active_at`` heartbeat. Extracted
        from the interval loop so tests can drive a single iteration.

        The heartbeat fires only while the automation is ENABLED (effective mode
        != 'off'), giving the card a fresh "last active" the way pr-doctor already
        gets one; a disabled row is intentionally left to go stale. Best-effort —
        neither the tick nor the heartbeat may crash the loop."""
        if not await self._schedule_allows_tick("backlog-runner"):
            return
        try:
            await self._backlog_loop.tick()
        except Exception:  # pragma: no cover — tick() already swallows; belt+braces
            log.warning("backlog_runner.tick_loop_error", exc_info=True)
        try:
            mode, _ = await self._backlog_read_config()
            if mode != "off":
                await self._bump_automation_active("backlog-runner")
        except Exception:  # pragma: no cover — a heartbeat failure must not stall
            log.debug("backlog_runner.bump_active_failed", exc_info=True)

    # ------------------------------------------------------------------
    # SDLC Intake control-loop dependencies (injected into SdlcIntakeLoop)
    # ------------------------------------------------------------------

    async def _sdlc_intake_read_config(self) -> tuple[str, dict]:
        """The sdlc-intake row's (mode, config) merged onto schema defaults.

        Mode is ``off`` unless the row is enabled AND carries propose/auto — an
        absent/invalid row falls back to ``off`` + pure defaults so a bad config
        never spawns. Mirrors ``_backlog_read_config`` (single source of truth)."""
        from sqlalchemy import select
        from server.automations.base import validate_config
        from server.automations.sdlc_intake import SdlcIntake
        from server.db.models import Automation
        enabled, mode, raw = False, None, {}
        try:
            async with db_session() as db:
                row = (await db.execute(select(Automation).where(
                    Automation.id == "sdlc-intake"))).scalar_one_or_none()
                if row is not None:
                    enabled = bool(row.enabled)
                    mode = row.mode
                    raw = dict(row.config) if row.config else {}
        except Exception:
            log.debug("sdlc_intake.config.load_failed", exc_info=True)
        try:
            config = validate_config(SdlcIntake.config_schema, raw)
        except ValueError:
            config = validate_config(SdlcIntake.config_schema, {})
        effective = mode if (enabled and mode in ("propose", "auto")) else "off"
        return effective, config

    async def _sdlc_intake_read_triggers(self) -> list:
        """Pending intake triggers. Empty until a trigger source is wired: the
        Phase-2 triage queue (#804) and the /api/sdlc intake path (#802) feed
        this seam. Returning [] keeps every tick a cheap no-op today."""
        return []

    async def _sdlc_intake_ensure_initiative(self, trigger):
        """Find-or-create the Initiative for *trigger* (idempotent by slug) at the
        intake stage, and return its pure view + committed-or-proposed artifacts."""
        from sqlalchemy import select
        from server.db.models import Initiative, InitiativeArtifact
        from server.sdlc.engine import ArtifactView, InitiativeView
        async with db_session() as db:
            row = (await db.execute(select(Initiative).where(
                Initiative.slug == trigger.slug))).scalar_one_or_none()
            if row is None:
                row = Initiative(
                    slug=trigger.slug, repo=trigger.repo or None,
                    title=trigger.title or None, stage="intake", status="active",
                    origin=trigger.origin, origin_ref=trigger.origin_ref)
                db.add(row)
                await db.flush()
            arts = (await db.execute(select(InitiativeArtifact).where(
                InitiativeArtifact.initiative_id == row.id))).scalars().all()
            view = InitiativeView(
                slug=row.slug or trigger.slug, stage=row.stage or "intake",
                status=row.status or "active", repo=row.repo or "")
            artifacts = [
                ArtifactView(kind=a.kind or "", commit_sha=a.commit_sha,
                             pr_number=a.pr_number, pr_url=a.pr_url) for a in arts]
        return view, artifacts

    async def _sdlc_intake_record_intent(self, slug: str, repo_path: str,
                                         session_id: str | None) -> None:
        """Insert the intent artifact row for *slug* (kind=intent, PR pending)."""
        from sqlalchemy import select
        from server.db.models import Initiative, InitiativeArtifact
        async with db_session() as db:
            row = (await db.execute(select(Initiative).where(
                Initiative.slug == slug))).scalar_one_or_none()
            if row is None:
                log.warning("sdlc_intake.record_intent.no_initiative slug=%s", slug)
                return
            db.add(InitiativeArtifact(
                initiative_id=row.id, kind="intent", repo_path=repo_path,
                session_id=session_id))

    async def _sdlc_intake_spawn(self, trigger, prompt: str):
        """Spawn a de-duped intent session for *trigger*, seeded with *prompt*.

        Resolves the repo clone cwd + a healthy account off the event loop, then
        spawns via the de-dup-safe context spawner (origin_ref=``<slug>:intent``);
        ``create_session`` falls back to its own account/cwd if these are None."""
        account = await self._backlog_healthy_account()
        cwd = None
        if trigger.repo:
            url = f"https://github.com/{trigger.repo}.git"
            try:
                cwd = await asyncio.to_thread(
                    gitrepos.ensure_clone, await self._effective_repo_root(),
                    trigger.repo, url)
            except Exception:
                log.warning("sdlc_intake.spawn.clone_failed repo=%s", trigger.repo,
                            exc_info=True)
        return await self._sdlc_intake_ctx.spawn_session(
            origin_ref=f"{trigger.slug}:intent",
            dedupe_key=f"{trigger.slug}:intent",
            repo=trigger.repo or None,
            cwd=cwd,
            account=account,
            name=f"sdlc intake {trigger.slug}",
            initial_prompt=prompt,
            detail={"slug": trigger.slug, "kind": "intent"},
        )

    async def _sdlc_intake_tick_loop(self, interval: float) -> None:
        """Periodic driver for the intake control loop (mirrors _backlog_tick_loop)."""
        while True:
            await self._sdlc_intake_tick_once()
            await asyncio.sleep(interval)

    async def _sdlc_intake_tick_once(self) -> None:
        """One intake tick + an enabled-only ``last_active_at`` heartbeat.
        Best-effort — neither the tick nor the heartbeat may crash the loop."""
        if not await self._schedule_allows_tick("sdlc-intake"):
            return
        try:
            await self._sdlc_intake_loop.tick()
        except Exception:  # pragma: no cover — tick() already swallows; belt+braces
            log.warning("sdlc_intake.tick_loop_error", exc_info=True)
        try:
            mode, _ = await self._sdlc_intake_read_config()
            if mode != "off":
                await self._bump_automation_active("sdlc-intake")
        except Exception:  # pragma: no cover — a heartbeat failure must not stall
            log.debug("sdlc_intake.bump_active_failed", exc_info=True)

    # ------------------------------------------------------------------
    # PM Sync control-loop dependencies (injected into PmSyncLoop)
    # ------------------------------------------------------------------

    @staticmethod
    def _pm_sync_now_utc():
        """Return the current UTC time (injectable seam for tests)."""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)

    async def _pm_sync_read_config(self) -> tuple[str, dict]:
        """The pm-sync row's (mode, config) merged onto schema defaults.

        Mode is ``off`` unless the row is enabled AND carries ``on`` — an
        absent/invalid row falls back to ``off`` + pure defaults so a bad config
        never starts syncing. Mirrors ``_sdlc_intake_read_config`` exactly."""
        from sqlalchemy import select
        from server.automations.base import validate_config
        from server.automations.pm_sync import PmSync
        from server.db.models import Automation
        enabled, mode, raw = False, None, {}
        try:
            async with db_session() as db:
                row = (await db.execute(select(Automation).where(
                    Automation.id == "pm-sync"))).scalar_one_or_none()
                if row is not None:
                    enabled = bool(row.enabled)
                    mode = row.mode
                    raw = dict(row.config) if row.config else {}
        except Exception:
            log.debug("pm_sync.config.load_failed", exc_info=True)
        try:
            config = validate_config(PmSync.config_schema, raw)
        except ValueError:
            config = validate_config(PmSync.config_schema, {})
        effective = mode if (enabled and mode == "on") else "off"
        return effective, config

    async def _pm_sync_list_repos(self) -> list:
        """Query every ProjectRepo (+ owning Project slug) and return views."""
        from sqlalchemy import select
        from server.automations.pm_sync import RepoSyncView
        from server.db.models import Project, ProjectRepo
        async with db_session() as db:
            rows = (await db.execute(
                select(ProjectRepo, Project.slug)
                .join(Project, Project.id == ProjectRepo.project_id)
            )).all()
        return [RepoSyncView(
            project_id=pr.project_id, project_slug=slug or "",
            repo=pr.repo or "", is_primary=bool(pr.is_primary),
            backfill_done=bool(pr.backfill_done),
            backfill_phase=pr.backfill_phase or "issues",
            backfill_cursor=pr.backfill_cursor, watermark=pr.watermark,
            last_synced_at=pr.last_synced_at,
            sync_requested_at=pr.sync_requested_at,
        ) for pr, slug in rows]

    async def _pm_sync_fetch(self, query: str) -> dict:
        """Run a GraphQL query via the gh seam. GraphQL only — never REST."""
        import json
        proc = await asyncio.to_thread(
            self._gh_run, ["api", "graphql", "-f", f"query={query}"])
        if getattr(proc, "returncode", 1) != 0:
            raise RuntimeError(
                f"gh graphql failed: {(getattr(proc, 'stderr', '') or '').strip()[:500]}")
        return json.loads(getattr(proc, "stdout", "") or "{}")

    async def _pm_sync_upsert_items(self, items: list) -> None:
        """Idempotent upsert into work_items keyed by (repo, number, kind)."""
        from datetime import datetime, timezone
        from sqlalchemy import select
        from server.db.models import WorkItem
        async with db_session() as db:
            for item in items:
                existing = (await db.execute(
                    select(WorkItem).where(
                        WorkItem.repo == item.repo,
                        WorkItem.number == item.number,
                        WorkItem.kind == item.kind,
                    )
                )).scalar_one_or_none()
                now = datetime.now(timezone.utc)
                if existing is None:
                    existing = WorkItem(repo=item.repo, number=item.number,
                                       kind=item.kind)
                    db.add(existing)
                existing.title = item.title
                existing.body = item.body
                existing.state = item.state
                existing.labels = list(item.labels)
                existing.author = item.author
                existing.url = item.url
                existing.linked_issue_numbers = list(item.linked_issue_numbers)
                existing.gh_created_at = item.gh_created_at
                existing.gh_updated_at = item.gh_updated_at
                existing.gh_closed_at = item.gh_closed_at
                existing.last_synced_at = now

    async def _pm_sync_save_state(self, view, **updates) -> None:
        """Map keyword updates onto the ProjectRepo row for *view*."""
        from sqlalchemy import select
        from server.db.models import ProjectRepo
        async with db_session() as db:
            row = (await db.execute(
                select(ProjectRepo).where(
                    ProjectRepo.project_id == view.project_id,
                    ProjectRepo.repo == view.repo,
                )
            )).scalar_one_or_none()
            if row is None:
                return
            for key, value in updates.items():
                setattr(row, key, value)

    async def _pm_sync_read_feature_docs(self, repo: str) -> list[tuple[str, str]]:
        """Read docs/product/features/*.md at origin's default branch in the
        shared clone. Uses git plumbing only — no GitHub API cost.

        ATOMIC-OR-RAISE. The read resolves the default branch to an IMMUTABLE
        commit SHA once, then reads every blob against THAT snapshot — so a
        concurrent fetch moving origin/master (the shared clone is touched by
        many automations) can't make the per-doc reads race. Any failure —
        clone, ref resolution, ls-tree, or a single unreadable blob — RAISES
        rather than returning a subset. Returning a subset is what orphaned 21
        live features in the dogfood: the caller (``_feature_index``) treats a
        missing doc as deleted and orphans its row. A raise makes the caller
        skip the pass instead, leaving the index untouched until a clean read."""

        async def _read() -> list[tuple[str, str]]:
            import subprocess
            repo_root = await self._effective_repo_root()
            url = f"https://github.com/{repo}.git"
            # A clone failure must NOT degrade to an empty list — that would
            # orphan every indexed feature. Let it raise so the pass is skipped.
            clone = await asyncio.to_thread(
                gitrepos.ensure_clone, repo_root, repo, url)
            await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", clone, "fetch", "origin", "--quiet"],
                check=False, capture_output=True, timeout=120,
            )
            head = await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", clone, "symbolic-ref", "--quiet",
                 "refs/remotes/origin/HEAD"],
                capture_output=True, text=True, timeout=30,
            )
            ref = head.stdout.strip() or "refs/remotes/origin/master"
            # Pin the ref to a concrete commit so every blob read below is against
            # one consistent snapshot, immune to a concurrent ref-moving fetch.
            rp = await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", clone, "rev-parse", "--verify", f"{ref}^{{commit}}"],
                capture_output=True, text=True, timeout=30,
            )
            if rp.returncode != 0:
                raise RuntimeError(
                    f"pm_sync.read_feature_docs: cannot resolve {ref} in {repo}: "
                    f"{(rp.stderr or '').strip()[:200]}")
            sha = rp.stdout.strip()
            ls = await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", clone, "ls-tree", "-r", "--name-only", sha,
                 "docs/product/features/"],
                capture_output=True, text=True, timeout=30,
            )
            if ls.returncode != 0:
                raise RuntimeError(
                    f"pm_sync.read_feature_docs: ls-tree failed for {repo}: "
                    f"{(ls.stderr or '').strip()[:200]}")
            out: list[tuple[str, str]] = []
            for path in ls.stdout.splitlines():
                if not path.endswith(".md"):
                    continue
                show = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "-C", clone, "show", f"{sha}:{path}"],
                    capture_output=True, text=True, timeout=30,
                )
                if show.returncode != 0:
                    # One unreadable blob makes the whole read untrustworthy —
                    # raise so the caller skips rather than orphaning the rest.
                    raise RuntimeError(
                        f"pm_sync.read_feature_docs: read failed for {path} in "
                        f"{repo}: {(show.stderr or '').strip()[:200]}")
                out.append((path, show.stdout))
            return out

        return await _read()

    async def _pm_sync_list_feature_rows(self, project_id: str) -> list:
        """Return FeatureRowView for every Feature belonging to *project_id*."""
        from sqlalchemy import select
        from server.db.models import Feature
        from server.pm.feature_index import FeatureRowView
        async with db_session() as db:
            rows = (await db.execute(
                select(Feature).where(Feature.project_id == project_id)
            )).scalars().all()
        return [FeatureRowView(slug=f.slug or "", orphaned=bool(f.orphaned))
                for f in rows]

    async def _pm_sync_apply_feature_plan(
        self, project_id: str, plan, by_slug: dict
    ) -> None:
        """Write upserts/orphans from *plan* into the Feature table.

        After upserting, if the project is in ``map_proposed`` and now has ≥1
        non-orphaned Feature, advance it to ``map_merged``.  This ties the
        transition to the observable "map merged & indexed" state — no PR
        webhook needed.  No other states are touched."""
        from sqlalchemy import func, select
        from server.db.models import Feature, Project
        async with db_session() as db:
            for up in plan.upserts:
                row = (await db.execute(
                    select(Feature).where(
                        Feature.project_id == project_id,
                        Feature.slug == up.slug,
                    )
                )).scalar_one_or_none()
                if row is None:
                    row = Feature(project_id=project_id, slug=up.slug)
                    db.add(row)
                row.title = up.title
                row.state = up.state
                row.summary = up.summary
                row.repo_path = up.repo_path
                row.orphaned = False
            for slug in plan.orphan_slugs:
                row = (await db.execute(
                    select(Feature).where(
                        Feature.project_id == project_id,
                        Feature.slug == slug,
                    )
                )).scalar_one_or_none()
                if row is not None:
                    row.orphaned = True
            # Advance map_proposed → map_merged when ≥1 non-orphaned feature
            # exists after this upsert.  Other states are intentionally left alone.
            proj = (await db.execute(
                select(Project).where(Project.id == project_id)
            )).scalar_one_or_none()
            if proj is not None and proj.onboarding_state == "map_proposed":
                live_count = (await db.execute(
                    select(func.count(Feature.id)).where(
                        Feature.project_id == project_id,
                        Feature.orphaned.is_not(True),
                    )
                )).scalar_one()
                if live_count >= 1:
                    proj.onboarding_state = "map_merged"

    async def _pm_sync_tick_loop(self, interval: float) -> None:
        """Periodic driver for the pm-sync control loop (mirrors _sdlc_intake_tick_loop)."""
        while True:
            await self._pm_sync_tick_once()
            await asyncio.sleep(interval)

    async def _pm_sync_tick_once(self) -> None:
        """One pm-sync tick + an enabled-only ``last_active_at`` heartbeat.
        Best-effort — neither the tick nor the heartbeat may crash the loop."""
        if not await self._schedule_allows_tick("pm-sync"):
            return
        try:
            await self._pm_sync_loop.tick()
        except Exception:  # pragma: no cover — tick() already swallows; belt+braces
            log.warning("pm_sync.tick_loop_error", exc_info=True)
        try:
            mode, _ = await self._pm_sync_read_config()
            if mode != "off":
                await self._bump_automation_active("pm-sync")
        except Exception:  # pragma: no cover — a heartbeat failure must not stall
            log.debug("pm_sync.bump_active_failed", exc_info=True)

    # ---------------------- pm onboarding seams (2026-08-31) --------------- #

    async def pm_onboard_spawn_map(
            self, slug: str, *, scope: str = "initial",
            suggestion: str | None = None,
            item_numbers: tuple[int, ...] = (),
            repo: str | None = None):
        """ONE parameterised map-synthesis spawn seam (phase 3 reuses it with
        ``scope="diff"`` — there is no separate diff-spawn method).

        ``scope="initial"`` (default): spawn ONE archaeology map-synthesis
        session for *slug*'s primary repo and move the project to
        ``map_proposed``. Deduped on ``<slug>:feature-map`` so a re-tap does not
        double-spawn.

        ``scope="diff"``: spawn a diff-scoped session whose prompt names
        ``suggestion`` + ``item_numbers``, deduped ``<slug>:map-diff:<suggestion>``,
        and does NOT touch ``onboarding_state`` (phase-3 drift path). ``repo``
        overrides the resolved primary repo when supplied.

        Returns the SessionRecord, or None when no repo / no healthy account.
        Propose-only: the spawned session opens ONE non-draft PR and NEVER arms
        auto-merge — the merge is the human curation gate."""
        from sqlalchemy import func, select
        from server.db.models import Project, ProjectRepo, WorkItem
        from server.pm.classification import build_map_synthesis_prompt
        async with db_session() as db:
            proj = (await db.execute(select(Project).where(
                Project.slug == slug))).scalar_one_or_none()
            if proj is None:
                return None
            resolved_repo = repo
            if resolved_repo is None:
                repo_row = (await db.execute(select(ProjectRepo).where(
                    ProjectRepo.project_id == proj.id,
                    ProjectRepo.is_primary.is_(True)))).scalar_one_or_none()
                if repo_row is None or not repo_row.repo:
                    return None
                resolved_repo = repo_row.repo
            open_count = (await db.execute(select(func.count(WorkItem.id)).where(
                WorkItem.repo == resolved_repo,
                WorkItem.state == "open"))).scalar_one()
            closed_count = (await db.execute(select(func.count(WorkItem.id)).where(
                WorkItem.repo == resolved_repo,
                WorkItem.state != "open"))).scalar_one()
            project_id = proj.id
        repo = resolved_repo
        account = await self._backlog_healthy_account()
        if account is None:
            return None
        url = f"https://github.com/{repo}.git"
        cwd = await asyncio.to_thread(
            gitrepos.ensure_clone, await self._effective_repo_root(), repo, url)
        default_branch = await self._sdlc_default_branch(repo)
        prompt = build_map_synthesis_prompt(
            slug=slug, repo=repo, default_branch=default_branch,
            open_count=int(open_count), closed_count=int(closed_count),
            scope=scope, suggestion=suggestion, item_numbers=item_numbers)
        if scope == "diff":
            origin_ref = f"{slug}:map-diff:{suggestion}"
            name = f"pm map-diff {slug}"
            detail: dict = {"slug": slug, "kind": "feature-map-diff",
                            "suggestion": suggestion,
                            "itemNumbers": list(item_numbers)}
        else:
            origin_ref = f"{slug}:feature-map"
            name = f"pm map {slug}"
            detail = {"slug": slug, "kind": "feature-map"}
        rec = await self._pm_onboard_ctx.spawn_session(
            origin_ref=origin_ref,
            dedupe_key=origin_ref,
            repo=repo, cwd=cwd, account=account,
            name=name, initial_prompt=prompt, detail=detail)
        if rec is not None and scope == "initial":
            async with db_session() as db:
                proj = (await db.execute(select(Project).where(
                    Project.id == project_id))).scalar_one_or_none()
                if proj is not None:
                    proj.onboarding_state = "map_proposed"
                    await db.commit()
        return rec

    async def _pm_onboard_spawn_critic(self, slug: str, repo: str,
                                       branch: str):
        """Independent completeness-critic session on the open map PR branch
        (design §3.2). Internal-only — spawned by the chain after synthesis; never
        reached by the request layer, so it stays private (no facade/RPC forward).
        Propose-only: augments the SAME PR, never opens a new one."""
        from server.pm.classification import build_coverage_critic_prompt
        account = await self._backlog_healthy_account()
        if account is None:
            return None
        url = f"https://github.com/{repo}.git"
        cwd = await asyncio.to_thread(
            gitrepos.ensure_clone, await self._effective_repo_root(), repo, url)
        default_branch = await self._sdlc_default_branch(repo)
        prompt = build_coverage_critic_prompt(
            slug=slug, repo=repo, default_branch=default_branch, branch=branch,
            open_count=0, closed_count=0)
        origin_ref = f"{slug}:feature-map-critic"
        return await self._pm_onboard_ctx.spawn_session(
            origin_ref=origin_ref, dedupe_key=origin_ref, repo=repo, cwd=cwd,
            account=account, name=f"pm map-critic {slug}", initial_prompt=prompt,
            detail={"slug": slug, "kind": "feature-map-critic", "branch": branch})

    async def pm_classify_run(
            self, slug: str, *,
            items: list[tuple[str, int, str]] | None = None) -> int:
        """One classify-spawn seam (phase 3 reuses it with an injected batch).

        With ``items=None`` (default, phase-2 onboarding path) it self-selects up
        to CLASSIFY_CHUNK_SIZE open, unclassified items with no pending proposal.
        With ``items=[(repo, number, kind), ...]`` it classifies exactly that
        caller-supplied set (phase-3 loop path). Moves the project to
        ``classifying``. Returns the item count dispatched (0 if none/no account).
        Closed history is evidence, not classified (spec §5)."""
        from sqlalchemy import select, tuple_ as sa_tuple
        from server.db.models import (
            Feature, Project, ProjectRepo, TriageProposal, WorkItem)
        from server.pm.classification import (
            CLASSIFY_CHUNK_SIZE, FeatureBrief, WorkItemBrief,
            build_classification_prompt)
        async with db_session() as db:
            proj = (await db.execute(select(Project).where(
                Project.slug == slug))).scalar_one_or_none()
            if proj is None:
                return 0
            repos = (await db.execute(select(ProjectRepo.repo).where(
                ProjectRepo.project_id == proj.id))).scalars().all()
            repo_names = [r for r in repos if r]
            primary = (await db.execute(select(ProjectRepo).where(
                ProjectRepo.project_id == proj.id,
                ProjectRepo.is_primary.is_(True)))).scalar_one_or_none()
            if items is not None:
                # Phase-3 injected batch: classify exactly the caller's set.
                if not items:
                    return 0
                keys = [(r, n) for (r, n, _k) in items]
                chunk = (await db.execute(select(WorkItem).where(
                    sa_tuple(WorkItem.repo, WorkItem.number).in_(keys)
                ))).scalars().all()
                if not chunk:
                    return 0
            else:
                pending_ids = set((await db.execute(select(
                    TriageProposal.work_item_id).where(
                        TriageProposal.status == "pending"))).scalars().all())
                selected = (await db.execute(select(WorkItem).where(
                    WorkItem.repo.in_(repo_names), WorkItem.state == "open",
                    WorkItem.triage_state == "unclassified"
                ).order_by(WorkItem.gh_updated_at.desc()).limit(
                    CLASSIFY_CHUNK_SIZE * 2))).scalars().all()
                chunk = [w for w in selected
                         if w.id not in pending_ids][:CLASSIFY_CHUNK_SIZE]
                if not chunk:
                    return 0
            features = (await db.execute(select(Feature).where(
                Feature.project_id == proj.id,
                Feature.orphaned.isnot(True)))).scalars().all()
            briefs = [WorkItemBrief(
                number=w.number or 0, kind=w.kind or "issue",
                title=w.title or "", body=w.body or "",
                labels=tuple(w.labels or []), url=w.url or "") for w in chunk]
            feat_briefs = [FeatureBrief(
                slug=f.slug or "", title=f.title or "",
                summary=f.summary or "") for f in features]
            repo = (primary.repo if primary else repo_names[0]) if repo_names else ""
            project_id = proj.id
        account = await self._backlog_healthy_account()
        if account is None:
            return 0
        prompt = build_classification_prompt(items=briefs, features=feat_briefs)
        cwd = None
        if repo:
            url = f"https://github.com/{repo}.git"
            cwd = await asyncio.to_thread(
                gitrepos.ensure_clone, await self._effective_repo_root(), repo, url)
        import hashlib as _hashlib
        _chunk_key = _hashlib.sha1(
            ",".join(f"{w.repo}#{w.number}" for w in sorted(
                chunk, key=lambda w: (w.repo or "", w.number or 0))
            ).encode()).hexdigest()[:12]
        rec = await self._pm_classify_ctx.spawn_session(
            origin_ref=f"{slug}:classify:{_chunk_key}",
            dedupe_key=f"{slug}:classify:{_chunk_key}",
            repo=repo or None, cwd=cwd, account=account,
            name=f"pm classify {slug}", initial_prompt=prompt,
            detail={"slug": slug, "kind": "classify", "count": len(chunk)})
        if rec is None:
            return 0
        async with db_session() as db:
            proj = (await db.execute(select(Project).where(
                Project.id == project_id))).scalar_one_or_none()
            if proj is not None and proj.onboarding_state in (
                    "map_merged", "map_proposed"):
                proj.onboarding_state = "classifying"
                await db.commit()
        return len(chunk)

    async def _pm_classify_ingest(
            self, session_id: str, project_slug: str, output_text: str) -> int:
        """Turn a classify session's output into TriageProposal rows. Resolves
        each feature_slug to a Feature.id within the project; skips a row whose
        work item is missing or already has a pending proposal (idempotent
        re-ingest). Returns rows written.

        For each written row, sets ``WorkItem.triage_state = "proposed"`` as a
        scheduling marker (mirrors the fact that a proposal now exists). NEVER
        touches the SCORE overlay columns (``feature_id``, ``reach``, ``impact``,
        ``effort``, ``confidence``, ``classified_by``, ``classified_at``) — those
        are written only when a human accepts the proposal."""
        from sqlalchemy import select
        from server.db.models import (
            Feature, Project, ProjectRepo, TriageProposal, WorkItem)
        from server.pm.classification import (
            ClassificationParseError, parse_classification_output)
        try:
            rows = parse_classification_output(output_text)
        except ClassificationParseError:
            log.warning("pm_classify.parse_failed session=%s", session_id,
                        exc_info=True)
            return 0
        written = 0
        async with db_session() as db:
            proj = (await db.execute(select(Project).where(
                Project.slug == project_slug))).scalar_one_or_none()
            if proj is None:
                return 0
            repo_names = [r for r in (await db.execute(select(
                ProjectRepo.repo).where(
                    ProjectRepo.project_id == proj.id))).scalars().all() if r]
            feat_by_slug = {f.slug: f.id for f in (await db.execute(select(
                Feature).where(Feature.project_id == proj.id))).scalars().all()}
            for row in rows:
                wi = (await db.execute(select(WorkItem).where(
                    WorkItem.repo.in_(repo_names),
                    WorkItem.number == row.number,
                    WorkItem.kind == row.kind))).scalar_one_or_none()
                if wi is None:
                    continue
                dup = (await db.execute(select(TriageProposal.id).where(
                    TriageProposal.work_item_id == wi.id,
                    TriageProposal.status == "pending"))).scalar_one_or_none()
                if dup is not None:
                    continue
                db.add(TriageProposal(
                    work_item_id=wi.id,
                    proposed_feature_id=feat_by_slug.get(row.feature_slug)
                    if row.feature_slug else None,
                    no_fit_suggestion=row.no_fit_suggestion,
                    reach=row.reach, impact=row.impact, effort=row.effort,
                    confidence=row.confidence, rationale=row.rationale,
                    confidence_level=row.confidence_level,
                    status="pending", session_id=session_id))
                wi.triage_state = "proposed"
                written += 1
            if written:
                await db.commit()
        return written

    async def pm_onboarding_counts(self, project_id: str) -> ProjectOnboardingView:
        """Build the onboarding engine's view for a project in one grouped read
        (used by the API's wizard-state endpoint)."""
        from sqlalchemy import func, select
        from server.db.models import (
            Feature, Project, ProjectRepo, TriageProposal, WorkItem)
        from server.pm.onboarding import ProjectOnboardingView
        async with db_session() as db:
            repos = (await db.execute(select(ProjectRepo).where(
                ProjectRepo.project_id == project_id))).scalars().all()
            repo_names = [r.repo for r in repos if r.repo]
            feature_count = (await db.execute(select(func.count(Feature.id)).where(
                Feature.project_id == project_id,
                Feature.orphaned.isnot(True)))).scalar_one()
            open_unclassified = 0
            pending = 0
            if repo_names:
                open_unclassified = (await db.execute(select(
                    func.count(WorkItem.id)).where(
                        WorkItem.repo.in_(repo_names), WorkItem.state == "open",
                        WorkItem.triage_state == "unclassified"))).scalar_one()
                pending = (await db.execute(select(func.count(
                    TriageProposal.id)).join(
                    WorkItem, WorkItem.id == TriageProposal.work_item_id).where(
                        WorkItem.repo.in_(repo_names),
                        TriageProposal.status == "pending"))).scalar_one()
            proj = (await db.execute(select(Project).where(
                Project.id == project_id))).scalar_one()
        return ProjectOnboardingView(
            state=proj.onboarding_state or "registered",
            repo_count=len(repos),
            backfill_done_count=sum(1 for r in repos if r.backfill_done),
            feature_count=int(feature_count),
            open_unclassified_count=int(open_unclassified),
            pending_proposal_count=int(pending),
            map_pr_open=False, map_pr_merged=False)

    # NOTE on map_pr_open / map_pr_merged: phase 2 does not correlate the map PR's
    # live state into the counts view. They are left False — the engine treats
    # map_proposed with neither flag as "re-offer ProposeMap", which is the correct
    # resumable behavior. Wiring PR-state correlation is a phase-3 enrichment.

    async def pm_advance_map_merged(self, slug: str) -> None:
        """Advance a project from ``map_proposed`` to ``map_merged`` after the
        human merges the archaeology map PR. The wizard calls this endpoint once
        the user confirms the merge; pm-sync will index the merged docs on its
        next tick and the classify step becomes available."""
        from sqlalchemy import select
        from server.db.models import Project
        async with db_session() as db:
            proj = (await db.execute(select(Project).where(
                Project.slug == slug))).scalar_one_or_none()
            if proj is not None and proj.onboarding_state == "map_proposed":
                proj.onboarding_state = "map_merged"
                await db.commit()

    # ------------------------------------------------------------------
    # SDLC Synthesizer control-loop dependencies (injected into SdlcSynthesizerLoop)
    # ------------------------------------------------------------------

    async def _sdlc_synth_read_config(self) -> tuple[str, dict]:
        """The sdlc-synthesizer row's (mode, config) merged onto schema defaults.
        Mirrors ``_sdlc_intake_read_config``: off + pure defaults on any absent
        or invalid row so a bad config never spawns."""
        from sqlalchemy import select
        from server.automations.base import validate_config
        from server.automations.sdlc_synthesizer import SdlcSynthesizer
        from server.db.models import Automation
        enabled, mode, raw = False, None, {}
        try:
            async with db_session() as db:
                row = (await db.execute(select(Automation).where(
                    Automation.id == "sdlc-synthesizer"))).scalar_one_or_none()
                if row is not None:
                    enabled = bool(row.enabled)
                    mode = row.mode
                    raw = dict(row.config) if row.config else {}
        except Exception:
            log.debug("sdlc_synthesizer.config.load_failed", exc_info=True)
        try:
            config = validate_config(SdlcSynthesizer.config_schema, raw)
        except ValueError:
            config = validate_config(SdlcSynthesizer.config_schema, {})
        effective = mode if (enabled and mode in ("propose", "auto")) else "off"
        return effective, config

    async def _sdlc_list_initiatives(self) -> list[dict]:
        """Every non-done Initiative as a plain dict (the loop's seam shape).

        An initiative is 'done' when EITHER its status or its stage is ``done`` —
        both terminal signals drop it from the loop's candidate pool."""
        from sqlalchemy import select
        from server.db.models import Initiative
        async with db_session() as db:
            rows = (await db.execute(select(Initiative).where(
                Initiative.status != "done", Initiative.stage != "done"))).scalars().all()
        return [{"id": r.id, "slug": r.slug, "repo": r.repo, "title": r.title,
                 "stage": r.stage, "status": r.status, "origin": r.origin,
                 "origin_ref": r.origin_ref} for r in rows]

    async def _sdlc_list_artifacts(self, initiative_id: str) -> list[dict]:
        """Every InitiativeArtifact index row for *initiative_id* as a plain dict."""
        from sqlalchemy import select
        from server.db.models import InitiativeArtifact
        async with db_session() as db:
            rows = (await db.execute(select(InitiativeArtifact).where(
                InitiativeArtifact.initiative_id == initiative_id))).scalars().all()
        return [{"id": r.id, "kind": r.kind, "pr_number": r.pr_number,
                 "pr_url": r.pr_url, "commit_sha": r.commit_sha,
                 "session_id": r.session_id} for r in rows]

    async def _sdlc_record_artifact(self, *, initiative_id: str, kind: str,
                                    repo_path: str, session_id: str | None,
                                    model: str | None) -> None:
        """Insert an InitiativeArtifact index row for a freshly-spawned session."""
        from server.db.models import InitiativeArtifact
        async with db_session() as db:
            db.add(InitiativeArtifact(
                initiative_id=initiative_id, kind=kind, repo_path=repo_path,
                session_id=session_id, model=model))
            await db.commit()

    async def _sdlc_set_status(self, initiative_id: str, status: str) -> None:
        """Update an Initiative's status (e.g. → ``awaiting_human`` post-spawn)."""
        from sqlalchemy import update
        from server.db.models import Initiative
        async with db_session() as db:
            await db.execute(update(Initiative).where(
                Initiative.id == initiative_id).values(status=status))
            await db.commit()

    async def _sdlc_update_artifact(self, artifact_id: str, **fields) -> None:
        """Update an InitiativeArtifact (PR-metadata enrichment: pr_number/url/sha)."""
        if not fields:
            return
        from sqlalchemy import update
        from server.db.models import InitiativeArtifact
        async with db_session() as db:
            await db.execute(update(InitiativeArtifact).where(
                InitiativeArtifact.id == artifact_id).values(**fields))
            await db.commit()

    async def _sdlc_default_branch(self, repo: str) -> str:
        """The repo's default branch (``main``/``master``), resolved the same way
        ``_backlog_spawn`` does — never hardcode ``master`` (the prompts warn)."""
        try:
            return await asyncio.to_thread(
                lambda: gitrepos.list_remote_branches(repo).get("default") or "master")
        except Exception:
            return "master"

    async def _sdlc_spawn_synth(self, ini: dict, kind: str):
        """Spawn a de-duped spec/plan session for *ini*, seeding the prompt with
        the upstream artifact's committed content read off ``origin/<default>``.

        Mirrors ``_backlog_spawn``: healthy-account gate, ensure_clone off the
        loop, ``resolve_model`` for the model, and the same create-kwargs shape
        through the de-dup-safe context spawner (origin_ref=``<slug>:<kind>``).
        Returns the SessionRecord or None when no account has headroom (retry
        next tick)."""
        from server.automations.model_pick import resolve_model
        from server.sdlc.prompts import (
            artifact_path, build_plan_prompt, build_spec_prompt)
        repo = str(ini.get("repo") or "")
        slug = str(ini.get("slug") or "")
        if not repo or not slug:
            return None
        if kind not in ("spec", "plan"):  # pragma: no cover — loop only asks these
            return None
        account = await self._backlog_healthy_account()
        if account is None:
            return None
        _mode, config = await self._sdlc_synth_read_config()
        url = f"https://github.com/{repo}.git"
        cwd = await asyncio.to_thread(
            gitrepos.ensure_clone, await self._effective_repo_root(), repo, url)
        default_branch = await self._sdlc_default_branch(repo)
        upstream = artifact_path(slug, "intent" if kind == "spec" else "spec")

        def build(cwd: str, default_branch: str) -> str:
            content = self._sdlc_show_committed_file(
                cwd, f"origin/{default_branch}", upstream)
            if kind == "spec":
                return build_spec_prompt(
                    content, [], slug=slug, repo=repo, default_branch=default_branch)
            return build_plan_prompt(
                content, slug=slug, repo=repo, default_branch=default_branch)

        prompt = await asyncio.to_thread(build, cwd, default_branch)
        model, model_source = await resolve_model(
            config, f"{ini.get('title') or slug}  ({kind})")
        detail: dict = {"initiative": slug, "kind": kind}
        if model is not None:
            detail.update(model=model, model_source=model_source)
        return await self._sdlc_synth_ctx.spawn_session(
            origin_ref=f"{slug}:{kind}",
            dedupe_key=f"{slug}:{kind}",
            repo=repo,
            cwd=cwd,
            account=account,
            name=f"sdlc {kind} {slug}",
            initial_prompt=prompt,
            model=model,
            detail=detail,
        )

    @staticmethod
    def _sdlc_show_committed_file(cwd: str, ref: str, path: str) -> str:
        """Contents of *path* at *ref* in the clone at *cwd* (sync — call off-loop).

        Falls back to an instruction to read the file in-repo when unreadable, so
        a stale clone can never block the stage — the artifact is committed on the
        default branch by the time the engine proposes the next stage."""
        try:
            out = subprocess.run(
                ["git", "-C", cwd, "show", f"{ref}:{path}"],
                capture_output=True, text=True, timeout=30, check=False)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout
        except Exception:
            log.debug("sdlc.show_artifact_failed", exc_info=True)
        return (f"(could not inline `{path}` from `{ref}` — read that file from "
                "the repository before starting)")

    async def _sdlc_pr_lookup(self, art: dict) -> dict | None:
        """The artifact session's PR as the enrichment dict shape, or None.

        Resolves the artifact's ``session_id`` to its repo + branch(es) and reads
        the PR-poller cache — the SAME session→PR source of truth
        ``_backlog_pr_status_for`` uses (live record first, then the DB row's
        ``branch``/``authored_branches``). No network client: an artifact whose PR
        the poller has not cached yet simply returns None and enriches next tick.
        state ``merged`` carries the merge-commit sha (the enrich writer's key)."""
        sid = art.get("session_id")
        if not sid:
            return None
        from sqlalchemy import select as sa_select
        repo: str | None = None
        branches: list[str] = []
        rec = self._records.get(sid)
        if rec is not None:
            repo = rec.repo
            if rec.branch:
                branches.append(rec.branch)
            branches.extend(getattr(rec, "authored_branches", None) or [])
        else:
            async with db_session() as db:
                row = (await db.execute(sa_select(
                    DbSession.repo, DbSession.branch, DbSession.authored_branches,
                ).where(DbSession.id == sid))).first()
            if row is not None:
                repo = row.repo
                if row.branch:
                    branches.append(row.branch)
                branches.extend(row.authored_branches or [])
        if not repo:
            return None
        for branch in dict.fromkeys(branches):
            info = self._pr_poller.cached(repo, branch)
            if info is None:
                continue
            state = "merged" if info.merged else info.state
            sha = info.merge_commit_sha or None
            return {"number": info.number, "url": info.url,
                    "state": state, "sha": sha if state == "merged" else None}
        return None

    async def _sdlc_synth_tick_loop(self, interval: float) -> None:
        """Periodic driver for the synthesizer loop (mirrors _sdlc_intake_tick_loop)."""
        while True:
            await self._sdlc_synth_tick_once()
            await asyncio.sleep(interval)

    async def _sdlc_synth_tick_once(self) -> None:
        """One synthesizer tick + an enabled-only ``last_active_at`` heartbeat.
        Best-effort — neither the tick nor the heartbeat may crash the loop."""
        if not await self._schedule_allows_tick("sdlc-synthesizer"):
            return
        try:
            await self._sdlc_synthesizer_loop.tick()
        except Exception:  # pragma: no cover — tick() already swallows; belt+braces
            log.warning("sdlc_synthesizer.tick_loop_error", exc_info=True)
        try:
            mode, _ = await self._sdlc_synth_read_config()
            if mode != "off":
                await self._bump_automation_active("sdlc-synthesizer")
        except Exception:  # pragma: no cover — a heartbeat failure must not stall
            log.debug("sdlc_synthesizer.bump_active_failed", exc_info=True)

    # ------------------------------------------------------------------
    # CI/CD Doctor control-loop dependencies (injected into CicdDoctorLoop)
    # ------------------------------------------------------------------

    async def _cicd_tick_loop(self, interval: float) -> None:
        while True:
            await self._cicd_tick_once()
            await asyncio.sleep(interval)

    async def _cicd_tick_once(self) -> None:
        """One tick + enabled-only last_active_at heartbeat (mirrors backlog)."""
        if not await self._schedule_allows_tick("cicd-doctor"):
            return
        await self._cicd_loop.tick()
        try:
            mode, _ = await self._cicd_read_config()
            if mode != "off":
                await self._bump_automation_active("cicd-doctor")
        except Exception:
            log.debug("cicd_doctor.heartbeat_failed", exc_info=True)

    async def _cicd_read_config(self) -> tuple[str, dict]:
        """(effective mode, validated config) for cicd-doctor; off + defaults on
        any bad row so a corrupt config can never act."""
        from sqlalchemy import select
        from server.automations.base import validate_config
        from server.automations.cicd_doctor import CicdDoctor
        from server.db.models import Automation
        enabled, mode, raw = False, None, {}
        try:
            async with db_session() as db:
                row = (await db.execute(select(Automation).where(
                    Automation.id == "cicd-doctor"))).scalar_one_or_none()
                if row is not None:
                    enabled, mode = bool(row.enabled), row.mode
                    raw = dict(row.config) if row.config else {}
        except Exception:
            log.debug("cicd_doctor.config.load_failed", exc_info=True)
        try:
            config = validate_config(CicdDoctor.config_schema, raw)
        except ValueError:
            config = validate_config(CicdDoctor.config_schema, {})
        # cicd-doctor is a 2-mode (off/propose) automation, so the UI hides the
        # mode <select> and its toggle enables the row with mode left NULL. Resolve
        # enabled + no explicit mode to the first non-off mode (the least-invasive
        # active mode by convention), else an enabled row is stuck 'off' forever.
        active = [m for m in CicdDoctor.modes if m != "off"]
        effective = (mode if mode in active else active[0]) if enabled else "off"
        return effective, config

    async def _cicd_list_repos(self, config: dict) -> list[str]:
        """enabled_repos config, or every distinct repo ccswitch manages."""
        from sqlalchemy import select
        pinned = config.get("enabled_repos") or []
        if pinned:
            return list(pinned)
        async with db_session() as db:
            rows = (await db.execute(
                select(DbSession.repo).where(DbSession.repo.is_not(None))
                .distinct())).scalars().all()
        return sorted(r for r in rows if r)

    # ------------------------------------------------------------------
    # Archive Audit control-loop dependencies (injected into ArchiveAuditLoop)
    # ------------------------------------------------------------------

    async def _archaudit_tick_loop(self, interval: float) -> None:
        while True:
            await self._archaudit_tick_once()
            await asyncio.sleep(interval)

    async def _archaudit_tick_once(self) -> None:
        """One tick + enabled-only last_active_at heartbeat (mirrors cicd)."""
        if not await self._schedule_allows_tick("archive-audit"):
            return
        await self._archaudit_loop.tick()
        try:
            mode, _ = await self._archaudit_read_config()
            if mode != "off":
                await self._bump_automation_active("archive-audit")
        except Exception:
            log.debug("archive_audit.heartbeat_failed", exc_info=True)

    async def _archaudit_read_config(self) -> tuple[str, dict]:
        """(effective mode, validated config) for archive-audit; off + defaults on
        any bad row so a corrupt config can never act."""
        from sqlalchemy import select
        from server.automations.base import validate_config
        from server.automations.archive_audit import ArchiveAudit
        from server.db.models import Automation
        enabled, mode, raw = False, None, {}
        try:
            async with db_session() as db:
                row = (await db.execute(select(Automation).where(
                    Automation.id == "archive-audit"))).scalar_one_or_none()
                if row is not None:
                    enabled, mode = bool(row.enabled), row.mode
                    raw = dict(row.config) if row.config else {}
        except Exception:
            log.debug("archive_audit.config.load_failed", exc_info=True)
        try:
            config = validate_config(ArchiveAudit.config_schema, raw)
        except ValueError:
            config = validate_config(ArchiveAudit.config_schema, {})
        # archive-audit is a 2-mode (off/act) automation; resolve enabled + no explicit
        # mode to the first non-off mode (act), else an enabled row is stuck 'off' forever.
        active = [m for m in ArchiveAudit.modes if m != "off"]
        effective = (mode if mode in active else active[0]) if enabled else "off"
        return effective, config

    # ------------------------------------------------------------------
    # Recurring-session scheduler (#603 dispatch): fire cron-scheduled sessions
    # ------------------------------------------------------------------

    async def _recurring_tick_loop(self, interval: float) -> None:
        """Fire cron-scheduled recurring sessions every *interval* seconds. Each
        tick evaluates every recurring-session Automation row; a firing is gated by
        the row's active-hours window and made idempotent by its persisted
        last_fired_at watermark."""
        while True:
            await self._recurring_tick_once()
            await asyncio.sleep(interval)

    async def _recurring_tick_once(self, now: datetime | None = None) -> None:
        """One scheduler pass. For each enabled recurring-session row: honor the
        active-hours gate (``should_tick``), then ``evaluate_fire`` to decide
        whether a cron minute is due, spawn a de-duped session, and persist the
        advanced watermark. Best-effort throughout — one bad row can't wedge the
        loop, and the watermark is advanced only after a spawn is accepted so a
        transient 'all accounts busy' retries on the next tick (within grace)."""
        from datetime import datetime

        from server.automations.schedule import ScheduleError, evaluate_fire, should_tick
        now = now or datetime.now()
        try:
            rows = await self._recurring_rows()
        except Exception:  # pragma: no cover — a load failure must never stall the loop
            log.warning("recurring_scheduler.load_failed", exc_info=True)
            return
        for automation_id, schedule_raw, spec, last_fired in rows:
            try:
                if not should_tick(schedule_raw, now):
                    continue  # outside the row's active-hours window
                decision = evaluate_fire(schedule_raw, last_fired=last_fired, now=now)
            except ScheduleError:
                log.warning("recurring_scheduler.bad_schedule id=%s", automation_id)
                continue
            except Exception:  # pragma: no cover — defensive; one row can't wedge the rest
                log.warning("recurring_scheduler.eval_failed id=%s", automation_id,
                            exc_info=True)
                continue
            if not decision.spawn:
                # A first-sight baseline still records its watermark (no session).
                if decision.watermark is not None:
                    await self._set_last_fired(automation_id, decision.watermark)
                continue
            spawned = await self._recurring_spawn(automation_id, spec, decision.fire_at)
            if spawned and decision.watermark is not None:
                await self._set_last_fired(automation_id, decision.watermark)

    async def _recurring_rows(self) -> list[tuple[str, dict, dict, datetime | None]]:
        """(id, schedule, spec, last_fired) for every enabled recurring-session
        Automation row. A recurring-session row is one whose ``config`` carries a
        ``recurringSession`` spawn spec AND whose ``schedule`` has a cron — the
        marker is what keeps the continuous automations (which may also carry an
        active-hours schedule) out of the dispatcher."""
        from sqlalchemy import select
        from server.db.models import Automation
        async with db_session() as db:
            rows = (await db.execute(select(Automation).where(
                Automation.enabled.is_(True),
                Automation.schedule.is_not(None)))).scalars().all()
        out: list[tuple[str, dict, dict, datetime | None]] = []
        for r in rows:
            spec = (r.config or {}).get("recurringSession")
            sched = r.schedule or {}
            if isinstance(spec, dict) and isinstance(sched, dict) and sched.get("cron"):
                out.append((r.id, sched, spec, r.last_fired_at))
        return out

    async def _set_last_fired(self, automation_id: str, when: datetime) -> None:
        """Persist the dispatch watermark. Best-effort: a write failure is logged
        but never propagated (the next tick simply re-evaluates)."""
        from sqlalchemy import update as sa_update
        from server.db.models import Automation
        try:
            async with db_session() as db:
                await db.execute(sa_update(Automation)
                                 .where(Automation.id == automation_id)
                                 .values(last_fired_at=when))
        except Exception:  # pragma: no cover — heartbeat-style best-effort write
            log.warning("recurring_scheduler.watermark_failed id=%s", automation_id,
                        exc_info=True)

    async def _recurring_spawn(
        self, automation_id: str, spec: dict, fire_at: datetime | None
    ) -> bool:
        """Spawn a de-duped session for a due cron firing. Returns True when a
        session was created (or an equivalent one already existed) so the caller
        advances the watermark; False when the firing should be retried next tick
        (no account had headroom). ``origin_ref`` is the fire minute, so two ticks
        landing in the same minute reuse the one session rather than double-spawn."""
        from server.automations.context import AutomationContext
        ref = fire_at.isoformat(timespec="minutes") if fire_at else "now"
        name = spec.get("name") or f"scheduled {automation_id}"
        ctx = AutomationContext(automation_id, create_session=self.create_session)
        kwargs: dict = {"name": name, "initial_prompt": spec.get("prompt")}
        for key in ("repo", "branch", "account", "model"):
            if spec.get(key):
                kwargs[key] = spec[key]
        try:
            await ctx.spawn_session(origin_ref=ref, dedupe_key=ref,
                                    detail={"fireAt": ref, "scheduled": True}, **kwargs)
            return True
        except AllAccountsBusy:
            # Transient: leave the watermark so the firing retries within grace.
            await ctx.emit("info", f"scheduled run deferred — all accounts busy ({name})",
                           kind="skipped", target=ref)
            return False
        except Exception:
            log.warning("recurring_scheduler.spawn_failed id=%s", automation_id,
                        exc_info=True)
            await ctx.emit("error", f"scheduled run failed to spawn ({name})",
                           kind="error", target=ref)
            return True  # a hard error is not retriable; advance past it

    # ------------------------------------------------------------------
    # CI/CD Doctor action-provider seams (CicdDoctorActionProvider)
    # ------------------------------------------------------------------

    async def cicd_actionable_findings(self) -> list[dict]:
        """Open findings that still need a GitHub issue filed — the only
        finding-level action the card exposes (#465)."""
        from sqlalchemy import select
        from server.db.models import CiFinding
        async with db_session() as db:
            rows = (await db.execute(select(CiFinding).where(
                CiFinding.status == "open", CiFinding.issue_number.is_(None))
                .order_by(CiFinding.last_seen_at.desc()))).scalars().all()
            return [{"id": r.id, "repo": r.repo, "title": r.title, "kind": r.kind}
                    for r in rows]

    async def cicd_file_issue_now(self, finding_id: str) -> str:
        """File a ci-doctor issue for one finding immediately, reusing the loop's
        issue-filing machinery. Returns a status string the provider maps to a
        level/message: filed | exists | unknown | file_failed."""
        from sqlalchemy import select, update as sa_update
        from server.automations.cicd.findings import issue_body
        from server.db.models import CiFinding
        from server.github import ci_issues
        async with db_session() as db:
            row = (await db.execute(select(CiFinding).where(
                CiFinding.id == finding_id))).scalar_one_or_none()
            if row is None or row.status != "open":
                return "unknown"
            if row.issue_number is not None:
                return "exists"
            if row.repo is None:
                return "unknown"
            repo, title, detail = row.repo, row.title or "", dict(row.detail or {})
        number = await asyncio.to_thread(
            ci_issues.create_issue, repo, title, issue_body(title, detail))
        if number is None:
            return "file_failed"
        async with db_session() as db:
            await db.execute(sa_update(CiFinding)
                             .where(CiFinding.id == finding_id, CiFinding.issue_number.is_(None))
                             .values(issue_number=number))
        return "filed"

    # ------------------------------------------------------------------
    # Autopilot control-loop dependencies (injected into AutopilotLoop)
    # ------------------------------------------------------------------

    async def _autopilot_read_config(self) -> tuple[str, dict]:
        """The autopilot row's (mode, config) merged onto schema defaults. Returns
        'off' when disabled, invalid, OR while draining (autopilot must never fight a
        deploy drain)."""
        from server.automations.autopilot import Autopilot
        from server.automations.base import validate_config
        from sqlalchemy import select
        from server.db.models import Automation
        enabled, mode, raw = False, None, {}
        try:
            async with db_session() as db:
                row = (await db.execute(select(Automation).where(
                    Automation.id == "autopilot"))).scalar_one_or_none()
                if row is not None:
                    enabled, mode, raw = bool(row.enabled), row.mode, dict(row.config or {})
        except Exception:
            log.debug("autopilot.config.load_failed", exc_info=True)
        try:
            config = validate_config(Autopilot.config_schema, raw)
        except ValueError:
            config = validate_config(Autopilot.config_schema, {})
        effective = mode if (enabled and mode in ("propose", "auto")) else "off"
        if getattr(self, "_draining", False):
            effective = "off"
        return effective, config

    def _turn_restart_in_flight(self, session_id: str) -> bool:
        """True while a turn restart (model swap / reassign) may still be unwinding —
        autopilot must not act on the session until the replayed turn has had time to
        start (or the defer window lapses for a swap with nothing to replay)."""
        ts = self._turn_restart_at.get(session_id)
        return ts is not None and (self._clock() - ts) < TURN_RESTART_DEFER_S

    async def _autopilot_list_governed(self) -> list[str]:
        """Session ids autopilot may act on this tick: non-terminal, not opted-out,
        in an actionable status, not mid turn-restart, and (if enabled_repos is set)
        in one of those repos."""
        from server.automations.autopilot_policy import ACTIONABLE_STATUSES
        _mode, config = await self._autopilot_read_config()
        repos = set(config.get("enabled_repos") or [])
        out: list[str] = []
        for sid, rec in list(self._records.items()):
            if rec.status not in ACTIONABLE_STATUSES:
                continue
            if self._turn_restart_in_flight(sid):
                continue
            # Every `complete` session (marker emitted) is owned by the deterministic
            # completion auto-archive path (`_maybe_autoarchive_completed`), which applies
            # the readiness gate and archives or nudges independent of the autopilot mode.
            # Governing it here too would let two paths race to archive the same session.
            # Autopilot still governs goal-met inference for sessions that never emitted
            # the marker (`done`/`idle`) plus every stuck-awaiting-input status.
            if rec.status == "complete":
                continue
            if getattr(rec, "autopilot_paused", False):
                continue
            if repos and rec.repo not in repos:
                continue
            out.append(sid)
        return out

    async def _autopilot_get_context(self, session_id: str):
        """Assemble the pure DecisionContext for a governed session: its effective
        goal plus the pending permission / plan / question payload driving its status."""
        from server.automations.autopilot_policy import DecisionContext
        from server.sessions.goals import effective_goal
        rec = self._records.get(session_id)
        if rec is None:
            return None
        goal_text, goal_source = effective_goal(rec)
        stuck: dict = {"kind": rec.status}
        reg = getattr(self, "_registry", None)
        perms = reg.pending(session_id) if reg is not None else []
        plan = next((p for p in perms if p.get("tool") == "ExitPlanMode"), None)
        other = next((p for p in perms if p.get("tool") != "ExitPlanMode"), None)
        if plan is not None:
            stuck = {"kind": "plan", "perm_id": plan["permId"],
                     "tool_name": "ExitPlanMode", "tool_input": plan.get("input", {}),
                     "plan_text": str(plan.get("input", {}))}
        elif other is not None:
            stuck = {"kind": "permission", "perm_id": other["permId"],
                     "tool_name": other["tool"], "tool_input": other.get("input", {})}
        else:
            qs = self.pending_questions(session_id)
            if qs:
                q = qs[0]
                stuck = {"kind": "question", "question_id": q["question_id"],
                         "question": q.get("question", ""), "options": q.get("options", []),
                         "multi_select": q.get("multi_select", False)}
        from datetime import datetime as _dtmod
        _last = getattr(rec, "last_user_interaction_at", None)
        _secs_since_human = (_dtmod.utcnow() - _last).total_seconds() if _last is not None else None
        pr_facts = self._autopilot_pr_facts(rec)
        transcript_tail = await self._autopilot_transcript_tail(session_id)
        # Context assembly (including this resolution) runs BEFORE pre_decide, so
        # human-deference / budget / loop-detection skips do not suppress it.
        pr_resolution = await self._autopilot_resolve_pr_intent(
            rec, goal_text, goal_source, pr_facts, transcript_tail)
        return DecisionContext(
            session_id=session_id, status=rec.status, account=rec.account, repo=rec.repo,
            branch=getattr(rec, "branch", None), effective_goal=goal_text,
            goal_source=goal_source, stuck=stuck,
            transcript_tail=transcript_tail, ledger={},
            pr_facts=pr_facts,
            no_auto_archive=bool(getattr(rec, "auto_archive_opt_out_reason", None)),
            reversal_hint=await self._autopilot_reversal_hint(rec.repo),
            seconds_since_human=_secs_since_human,
            fleet_context=await self._autopilot_fleet_context(session_id),
            pr_resolution=pr_resolution,
            instructions=getattr(rec, "initial_prompt", None))

    async def _autopilot_reversal_hint(self, repo: str | None) -> str | None:
        """Summarize recent manual reversals of auto-archives into a compact caution the
        decider weighs before it calls `complete`. Repo-scoped when the session has a repo
        (a reversal in THIS repo is the strongest signal); otherwise global. Best-effort:
        any failure returns None (the decider still runs, just without the hint).

        --- Tunable heuristic (the learning policy lives here) -------------------------
        How far back to look, whether to scope by repo, and how the count/goals are phrased
        all shape how cautious autopilot becomes. Adjust freely — the loop and decider don't
        depend on the exact wording, only that a non-None string means "be more careful"."""
        from datetime import datetime, timedelta
        from sqlalchemy import select as sa_select
        from server.db.models import AutomationEvent
        LOOKBACK_DAYS = 30
        MAX_EXAMPLES = 3
        try:
            since = datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)
            stmt = (sa_select(AutomationEvent)
                    .where(AutomationEvent.automation_id == "autopilot",
                           AutomationEvent.kind == "archive_reversed",
                           AutomationEvent.ts >= since)
                    .order_by(AutomationEvent.ts.desc())
                    .limit(50))
            if repo:
                stmt = stmt.where(AutomationEvent.target == repo)
            async with db_session() as db:
                rows = (await db.execute(stmt)).scalars().all()
        except Exception:  # pragma: no cover — a hint failure must never stall a decision
            log.debug("autopilot.reversal_hint failed", exc_info=True)
            return None
        if not rows:
            return None
        scope = f"in {repo}" if repo else "across all repos"
        goals = [str(g)[:80] for r in rows[:MAX_EXAMPLES]
                 if isinstance(r.detail, dict) and (g := r.detail.get("goal"))]
        tail = f" Recent reversed goals: {'; '.join(goals)}." if goals else ""
        return (f"CAUTION: {len(rows)} recently auto-archived session(s) {scope} were "
                f"manually unarchived — the goal-met call was wrong there. Hold `complete` "
                f"to a clearly met goal (armed/merged PR, or non-code work).{tail}")

    async def _autopilot_fleet_context(self, session_id: str) -> dict | None:
        """This session's view of the fleet snapshot (see fleet_context.build_fleet_view).
        Best-effort: any failure returns None — the decider still runs, just without
        cross-session awareness (mirrors reversal_hint's degradation contract)."""
        from server.automations.fleet_context import build_fleet_view
        try:
            active, archived = await self._autopilot_fleet_snapshot()
            return build_fleet_view(session_id, active, archived)
        except Exception:  # pragma: no cover — a fleet failure must never stall a decision
            log.debug("autopilot.fleet_context failed sid=%s", session_id, exc_info=True)
            return None

    async def _autopilot_fleet_snapshot(self) -> tuple[list, list]:
        """(active FleetSessions, recently archived) for the whole fleet, cached ~15s
        so one snapshot serves every governed session within a 20s autopilot tick."""
        from server.automations.fleet_context import FleetSession
        from server.sessions.goals import effective_goal
        from server.automations.fleet_context import pr_summary
        TTL_S = 15.0
        now = self._clock()
        cached = getattr(self, "_fleet_snapshot_cache", None)
        if cached is not None and now - cached[0] < TTL_S:
            return cached[1]
        active: list[FleetSession] = []
        for sid, rec in list(self._records.items()):
            if rec.status in INACTIVE_SESSION_STATUSES:
                continue
            goal_text, _src = effective_goal(rec)
            active.append(FleetSession(
                session_id=sid, name=rec.name, repo=rec.repo,
                branch=getattr(rec, "branch", None), status=rec.status,
                goal=goal_text, pr=pr_summary(self._autopilot_pr_facts(rec))))
        snapshot = (active, await self._autopilot_recent_archived())
        self._fleet_snapshot_cache = (now, snapshot)
        return snapshot

    async def _autopilot_recent_archived(self, *, window_hours: float = 24.0,
                                         cap: int = 10) -> list:
        """Sessions archived within the window, newest first, with their landed
        outcome (from the PR poller cache — no new GitHub I/O). The window/cap are
        the tunable 'how far back does autopilot remember' policy."""
        from datetime import datetime, timedelta
        from sqlalchemy import select as sa_select
        from server.automations.fleet_context import ArchivedFleetSession, pr_summary
        since = datetime.utcnow() - timedelta(hours=window_hours)
        async with db_session() as db:
            rows = (await db.execute(
                sa_select(DbSession)
                .where(DbSession.archived_at.is_not(None), DbSession.archived_at >= since)
                .order_by(DbSession.archived_at.desc())
                .limit(cap))).scalars().all()
        now = datetime.utcnow()
        out: list[ArchivedFleetSession] = []
        for r in rows:
            goal = (r.active_goal or r.inferred_goal or r.title or r.name or "").strip()
            # A DB row quacks like a record for _autopilot_pr_facts (repo, branch,
            # authored_branches) — reuse it rather than re-deriving PR lookup.
            outcome = pr_summary(self._autopilot_pr_facts(r)) if r.repo else None
            hours = max(0.0, (now - r.archived_at).total_seconds() / 3600.0) if r.archived_at else 0.0
            out.append(ArchivedFleetSession(
                name=r.name, repo=r.repo, goal=goal,
                outcome=outcome or "archived", hours_ago=round(hours, 1)))
        return out

    def _autopilot_pr_facts(self, rec) -> "PrFacts":
        """Verified PR ground-truth for a governed session, from the cache only (no
        new I/O). Mirrors request_pr_fix: check every authored branch plus the scratch
        branch, dedupe by PR number. On a per-branch cache miss, falls back to the
        population cache by head_ref so a just-opened PR the poller hasn't cached yet
        is still seen — the same merge _autopilot_refresh_pr_facts does, but here on
        the cheap per-tick path that feeds met_bar and the fleet snapshot."""
        from server.automations.completion_bar import PrFact, PrFacts
        seen: dict[int, PrFact] = {}
        for br in self._session_pr_branches(rec):
            if not rec.repo:
                continue
            info = self.pr_for(rec.repo, br)
            # Per-branch poller cache miss (cooldown / high-volume repo / branch not
            # yet polled) → resolve from the population cache by head_ref. Without this
            # the snapshot stays prs=[] and met_bar re-fires a spurious "open a PR"
            # nudge once the PR mention leaves the transcript tail (compaction), since
            # facts_contradicted's transcript gate is then the ONLY other path that
            # would trigger the population-cache-aware refresh.
            if info is None and getattr(self, "_pr_population", None) is not None:
                info = self._pr_population_pr(rec.repo, br)
            if info is not None and info.number not in seen:
                seen[info.number] = PrFact(
                    number=info.number, state=info.state, is_draft=info.is_draft,
                    auto_merge_enabled=info.auto_merge_enabled,
                    in_merge_queue=info.in_merge_queue, url=info.url,
                    checks=info.checks)
        return PrFacts(has_repo=bool(rec.repo), prs=tuple(seen.values()))

    async def _autopilot_resolve_pr_intent(self, rec, goal_text, goal_source,
                                           pr_facts, transcript_tail) -> "str | None":
        """Durable PR-resolution for a governed session. Returns the record's stored
        value if set; else, ONLY on the 'code goal but no PR' met_bar branch, runs the
        gated Haiku classifier and persists a high-confidence 'resolved' verdict.
        Any classifier failure returns None (autopilot falls back to its normal nudge)."""
        existing = getattr(rec, "pr_resolution", None)
        if existing:
            return existing
        # Only classify a session that has self-declared done (emitted the completion
        # marker → status "complete"). For any still-active status the low-confidence
        # "no PR yet" met_bar verdict is just a healthy in-progress session, so running
        # the Haiku classifier every tick there would burn quota + add latency for no
        # outcome change. This is the one decision point where PR-intent matters.
        if getattr(rec, "status", None) != "complete":
            return None
        from server.automations.completion_bar import met_bar
        verdict = met_bar(goal_text, goal_source, pr_facts)
        # Only the low-confidence 'code goal but no PR opened' branch produces the buggy
        # nudge; that is the ONLY place intent can change the outcome.
        if verdict.met or verdict.confidence != "low":
            return None
        from server.automations.pr_intent import classify_pr_intent
        intent = await classify_pr_intent(transcript_tail, goal_text)
        if not (intent.resolved and intent.confidence == "high"):
            return None
        rec.pr_resolution = intent.reason
        rec.pr_resolution_source = "classifier"
        try:
            from sqlalchemy import update as sa_update
            async with db_session() as db:
                await db.execute(sa_update(DbSession).where(DbSession.id == rec.id).values(
                    pr_resolution=intent.reason, pr_resolution_source="classifier"))
        except Exception:  # pragma: no cover — a persist failure must not stall the tick
            log.debug("autopilot.pr_intent_persist_failed id=%s", rec.id, exc_info=True)
        return intent.reason

    async def _autopilot_transcript_tail(self, session_id: str,
                                         *, turns: int = 6, max_entries: int = 12,
                                         max_chars: int = 400,
                                         tool_chars: int = 160) -> list[dict]:
        """Compact tail of the session transcript for the autopilot decider.

        An empty tail starves the decider into 'not enough information' escalations
        (prod, 2026-07-03), so this is load-bearing context, not decoration. Flattens
        the last few replayed turns into small {role, text} dicts, merging consecutive
        assistant deltas. Best-effort: any failure returns [] (the decider still runs,
        it just decides with less)."""
        import json as _json
        from server.sessions.events import TextDelta, ToolCall, UserMessage
        try:
            window = await self.transcript_window(session_id, limit=turns)
        except Exception:
            log.debug("autopilot.transcript_tail failed sid=%s", session_id, exc_info=True)
            return []
        if window is None:
            return []
        tail: list[dict] = []
        for e in window.events:
            if isinstance(e, UserMessage) and (e.text or "").strip():
                tail.append({"role": "user", "text": e.text[:max_chars]})
            elif isinstance(e, TextDelta) and (e.text or "").strip():
                # Replay emits one delta per content block — merge consecutive
                # assistant text so one reply reads as one entry.
                if tail and tail[-1]["role"] == "assistant":
                    tail[-1]["text"] = (tail[-1]["text"] + e.text)[:max_chars]
                else:
                    tail.append({"role": "assistant", "text": e.text[:max_chars]})
            elif isinstance(e, ToolCall):
                try:
                    arg_head = _json.dumps(e.input, default=str)[:tool_chars]
                except Exception:
                    arg_head = str(e.input)[:tool_chars]
                tail.append({"role": "tool", "text": f"{e.name} {arg_head}"})
        return tail[-max_entries:]

    async def _autopilot_apply(self, session_id: str, decision, ctx=None) -> bool:
        """Perform an autopilot Decision through the existing session seams under the
        system identity. Returns True only when a mutation actually happened.

        Acts ONLY on the decide-time ``ctx`` (the stuck payload pre_decide +
        post_validate already validated) — never a freshly re-read one, so a new
        pending permission the session surfaced during the decider's await window can
        never be approved without passing the guardrails (TOCTOU safety)."""
        from server.sessions.types import SYSTEM_IDENTITY
        from server.automations.autopilot_policy import matches_denylist
        act = decision.action
        if act in ("escalate", "complete", "skip"):
            return False
        # A model swap / reassign that began during the decider's await window makes
        # the decide-time stuck state stale — the turn is being restarted, so acting
        # on it would inject into (or resolve state belonging to) the replaced turn.
        if self._turn_restart_in_flight(session_id):
            return False
        stuck = ctx.stuck if ctx is not None else {}
        ok = False
        if act in ("approve", "deny") and stuck.get("perm_id"):
            # Defense-in-depth: re-check the exact perm being approved against the
            # deny-list right before resolving, so an approve can never slip a
            # destructive command through even if the payload changed shape.
            if act == "approve" and matches_denylist(
                    stuck.get("tool_name", ""), stuck.get("tool_input", {}), []):
                return False
            await self.ensure_driver(session_id, SYSTEM_IDENTITY)
            ok = bool(self.resolve_permission(
                session_id, stuck["perm_id"], act == "approve", SYSTEM_IDENTITY))
        elif act == "answer" and stuck.get("question_id") is not None:
            await self.ensure_driver(session_id, SYSTEM_IDENTITY)
            opts = stuck.get("options") or []
            idx = decision.answer_index or 0
            selected = [opts[idx]] if 0 <= idx < len(opts) else []
            ok = bool(self.answer_question(
                session_id, stuck["question_id"], selected, SYSTEM_IDENTITY, "option"))
        elif act == "nudge" and decision.prompt:
            await self.ensure_driver(session_id, SYSTEM_IDENTITY)
            ok = (await self.send_prompt(session_id, decision.prompt)) == "sent"
        if ok:
            await self._autopilot_mark_assisted(session_id)
        return ok

    async def _autopilot_mark_assisted(self, session_id: str) -> None:
        """Stamp assist provenance the first time autopilot successfully acts on a
        session (drives the purple 'Assisted by Autopilot' badge). Idempotent and
        best-effort — a provenance write must never fail an applied action."""
        rec = self._records.get(session_id)
        if rec is not None and getattr(rec, "assisted_by", None):
            return
        if rec is not None:
            rec.assisted_by = "autopilot"
        try:
            async with db_session() as db:
                from sqlalchemy import update as sa_update
                await db.execute(sa_update(DbSession).where(DbSession.id == session_id)
                                 .values(assisted_by="autopilot"))
        except Exception:  # pragma: no cover — best-effort provenance
            log.debug("autopilot.mark_assisted_failed sid=%s", session_id, exc_info=True)

    async def _autopilot_decide(self, ctx):
        """Wrap the one-shot LLM decider, passing the account's config dir when resolvable."""
        from server.automations.autopilot_decider import decide
        try:
            cfg_dir = str(compose(ctx.account))
        except Exception:  # pragma: no cover — fall back to the process env config dir
            cfg_dir = None
        return await decide(ctx, _config_dir=cfg_dir)

    async def _autopilot_refresh_pr_facts(self, session_id: str):
        """On-demand PR ground-truth refresh for verify-before-directing (spec §2).
        Refreshes branch linkage + this session's poller pairs, then rebuilds facts,
        merging population-cache PRs the per-branch cache missed. Returns None on
        no-repo, poller cooldown, or failure — the caller composes against stale
        facts and says so. Rate limiting lives in the loop (ledger clock)."""
        from server.automations.completion_bar import PrFact, PrFacts
        rec = self._records.get(session_id)
        if rec is None or not rec.repo:
            return None
        poller = getattr(self, "_pr_poller", None)
        if poller is None or poller.in_cooldown:
            return None
        try:
            await self.refresh_authored_branches()
            pairs = [(rec.repo, br) for br in self._session_pr_branches(rec)]
            await poller.refresh_once(pairs)
        except Exception:
            log.warning("autopilot.refresh_pr_facts failed sid=%s", session_id, exc_info=True)
            return None
        facts = self._autopilot_pr_facts(rec)
        known = {p.number for p in facts.prs}
        extra = []
        for br in self._session_pr_branches(rec):
            info = self._pr_population_pr(rec.repo, br)
            if info is not None and info.number not in known:
                known.add(info.number)
                extra.append(PrFact(
                    number=info.number, state=info.state, is_draft=info.is_draft,
                    auto_merge_enabled=info.auto_merge_enabled,
                    in_merge_queue=info.in_merge_queue, url=info.url,
                    checks=info.checks))
        if extra:
            facts = PrFacts(has_repo=facts.has_repo, prs=facts.prs + tuple(extra))
        return facts

    async def _autopilot_compose(self, ctx, decision):
        """Compose the actual nudge text (spec §3): rich transcript window + the
        gate's intent hint + bar verdict + prior-nudge history. None -> escalate."""
        from server.automations import nudge_composer
        rich_tail = await self._autopilot_transcript_tail(
            ctx.session_id, turns=20, max_entries=40, max_chars=1500, tool_chars=400)
        try:
            cfg_dir = str(compose(ctx.account))
        except Exception:  # pragma: no cover — fall back to process env
            cfg_dir = None
        recent = [str(e.get("text", "")) for e in (ctx.ledger or {}).get("recent_nudges", [])]
        return await nudge_composer.compose_nudge(
            ctx, intent_hint=decision.prompt,
            verdict_reason=decision.verdict_reason,
            verdict_remedy=decision.verdict_remedy,
            recent_nudges=recent, rich_tail=rich_tail,
            facts_verified=getattr(ctx, "facts_verified", False),
            _config_dir=cfg_dir)

    async def _autopilot_archive(self, session_id: str) -> bool:
        """Archive a goal-met session (the loop's terminal-rest seam). Reversible —
        the session moves to the Archived tab and can be unarchived.

        Snapshots the decision features (goal, repo, PR state) so that if a human later
        unarchives it, `unarchive` can record a reversal heuristic the auto-archive
        algorithm learns from. Stamps provenance archived_by='autopilot'."""
        context = None
        rec = self._records.get(session_id)
        if rec is not None:
            from server.sessions.goals import effective_goal
            goal_text, goal_source = effective_goal(rec)
            pr = self._autopilot_pr_facts(rec)
            resolution = getattr(rec, "pr_resolution", None)
            context = {
                "repo": rec.repo, "goal": goal_text, "goal_source": goal_source,
                "pr_state": pr.to_prompt() if pr is not None else None,
                "pr_resolution": resolution,
                "reason": (f"resolved no PR ({resolution})" if resolution
                           else "autopilot: goal met"),
            }
        await self.archive_session(session_id, archived_by="autopilot", context=context)
        return True

    async def _autopilot_tick_loop(self, interval: float) -> None:
        """Periodic driver for the autopilot control loop (also nudged on status change)."""
        while True:
            try:
                # Deploy-resume recovery runs here in the always-on LOOP — before the
                # mode-gated tick() — so dropped sessions recover even when autopilot
                # governance is off ('all failed sessions, always').
                await self._recover_resume_failures_once()
            except Exception:  # pragma: no cover — recovery must never stall the loop
                log.warning("deploy.resume.recover_failed", exc_info=True)
            try:
                # Also always-on (not mode-gated): evict mirror records for sessions
                # deleted on another fleet host, so autopilot never proposes for a
                # ghost row (FK-violation insert spam) — see _reconcile_mirror_once.
                await self._reconcile_mirror_once()
            except Exception:  # pragma: no cover — reconcile must never stall the loop
                log.warning("mirror.reconcile_failed", exc_info=True)
            try:
                await self._autopilot_loop.tick()
            except Exception:  # pragma: no cover — tick() already swallows; belt+braces
                log.warning("autopilot.tick_loop_error", exc_info=True)
            await asyncio.sleep(interval)

    async def _pr_doctor_config(self) -> dict:
        """The pr-doctor row's config merged onto the PrDoctor schema defaults.

        Single source of truth for the flags ``plan_recovery`` reads
        (``auto_*``, ``max_attempts``, ``mechanical_attempts``, ``enabled_repos``).
        Best-effort: an absent/invalid row falls back to pure defaults."""
        from server.automations.pr_doctor import PrDoctor
        from server.automations.base import validate_config
        from sqlalchemy import select
        from server.db.models import Automation
        raw: dict = {}
        try:
            async with db_session() as db:
                row = (await db.execute(select(Automation).where(
                    Automation.id == "pr-doctor"))).scalar_one_or_none()
                raw = dict(row.config) if row and row.config else {}
        except Exception:
            log.debug("pr_doctor.config.load_failed", exc_info=True)
        try:
            return validate_config(PrDoctor.config_schema, raw)
        except ValueError:
            # A stale/invalid stored config must not break the poll loop — fall
            # back to pure schema defaults.
            return validate_config(PrDoctor.config_schema, {})

    async def _bump_automation_active(self, automation_id: str) -> None:
        """Best-effort ``last_active_at`` heartbeat on an automation row.

        Shared by pr-doctor (per poll-cycle) and the backlog runner (per enabled
        tick) so the card's "last active" reflects a live loop for either."""
        try:
            from sqlalchemy import update as sa_update
            from server.db.models import Automation
            from datetime import datetime, timezone
            async with db_session() as db:
                await db.execute(sa_update(Automation)
                                 .where(Automation.id == automation_id)
                                 .values(last_active_at=datetime.now(timezone.utc)))
        except Exception:
            log.debug("automation.bump_active_failed id=%s", automation_id, exc_info=True)

    async def _schedule_allows_tick(self, automation_id: str) -> bool:
        """Whether ``automation_id`` may tick now given its user-set schedule (#603).

        Reads ``Automation.schedule`` and evaluates the active-hours window. No
        schedule / no window → always True. When a tick is skipped, one event is
        emitted per open→closed transition (throttled) so the activity feed shows
        the schedule being honored without spamming a row every tick."""
        from datetime import datetime
        from server.automations.context import AutomationContext
        from server.automations.schedule import should_tick
        try:
            from sqlalchemy import select
            from server.db.models import Automation
            async with db_session() as db:
                row = (await db.execute(select(Automation).where(
                    Automation.id == automation_id))).scalar_one_or_none()
                raw = row.schedule if row is not None else None
        except Exception:
            return True  # a read failure must never wedge the loop
        if not raw:
            return True
        try:
            allowed = should_tick(raw, datetime.now())
        except Exception:
            return True  # a malformed schedule fails open (never silently stalls)
        prev = self._schedule_blocked.get(automation_id, False)
        if not allowed and not prev:
            await AutomationContext(automation_id).emit(
                "info", "Paused — outside its scheduled active hours",
                kind="skipped", detail={"reason": "schedule"})
        self._schedule_blocked[automation_id] = not allowed
        return allowed

    async def _on_pr_change(
        self, repo: str, branch: str, old_checks: str | None, new_checks: str
    ) -> None:
        """PR-poller transition callback: dispatch a session's PR through PR Doctor.

        Routed to the owning session by (repo, live branch). Recovery/notify are
        debounced via ``_pushed_pr`` and the durable pr-doctor target state; the
        failing/transition handling delegates to ``_pr_doctor_step`` which runs
        the pure engine (classify → plan_recovery → dispatch). A transition for a
        (repo, branch) with no live session is a no-op (M2b.2 / AUT-02).
        """
        key = (repo, branch)
        if new_checks != "failing":
            self._pushed_pr.discard(key)
            # Re-arm the auto-fix guard ONLY on a genuine green, never on a
            # transient ``pending``/``none``. CI normally cycles
            # failing→pending→failing while a pushed fix re-runs; clearing on
            # ``pending`` would let the next ``failing`` trigger a fresh auto-fix
            # and loop forever on a PR the agent can't fix. The guard lives in the
            # durable pr-doctor target state (automation_target_state), keyed by
            # ``f"{repo}#{number}"`` — clearing == setting it back to ``{}``.
            if new_checks == "passing":
                info = self.pr_for(repo, branch)
                if info is not None:
                    target = f"{repo}#{info.number}"
                    state = await self._pr_doctor_ctx.get_target_state(target)
                    # Any recorded attempt (session-fix or mechanical) means we
                    # tried and CI is now green → clear the per-target state so a
                    # fresh break re-attempts. ``attempted`` kept for back-compat
                    # with rows written before the engine migration.
                    if (state.get("attempted") or state.get("attempts")
                            or state.get("mechanical_attempts")):
                        await self._pr_doctor_ctx.set_target_state(target, {})
                        await self._pr_doctor_ctx.emit(
                            "info", f"{repo}#{info.number} CI recovered",
                            kind="recovered", target=target)
            return
        rec = next(
            (r for r in self._records.values() if r.repo == repo and r.branch == branch),
            None,
        )
        if rec is None:
            return

        info = self.pr_for(repo, branch)
        # Surface the failure on the activity feed once, on the transition INTO
        # failing (not on every failing→failing re-poll).
        if old_checks != "failing":
            target = f"{repo}#{info.number}" if info is not None else f"{repo}#{branch}"
            await self._pr_doctor_ctx.emit(
                "warn", f"{repo} CI failing on {target}", kind="detected", target=target)

        injected = False
        if info is not None:
            injected = await self._pr_doctor_step(repo, branch, info, rec=rec)
        if injected:
            return  # a recovery action fired; skip the redundant break notification

        # Notify (off, propose, GiveUp, or auto-fallback). Debounced like before.
        if key in self._pushed_pr:
            return
        self._pushed_pr.add(key)
        await push_mod.notify_event(
            "pr_broke", "ccswitch — PR broke", f"{rec.name}: {repo} CI failing",
            repo=repo, account=getattr(rec, "account_name", None),
            url=f"/session/{rec.id}",
        )

    async def _pr_doctor_sweep(self) -> None:
        """Per-poll-cycle sweep over the fresh PR cache — the on_cycle hook.

        ``_on_pr_change`` only fires on a CI-``checks`` *transition* to failing, so
        a PR that goes out-of-date (BEHIND) or into a merge conflict (DIRTY) with
        green checks is never classified live. This sweep closes that gap: after
        every poll refresh it walks all tracked ``(repo, branch)`` pairs and runs
        ``_pr_doctor_step`` on each non-terminal cached PR, so ALL failure modes
        (out_of_date / merge_conflict / failing_ci) are evaluated every cycle.

        Safe to run every cycle and alongside ``_on_pr_change``: ``_pr_doctor_step``
        is idempotent — the per-head guard (``last_action_sha`` + a positive attempt
        count) plus the durable per-target state and the ``detected``-once debounce
        prevent any double-action across repeated sweeps, so this re-affirms a
        failing PR harmlessly and never double-injects. Best-effort: a per-PR error
        is swallowed so one bad PR can't abort the sweep.
        """
        from server.automations import pr_doctor_engine as engine

        if not await self._schedule_allows_tick("pr-doctor"):
            return
        pairs = await self._pr_doctor_scope_pairs()
        for repo, branch in pairs:
            info = self.pr_for(repo, branch) or self._pr_population_pr(repo, branch)
            # Reconcile: per-branch cache can lag behind a merge; if population confirms
            # the PR is terminal while per-branch still shows it open, trust population.
            if info is not None and not engine.target_terminal(info):
                pop = self._pr_population_pr(repo, branch)
                if pop is not None and engine.target_terminal(pop):
                    info = pop
            if info is None or engine.target_terminal(info):
                continue
            rec = self._pr_doctor_owning_rec(repo, branch)
            try:
                await self._pr_doctor_step(repo, branch, info, rec=rec)
            except Exception:
                log.warning("orchestrator._pr_doctor_sweep_step_failed", exc_info=True)
        # Stash a live summary on the ctx so ``PrDoctor.status(ctx)`` renders
        # "watching N failing PR(s)" instead of always "idle" (parity with the
        # backlog runner's ``_stash_counts``). Delegates to ``automation_actionable``
        # — the generic action-provider path — rather than a parallel counter.
        # Best-effort: a count read must not break the sweep.
        try:
            actionable = await self.automation_actionable("pr-doctor")
            # ``setattr`` (not a typed attribute) mirrors the backlog runner's
            # ``_stash_counts`` — the ctx carries dynamic per-automation summaries.
            setattr(self._pr_doctor_ctx, "pr_doctor_counts", {"failing": len(actionable)})
        except Exception:  # pragma: no cover — defensive: never break the sweep
            log.debug("pr_doctor.stash_counts_failed", exc_info=True)

    async def _pr_doctor_step(
        self, repo: str, branch: str, info: PullRequestInfo, *, rec=None
    ) -> bool:
        """Run the pure PR Doctor engine for one poll of a (repo, PR) and dispatch.

        Reads mode + config, applies repo-scope gating, then
        ``apply_rearm`` → ``classify`` → ``plan_recovery`` and dispatches on the
        action type. Returns True iff a recovery action was actually taken (a
        real injection / mechanical update), so ``_on_pr_change`` can skip the
        redundant break notification. Behaviour-preserving superset of the old
        failing-CI auto path: one attempt per failure event, no re-arm on
        transient pending (``apply_rearm`` encodes that), ``record_attempt`` only
        on a real injection.

        Action dispatch: ``Notify`` (debounced push, handled by the caller),
        ``Mechanical`` (gh update-branch via ``_pr_doctor_mechanical``),
        ``Propose`` (observable intent event, no side effect), and ``SessionFix``.
        ``SessionFix`` injects the kind-appropriate first prompt into a LIVE
        driver when the owning session is still running; otherwise it resolves the
        repo's ccswitch clone ``cwd`` (via ``gitrepos.ensure_clone``, exactly like
        the New-Session API) and spawns a fresh, de-duped session seeded with that
        prompt against a real repo checkout + the PR branch. A clone or spawn
        failure emits an error, notifies, and does NOT consume an attempt (the
        target re-arms for a later retry); only a real inject/spawn consumes one.
        """
        from server.automations import pr_doctor_engine as engine

        # Enabled gate (single chokepoint for BOTH the per-cycle sweep and the
        # _on_pr_change callback): a disabled automation takes no recovery action,
        # exactly like 'off' mode. Status counts and the caller's "PR broke"
        # notification still surface — only the autonomous inject/spawn/mechanical
        # is suppressed.
        if not await self._pr_doctor_enabled():
            return False

        mode = await self._pr_autofix_mode()
        config = await self._pr_doctor_config()
        await self._bump_automation_active("pr-doctor")

        # Terminal (merged/closed): clear per-target state and stop.
        if engine.target_terminal(info):
            target = f"{repo}#{info.number}"
            await self._pr_doctor_ctx.set_target_state(target, {})
            await self._pr_doctor_ctx.emit(
                "info", f"{target} {info.state}", kind="terminal", target=target)
            return False

        # Repo-scope gate: a non-empty allowlist that excludes this repo → off.
        enabled_repos = config.get("enabled_repos") or []
        if enabled_repos and repo not in enabled_repos:
            return False

        target = f"{repo}#{info.number}"
        st = await self._pr_doctor_ctx.get_target_state(target)
        st = engine.apply_rearm(st, info)
        fm = engine.classify(info)
        action = engine.plan_recovery(fm, mode, config, st)

        # One auto-attempt per failure *event*: once an action has run against the
        # current head, don't re-run it until the PR re-arms (apply_rearm resets
        # the counter on an external push / changed failure class). This preserves
        # the old ``attempted`` boolean guard (a repeated failing→failing signal on
        # the same head does NOT re-inject). ``plan_recovery``'s ``max_attempts``
        # cap still bounds the total; the re-arm supplies fresh budget per push.
        already_acted = (
            st.get("last_action_sha") == info.head_sha
            and (st.get("mechanical_attempts", 0) if isinstance(action, engine.Mechanical)
                 else st.get("attempts", 0)) > 0
        )
        if already_acted and isinstance(action, (engine.Mechanical, engine.SessionFix)):
            await self._pr_doctor_ctx.set_target_state(target, st)
            return False

        took_action = False
        if isinstance(action, engine.GiveUp):
            # Monotonic ceiling reached — stop auto-acting and hand off to a human.
            # Mark the target sticky-gave_up (persisted below), emit an observable
            # `gave_up` event carrying the owning session's origin_ref/target so
            # the Backlog Runner can react, and notify needs-human via the caller's
            # debounced push. Do NOT re-inject.
            st["gave_up"] = True
            await self._pr_doctor_ctx.emit(
                "warn", f"gave up auto-fixing {target}",
                kind="gave_up", target=target, detail={"origin_ref": target})
            took_action = False
        elif isinstance(action, engine.Notify):
            # Notify: the caller fires the debounced push (no further auto action).
            took_action = False
        elif isinstance(action, engine.Propose):
            # Propose surface is LIVE: the one-tap action row is served by
            # ``automation_actionable("pr-doctor")`` and triggered via
            # ``automation_trigger("pr-doctor", item_id, kind)``. This emitted
            # ``proposed`` event is observability-only — it records the intent on
            # the activity feed with no auto side effect (does not consume an
            # attempt, so it is NOT a "took_action"; the caller still fires the
            # debounced notify).
            await self._pr_doctor_ctx.emit(
                "info", f"proposed {action.kind} fix for {target}",
                kind="proposed", target=target)
            took_action = False
        elif isinstance(action, engine.Mechanical):
            ok = await self._pr_doctor_mechanical(repo, info, action.kind)
            if ok:
                st = engine.record_attempt(st, action, info.head_sha)
                took_action = True
            # else: gh failed → attempt NOT consumed; re-arms next poll.
        elif isinstance(action, engine.SessionFix):
            # Delegate to the shared inject-or-spawn executor (single source of
            # truth for both the auto path here and the manual one-tap trigger
            # via ``automation_trigger("pr-doctor", item_id, kind)``). It returns
            # ``"sent"``/``"spawned"`` on a real injection (which consumes an
            # attempt) or a transient/error status.
            status, _ = await self._pr_doctor_session_fix(
                repo, branch, info, action.kind, rec)
            if status in ("sent", "spawned"):
                st = engine.record_attempt(st, action, info.head_sha)
                # Back-compat marker: pre-engine rows/tests keyed the guard on
                # ``attempted``. record_attempt already tracks attempts +
                # last_action_sha (the real guard); this keeps the old key
                # present for anything still reading it.
                st["attempted"] = True
                took_action = True
            # else: transient / clone_failed / spawn_failed → no attempt consumed;
            # apply_rearm will let the next failing poll retry.

        await self._pr_doctor_ctx.set_target_state(target, st)
        return took_action

    async def _pr_doctor_session_fix(
        self, repo: str, branch: str, info: PullRequestInfo, kind: str, rec,
        on_phase: "Callable[[str], None] | None" = None,
    ) -> tuple[str, str | None]:
        """Inject-or-spawn a session-driven PR fix — the shared session executor.

        Single source of truth for both the auto path (``_pr_doctor_step``) and
        the manual one-tap propose action (``automation_trigger("pr-doctor", ...)``). Selects the
        branch-aware first prompt by *kind*; if the owning session is a LIVE
        driver it injects via the same send-prompt path (``request_pr_fix`` for
        failing_ci to preserve the old re-resolve+gate behaviour, direct
        ``send_prompt`` of the conflict prompt for merge_conflict), otherwise it
        resolves the ccswitch clone ``cwd`` and spawns a fresh, de-duped session
        seeded with that prompt. Emits the same ``attempted`` / ``spawn_failed``
        events as before. Returns ``(status, session_id)`` where status is
        ``"sent"`` | ``"spawned"`` (real injection) | ``"transient"`` |
        ``"clone_failed"`` | ``"spawn_failed"``. Does NOT touch per-target engine
        state — the caller records the attempt on a real injection.

        ``on_phase(phase)`` — optional progress hook the async manual trigger
        (issue #668) uses to advance its job through ``cloning`` → ``assessing``
        → ``spawning``. Defaults to a no-op so the auto sweep path is unchanged.
        """
        phase = on_phase or (lambda _p: None)
        target = f"{repo}#{info.number}"
        # Select the branch-aware first prompt by failure kind. Single source of
        # truth for both the live-driver inject and the fresh-spawn seed.
        if kind == "merge_conflict":
            prompt = build_conflict_fix_prompt(repo, info)
        else:  # "failing_ci"
            prompt = build_pr_fix_prompt(repo, info)

        live_rec = rec if (rec is not None and rec.id in self._drivers) else None
        if live_rec is not None:
            # LIVE driver → near-instant injection, no clone/assess. Report the
            # spawning phase so a polling job still sees forward motion.
            phase("spawning")
            # LIVE driver → inject the kind-appropriate prompt via the same
            # send-prompt path request_pr_fix uses. For failing_ci we keep calling
            # request_pr_fix to preserve the old auto behaviour exactly (it
            # re-resolves the PR + gates on an actionable state); for
            # merge_conflict we inject the conflict prompt directly through
            # send_prompt (the same mechanism).
            if kind == "merge_conflict":
                result = await self.send_prompt(live_rec.id, prompt)
            else:
                result = await self.request_pr_fix(live_rec.id, info.number)
            if result == "sent":
                await self._pr_doctor_ctx.emit(
                    "info", f"auto-fix injected for {target}",
                    kind="attempted", target=target, session_id=live_rec.id)
                return "sent", live_rec.id
            # Unlike the completion nudge, this call site does NOT need an
            # ensure_driver: `live_rec` is only set when the session is already in
            # `self._drivers`, so a released (complete/detached) session takes the
            # fresh-spawn path below instead of silently dropping the prompt. A
            # "no_driver" here means the driver was released between that check and
            # the send — rare, and retried by the next sweep, but never silent.
            if result == "no_driver":
                log.warning("pr_doctor: driver for %s vanished mid-inject (target=%s)",
                            live_rec.id, target)
            # transient (draining / not actionable) → no attempt consumed.
            return "transient", None

        # NO live driver (owning session archived/gone) → spawn a fresh, de-duped
        # session seeded with the kind-appropriate first prompt. spawn_session
        # enforces the (origin, dedupe_key) de-dup + stamps provenance and emits
        # `spawned`; a repeated poll for the same target returns the existing
        # session instead of a second spawn.
        #
        # Resolve the ccswitch-owned clone `cwd` first (mirrors the New-Session
        # API): create_session's worktree block only runs when `cwd` is a real git
        # repo, so without it the spawn falls to an empty scratch dir with
        # branch=None and the seeded `gh pr checkout` can't work. The user's real
        # ~/GitHub checkouts are never touched.
        url = f"https://github.com/{repo}.git"
        phase("cloning")
        try:
            cwd = await asyncio.to_thread(
                gitrepos.ensure_clone, await self._effective_repo_root(), repo, url
            )
        except (gitrepos.CloneError, gitrepos.GhError) as exc:
            # Clone failed → error + notify, attempt NOT consumed (the target
            # re-arms so a later poll can retry).
            await self._pr_doctor_ctx.emit(
                "error", f"clone failed for {target}: {exc}",
                kind="spawn_failed", target=target)
            return "clone_failed", None
        from server.automations.model_pick import resolve_model
        phase("assessing")
        failing = ", ".join(
            d.name for d in info.checks_detail if d.conclusion == "failure")
        model, model_source = await resolve_model(
            await self._pr_doctor_config(),
            f"{kind} fix for PR {target}: {info.title}"
            + (f"\nfailing checks: {failing}" if failing else ""))
        phase("spawning")
        try:
            rec2 = await self._pr_doctor_ctx.spawn_session(
                origin_ref=target,
                dedupe_key=target,
                repo=repo,
                branch=branch,
                cwd=cwd,
                account=None,  # create_session picks a healthy account
                name=f"pr-doctor fix {target}",
                initial_prompt=prompt,
                model=model,
                detail={"model": model, "model_source": model_source}
                if model is not None else None,
            )
        except AllAccountsBusy as exc:
            # Distinguish genuine ceiling (all accounts exhausted) from a transient
            # reservation or reauth block so the caller and UI give accurate feedback.
            if exc.kind == "at_ceiling":
                await self._pr_doctor_ctx.emit(
                    "warn", f"spawn deferred for {target}: all accounts at/above usage ceiling",
                    kind="spawn_failed", target=target)
                return "ceiling", None
            # no_usable_accounts: reserved or needs-reauth — retry soon, not a ceiling.
            await self._pr_doctor_ctx.emit(
                "warn", f"spawn deferred for {target}: no usable account available",
                kind="spawn_failed", target=target)
            return "transient", None
        except Exception as exc:
            # Spawn failed → error + notify, attempt NOT consumed.
            await self._pr_doctor_ctx.emit(
                "error", f"spawn failed for {target}: {exc}",
                kind="spawn_failed", target=target)
            return "spawn_failed", None
        # Real successful spawn.
        sid = getattr(rec2, "id", None)
        await self._pr_doctor_ctx.emit(
            "info", f"auto-fix spawned for {target}",
            kind="attempted", target=target, session_id=sid)
        return "spawned", sid

    async def _pr_doctor_trigger_async(
        self, repo: str, branch: str, info: PullRequestInfo, kind: str, rec
    ) -> str:
        """Kick off a manual session-fix in the background; return its job id now.

        The clone→assess→spawn tail runs 12–41 s — far too long to hold the
        edge→daemon RPC open (issue #668). We create a job, schedule the tail as
        a background task that advances the job through its phases, and answer
        immediately with the id so the UI can poll ``pr_doctor_job``."""
        target = f"{repo}#{info.number}"
        job = self._pr_doctor_jobs.create(target=target, kind=kind)
        task = asyncio.create_task(
            self._pr_doctor_job_run(job, repo, branch, info, kind, rec),
            name=f"pr-doctor-job:{job.job_id}")
        self._pr_doctor_job_tasks.add(task)
        task.add_done_callback(self._pr_doctor_job_tasks.discard)
        return job.job_id

    async def _pr_doctor_job_run(self, job, repo: str, branch: str,
                                 info: PullRequestInfo, kind: str, rec) -> None:
        """Background tail of an async manual trigger: run the shared session
        executor, advancing ``job`` through its phases, then settle it. Reuses
        ``_pr_doctor_session_fix`` verbatim (single source of truth for the fix
        pipeline + its feed events) — only the job bookkeeping is new."""
        from server.automations.action_providers import _PR_STATUS_MAP
        from server.automations.pr_doctor_jobs import finalize
        try:
            status, sid = await self._pr_doctor_session_fix(
                repo, branch, info, kind, rec,
                on_phase=lambda p: self._pr_doctor_jobs.advance(job, p))
        except Exception:  # noqa: BLE001 — a bug in the tail must still settle the job.
            log.exception("pr_doctor.job_failed job=%s target=%s", job.job_id, job.target)
            finalize(job, "spawn_failed", level="error",
                     message="Action failed — see the automation feed")
            return
        level, message = _PR_STATUS_MAP.get(status, ("error", "Action failed — see the feed"))
        finalize(job, status, level=level, message=message, session_id=sid)

    def pr_doctor_job(self, job_id: str) -> dict | None:
        """Current state of an async manual-trigger job, or None if unknown
        (never created, or evicted / lost to a daemon restart). JSON-safe."""
        job = self._pr_doctor_jobs.get(job_id)
        return job.to_dict() if job is not None else None

    async def consolidation_candidates(self, repo: str | None = None) -> dict:
        """Open, non-draft PRs per repo (from the population cache, within
        pr-doctor scope) as ConsolidationCandidate dicts + suggested clusters,
        plus `active`: live consolidation sessions covering PRs in that repo
        (so the picker can flag rows already underway and link the session).
        Read-only; the picker calls this. Broader than _pr_doctor_scope_pairs
        (every open PR, not just session-authored ones)."""
        from sqlalchemy import select
        from server.automations.consolidation_engine import (
            ConsolidationCandidate, derive_scope, parse_consolidation_target,
            suggest_clusters)
        scope = await self._pr_doctor_scope()          # set[str] | None (None = all)
        out: dict[str, dict] = {}
        for r, prs in self._pr_population.all().items():
            if scope is not None and r not in scope:
                continue
            if repo is not None and r != repo:
                continue
            cands = [
                ConsolidationCandidate(
                    number=p.number, title=p.title, url=p.url, checks=p.checks,
                    mergeable=p.mergeable, updated_at=p.updated_at,
                    scope=derive_scope(p.title))
                for p in prs if p.state == "open" and not p.is_draft
            ]
            if not cands:
                continue
            out[r] = {"candidates": [c.to_dict() for c in cands],
                      "clusters": [cl.to_dict() for cl in suggest_clusters(cands)],
                      "active": []}
        if out:
            async with db_session() as db:
                rows = (await db.execute(select(DbSession).where(
                    DbSession.origin == "pr-doctor",
                    DbSession.origin_ref.like("consolidate:%"),
                    DbSession.status.not_in(INACTIVE_SESSION_STATUSES),
                ))).scalars().all()
            for row in rows:
                parsed = parse_consolidation_target(row.origin_ref or "")
                if parsed is None or parsed[0] not in out:
                    continue
                out[parsed[0]]["active"].append(
                    {"session_id": row.id, "numbers": parsed[1]})
            for group in out.values():
                group["active"].sort(key=lambda a: a["numbers"])
        return {"repos": out}

    async def _pr_doctor_consolidate(
        self, repo: str, numbers: list[int]
    ) -> tuple[str, str | None]:
        """Start ONE session consolidating the given PRs; answer immediately.

        Validates the selection against the population cache and de-dups on the
        sorted number set (live DB row OR an in-flight background spawn), all
        fast and inline. The slow tail — default-branch lookup, clone, model
        pick, spawn — runs in a background task (_pr_doctor_consolidate_spawn)
        so the edge→daemon RPC answers in milliseconds instead of 503ing at its
        timeout while the daemon spawns anyway (issue #567). Returns
        ("spawning", None) once the tail is scheduled; its outcome reaches the
        user through the automation feed events the tail emits."""
        from sqlalchemy import select
        from server.automations.consolidation_engine import (
            ConsolidationCandidate, consolidation_target, validate_selection)

        want = sorted({int(n) for n in numbers})
        by_number = {p.number: p for p in self._pr_population.cached(repo)}
        prs = [by_number[n] for n in want
               if n in by_number and by_number[n].state == "open"
               and not by_number[n].is_draft]
        if len(prs) != len(want):
            return "not_actionable", None
        cands = [ConsolidationCandidate(
            number=p.number, title=p.title, url=p.url, checks=p.checks,
            mergeable=p.mergeable, updated_at=p.updated_at, scope="") for p in prs]
        err = validate_selection(cands)
        if err is not None:
            return err, None                      # "too_few"

        target = consolidation_target(repo, want)
        # De-dup, cheapest first: a background spawn for this exact set is still
        # in flight (its DB row doesn't exist yet) → duplicate; then a live
        # consolidation session in the DB → duplicate. No await between the
        # in-flight check and the add below, so concurrent RPCs can't both pass.
        if target in self._consolidation_inflight:
            return "duplicate", None
        async with db_session() as db:
            existing = (await db.execute(select(DbSession).where(
                DbSession.origin == "pr-doctor",
                DbSession.origin_ref == target,
                DbSession.status.not_in(INACTIVE_SESSION_STATUSES)))).scalars().first()
        if existing is not None:
            return "duplicate", existing.id

        self._consolidation_inflight.add(target)
        task = asyncio.create_task(
            self._pr_doctor_consolidate_spawn(repo, target, prs),
            name=f"pr-doctor-consolidate:{target}")
        self._consolidation_tasks.add(task)
        task.add_done_callback(self._consolidation_tasks.discard)
        return "spawning", None

    async def _pr_doctor_consolidate_spawn(self, repo: str, target: str, prs) -> None:
        """Background tail of _pr_doctor_consolidate: clone, model pick, spawn.

        Every outcome (spawned / ceiling deferral / clone or spawn failure) is
        emitted to the pr-doctor feed — there is no RPC caller left to return a
        status to. Always releases the in-flight guard so a failure never wedges
        the PR set against retries."""
        from server.sessions.pr_fix import build_consolidation_prompt

        try:
            try:
                default_branch = await asyncio.to_thread(
                    lambda: gitrepos.list_remote_branches(repo).get("default") or "master"
                )
            except Exception:
                default_branch = "master"
            prompt = build_consolidation_prompt(repo, default_branch, prs)
            url = f"https://github.com/{repo}.git"
            try:
                cwd = await asyncio.to_thread(
                    gitrepos.ensure_clone, await self._effective_repo_root(), repo, url)
            except (gitrepos.CloneError, gitrepos.GhError) as exc:
                await self._pr_doctor_ctx.emit(
                    "error", f"clone failed for {target}: {exc}",
                    kind="spawn_failed", target=target)
                return
            from server.automations.model_pick import resolve_model
            model, model_source = await resolve_model(
                await self._pr_doctor_config(),
                f"consolidate {len(prs)} PRs in {repo}:\n"
                + "\n".join(f"#{p.number}  {p.title}" for p in prs))
            try:
                rec = await self._pr_doctor_ctx.spawn_session(
                    origin_ref=target, dedupe_key=target, repo=repo,
                    account=None, cwd=cwd,
                    name=f"pr-doctor consolidate {repo} ({len(prs)} PRs)",
                    initial_prompt=prompt,
                    model=model,
                    detail={"model": model, "model_source": model_source}
                    if model is not None else None)
            except AllAccountsBusy:
                await self._pr_doctor_ctx.emit(
                    "warn", f"consolidate deferred for {target}: all accounts at/above usage ceiling",
                    kind="spawn_failed", target=target)
                return
            except Exception as exc:  # noqa: BLE001
                await self._pr_doctor_ctx.emit(
                    "error", f"consolidate spawn failed for {target}: {exc}",
                    kind="spawn_failed", target=target)
                return
            sid = getattr(rec, "id", None)
            await self._pr_doctor_ctx.emit(
                "info", f"consolidation spawned for {target}",
                kind="attempted", target=target, session_id=sid)
        except Exception:  # noqa: BLE001 — feed emit itself failed, or a bug
            log.exception("pr_doctor.consolidate_background_failed target=%s", target)
        finally:
            self._consolidation_inflight.discard(target)

    async def _pr_doctor_mechanical(self, repo: str, info, kind: str) -> bool:
        """Run a mechanical (no-session) recovery action via the gh CLI seam.

        For ``kind == "out_of_date"`` calls GitHub's update-branch API to merge
        the base into the PR head so CI re-runs.  Uses ``self._gh_run`` which is
        injectable for tests (defaults to ``gitrepos._run_gh``).

        Returns True on gh success (caller should call record_attempt + set
        took_action); False on failure / exception (attempt NOT consumed so the
        target re-arms and retries next poll).
        """
        target = f"{repo}#{info.number}"
        if kind in ("out_of_date", "stuck_enqueue"):
            # stuck_enqueue: armed PR silently dropped from the merge queue (e.g.
            # ci-gate suite-shadowing). update-branch produces a new head → fresh CI
            # run → GitHub re-evaluates queue eligibility and re-enqueues.
            argv = ["api", "-X", "PUT", f"repos/{repo}/pulls/{info.number}/update-branch"]
            try:
                proc = self._gh_run(argv)
                if proc.returncode == 0:
                    await self._pr_doctor_ctx.emit(
                        "info", f"update-branch succeeded for {target}",
                        kind="updated_branch", target=target)
                    return True
                # 422 "no new commits": the base hasn't advanced since the poller
                # snapshot — the PR is effectively up to date. Treat as done (consume
                # the attempt) so already_acted fires and we stop retrying this head.
                body = getattr(proc, "stdout", "") or getattr(proc, "stderr", "") or ""
                if proc.returncode != 0 and "no new commits" in body.lower():
                    await self._pr_doctor_ctx.emit(
                        "info", f"update-branch no-op (already current) for {target}",
                        kind="updated_branch", target=target)
                    return True
                await self._pr_doctor_ctx.emit(
                    "warn", f"update-branch failed (rc={proc.returncode}) for {target}",
                    kind="mechanical_failed", target=target,
                    detail={"body": body[:300]})
                return False
            except Exception as exc:  # noqa: BLE001
                await self._pr_doctor_ctx.emit(
                    "warn", f"update-branch raised {exc!r} for {target}",
                    kind="mechanical_failed", target=target)
                return False
        # Unknown mechanical kind — warn and return False so the attempt is NOT
        # consumed; the engine will re-classify next poll rather than burning a try.
        log.warning("pr_doctor_mechanical: unknown kind=%r for %s", kind, target)
        return False

    async def _condense_estimate(self) -> float:
        from server.sessions import compaction_stats
        return await compaction_stats.current_estimate()

    async def _record_condense_ms(self, ms: float) -> None:
        from server.sessions import compaction_stats
        await compaction_stats.record_duration(ms)

    async def _persist_sub_agents(self, session_id: str) -> None:
        """Recompute the session's sub-agent summary from the transcript and persist
        it to the in-memory record + DB row. Best-effort; swallows all errors."""
        try:
            from .subagents import summarize_transcript
            # Full transcript (max_turns=None): the summary is persisted and shown
            # as the session's sub-agent chips — a 20-turn tail would silently drop
            # every agent spawned earlier in a long session.
            events = await self.transcript_events(session_id, max_turns=None)
            summary = summarize_transcript(events)
            rec = self._records.get(session_id)
            if rec is not None:
                rec.sub_agents = summary
            async with db_session() as db:
                from sqlalchemy import update as sa_update
                await db.execute(
                    sa_update(DbSession).where(DbSession.id == session_id).values(sub_agents=summary)
                )
        except Exception:
            log.debug("persist_sub_agents failed (non-fatal) session=%s", session_id, exc_info=True)

    async def _default_title_fn(self, prompts, *, account, cwd):
        """Default fallback title generator: a one-shot Haiku query under the account.

        Same *credential* gate as ``_generate_detached_suggestions`` (#749): a dead
        browser-OAuth refresh (``AuthError``/``FileNotFoundError`` from
        ``acquire_valid_token``) does not by itself mean the account is unusable — its
        setup-token (``oauth_env_for``, the single source of truth for model-auth env) may
        still work, the same way session launch treats it. Only give up, logging the
        account (never the token), when neither credential is available. Unlike #749's give-
        up path, this one is invoked repeatedly on a timer (``_titling_monitor`` ->
        ``_titling_sweep``, every ``_titling_interval``\\ s — 15s by default — for every
        still-auto-named session), so the give-up WARNING is separately rate-limited by
        ``self._title_auth_failed_until``/``_TITLE_AUTH_WARN_BACKOFF_S`` — see there for why
        this needed its own backoff rather than reusing ``_suggest_auth_failed_until``.
        Fail-open throughout: any lookup failure degrades to ``None`` rather than raising —
        this is a best-effort title generator.
        """
        from ccswitch.oauth import AuthError
        try:
            await asyncio.to_thread(ccswitch_accounts.acquire_valid_token, account)
        except (AuthError, FileNotFoundError) as exc:
            log.debug("title gen — token refresh failed account=%s: %s", account, exc)
            try:
                has_setup_token = bool(oauth_env_for(account))
            except Exception as lookup_exc:
                log.debug("title gen — setup-token lookup failed account=%s: %s", account, lookup_exc)
                has_setup_token = False
            if not has_setup_token:
                failed_until = self._title_auth_failed_until.get(account)
                if failed_until is None or time.monotonic() >= failed_until:
                    log.warning(
                        "title gen: giving up for account=%s — no valid OAuth token and no "
                        "setup-token", account,
                    )
                    self._title_auth_failed_until[account] = (
                        time.monotonic() + _TITLE_AUTH_WARN_BACKOFF_S
                    )
                return None
        return await generate_title(prompts, config_dir=str(compose(account)), cwd=cwd)

    def _account_limited(self, account: str) -> bool:
        """True if *account*'s last-known session usage is at/above the ceiling.

        Best-effort: an unknown account (no cached usage) is treated as not
        limited — a doomed title query would just degrade silently anyway.
        """
        usage = self._poller.cached(account)
        return usage is not None and getattr(usage, "session_pct", 0.0) >= _ROTATION_CEILING

    async def _generate_fallback_title(self, rec: SessionRecord, path: str) -> str | None:
        """Summarize the session's user prompts into a title, or ``None``.

        Used when Claude wrote no ``ai-title`` (the common case for SDK-driven
        sessions). Skips accounts at their usage ceiling and sessions with no
        readable prompts. Best-effort: any miss leaves the name unchanged.
        """
        if self._account_limited(rec.account):
            return None
        # The fallback title runs a Claude query *in the session's cwd*. If that working
        # dir has vanished (worktree removed/archived out from under a still-active record),
        # the query fails with "Working directory does not exist" on every sweep. Skip it —
        # the session is effectively broken; surface it once rather than spamming.
        if rec.cwd and not await asyncio.to_thread(os.path.isdir, rec.cwd):
            if rec.id not in self._missing_cwd_warned:
                self._missing_cwd_warned.add(rec.id)
                log.warning("session.titling_skipped id=%s — cwd missing: %s", rec.id, rec.cwd)
            return None
        # cwd is back (e.g. unarchived) — allow future warnings if it vanishes again.
        self._missing_cwd_warned.discard(rec.id)
        prompts = await asyncio.to_thread(read_user_prompts_from_file, path)
        if not prompts:
            return None
        return await self._title_fn(prompts, account=rec.account, cwd=rec.cwd)

    async def _maybe_apply_auto_name(self, session_id: str) -> None:
        """If the session name is still auto, adopt a title for it.

        Prefers Claude's own transcript ``ai-title``; when absent (SDK-driven
        sessions never get one) falls back to summarizing the user prompts. Updates
        the DB + in-memory record and, when a live driver exists, pushes a ``Title``
        event so the in-session header converges with the dashboard. Idempotent: a
        title equal to the current name is a no-op.
        """
        rec = self._records.get(session_id)
        if rec is None or not rec.name_is_auto:
            return
        path = self.transcript_path(session_id)
        if not path:
            return
        title = await asyncio.to_thread(read_ai_title_from_file, path)
        source = "claude"
        if not title:
            title = await self._generate_fallback_title(rec, path)
            source = "generated"
        rec = self._records.get(session_id)
        if rec is None or not rec.name_is_auto or not title or title == rec.name:
            return
        rec.name = title
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id).values(name=title)
            )
        driver = self._drivers.get(session_id)
        if driver is not None:
            driver.emit(ev.Title(title=title, source=source))
        log.info("session.auto_name id=%s title=%r source=%s", session_id, title, source)

    async def _titling_sweep(self) -> None:
        """One titling pass: refresh the auto-name of every active, still-auto session.

        The turn-boundary trigger ("done") handles the common case; this catches titles
        Claude writes after the final turn or slowly mid-turn. Never raises.
        """
        for sid, rec in list(self._records.items()):
            if rec.status == "ended" or not rec.name_is_auto:
                continue
            try:
                await self._maybe_apply_auto_name(sid)
            except Exception:  # never let one session break the sweep
                log.warning("titling sweep failed for %s", sid, exc_info=True)

    async def _titling_monitor(self) -> None:
        """Background loop: sweep titles every ``self._titling_interval`` seconds."""
        while True:
            await self._titling_sweep()
            await asyncio.sleep(self._titling_interval)

    async def _infer_goal_for(self, session_id: str) -> "str | None":
        """One-shot inferred-goal generation for a session (LLM; None on failure)."""
        from server.sessions.goal_inference import infer_goal
        rec = self._records.get(session_id)
        if rec is None:
            return None
        tail = await self._autopilot_transcript_tail(session_id)
        return await infer_goal(rec.name, tail,
                                config_dir=str(compose(rec.account)), cwd=rec.cwd)

    async def _goal_sweep(self) -> None:
        """One inferred-goal pass: refresh Session.inferred_goal for every active
        session whose transcript has ADVANCED since its last inference.

        Skips sessions with an explicit /goal (explicit wins — inferring is wasted)
        and never clobbers a prior inferred goal with a failed inference. Peer of
        _titling_sweep; the goal is the middle tier of effective_goal(), which the
        session header and autopilot's completion judgement read."""
        from sqlalchemy import update as sa_update
        for sid, rec in list(self._records.items()):
            if rec.status in ("ended", "archived") or (rec.active_goal or "").strip():
                continue
            try:
                window = await self.transcript_window(sid, limit=1)
                if window is None or window.total_turns == 0:
                    continue
                if self._goal_marker.get(sid) == window.total_turns:
                    continue  # transcript unchanged since last inference — skip (cost)
                goal = await self._infer_goal_for(sid)
                # Mark BEFORE checking the result so a failed inference is not
                # retried every sweep on an idle session; the next real turn
                # changes the marker and re-triggers.
                self._goal_marker[sid] = window.total_turns
                if not goal:
                    continue  # keep the prior value — never clobber on failure
                rec.inferred_goal = goal
                async with db_session() as db:
                    await db.execute(sa_update(DbSession).where(DbSession.id == sid)
                                     .values(inferred_goal=goal))
            except Exception:  # one session must never break the sweep
                log.warning("goal sweep failed for %s", sid, exc_info=True)

    async def _goal_monitor(self) -> None:
        """Background loop: sweep inferred goals every ~2 minutes."""
        while True:
            await self._goal_sweep()
            await asyncio.sleep(120.0)

    async def _persist_claude_session_id(self, session_id: str, claude_session_id: str) -> None:
        """Callback fired by the driver the first time it captures the Claude session id.

        Keeps the in-memory record in sync and immediately persists to the DB so the id
        survives a restart without waiting for rotation.
        """
        rec = self._records.get(session_id)
        if rec is not None:
            rec.claude_session_id = claude_session_id
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession)
                .where(DbSession.id == session_id)
                .values(claude_session_id=claude_session_id)
            )

    def _record_from_row(self, row, **overrides) -> SessionRecord:
        """Single source of truth for rebuilding an in-memory SessionRecord from a
        DB row (rehydrate + unarchive). Every row-backed field maps HERE, once — the
        previous hand-maintained parallel constructions drifted and silently dropped
        fields (origin/origin_ref vanished on restart, autopilot_paused on unarchive:
        the purple provenance badge disappearing after a redeploy was this bug).
        Call sites pass only their genuine differences as ``overrides``."""
        fields = dict(
            id=row.id,
            name=row.name,
            account=row.account_name,
            claude_session_id=row.claude_session_id or "",
            cols=80,
            rows=24,
            cwd=row.cwd,
            repo=row.repo,
            branch=getattr(row, "branch", None),
            origin=getattr(row, "origin", None),
            origin_ref=getattr(row, "origin_ref", None),
            assisted_by=getattr(row, "assisted_by", None),
            detached=True,
            status=row.status,
            name_is_auto=row.name_is_auto,
            permission_mode=row.permission_mode,
            created_at=row.created_at,
            updated_at=getattr(row, "updated_at", None),
            last_turn_at=getattr(row, "last_turn_at", None),
            adopted_in_place=bool(getattr(row, "adopted_in_place", False)),
            model=getattr(row, "model", None),
            pending_model=getattr(row, "pending_model", None),
            account_pinned=bool(getattr(row, "account_pinned", False)),
            active_goal=getattr(row, "active_goal", None),
            inferred_goal=getattr(row, "inferred_goal", None),
            autopilot_paused=bool(getattr(row, "autopilot_paused", False)),
            auto_archive_opt_out_reason=getattr(row, "auto_archive_opt_out_reason", None),
            authored_branches=list(getattr(row, "authored_branches", None) or []),
            context_used_tokens=getattr(row, "context_used_tokens", None),
            context_max_tokens=getattr(row, "context_max_tokens", None),
            command_catalog=getattr(row, "command_catalog", None),
            pr_resolution=getattr(row, "pr_resolution", None),
            pr_resolution_source=getattr(row, "pr_resolution_source", None),
            host=getattr(row, "host", None),
        )
        fields.update(overrides)
        return SessionRecord(**fields)

    async def rehydrate(self) -> int:
        """Rebuild detached SessionRecords from non-ended DB rows (lazy: no drivers).

        Idempotent: skips ids already present in _records. Normalizes a pre-crash
        'working' status to 'idle' (the process is gone). Returns the count of rows added.
        """
        from sqlalchemy import select as sa_select
        async with db_session() as db:
            rows = (await db.execute(
                sa_select(DbSession).where(
                DbSession.status.notin_(INACTIVE_SESSION_STATUSES)
            )
            )).scalars().all()
        # Host partition (#852): a worker daemon loads ONLY the rows the dispatcher
        # stamped with its own host name. NULL-host rows (pre-fleet legacy + the
        # primary's own automation spawns, which never pass through the dispatcher)
        # belong to the primary. The primary itself still loads every row: the fleet
        # dashboard is served from ITS _records, and it is the recovery actor of last
        # resort for rows whose host has left the registry — per-row ownership for
        # its acting loops is enforced by _owns_record, not here.
        if daemon_role() == "worker":
            me = self_host_name()
            foreign = [r for r in rows if r.host != me]
            if foreign:
                log.info(
                    "rehydrate: worker host=%r skipping %d row(s) owned elsewhere "
                    "(%d with host=NULL -> primary)", me, len(foreign),
                    sum(1 for r in foreign if r.host is None))
            rows = [r for r in rows if r.host == me]
        from .background_tasks import is_parked
        added = 0
        backfilled: dict[str, str] = {}
        healed: dict[str, str] = {}
        for row in rows:
            if row.id in self._records:
                continue
            # A pre-crash live status has no driver anymore → normalize to idle. This
            # covers both mid-turn statuses AND the awaiting-human ones (waiting_input /
            # waiting_question / ready_review): all are backed only by the live driver's
            # in-memory registries, which the restart wiped, so none can re-derive — left
            # as-is they show a dead "needs you" badge with no prompt (the orphaned
            # permission the user reported). When we DO normalize, the daemon restart
            # dropped an in-flight turn — flag it (issue #434) so the dashboard says
            # "interrupted by daemon restart — re-send to resume" instead of a generic
            # idle card. Transient: the flag lives only on this in-memory record and
            # clears on the next turn.
            was_interrupted = row.status in _RESTART_TRANSIENT_STATUSES
            status = "idle" if was_interrupted else row.status
            # Persist waiting_input → idle to DB when the backing process is gone
            # (issue #789, companion to #785). waiting_input sessions where the process
            # died between daemon runs leave the DB row stale: the in-memory normalization
            # above corrects the live view, but the DB retains waiting_input until the next
            # _persist_status call — which never comes if the session stays idle. A null pid
            # means there is no tracked process to check; treat it as dead. A non-null pid
            # gets an explicit liveness probe so a session whose process genuinely survived
            # the restart (e.g. a soft reload) is not incorrectly marked idle in the DB.
            if was_interrupted and row.status == "waiting_input":
                pid = getattr(row, "pid", None)
                if pid is None or not _adopt._pid_alive(pid):
                    healed[row.id] = "idle"
            # Backfill the PR-join branch for legacy sessions created before the
            # branch was persisted (NULL column). Derive it once from the session's
            # worktree and write it back so the column becomes the source of truth.
            branch = getattr(row, "branch", None)
            if branch is None and row.cwd and worktrees.is_git_repo(row.cwd):
                derived = await asyncio.to_thread(worktrees.current_branch, row.cwd)
                if derived:
                    branch = derived
                    backfilled[row.id] = derived
            # interrupted_by_restart (#434) is a TRANSIENT in-memory flag, not a
            # row-backed field — so it threads through as a call-site override
            # rather than living in _record_from_row's row mapping.
            self._records[row.id] = self._record_from_row(
                row, status=status, branch=branch, interrupted_by_restart=was_interrupted)
            added += 1
            log_event(log, logging.INFO, "session.reattach",
                      session_id=row.id,
                      **{"from": row.status, "to": status, "reason": "rehydrate"})
            # Self-heal a stuck `waiting_external`: that status is only ever an overlay
            # on a finished `done` turn (see _persist_status), so if the session is no
            # longer parked — e.g. its background pollers were killed by a deploy and are
            # now dead, or a self-scheduled wakeup was lost — its true status is `done`.
            # Restore it so the card stops showing "Waiting…" forever. Cheap: only the
            # (few) waiting_external rows hit the transcript scan.
            if status == "waiting_external":
                path = self.transcript_path(row.id)
                if not await asyncio.to_thread(is_parked, path):
                    self._records[row.id].status = "done"
                    healed[row.id] = "done"
                    log_event(log, logging.INFO, "session.unpark",
                              session_id=row.id,
                              **{"from": "waiting_external", "to": "done",
                                 "reason": "bg_tasks_cleared"})
        # Persist any derived branches + healed statuses so the next rehydrate reads them directly.
        if backfilled or healed:
            from sqlalchemy import update as sa_update
            async with db_session() as db:
                for sid, br in backfilled.items():
                    await db.execute(
                        sa_update(DbSession).where(DbSession.id == sid).values(branch=br)
                    )
                for sid, st in healed.items():
                    await db.execute(
                        sa_update(DbSession).where(DbSession.id == sid).values(status=st)
                    )
        log.info("rehydrate: %d session(s) restored (%d branch backfilled, %d status healed)",
                 added, len(backfilled), len(healed))
        self._startup_note("rehydrated", added)
        # Reconcile 'queued' rows with no session_queue entry BEFORE draining, so
        # the drain never has to reach a row it structurally cannot see, and a
        # reconciled row (now 'ended') is reflected in the very same pass. Reuse
        # the rows already selected above (see _reconcile_orphan_queued's
        # candidate_rows docstring) instead of re-querying.
        await self._reconcile_orphan_queued(candidate_rows=rows)
        # After rebuilding in-memory records, drain the queue: a restarted daemon may
        # have fewer active sessions than the cap (e.g. some were closed before restart),
        # so queued sessions should fill those free slots immediately. Tracked via _spawn
        # so stop() can cancel before it writes into a later test's DB.
        self._spawn(self._maybe_start_queued(), name="queue-drain-on-rehydrate")
        return added

    async def rearm_background_tasks(self) -> int:
        """Relaunch background tasks snapshotted at the last drain. Called once after
        rehydrate() on daemon start; safe to call again (cleared snapshots + _rearmed
        make it idempotent). Returns the count of sessions re-armed."""
        from sqlalchemy import or_
        from sqlalchemy import select as sa_select
        async with db_session() as db:
            rows = (await db.execute(sa_select(DbSession).where(
                or_(DbSession.pending_rearm.isnot(None),
                    DbSession.pending_prompt.isnot(None))))).scalars().all()
        pending = [(r.id, r.pending_rearm, r.pending_prompt) for r in rows
                   if (r.pending_rearm or r.pending_prompt) and r.id not in self._rearmed]
        if not pending:
            # Idempotent re-calls must not reset a prior boot's count to 0.
            self._startup_note("rearmed", self.startup_report().get("rearmed") or 0)
            return 0
        sem = asyncio.Semaphore(int(os.environ.get("CCSWITCH_REARM_CONCURRENCY", "3")))

        async def _one(sid: str, snap: list | None, prompt: str | None) -> bool:
            async with sem:
                try:
                    return await self._rearm_session(sid, snap, prompt)
                except Exception:
                    log.warning("rearm: session %s failed; snapshot retained", sid,
                                exc_info=True)
                    return False

        results = await asyncio.gather(
            *(_one(sid, snap, prompt) for sid, snap, prompt in pending))
        n = sum(1 for ok in results if ok)
        log.info("rearm: re-armed %d/%d session(s)", n, len(pending))
        self._startup_note("rearmed", (self.startup_report().get("rearmed") or 0) + n)
        return n

    async def _rearm_session(self, sid: str, snap: list | None,
                             prompt: str | None = None) -> bool:
        from sqlalchemy import update as sa_update
        tasks = [OpenBgTask.from_dict(d) for d in (snap or []) if isinstance(d, dict)]
        if not tasks and not prompt:
            return False
        driver = await self.ensure_driver(sid)
        if driver is None:
            return False
        if tasks:
            await driver.send_prompt(build_rearm_prompt(tasks))
        if prompt:
            await driver.send_prompt(prompt)
        self._rearmed.add(sid)
        async with db_session() as db:
            await db.execute(sa_update(DbSession).where(DbSession.id == sid)
                             .values(pending_rearm=None, pending_prompt=None))
        return True

    async def _maybe_rearm_on_attach(self, session_id: str) -> None:
        """Retry a pending re-arm when a session is attached (covers sessions the auto
        pass skipped or failed). Idempotent via _rearmed; never raises into attach."""
        if session_id in self._rearmed:
            return
        from sqlalchemy import select as sa_select
        try:
            async with db_session() as db:
                row = (await db.execute(sa_select(DbSession).where(
                    DbSession.id == session_id))).scalar_one_or_none()
            snap = row.pending_rearm if row else None
            prompt = row.pending_prompt if row else None
            if snap or prompt:
                await self._rearm_session(session_id, snap, prompt)
        except Exception:
            log.warning("rearm-on-attach failed session=%s", session_id, exc_info=True)

    def list_sessions(self) -> list[SessionRecord]:
        """Return all in-memory SessionRecords."""
        return list(self._records.values())

    def get_session(self, session_id: str) -> SessionRecord | None:
        """Return the in-memory SessionRecord for session_id, or None."""
        return self._records.get(session_id)

    async def _refresh_live_branches(self) -> None:
        """Re-read each repo-backed session's branch from its live worktree HEAD.

        The completion workflow moves a worktree onto a fresh PR branch after the
        record's branch was first set, so the stored value goes stale. Re-derive it
        off the event loop; only overwrite with a real (non-None) live branch.
        """
        for rec in list(self._records.values()):
            if not (rec.repo and rec.cwd):
                continue
            if not await asyncio.to_thread(worktrees.is_git_repo, rec.cwd):
                continue
            live = await asyncio.to_thread(worktrees.current_branch, rec.cwd)
            if live and live != rec.branch:
                rec.branch = live

    async def _pr_pairs(self) -> list[tuple[str, str]]:
        """Distinct (repo, branch) pairs for every PR a session may have authored.

        Unions three sources so neither PR-badge mechanism is lost and archived
        sessions remain visible to PR Doctor:

        1. Live in-memory ``_records``: ``authored_branches`` (multi-PR set)
           and the live-refreshed ``record.branch`` (current PR head).
        2. DB-persisted sessions (including ARCHIVED / ended): their
           ``authored_branches`` and ``branch`` columns are added best-effort.
           A DB failure falls back gracefully to the live-only set.
        3. Self-prune: any ``(repo, branch)`` whose previous-poll cache says
           the PR is terminal (merged / closed) is excluded before returning,
           so ``authored_branches`` that only grows never bloats the GraphQL
           query unboundedly.  A branch with no cache yet (None) is kept.
        """
        await self._refresh_live_branches()
        # Remote-tracking names (origin/*) can persist in rows written before
        # authored_branches filtered them; a PR headRefName never carries the
        # remote prefix, so drop them here rather than migrate old rows.
        seen: set[tuple[str, str]] = set()

        def _add(repo: str, br: str | None) -> None:
            if br and not worktrees.is_remote_ref(br):
                seen.add((repo, br))

        for r in self._records.values():
            if not r.repo:
                continue
            for br in self._session_pr_branches(r):
                _add(r.repo, br)

        # --- Source 2: archived / all DB sessions (best-effort) ---
        try:
            from sqlalchemy import select as sa_select
            async with db_session() as db:
                rows = (
                    await db.execute(
                        sa_select(
                            DbSession.repo,
                            DbSession.branch,
                            DbSession.authored_branches,
                        ).where(DbSession.repo.is_not(None))
                    )
                ).all()
            for row_repo, row_branch, row_authored in rows:
                if not row_repo:
                    continue
                for br in (row_authored or []):
                    _add(row_repo, br)
                _add(row_repo, row_branch)
        except Exception:
            log.warning("orchestrator._pr_pairs_db_query_failed", exc_info=True)

        # --- Source 3: self-prune terminal PRs to bound growth ---
        # Defensive: a minimally-constructed orchestrator (some tests build via
        # __new__) may have no poller; skip pruning rather than raise.
        poller = getattr(self, "_pr_poller", None)
        if poller is not None:
            pruned: set[tuple[str, str]] = set()
            for repo, br in seen:
                cached = poller.cached(repo, br)
                if cached is not None and cached.state in ("merged", "closed"):
                    pruned.add((repo, br))
            seen -= pruned

        return list(seen)

    def pr_for(self, repo, branch):
        """Cached PullRequestInfo for a (repo, branch), or None."""
        return self._pr_poller.cached(repo, branch)

    def _pr_population_pr(self, repo: str, branch: str) -> "PullRequestInfo | None":
        """A population-cached PR for (repo, branch), or None.

        The population poller caches every open PR per repo (keyed by repo only);
        each PR carries its ``head_ref`` so PR Doctor can resolve one that no
        ccswitch session authored — i.e. is absent from the per-branch cache."""
        for info in self._pr_population.cached(repo):
            if info.head_ref == branch:
                return info
        return None

    async def _pr_doctor_scope(self) -> "set[str] | None":
        """PR Doctor's configured repo scope; None means 'all watched repos'."""
        from sqlalchemy import select
        from server.db.models import Automation
        try:
            async with db_session() as db:
                row = (await db.execute(select(Automation).where(
                    Automation.id == "pr-doctor"))).scalar_one_or_none()
            repos = list((row.config or {}).get("enabled_repos") or []) if row else []
        except Exception:  # pragma: no cover — defensive
            log.warning("orchestrator._pr_doctor_scope_failed", exc_info=True)
            repos = []
        return set(repos) or None

    async def _pr_doctor_scope_pairs(self) -> list[tuple[str, str]]:
        """(repo, branch) pairs PR Doctor should evaluate: every open PR in the
        population cache within the configured scope, UNION the authored pairs.

        The union keeps archived-session PRs and not-yet-populated repos covered
        while widening PR Doctor from authored-only to every open PR in scope."""
        scope = await self._pr_doctor_scope()
        seen: set[tuple[str, str]] = set(await self._pr_pairs())
        for repo, prs in self._pr_population.all().items():
            if scope is not None and repo not in scope:
                continue
            for info in prs:
                # Population cache includes recently merged PRs; only open/draft PRs
                # need PR Doctor attention — merged/closed ones are terminal.
                if info.head_ref and info.state not in ("merged", "closed"):
                    seen.add((repo, info.head_ref))
        return list(seen)

    @staticmethod
    def _session_pr_branches(rec) -> list[str]:
        """Every branch that may carry a PR this session authored: the accumulated
        authored_branches set plus the live-refreshed current branch.
        refresh_authored_branches filters rec.branch as the scratch branch, so a
        session sitting ON its PR branch has it ONLY in rec.branch — every
        consumer (prs_for, _autopilot_pr_facts, _pr_pairs) must union both or a
        just-opened PR stays invisible until the session checks out elsewhere."""
        branches = list(getattr(rec, "authored_branches", None) or [])
        br = getattr(rec, "branch", None)
        if br and br not in branches:
            branches.append(br)
        return branches

    def prs_for(self, session_id: str) -> list[PullRequestInfo]:
        """Deduped, ordered PR list for a session's authored branches.

        Open/draft first, then merged/closed, newest updatedAt first — matches the
        single-PR ranking. Deduped by PR number (the same PR can surface via more
        than one branch ref).
        """
        from server.github.pr_status import _STATE_RANK
        rec = self._records.get(session_id)
        if rec is None or not rec.repo:
            return []
        by_number: dict[int, PullRequestInfo] = {}
        for br in self._session_pr_branches(rec):
            info = self._pr_poller.cached(rec.repo, br)
            if info is not None:
                by_number.setdefault(info.number, info)
        # PullRequestInfo carries no updatedAt (dropped when built from the poller),
        # so -number is the newest-first tie-break within a state rank.
        return sorted(
            by_number.values(),
            key=lambda p: (_STATE_RANK.get(p.state, 99), -p.number),
        )

    def prs_for_many(self, session_ids: list[str]) -> dict[str, list[PullRequestInfo]]:
        """Batched :meth:`prs_for` — one entry per requested id.

        Isolated per session: a failure yields [] for that id alone. This mirrors the
        blast radius of the per-session calls it replaces (#742); a batch that failed
        whole-hog would turn one bad session into a blank page.
        """
        out: dict[str, list[PullRequestInfo]] = {}
        for sid in session_ids:
            try:
                out[sid] = self.prs_for(sid)
            except Exception:
                log.warning("prs_for_many: failed for %s", sid, exc_info=True)
                out[sid] = []
        return out

    async def _pr_repos(self) -> list[str]:
        """Distinct repos to watch: live in-memory sessions ∪ all persisted sessions
        (incl. archived), so a repo with only an archived session still appears."""
        repos = {r.repo for r in self._records.values() if r.repo}
        from sqlalchemy import select as sa_select
        try:
            async with db_session() as db:
                rows = (
                    await db.execute(
                        sa_select(DbSession.repo).where(DbSession.repo.is_not(None)).distinct()
                    )
                ).scalars().all()
            repos.update(r for r in rows if r)
        except Exception:
            log.warning("orchestrator.pr_repos_db_query_failed", exc_info=True)
        return sorted(repos)

    @staticmethod
    def _stage_models_sync(pop: dict, history) -> "dict[str, StageModel]":
        """The blocking half of ``all_repos_stage_models``: one ``ci_graph``
        SQLite read per repo plus pure compute.

        Runs on a worker thread (RC1) — it must not touch the event loop,
        awaitables, or any asyncio primitive. ``pop`` is the already-snapshotted
        PR population, so nothing here reads mutable orchestrator state.
        """
        from server.pipeline.track import build_stage_model, order_deploy_envs
        models: dict = {}
        for repo in list(pop.keys()):
            prs = pop.get(repo, ())
            has_queue = any(p.stage == "queue" for p in prs)
            repo_envs: set[str] = set()
            for p in prs:
                repo_envs.update(getattr(p, "deployed_envs", ()) or ())
            ci_graph = history.ci_graph(repo) if history is not None else None
            models[repo] = build_stage_model(
                repo, ci_graph=ci_graph, has_queue=has_queue,
                deploy_envs=order_deploy_envs(repo_envs),
            )
        return models

    async def all_repos_stage_models(self) -> "dict[str, StageModel]":
        """Per-repo StageModel for every watched repo (derived from CI/CD facts).

        The edge's /api/pipeline/events SSE generator drives this every 10s *per
        connected client*, and each repo costs a synchronous ``HistoryStore``
        read, so the whole build runs on a worker thread (RC1). Callers already
        route through ``inspect.isawaitable``/``_maybe_await``; the payload is
        unchanged.
        """
        pop = self._pr_population.all()
        history = self._pr_population.history_store()
        return await asyncio.to_thread(self._stage_models_sync, pop, history)

    async def all_merge_trains(self) -> "dict[str, MergeTrainState]":
        """Merge-train state for every watched repo, derived from the 24-hour
        merge-queue observation window plus existing PR population data.
        """
        from server.pipeline.merge_train import derive_merge_train_state
        from server.pipeline import merge_queue_store
        import time as _time
        now = int(_time.time())
        since = now - 86400
        out: dict = {}
        pop = self._pr_population.all()
        for repo, prs in pop.items():
            obs = await merge_queue_store.window(repo, since)
            qa, prod = self._deploy_backlog(prs)
            out[repo] = derive_merge_train_state(repo, obs, now, qa, prod)
        return out

    async def latest_queue_entries(self) -> "dict[str, list[QueueEntry]]":
        """Newest merge-queue observation per repo (for the pipeline SSE feed).

        Same 24-hour window all_merge_trains reads; only the last (newest)
        observation matters here — it is the queue's current shape.
        """
        from server.pipeline import merge_queue_store
        import time as _time
        since = int(_time.time()) - 86400
        out: dict[str, list[QueueEntry]] = {}
        for repo in self._pr_population.all():
            obs = await merge_queue_store.window(repo, since)
            if obs:
                out[repo] = list(obs[-1].entries)
        return out

    async def ready_merge(self, repo: str, number: int) -> dict:
        """Plotroom PrRow 'Ready + auto-merge' action: mark a draft PR ready,
        then arm squash auto-merge. Tolerates already-ready / already-armed."""
        def _do() -> dict:
            ready = self._gh_run(["pr", "ready", str(number), "-R", repo])
            # gh exits non-zero when the PR is already ready — that's not an error.
            marked = ready.returncode == 0
            merge = self._gh_run(
                ["pr", "merge", str(number), "-R", repo, "--squash", "--auto"]
            )
            # gh (2.x) fails with "...auto-merge is already enabled for this pull
            # request" when re-arming; that state is the caller's goal, not an error.
            already = merge.returncode != 0 and "already enabled" in (merge.stderr or "")
            armed = merge.returncode == 0 or already
            # cleanReadyToMerge requires a mergeability probe (Phase 3); honest False
            # until that probe exists — never fabricate.
            return {"markedReady": marked, "autoMergeArmed": armed,
                    "alreadyArmed": already, "cleanReadyToMerge": False}

        return await asyncio.to_thread(_do)

    # ------------------------------------------------------------------
    # Deploy environment config + status (SPEC §3)
    # ------------------------------------------------------------------

    async def deploy_config_list(self) -> dict:
        """All RepoDeployConfig rows as {repo: {environments, enabled}}."""
        import json as _json
        from sqlalchemy import select as _select
        from server.db import db_session as _db_session
        from server.db.models import RepoDeployConfig as _Cfg

        async with _db_session() as db:
            rows = (await db.execute(_select(_Cfg))).scalars().all()
        out: dict[str, dict] = {}
        for row in rows:
            try:
                envs = _json.loads(row.environments or "[]")
            except (ValueError, TypeError):
                envs = []
            out[row.repo] = {"environments": envs, "enabled": bool(row.enabled)}
        return out

    async def deploy_config_put(
        self, repo: str, environments: list[dict], enabled: bool = True
    ) -> None:
        """Upsert one repo's deploy-environment config."""
        import json as _json
        from server.db import db_session as _db_session, dialect_insert as _dialect_insert
        from server.db.models import RepoDeployConfig as _Cfg

        stmt = _dialect_insert(_Cfg).values(
            repo=repo, environments=_json.dumps(environments), enabled=enabled
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["repo"],
            set_={
                "environments": stmt.excluded.environments,
                "enabled": stmt.excluded.enabled,
            },
        )
        async with _db_session() as db:
            await db.execute(stmt)

    def deploy_status(self) -> dict:
        """Raw per-repo env health cache from the watcher (Task 6 payload)."""
        return self._deploy_watcher.status()

    def git_credential_alerts(self) -> dict:
        """Repos whose clone can't fetch due to stale git credentials (#670).

        Reads the daemon-process ``gitrepos`` registry that ``ensure_clone``
        populates on an auth-class fetch failure, so the edge can surface a
        "git credential refresh needed" banner. Auto-clears once a fetch
        succeeds (the registry drops the entry)."""
        return {"alerts": gitrepos.stale_credential_repos()}

    def dismiss_git_credential_alert(self, repo: str) -> dict:
        """Clear one repo's stale-credential alert (#670).

        A dismissal is advisory: if the credential is still broken, the next
        failed fetch re-adds the entry. Returns whether an entry was present."""
        return {"cleared": gitrepos.clear_stale_credentials(repo)}

    @staticmethod
    def _check_insights_sync(store, repo: str, win: str, empty: dict,
                             since_iso: str, prev_since_iso: str) -> dict:
        """The blocking half of ``check_insights``: four synchronous
        ``HistoryStore`` queries (one of them 1+N) plus JSON serialization.

        Runs on a worker thread (RC1) — it must not touch the event loop,
        awaitables, or any asyncio primitive.
        """
        try:
            slowest = store.slowest_checks(repo, since_iso)
            flakiest = store.flakiest_checks(repo, since_iso)
            regressions = store.duration_regressions(repo)
            runner_wait = store.runner_wait_health(repo, since_iso, prev_since_iso)
        except Exception:
            return empty
        return {
            "repo": repo, "window": win,
            "slowest": [s.to_dict() for s in slowest],
            "flakiest": [f.to_dict() for f in flakiest],
            "regressions": [r.to_dict() for r in regressions],
            "runner_wait_health": [w.to_dict() for w in runner_wait],
        }

    async def check_insights(self, repo: str, window: str, now=None) -> dict:
        """Per-repo Check Health: slowest + flakiest checks, duration regressions,
        and runner-wait health over a time window.
        Reads the read-only HistoryStore; returns a JSON-safe payload. Empty lists
        when history is unavailable or the repo has no rows (never raises).

        The four queries are synchronous SQLite over the full window (and
        ``duration_regressions`` is 1+N), so they run on a worker thread (RC1) —
        opening the Check Health panel must not stall the daemon's event loop.
        Callers already route through ``inspect.isawaitable``/``_maybe_await``;
        the payload is unchanged."""
        from datetime import datetime, timedelta, timezone
        _WINDOWS = {"24h": timedelta(hours=24), "7d": timedelta(days=7),
                    "14d": timedelta(days=14), "30d": timedelta(days=30)}
        win = window if window in _WINDOWS else "7d"
        store = self._pr_population.history_store()
        empty = {"repo": repo, "window": win, "slowest": [], "flakiest": [],
                 "regressions": [], "runner_wait_health": []}
        if store is None or not repo:
            return empty
        now = now or datetime.now(timezone.utc)
        since_iso = (now - _WINDOWS[win]).strftime("%Y-%m-%dT%H:%M:%SZ")
        prev_since_iso = (now - 2 * _WINDOWS[win]).strftime("%Y-%m-%dT%H:%M:%SZ")
        return await asyncio.to_thread(
            self._check_insights_sync, store, repo, win, empty,
            since_iso, prev_since_iso,
        )

    @staticmethod
    def _deploy_backlog(prs) -> "tuple[int, int]":
        """Compute awaiting_qa / awaiting_prod from the cached PR population.

        Reuses existing PipelinePr / PullRequestInfo fields — does NOT re-query GitHub.
        awaiting_qa  = merged & not deployed to any environment.
        awaiting_prod = merged & not live in a prod-category env.

        ``p.deployed`` is the canonical "live in production" bool (routed through
        ``classify_env`` in pr_status). Reusing it fixes namespaced prod envs
        (e.g. "kindash / production") here too — the old ``"production" not in
        deployed_envs`` exact-match silently counted every KinDash merge.
        """
        awaiting_prod = sum(1 for p in prs if p.merged and not p.deployed)
        awaiting_qa = sum(1 for p in prs if p.merged and not p.deployed_envs)
        return awaiting_qa, awaiting_prod

    async def all_prs(self) -> "list[PipelinePr]":
        """Full PR population (all watched repos) joined to authoring sessions.

        Session join reuses ``prs_for`` (the session-branch poller's knowledge),
        so a PR is linked iff some session authored its branch. Sorted by stage
        urgency (parked→ci→ready→queue→merged), then PR number ascending.

        Async because the stage models it places PRs on are built off the loop
        (RC1 — see ``all_repos_stage_models``); the payload is unchanged and
        every caller already routes through ``inspect.isawaitable``/
        ``_maybe_await``.
        """
        from server.github.pr_status import PipelinePr
        from server.pipeline.track import place_pr_on_track
        sess_by_pr: dict[tuple[str, int], tuple[str, str]] = {}
        for rec in self._records.values():
            if not rec.repo:
                continue
            for info in self.prs_for(rec.id):
                sess_by_pr.setdefault((rec.repo, info.number), (rec.id, rec.name))
        order = {"parked": 0, "ci": 1, "ready": 2, "queue": 3, "merged": 4}
        models = await self.all_repos_stage_models()
        out: list[PipelinePr] = []
        for repo, prs in self._pr_population.all().items():
            model = models.get(repo)
            for info in prs:
                sid, sname = sess_by_pr.get((repo, info.number), (None, None))
                states = tuple(place_pr_on_track(info, model)) if model else ()
                out.append(PipelinePr(pr=info, session_id=sid, session_name=sname, node_states=states))
        out.sort(key=lambda p: (order.get(p.pr.stage, 99), p.pr.number))
        return out

    async def _pr_autofix_mode(self) -> str:
        """Current pr-doctor mode from the automation registry row; NULL/unknown → 'propose'."""
        from sqlalchemy import select
        from server.db.models import Automation
        async with db_session() as db:
            row = (await db.execute(
                select(Automation).where(Automation.id == "pr-doctor"))).scalar_one_or_none()
            mode = (row.mode if row else None) or "propose"
        from server.sessions.pr_fix import VALID_AUTOFIX_MODES
        return mode if mode in VALID_AUTOFIX_MODES else "propose"

    async def request_pr_fix(self, session_id: str, pr_number: int) -> str:
        """Inject a branch-aware fix prompt for the session's PR #pr_number.

        Single source of truth for "restore this PR", shared by the auto path
        (_on_pr_change) and the manual propose endpoint. Resolves the (repo,
        branch) for the PR number from the session's authored branches, gates on
        an actionable (open/draft) state, and injects via send_prompt. Returns a
        status string; does not resume a driver and does not apply the loop guard.
        """
        rec = self._records.get(session_id)
        if rec is None or not rec.repo:
            return "no_session"
        info = None
        for br in getattr(rec, "authored_branches", None) or []:
            cached = self._pr_poller.cached(rec.repo, br)
            if cached is not None and cached.number == pr_number:
                info = cached
                break
        if info is None:
            return "no_pr"
        if info.state not in ("open", "draft"):
            return "not_actionable"
        prompt = build_pr_fix_prompt(rec.repo, info)
        return await self.send_prompt(session_id, prompt)

    def _pr_doctor_owning_rec(self, repo: str, branch: str):
        """The live session record that authored *branch* in *repo*, or None.

        Used by ``_pr_doctor_session_fix`` to route the session executor at a
        LIVE driver when the owning session is still running (inject) vs. a fresh
        spawn when it is gone. Prefers a record with a live driver.
        """
        fallback = None
        for r in self._records.values():
            if r.repo != repo:
                continue
            brs = set(getattr(r, "authored_branches", None) or [])
            if r.branch:
                brs.add(r.branch)
            if branch in brs:
                if r.id in self._drivers:
                    return r
                fallback = fallback or r
        return fallback

    def automation_statuses(self) -> dict[str, dict]:
        """Rendered live status per automation id, for the automations list.

        The HTTP layer talks to an async adapter (or the remote daemon proxy) and
        cannot reach the in-process ``AutomationContext`` objects where each loop
        stashes its live ``*_counts`` every tick. So status rendering — which
        reads those counts — happens HERE, where the ctx objects live, and ships a
        plain ``{id: status_dict}`` map across the boundary. Without this the list
        endpoint could only pass ``ctx=None`` and every badge showed a hardcoded
        "idle". Best-effort per automation: a render error falls back to that
        automation's idle status so one bad automation can't blank the whole list.
        """
        from server.automations.registry import registry
        ctxs = {
            "pr-doctor": getattr(self, "_pr_doctor_ctx", None),
            "backlog-runner": getattr(self, "_backlog_ctx", None),
            "autopilot": getattr(self, "_autopilot_ctx", None),
            "cicd-doctor": getattr(self, "_cicd_ctx", None),
        }
        out: dict[str, dict] = {}
        for a in registry.all():
            try:
                out[a.id] = a.status(ctx=ctxs.get(a.id)).to_dict()
            except Exception:  # pragma: no cover — one bad automation must not blank the list
                log.debug("automation_statuses.render_failed id=%s", a.id, exc_info=True)
                out[a.id] = a.status(ctx=None).to_dict()
        return out

    async def automation_actionable(self, automation_id: str) -> list[dict]:
        provider = self._action_providers.get(automation_id)
        if provider is None:
            return []
        return [it.to_dict() for it in await provider.actionable()]

    async def automation_trigger(self, automation_id: str, item_id: str, kind: str) -> dict:
        """Dispatch a manual action; returns a TriggerResult dict, or a structured
        error status the API maps to HTTP codes (no exceptions across the RPC seam)."""
        provider = self._action_providers.get(automation_id)
        if provider is None:
            return {"status": "no_provider", "level": "error",
                    "message": "automation has no action provider"}
        if kind not in provider.valid_kinds:
            return {"status": "invalid_kind", "level": "error",
                    "message": f"invalid kind {kind!r}"}
        return (await provider.trigger(item_id, kind)).to_dict()

    async def automation_consolidate(self, repo: str, numbers: list[int]) -> dict:
        """Public seam for the consolidate API route — delegates to the
        pr-doctor provider so the outcome mapping stays in one place."""
        from typing import cast
        from server.automations.action_providers import PrDoctorActionProvider
        raw = self._action_providers.get("pr-doctor")
        if raw is None:
            return {"status": "no_provider", "level": "error",
                    "message": "no action provider"}
        provider = cast(PrDoctorActionProvider, raw)
        return (await provider.consolidate(repo, numbers)).to_dict()

    async def refresh_authored_branches(self) -> None:
        """Union each live session's reflog+live branches into its record, persist changes.

        Runs git off the event loop. Best-effort: a session whose cwd is gone or not a
        git repo simply keeps its existing set. Persists only changed rows so the set
        survives reflog GC and restarts.
        """
        changed: dict[str, list[str]] = {}
        for rec in list(self._records.values()):
            if not rec.cwd:
                continue
            try:
                fresh = await asyncio.to_thread(
                    worktrees.authored_branches, rec.cwd, getattr(rec, "branch", None)
                )
            except Exception:
                log.debug("authored_branches.resolve_failed id=%s", rec.id, exc_info=True)
                continue
            if not fresh:
                continue
            merged = sorted(set(getattr(rec, "authored_branches", None) or []) | set(fresh))
            if merged != (rec.authored_branches or []):
                rec.authored_branches = merged
                changed[rec.id] = merged
        if changed:
            from sqlalchemy import update as sa_update
            async with db_session() as db:
                for sid, brs in changed.items():
                    await db.execute(
                        sa_update(DbSession).where(DbSession.id == sid).values(authored_branches=brs)
                    )

    async def _pr_branch_monitor(self) -> None:
        """Periodically refresh authored branches so new feature branches get badges."""
        while True:
            try:
                await self.refresh_authored_branches()
            except Exception:
                log.warning("pr_branch_monitor.refresh_failed", exc_info=True)
            await asyncio.sleep(60)

    def record(self, session_id: str) -> SessionRecord:
        """Return the in-memory SessionRecord for session_id (KeyError if missing)."""
        try:
            return self._records[session_id]
        except KeyError:
            raise KeyError(f"No session with id {session_id!r}") from None

    # ------------------------------------------------------------------
    # Event bus — WS bridge subscribes/unsubscribes for lifecycle events
    # ------------------------------------------------------------------

    def subscribe_events(self) -> "asyncio.Queue[dict]":
        """Return a queue that receives lifecycle events (rotated, sessionEnded, etc.)."""
        q: asyncio.Queue[dict] = asyncio.Queue()
        self._event_subscribers.append(q)
        return q

    def unsubscribe_events(self, q: "asyncio.Queue[dict]") -> None:
        """Remove a previously-subscribed event queue."""
        try:
            self._event_subscribers.remove(q)
        except ValueError:
            pass

    def active_session_count(self) -> int:
        """Number of sessions currently mid-turn or awaiting input (restart-blocking)."""
        return sum(1 for r in self._records.values() if r.status in _ACTIVE_STATUSES)

    def condensing_session_count(self) -> int:
        """Number of sessions mid-compaction. A subset of active_session_count():
        compaction is uninterruptible and loses data if killed mid-write, so the
        deploy drain protects these past the ordinary turn cap (see deploy.drain)."""
        return sum(1 for r in self._records.values() if r.status == "condensing")

    def restart_safe(self) -> bool:
        """True when a restart now would interrupt no in-flight work."""
        return self.active_session_count() == 0

    def _publish_event(self, event: dict) -> None:
        """Fan an event out to all subscribers (non-blocking)."""
        for q in list(self._event_subscribers):
            q.put_nowait(event)

    async def _snapshot_open_tasks(self) -> None:
        """At drain, persist each live session's open background tasks so the post-restart
        daemon can relaunch them. Only sessions with a live driver can have live tasks."""
        from sqlalchemy import update as sa_update
        session_ids = list(self._drivers.keys())
        if not session_ids:
            return

        async def _scan(sid: str):
            path = self.transcript_path(sid)
            if not path:
                return sid, []
            tasks = await asyncio.to_thread(scan_open_background_tasks, path)
            log_event(log, logging.INFO, "bg_task_rescan", session_id=sid, open=len(tasks))
            return sid, [t.to_dict() for t in tasks]

        results = await asyncio.gather(*(_scan(s) for s in session_ids),
                                       return_exceptions=True)
        n = 0
        async with db_session() as db:
            for r in results:
                if isinstance(r, BaseException):
                    log.warning("rearm snapshot: scan failed", exc_info=r)
                    continue
                sid, snap = r
                if snap:
                    await db.execute(sa_update(DbSession).where(DbSession.id == sid)
                                     .values(pending_rearm=snap))
                    n += len(snap)
            # Parallel pass: snapshot each live session's in-flight foreground prompt
            # iff its turn produced no durable output yet (driver gates this). Writing
            # None for sessions with nothing resumable also clears any stale value.
            for sid in session_ids:
                drv = self._drivers.get(sid)
                getter = getattr(drv, "pending_foreground_prompt", None)
                prompt = getter() if callable(getter) else None
                await db.execute(sa_update(DbSession).where(DbSession.id == sid)
                                 .values(pending_prompt=prompt))
        if n:
            log.info("rearm snapshot: %d open task(s) across %d session(s)",
                     n, len(session_ids))

    async def _clear_rearm_snapshots(self) -> None:
        from sqlalchemy import update as sa_update
        async with db_session() as db:
            await db.execute(sa_update(DbSession).values(pending_rearm=None, pending_prompt=None))

    def drain_status(self) -> dict:
        """Report drain state: 'idle' (not draining), 'draining' (turns still running),
        or 'ready' (draining and nothing mid-turn — safe to restart)."""
        active = self.active_session_count()
        if not self._draining:
            state = "idle"
        elif active == 0:
            state = "ready"
        else:
            state = "draining"
        elapsed = (time.monotonic() - self._drain_started_at) if self._draining else 0.0
        return {"state": state, "busySessions": active,
                "condensing": self.condensing_session_count(),
                "elapsed": round(elapsed, 1)}

    async def begin_drain(self, selfclear_sec: float | None = None) -> dict:
        """Enter DRAINING: refuse new turns, tell the UI, and arm a self-clear timer so a
        deploy that never restarts can't wedge us. Idempotent while already draining."""
        if not self._draining:
            self._draining = True
            self._drain_started_at = time.monotonic()
            try:
                await self._snapshot_open_tasks()
            except Exception:
                log.warning("rearm snapshot failed at drain", exc_info=True)
            self._publish_event({"type": "updatePending"})
            timeout = (
                selfclear_sec if selfclear_sec is not None
                else float(os.environ.get("CCSWITCH_DRAIN_SELFCLEAR_SEC", "600"))
            )
            self._drain_timer = asyncio.create_task(
                self._drain_selfclear(timeout), name="drain-selfclear")
        return self.drain_status()

    def cancel_drain(self) -> dict:
        """Leave DRAINING (deploy aborted / self-clear): resume normal prompt acceptance
        and tell the UI to flush any prompts it was holding."""
        if self._draining:
            self._draining = False
            self._publish_event({"type": "updateCancelled"})
            self._spawn(self._clear_rearm_snapshots(), name="clear-rearm-snapshots")
            if self._drain_timer is not None and not self._drain_timer.done():
                self._drain_timer.cancel()
            self._drain_timer = None
        return self.drain_status()

    async def _drain_selfclear(self, timeout: float) -> None:
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return
        if self._draining:
            log.warning("drain self-clear: no restart within %.0fs — cancelling drain", timeout)
            self.cancel_drain()

    # ------------------------------------------------------------------
    # Accessors / delegating helpers for the WS bridge
    # ------------------------------------------------------------------

    @property
    def poller(self) -> UsagePoller:
        return self._poller

    def has_live_session(self, account: str) -> bool:
        """Return True if any live SessionDriver is currently running on *account*.

        Used by the poller's usage_fn to decide whether it is safe to call
        oauth.refresh_access_token: while the SDK subprocess owns the token
        family (live session), only the SDK must refresh so we avoid
        single-use-token reuse-detection.
        """
        # Snapshot values() before iterating: _drivers is mutated by the event
        # loop (create_session / close_session / _do_rotate) while this method
        # can be called from a threadpool thread (the poller's usage_fn closure).
        # Iterating the live dict while the event loop mutates it raises
        # RuntimeError: dictionary changed size during iteration.
        return any(d.account == account for d in list(self._drivers.values()))

    def live_session_load(self) -> dict[str, float]:
        """Weighted count of live sessions per account, for load-aware selection.

        A live session is one with an entry in ``_drivers`` (same liveness notion as
        ``has_live_session``). Actively-working sessions (_ACTIVE_STATUSES) weigh 1.0;
        idle live sessions weigh ``cfg.idle_session_weight``. Accounts with no live
        session are absent from the map (treated as load 0 by the selector).

        Snapshots ``_drivers`` before iterating — the event loop mutates it while this
        may run from a threadpool thread (see ``has_live_session``).
        """
        cfg = load_config()
        load: dict[str, float] = {}
        for sid in list(self._drivers.keys()):
            rec = self._records.get(sid)
            if rec is None:
                continue
            weight = 1.0 if rec.status in _ACTIVE_STATUSES else cfg.idle_session_weight
            load[rec.account] = load.get(rec.account, 0.0) + weight
        return load

    def live_session_counts(self) -> dict[str, dict[str, int]]:
        """Raw active/idle live-session counts per account, for the Accounts UI.

        Same liveness notion as ``live_session_load`` (``_drivers`` ∩ ``_records``,
        ``_ACTIVE_STATUSES`` = active) but returns integer counts split into
        ``{"active", "idle"}`` instead of weighted floats. Accounts with no live
        session are omitted. Snapshots ``_drivers`` before iterating (event-loop
        mutation guard; see ``has_live_session``).
        """
        counts: dict[str, dict[str, int]] = {}
        for sid in list(self._drivers.keys()):
            rec = self._records.get(sid)
            if rec is None:
                continue
            bucket = counts.setdefault(rec.account, {"active": 0, "idle": 0})
            key = "active" if rec.status in _ACTIVE_STATUSES else "idle"
            bucket[key] += 1
        return counts

    # ------------------------------------------------------------------
    # Copilot account management
    # ------------------------------------------------------------------

    async def copilot_add_start(self, account: str) -> dict:
        """Start the GitHub device-flow login for a copilot account."""
        return await self._copilot_auth.start(account)

    async def copilot_add_status(self, account: str) -> dict:
        """Return the current device-flow auth state for a copilot account (sync)."""
        return self._copilot_auth.status(account)   # status() is sync — do NOT await

    async def copilot_add_cancel(self, account: str) -> dict:
        """Cancel an in-flight device-flow login for a copilot account."""
        return await self._copilot_auth.cancel(account)

    async def copilot_usage(self, account: str) -> dict | None:
        """Return the proxy /usage payload for a copilot account, or None."""
        return self._copilot.usage(account) if self._copilot is not None else None

    def driver(self, session_id: str) -> SessionDriver | None:
        """Return the live SessionDriver for session_id, or None."""
        return self._drivers.get(session_id)

    def seed_idle_suggestions(self, session_id: str, user_text: str, assistant_text: str) -> None:
        """Seed idle-session suggestions on the live driver (best-effort).

        Forwarded here over RPC by the edge's RemoteDriver so daemon mode matches
        the in-process path, where ws.py calls the driver directly. No live driver
        for the session → no-op (the seeding is best-effort and gated driver-side).
        """
        d = self._drivers.get(session_id)
        if d is not None:
            d.seed_idle_suggestions(user_text, assistant_text)

    async def ensure_driver(self, session_id: str, identity: str = SYSTEM_IDENTITY) -> SessionDriver | None:
        """Return the live SessionDriver for session_id, resuming from DB if detached.

        - If a live driver already exists, return it (idempotent).
        - If the SessionRecord exists but is detached, build a new SessionDriver with
          resume=<claude_session_id>, start it, store it, and mark the record as live.
        - If the session_id is unknown, return None.

        ``identity`` is the verified identity of the caller attaching to the session
        (the WebSocket connection's Tailscale identity). It MUST be threaded onto the
        resumed driver: AskUserQuestion / permission answers are registered under
        ``driver.identity`` and later resolved with the WS identity, so a resumed
        driver built with the wrong identity rejects every answer (silently dropping
        them and hanging the agent). Defaults to "system" for internal callers.
        """
        # Fast path: live driver already exists (no lock needed — read-only check)
        existing = self._drivers.get(session_id)
        if existing is not None:
            _rethread_identity(existing, identity)
            return existing

        # Look up the in-memory record (populated by rehydrate())
        rec = self._records.get(session_id)
        if rec is None:
            return None

        # Refuse to start a driver for a queued session — it has no account/cwd
        # yet and starting it would bypass the active-session cap.  Callers
        # (WS attach, prompt endpoint, fix-PR) all handle None gracefully.
        if rec.status == "queued":
            return None

        # Serialise the check-build-register section so that two concurrent WS
        # attaches to the same detached session cannot both pass the "no driver"
        # gate, each build+start a driver, and both register — which would attach
        # two SDK processes to the same Claude conversation (TOCTOU fix).
        async with self._ensure_lock:
            # Re-check inside the lock: another coroutine may have built the
            # driver while we were waiting.
            existing = self._drivers.get(session_id)
            if existing is not None:
                _rethread_identity(existing, identity)
                return existing

            # A completion-driver-release may still be in flight for this session
            # (see _persist_status): its driver is already out of `_drivers`, but
            # the old `claude` process may still be shutting down / flushing its
            # session file to disk. Building a replacement driver with the same
            # resume=claude_session_id against the same cwd while that write is
            # still in flight races it — the SDK's own subprocess_cli.py warns this
            # can lose the last assistant message. Wait for the release to finish
            # first; it always clears its own `_releasing` entry (success or
            # failure), so this cannot hang forever or leave the session stuck.
            # Bounded: this runs under the orchestrator-wide `_ensure_lock`, and
            # aclose() is only PARTLY bounded internally (its pump wait is; the SDK
            # client's __aexit__ and the sync-back call are not). An unbounded await
            # here would let one stuck teardown block every attach in the process and
            # leave the session permanently unopenable. On timeout we proceed to build
            # the replacement anyway: losing the tail of the old transcript is strictly
            # better than an un-openable session, and the release task keeps running
            # (it clears its own `_releasing` entry when it eventually finishes).
            release_task = self._releasing.get(session_id)
            if release_task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(release_task),
                                           timeout=DRIVER_RELEASE_WAIT_S)
                except asyncio.TimeoutError:
                    log.warning("session.resume id=%s driver release still in flight "
                                "after %.1fs — building a replacement anyway",
                                session_id, DRIVER_RELEASE_WAIT_S)
                except Exception:
                    pass

            # Build and start a new driver resuming from the stored claude_session_id
            resume = rec.claude_session_id or None
            cfg_dir = compose(rec.account)
            # Ensure the working directory exists. A rehydrated session's cwd (a no-repo
            # scratch dir or a git worktree) may have been removed when the session was
            # previously closed (close_session cleans those up); without recreating it the
            # SDK fails with "Working directory does not exist", which surfaced as a
            # misleading "account needs re-authentication" error.
            Path(rec.cwd).mkdir(parents=True, exist_ok=True)
            configdir.trust_project(rec.account, rec.cwd)

            # Allocate a virtual X11 display for this session (if X11Manager wired
            # in). Without this, a resumed/rehydrated session — the common path
            # after a daemon restart detaches every live session — builds its driver
            # with the display-injecting factory but x11.env_for() returns {}, so the
            # claude subprocess launches with no DISPLAY and X rendering escapes the
            # virtual viewport. start_display is idempotent (no-op if already leased).
            if self._x11 is not None:
                try:
                    await self._x11.start_display(session_id)
                except Exception:
                    log.exception("session.display_start_failed id=%s", session_id)

            # Ensure the Copilot proxy is running for copilot-backed accounts.
            # If the proxy can't start (quota exhausted / token dead), schedule a
            # rotation so the session moves to a working account without requiring
            # user intervention, then re-raise so the WS caller shows an error.
            if self._copilot is not None and _provider_of(rec.account) == _copilot_mod.PROVIDER:
                try:
                    await self._copilot.ensure(rec.account)
                except Exception:
                    log.warning("ensure_driver: copilot proxy start failed for %s; "
                                "scheduling rotation session=%s", rec.account, session_id,
                                exc_info=True)
                    if not self._shutting_down:
                        self._spawn(
                            self._rotate_away(rec, reason="proxy_start_failed"),
                            name=f"rotate-proxy-{session_id}",
                        )
                    raise

            def _build(resume_arg):
                return SessionDriver(
                    session_id,
                    rec.account,
                    rec.cwd,
                    str(cfg_dir),
                    mode=getattr(rec, "permission_mode", "interactive"),
                    sdk_factory=self._session_factory(session_id),
                    registry=self._registry,
                    identity=identity,
                    resume=resume_arg,
                    on_session_id=self._persist_claude_session_id,
                    on_sync_back=functools.partial(ccswitch_accounts.sync_back, rec.account),
                    on_status=self._persist_status,
                    on_context_usage=self._persist_context_usage,
                    on_record_condense_ms=self._record_condense_ms,
                    get_condense_estimate=self._condense_estimate,
                    on_recoverable_failure=self._handle_recoverable_failure,
                    suggester=self._suggester,
                )

            driver = _build(resume)
            try:
                await driver.start()
            except Exception:
                if resume is None:
                    # No resume to blame — the SDK genuinely can't start (e.g. a
                    # truly dead account token / system problem). Surface it.
                    raise
                # Resume failed — almost always because the stored claude_session_id
                # can't be resumed from rec.cwd (the original session lived under a
                # different project dir, or the claude session was pruned). Fall back
                # to a FRESH session so the record stays usable: the prior transcript
                # still shows via replay, and new turns get (and persist) a new
                # claude_session_id. Only if the fresh start ALSO fails do we error.
                log.warning("session.resume id=%s could not resume claude_sid=%s; starting fresh",
                            session_id, resume, exc_info=True)
                try:
                    await driver.aclose()
                except Exception:
                    pass
                driver = _build(None)
                await driver.start()
            self._drivers[session_id] = driver
            # Re-apply the persisted model, mirroring _reassign. ensure_driver
            # previously dropped rec.model on resume (rotation kept it, a plain
            # detach → attach lost it to the account default). Placed after the
            # final driver assignment so the fresh-start fallback gets it too.
            if rec.model is not None:
                await driver.set_model(rec.model)
            rec.detached = False
            # INVARIANT: a session holding a live driver is not capacity-exempt.
            # `complete` is exempt (see CAPACITY_EXEMPT_STATUSES) precisely because
            # its process was released; resuming it makes that false again. Without
            # this, every path that resumes a finished session (autopilot assist,
            # _rearm_session, restore_deploy / _recover_resume_failures_once, a user
            # prompt over the WS) produced a live `claude` process that never passed
            # the capacity gate AND stayed uncounted while it ran — the cap stops
            # being a ceiling. `done` (a live session between turns) is the correct
            # landing status: it has completed turns and is awaiting the next prompt.
            # Done here rather than as a second capacity check at each call site so
            # the existing accounting is simply true again (one source of truth).
            if rec.status == "complete":
                await self._exit_complete_on_resume(session_id, rec)
            await self._maybe_rearm_on_attach(session_id)
            log.info("session.resume id=%s resume=%s", session_id, resume or "<fresh>")
            return driver

    async def _exit_complete_on_resume(self, session_id: str, rec) -> None:
        """Move a just-resumed `complete` session back to `done` (in-memory + DB).

        Deliberately NOT routed through `_persist_status`: this is not a turn boundary,
        so it must not bump `last_turn_at` (the "active … ago" clock), re-scan the
        transcript for parked background tasks, or re-run the completion side effects.
        Best-effort on the DB write — a bookkeeping failure must never fail an attach —
        but the in-memory record (what `_active_session_count` reads) is always fixed."""
        rec.status = "done"
        from datetime import datetime
        rec.updated_at = datetime.utcnow()
        try:
            async with db_session() as db:
                from sqlalchemy import update as sa_update
                await db.execute(sa_update(DbSession).where(DbSession.id == session_id)
                                 .values(status="done"))
        except Exception:  # pragma: no cover — never fail an attach on bookkeeping
            log.warning("session.resume id=%s could not persist complete→done",
                        session_id, exc_info=True)
        # Same lifecycle frame _persist_status publishes, so the dashboard's status
        # patcher (_patchSessionStatus) sees the card leave "complete" immediately
        # instead of waiting for the 5s poll. One frame shape, not a second one.
        self._publish_event({"type": "status", "sessionId": session_id, "status": "done",
                             "updatedAt": rec.updated_at_iso,
                             "revAt": rec.rev_at,
                             "interruptedByRestart": bool(
                                 getattr(rec, "interrupted_by_restart", False))})

    # ------------------------------------------------------------------
    # Adoption — bring an untracked live claude session under management
    # ------------------------------------------------------------------

    def _discover_candidates(self) -> list:
        """Enumerate live adoption candidates from THIS host's /proc.

        Both entry points — the dashboard's adoptable list and _resolve_candidate
        for an adopt call — go through here so discovery always runs where the
        session processes actually live (the daemon host). known/tracked are read
        from the orchestrator's own account config and record map.
        """
        known = [a["name"] for a in ccswitch_accounts.list_accounts()]
        tracked = {r.claude_session_id for r in self._records.values() if r.claude_session_id}
        return _adopt.discover_candidates(known, tracked)

    async def list_adoptable_candidates(self) -> list[dict]:
        """Return all adoption candidates as dashboard wire dicts (daemon RPC).

        Runs /proc discovery on the daemon host — the edge (potentially a
        different machine) must not scan its own process table, which would leave
        the Adopt list empty or surface an unrelated claude process (issue #709,
        finding 4). Serializes here so the edge endpoint is a plain passthrough.
        """
        cands = await asyncio.to_thread(self._discover_candidates)
        return [_adopt.candidate_to_wire(c) for c in cands]

    async def migrate_archive_home(
        self, old_home: str | None, new_home: str | None, archived_ids: list[str],
    ) -> dict:
        """Move archived session dirs old_home→new_home on the DAEMON's filesystem.

        The archive dirs live under the daemon's CCSWITCH_HOME, so the moves must
        run on the daemon host — on the edge src.exists() is always False and the
        migration silently no-ops (issue #709, finding 5). archived_ids come from
        the (shared) DB; only the filesystem move is host-specific. Path defaults
        (archive_dir()) resolve here so they reflect the daemon's env. Returns
        {"moved": int, "failures": int}; failures are logged, never raised, so a
        settings save is never aborted by a stuck move.

        Each successfully moved session has ``archive_root`` re-stamped to the new root.
        Skipping that would leave every moved row pointing at a directory that is no
        longer there, and unarchive resolves through archive_root — so the move would
        silently break exactly the sessions it just relocated.
        """
        import shutil
        from pathlib import Path

        from ccswitch.paths import archive_dir

        old_dir = Path(old_home) if old_home else archive_dir()
        new_dir = Path(new_home) if new_home else archive_dir()
        if old_dir == new_dir or not archived_ids:
            return {"moved": 0, "failures": 0}

        def _move_all() -> tuple[list[str], int]:
            new_dir.mkdir(parents=True, exist_ok=True)
            moved: list[str] = []
            failures = 0
            for sid in archived_ids:
                src = old_dir / sid
                if not src.exists():
                    continue
                dst = new_dir / sid
                if dst.exists():
                    continue
                try:
                    shutil.move(str(src), str(dst))
                    moved.append(sid)
                except Exception:
                    log.exception("archive_home migrate: %s → %s failed", src, dst)
                    failures += 1
            return moved, failures

        moved_ids, failures = await asyncio.to_thread(_move_all)

        # Re-stamp AFTER the move: if this coroutine dies partway, every row still names
        # the root its directory is actually in, so the operation is resumable rather
        # than half-corrupt.
        if moved_ids:
            from sqlalchemy import update as sa_update
            async with db_session() as db:
                await db.execute(
                    sa_update(DbSession)
                    .where(DbSession.id.in_(moved_ids))
                    .values(archive_root=str(new_dir))
                )

        return {"moved": len(moved_ids), "failures": failures}

    async def cloned_repos(self, names: list[str]) -> list[str]:
        """Return the subset of nameWithOwner values whose clone exists on the DAEMON.

        The repo picker's "cloned" badge checks for <clones_base>/<owner>/<name>/.git;
        clones live on the daemon host, so an edge-side check is always false and
        may prompt a redundant clone (issue #709, finding 6). Resolves clones_base
        from the daemon's own settings.repo_root.
        """
        from ccswitch import gitrepos
        from server.config import get_settings

        base = gitrepos.clones_base(get_settings().repo_root)

        def _present() -> list[str]:
            out: list[str] = []
            for nwo in names:
                owner, _, name = nwo.partition("/")
                if owner and name and (base / owner / name / ".git").exists():
                    out.append(nwo)
            return out

        return await asyncio.to_thread(_present)

    def _resolve_candidate(self, claude_session_id: str):
        """Re-run discovery and return the AdoptionCandidate for this sid, or None.

        Re-resolving (rather than trusting a stale list) is deliberate: processes
        churn fast, so the candidate may have died or changed between the dashboard
        scan and the adopt call. Anchored to the transcript, which persists.
        """
        for cand in self._discover_candidates():
            if cand.claude_session_id == claude_session_id:
                return cand
        return None

    async def adopt_session(self, claude_session_id: str, identity: str, killer=os.kill):
        """Force-take-over an untracked live session: terminate it, resume in place.

        Returns ``(SessionRecord, {"resumed": bool, "terminatedPid": int|None})``.
        Raises KeyError if the session is no longer adoptable (gone, unmapped, or
        already tracked). The conversation is resumed in its ORIGINAL cwd with no
        worktree (documented isolation exception, flagged adopted_in_place=True).
        """
        cand = self._resolve_candidate(claude_session_id)
        if cand is None or not cand.mapped or cand.tracked:
            raise KeyError(claude_session_id)

        # Terminate the original process tree (best-effort; a dead PID is fine).
        terminated_pid: int | None = None
        if cand.pid is not None:
            sent = await asyncio.to_thread(_adopt.terminate_process_tree, cand.pid, killer)
            if sent:
                terminated_pid = cand.pid
                async with db_session() as db:
                    await record_audit(
                        db, "process.terminate", identity,
                        {"pid": cand.pid, "claudeSessionId": claude_session_id},
                    )

        session_id = str(uuid.uuid4())
        cfg_dir = compose(cand.account)
        Path(cand.cwd).mkdir(parents=True, exist_ok=True)
        configdir.trust_project(cand.account, cand.cwd)

        # Allocate a virtual X11 display for the adopted session (fresh allocation).
        if self._x11 is not None:
            try:
                await self._x11.start_display(session_id)
            except Exception:
                log.exception("session.display_start_failed id=%s", session_id)

        # Ensure the Copilot proxy is running for copilot-backed accounts.
        if self._copilot is not None and _provider_of(cand.account) == _copilot_mod.PROVIDER:
            await self._copilot.ensure(cand.account)

        # Same precedence as POST /api/sessions (account default → global Settings
        # default → interactive): a full-auto operator must not get a permission
        # prompt on every tool call of a session they just took over.
        async with db_session() as db:
            permission_mode = await resolve_permission_mode(db, account=cand.account)

        def _build(resume_arg):
            return SessionDriver(
                session_id, cand.account, cand.cwd, str(cfg_dir),
                mode=permission_mode, sdk_factory=self._session_factory(session_id), registry=self._registry,
                identity=identity, resume=resume_arg,
                on_session_id=self._persist_claude_session_id,
                on_sync_back=functools.partial(ccswitch_accounts.sync_back, cand.account),
                on_status=self._persist_status,
                on_context_usage=self._persist_context_usage,
                on_record_condense_ms=self._record_condense_ms,
                get_condense_estimate=self._condense_estimate,
                on_recoverable_failure=self._handle_recoverable_failure,
                suggester=self._suggester,
            )

        # Resume the conversation; fall back to a fresh session if resume fails so
        # the adopted record stays usable (prior transcript still shows via replay).
        resumed = True
        driver = _build(claude_session_id)
        try:
            await driver.start()
        except Exception:
            log.warning("adopt.resume_failed sid=%s; starting fresh",
                        claude_session_id, exc_info=True)
            try:
                await driver.aclose()
            except Exception:
                pass
            driver = _build(None)
            await driver.start()
            resumed = False
        self._drivers[session_id] = driver

        # The transcript mtime is the adopted session's real last-response time — seed the
        # "active … ago" clock from it (naive UTC to match the DB columns) instead of "now".
        adopted_last_turn = cand.last_activity.replace(tzinfo=None) if cand.last_activity else None
        adopt_host = local_host_stamp()
        async with db_session() as db:
            db.add(DbSession(
                id=session_id, name=cand.name, account_name=cand.account,
                claude_session_id=claude_session_id, status="idle", pid=None,
                cwd=cand.cwd, repo=None, permission_mode=permission_mode,
                name_is_auto=True, adopted_in_place=True, last_turn_at=adopted_last_turn,
                host=adopt_host,
            ))
            await record_audit(
                db, "session.adopt", identity,
                {"sessionId": session_id, "account": cand.account,
                 "claudeSessionId": claude_session_id, "resumed": resumed},
            )

        from datetime import datetime
        adopt_now = datetime.utcnow()
        rec = SessionRecord(
            id=session_id, name=cand.name, account=cand.account,
            claude_session_id=claude_session_id, cols=80, rows=24,
            cwd=cand.cwd, repo=None, status="idle", adopted_in_place=True,
            permission_mode=permission_mode,
            created_at=adopt_now, updated_at=adopt_now,  # row-write time
            last_turn_at=adopted_last_turn,  # real last-response time from the transcript
            host=adopt_host,
        )
        self._records[session_id] = rec
        log.info("session.adopt id=%s sid=%s resumed=%s killed=%s",
                 session_id, claude_session_id, resumed, terminated_pid)
        return rec, {"resumed": resumed, "terminatedPid": terminated_pid}

    def resolve_permission(self, session_id, perm_id, decision, identity) -> bool:
        d = self._drivers.get(session_id)
        return d.resolve_permission(perm_id, decision, identity) if d else False

    def answer_question(self, session_id, question_id, selected, identity, mode="option") -> bool:
        """Resolve a pending AskUserQuestion with the user's selected answers and mode.

        Returns True if resolved, False if no driver for session_id or unknown question_id.
        Mirrors resolve_permission.
        """
        d = self._drivers.get(session_id)
        return d.answer_question(question_id, selected, identity, mode) if d else False

    def pending_questions(self, session_id: str) -> list[dict]:
        """Return still-pending question payloads for session_id ([] if no driver).

        Each entry has: question_id, turn_id, header, question, options, multi_select.
        Mirrors the _registry.pending(session_id) pattern used for permissions in _handle_attach.
        """
        d = self._drivers.get(session_id)
        return d.pending_questions() if d else []

    def pop_pending_initial_prompt(self, session_id: str) -> str | None:
        """Return and remove the deferred initial prompt for session_id, or None.

        Called by the WS bridge on the first attach so the initial prompt is sent
        while a client is already watching.  Clears the entry so subsequent attaches
        (reconnects) do not re-send it.
        """
        return self._pending_initial_prompt.pop(session_id, None)

    def pop_pending_initial_attachment_ids(self, session_id: str) -> list[str] | None:
        """Return and remove the deferred initial attachment ids for session_id, or None.

        Called by the WS bridge on the first attach alongside pop_pending_initial_prompt.
        Clears the entry so subsequent attaches (reconnects) do not re-send them.
        """
        return self._pending_initial_attachment_ids.pop(session_id, None)

    async def send_prompt(self, session_id, text, attachment_ids=None) -> str:
        if self._draining:
            # No new turns during a deploy drain. Tell the browser so it re-queues
            # (promptRejected) and shows the hold banner — never silently swallow it.
            log.info("send_prompt rejected: drain in progress session=%s", session_id)
            self._publish_event({"type": "updatePending"})
            return "draining"
        d = self._drivers.get(session_id)
        if not d:
            # A dropped prompt is never routine. This return value used to be a silent
            # sentinel that several callers ignored, which is exactly how the discarded
            # completion nudge (below) stayed invisible in production — the caller's own
            # failure log never fired because nothing raised. Log it here, once, for
            # every caller: the fix at a call site is an `ensure_driver` before the send.
            log.warning("send_prompt: no live driver for session=%s — prompt dropped",
                        session_id)
            return "no_driver"
        ids = list(attachment_ids or [])
        if len(ids) > _attachments.MAX_PER_MESSAGE:
            log.warning("send_prompt: capping attachments from %d to %d session=%s",
                        len(ids), _attachments.MAX_PER_MESSAGE, session_id)
        resolved: list[dict] = []
        if ids:
            # Bind rows to this session + a fresh turn id, then resolve the
            # content-addressed on-disk paths the driver loads into image blocks.
            turn_id = uuid.uuid4().hex
            async with db_session() as db:
                resolved = await _attachments.resolve_and_bind(
                    db, session_id, turn_id, ids)
        self._last_prompt[session_id] = text
        await d.send_prompt(text, resolved)
        return "sent"

    async def interrupt(self, session_id):
        d = self._drivers.get(session_id)
        if d:
            await d.interrupt()

    async def latest_suggestions(self, session_id: str) -> list[str]:
        """Latest cached next-prompt suggestions for a session.

        Live (attached) sessions read straight off the driver. Detached
        ``done``/``idle`` sessions generate from their on-disk transcript and
        cache the result keyed by transcript mtime (see
        _generate_detached_suggestions). Best-effort: never raises.

        For done/idle detached sessions, the top open GitHub issue proposal is
        prepended as the first item when available (lazy TTL cache via
        _dispatch_proposal / _refresh_issue_proposal).
        """
        d = self._drivers.get(session_id)
        if d is not None:
            return list(getattr(d, "_last_suggestions", []) or [])

        rec = self._records.get(session_id)
        if rec is None:
            return []
        # Copilot sessions use a third-party model — Haiku suggestions are meaningless.
        if _provider_of(rec.account) == _copilot_mod.PROVIDER:
            return []
        if rec.status not in ("done", "idle"):
            return []

        # Issue-proposal: lazy TTL cache + bg refresh; best-effort, never blocks.
        proposal = self._dispatch_proposal(rec)

        try:
            path = await self.resolve_transcript_path(session_id)
            if not path:
                return _prepend_proposal(proposal, [])
            signature = os.path.getmtime(path)
        except Exception:
            return _prepend_proposal(proposal, [])

        cached = self._detached_suggestions.get(session_id)
        if cached is not None and cached[0] == signature:
            return _prepend_proposal(proposal, list(cached[1]))

        # Only schedule a background suggestion refresh when a suggester is available,
        # none is already in flight, and any auth-failure backoff has elapsed (a failed
        # attempt is not cached — see _generate_detached_suggestions — so without this
        # gate every 5s /sessions poll would re-spawn generation for an account with no
        # usable auth). Keyed by account, not session_id: the cooldown is shared by every
        # session on the same account (see _suggest_auth_failed_until).
        failed_until = self._suggest_auth_failed_until.get(rec.account)
        backed_off = failed_until is not None and time.monotonic() < failed_until
        if self._suggester is not None and session_id not in self._suggest_inflight and not backed_off:
            self._suggest_inflight.add(session_id)
            self._spawn(
                self._generate_detached_suggestions(session_id, rec, path, signature),
                name=f"detached-suggestions-{session_id}",
            )
        # Cache miss or stale (transcript changed): serve only the proposal (if any)
        # rather than stale suggestions from an outdated transcript.
        return _prepend_proposal(proposal, [])

    async def _generate_detached_suggestions(self, session_id, rec, path, signature) -> None:
        """Generate suggestions for a detached session from its transcript (best-effort).

        A result reached by actually attempting generation — including a genuinely empty
        one, and including the `self._suggester is None` and unhandled-exception cases
        below, neither of which reflects a real answer from the model — is cached as
        (signature, items), so the same transcript state is not re-generated on the next
        poll. But when the call could not even be made (no usable auth), nothing is
        cached: the give-up is logged (rate-limited by `_suggest_auth_failed_until`, see
        below) and a later poll retries once the backoff elapses, so fixing the account
        (e.g. a setup-token added, or its browser-OAuth refresh restored) is picked up
        without waiting on the transcript to change. Always clears the in-flight flag.
        """
        try:
            if self._suggester is None:
                self._detached_suggestions[session_id] = (signature, [])
                return
            from ccswitch.oauth import AuthError
            try:
                await asyncio.to_thread(ccswitch_accounts.acquire_valid_token, rec.account)
            except (AuthError, FileNotFoundError):
                # Browser-OAuth refresh is dead. Fall back to the same setup-token auth
                # session launch uses (oauth_env_for is the single source of truth for
                # model-auth env): a non-empty env means the account can still launch a
                # session even though acquire_valid_token gave up — though the API may
                # still reject the setup-token, in which case generate() below swallows
                # the error and returns [], which then IS cached as a genuine result.
                if not oauth_env_for(rec.account):
                    # Not caching means the next poll would re-attempt immediately (every
                    # 5s while a dashboard is open) and re-run the real refresh_access_token
                    # network POST + sync_back that acquire_valid_token just did. Back off
                    # instead: skip re-spawning (see latest_suggestions) until the window
                    # elapses, which also caps this WARNING to once per account per window
                    # rather than once per poll — the cooldown is keyed by account, so it
                    # covers every session sharing this account, not just this one.
                    self._suggest_auth_failed_until[rec.account] = (
                        time.monotonic() + _SUGGEST_AUTH_FAIL_BACKOFF_S
                    )
                    log.warning(
                        "detached suggestions: giving up for account=%s (session=%s) — no "
                        "valid OAuth token and no setup-token; not caching, retrying in %ss",
                        rec.account, session_id, _SUGGEST_AUTH_FAIL_BACKOFF_S,
                    )
                    return  # do NOT cache: this is "could not be made", not "no answer"
            events = await replay_events(path)
            user_text, assistant_text = last_turn_context(events)
            context = f"User: {user_text}\n\nAssistant: {assistant_text}"
            items = await self._suggester.generate(rec.cwd, str(compose(rec.account)), context, rec.account)
            self._detached_suggestions[session_id] = (signature, list(items or []))
            self._suggest_auth_failed_until.pop(rec.account, None)
        except Exception:
            log.warning("detached suggestion generation failed id=%s", session_id, exc_info=True)
            self._detached_suggestions[session_id] = (signature, [])
        finally:
            self._suggest_inflight.discard(session_id)

    async def generate_suggestions_now(self, session_id: str) -> list[str]:
        """Generate next-prompt suggestions on demand and return them.

        Backs the sparkle's "always generate a new response via LLM" action. A live
        (attached) session generates from the driver's in-memory turn context; a
        detached idle/done session generates from its on-disk transcript (the same
        throwaway resume-less query the passive path uses, so no session is resumed
        merely to suggest). Best-effort: returns [] when no suggester is configured,
        the session is unknown/not idle, or generation yields nothing — the caller
        must never silently fall back to a hard-coded prompt.
        """
        if self._suggester is None:
            return []

        d = self._drivers.get(session_id)
        if d is not None:
            gen = getattr(d, "generate_suggestions_now", None)
            if gen is None:
                return list(getattr(d, "_last_suggestions", []) or [])
            try:
                return list(await gen() or [])
            except Exception:
                log.warning("on-demand suggestion generation failed id=%s", session_id,
                            exc_info=True)
                return []

        rec = self._records.get(session_id)
        if rec is None or rec.status not in ("done", "idle"):
            return []
        try:
            from ccswitch.oauth import AuthError
            try:
                await asyncio.to_thread(ccswitch_accounts.acquire_valid_token, rec.account)
            except (AuthError, FileNotFoundError):
                return []
            path = await self.resolve_transcript_path(session_id)
            if not path:
                return []
            events = await replay_events(path)
            user_text, assistant_text = last_turn_context(events)
            context = f"User: {user_text}\n\nAssistant: {assistant_text}"
            items = await self._suggester.generate(rec.cwd, str(compose(rec.account)), context, rec.account)
            items = list(items or [])
            # Refresh the mtime-keyed cache too so the next passive poll agrees.
            try:
                self._detached_suggestions[session_id] = (os.path.getmtime(path), items)
            except Exception:
                pass
            return items
        except Exception:
            log.warning("on-demand detached suggestion generation failed id=%s", session_id,
                        exc_info=True)
            return []

    async def latest_summary(self, session_id: str, *, viewing: bool) -> str:
        """Latest cached one-line status summary for a session.

        Mirrors latest_suggestions: lazy, mtime-keyed, non-blocking. Schedules a
        background regeneration only when should_regenerate() allows (viewer present,
        active status, transcript changed, >=60s since last gen). Returns the cached
        text (or "") immediately — never blocks the /sessions GET. Never raises.
        """
        rec = self._records.get(session_id)
        if rec is None:
            return ""
        # Copilot sessions use a third-party model — Haiku summaries are meaningless.
        if _provider_of(rec.account) == _copilot_mod.PROVIDER:
            return ""
        cached = self._summaries.get(session_id)
        try:
            path = await self.resolve_transcript_path(session_id)
            signature = os.path.getmtime(path) if path else 0.0
        except Exception:
            return cached[2] if cached else ""
        now = time.monotonic()
        if (
            path
            and self._summarizer is not None
            and session_id not in self._summary_inflight
            and should_regenerate(viewing, cached, signature, now, rec.status)
        ):
            self._summary_inflight.add(session_id)
            self._spawn(self._generate_summary(session_id, rec, path, signature),
                        name=f"generate-summary-{session_id}")
        return cached[2] if cached else ""

    async def latest_suggestions_many(self, session_ids: list[str]) -> dict[str, list[str]]:
        """Batched :meth:`latest_suggestions`, isolated per session (see prs_for_many)."""
        ids = list(session_ids)
        results = await asyncio.gather(
            *(self.latest_suggestions(sid) for sid in ids), return_exceptions=True)
        out: dict[str, list[str]] = {}
        for sid, r in zip(ids, results):
            if isinstance(r, BaseException):
                log.warning("latest_suggestions_many: failed for %s", sid, exc_info=r)
                out[sid] = []
            else:
                out[sid] = r
        return out

    async def latest_summary_many(self, session_ids: list[str], *,
                                  viewing: bool) -> dict[str, str]:
        """Batched :meth:`latest_summary`, isolated per session (see prs_for_many).

        ``viewing`` is request-level, not per-session (api/sessions.py reads one query
        param for the whole page), so it applies uniformly to the batch.
        """
        ids = list(session_ids)
        results = await asyncio.gather(
            *(self.latest_summary(sid, viewing=viewing) for sid in ids),
            return_exceptions=True)
        out: dict[str, str] = {}
        for sid, r in zip(ids, results):
            if isinstance(r, BaseException):
                log.warning("latest_summary_many: failed for %s", sid, exc_info=r)
                out[sid] = ""
            else:
                out[sid] = r
        return out

    async def _generate_summary(self, session_id, rec, path, signature) -> None:
        """Generate a summary from the session's transcript (best-effort).

        Caches (signature, generated_at, text) even when text is "", so the same
        transcript state isn't re-generated until it changes AND 60s passes. Always
        clears the in-flight flag.
        """
        try:
            if self._summarizer is None:
                self._summaries[session_id] = (signature, time.monotonic(), "")
                return
            events = await replay_events(path)
            user_text, assistant_text = last_turn_context(events)
            context = f"User: {user_text}\n\nAssistant: {assistant_text}"
            text = await self._summarizer.generate(rec.cwd, str(compose(rec.account)), context, rec.account)
            self._summaries[session_id] = (signature, time.monotonic(), text or "")
        except Exception:
            log.warning("summary generation failed id=%s", session_id, exc_info=True)
            self._summaries[session_id] = (signature, time.monotonic(), "")
        finally:
            self._summary_inflight.discard(session_id)

    def _dispatch_proposal(self, rec: SessionRecord) -> str | None:
        """Return the cached issue-proposal string for *rec*'s repo (or None).

        Never blocks: if the cache is missing or stale, schedules a background
        refresh (fire-and-forget) and returns the stale value if any, else None.
        Never raises.
        """
        if not rec.repo:
            return None
        repo = rec.repo
        # Priority 1 — this session's own PR is failing CI: propose fixing it before
        # starting new backlog work. Cheap: reads the PR poller's in-memory cache (no
        # gh call). Guarded so a missing poller / any error never breaks suggestions.
        if rec.branch:
            try:
                pr = self.pr_for(repo, rec.branch)
            except Exception:
                pr = None
            if pr is not None and getattr(pr, "checks", None) == "failing":
                return dispatch.broken_pr_proposal_text(pr.number, pr.title)
            # Priority 2 — unresolved review comments on the session's own PR.
            # Only consulted when PR exists and is NOT failing (handled above).
            if pr is not None:
                try:
                    review_text = self._review_proposal(repo, pr.number, pr.title)
                    if review_text:
                        return review_text
                except Exception:
                    pass
        # Priority 3 — top open issue (cached, background-refreshed).
        # TTL uses a monotonic clock, coherent with the timestamps stored by
        # _refresh_issue_proposal. (self._clock is wall-clock `time.time`, used for
        # recovery timing elsewhere — mixing it with monotonic deltas would be wrong.)
        now = time.monotonic()
        cached = self._issue_proposals.get(repo)
        # Schedule a background refresh on cache miss/stale (return stale meanwhile).
        if cached is None or (now - cached[0]) >= _ISSUE_TTL_S:
            if repo not in self._issue_inflight:
                self._issue_inflight.add(repo)
                self._spawn(self._refresh_issue_proposal(repo),
                            name=f"refresh-issue-proposal-{repo}")
        text = cached[1] if cached is not None else None
        # Release this session's prior claim and prune claims for sessions that are no
        # longer idle/done (so an issue frees up once its session moves on or is removed).
        self._proposed_issue = {
            sid: n for sid, n in self._proposed_issue.items()
            if sid != rec.id
            and sid in self._records
            and self._records[sid].status in ("done", "idle")
        }
        if not text:
            return None
        # Collision-avoidance (US-8): if another live session already holds this repo's
        # top issue, don't propose the same one — return nothing rather than double-book.
        number = self._issue_numbers.get(repo)
        if number is not None:
            if number in self._proposed_issue.values():
                return None
            self._proposed_issue[rec.id] = number
        return text

    async def _refresh_issue_proposal(self, repo: str) -> None:
        """Fetch open issues and cache the top-issue proposal for *repo*.

        Best-effort: stores (now, None) on any error.  Always clears inflight flag.
        Never raises.
        """
        try:
            issues = await asyncio.to_thread(dispatch.fetch_open_issues, repo)
            top = dispatch.pick_top_issue(issues)
            text: str | None = dispatch.issue_proposal_text(top) if top else None
            self._issue_proposals[repo] = (time.monotonic(), text)
            self._issue_numbers[repo] = top["number"] if top else None
        except Exception:
            log.warning("_refresh_issue_proposal failed repo=%s", repo, exc_info=True)
            self._issue_proposals[repo] = (time.monotonic(), None)
            self._issue_numbers[repo] = None
        finally:
            self._issue_inflight.discard(repo)

    def _review_proposal(self, repo: str, number: int, title: str) -> str | None:
        """Return the cached review-comment proposal for *(repo, number)*, or None.

        Never blocks: on cache miss or TTL expiry, schedules a background refresh
        (fire-and-forget) and returns the stale value if any, else None.
        Never raises.
        """
        key = (repo, number)
        now = time.monotonic()
        cached = self._review_proposals.get(key)
        if cached is not None and (now - cached[0]) < _ISSUE_TTL_S:
            return cached[1]
        # Cache miss or stale: schedule a background refresh if not already in-flight.
        if key not in self._review_inflight:
            self._review_inflight.add(key)
            self._spawn(self._refresh_review_proposal(repo, number, title),
                        name=f"refresh-review-proposal-{repo}#{number}")
        # Return stale value if available, else None.
        return cached[1] if cached is not None else None

    async def _refresh_review_proposal(self, repo: str, number: int, title: str) -> None:
        """Fetch unresolved review count and cache the proposal for *(repo, number)*.

        Best-effort: stores (now, None) on any error.  Always clears inflight flag.
        Never raises.
        """
        key = (repo, number)
        try:
            count = await asyncio.to_thread(dispatch.fetch_unresolved_review_count, repo, number)
            text: str | None = dispatch.review_proposal_text(number, title, count) if count > 0 else None
            self._review_proposals[key] = (time.monotonic(), text)
        except Exception:
            log.warning("_refresh_review_proposal failed repo=%s pr=%d", repo, number, exc_info=True)
            self._review_proposals[key] = (time.monotonic(), None)
        finally:
            self._review_inflight.discard(key)

    async def set_permission_mode(self, session_id, mode):
        d = self._drivers.get(session_id)
        if d:
            await d.set_permission_mode(mode)

    def _cached_command_catalog(self, session_id) -> list[dict]:
        """Last-known catalog cached on the record, or []. Used when there is no live
        driver (a detached session) so the palette still populates on view."""
        rec = self._records.get(session_id)
        cached = getattr(rec, "command_catalog", None) if rec is not None else None
        return list(cached) if cached else []

    async def command_catalog(self, session_id) -> list[dict]:
        """Per-session slash-command catalog (wire dicts). Built from the live driver's
        get_server_info and cached on the record; falls back to the last-known cached
        catalog when no live driver exists (detached session) so the palette works on
        view without a resume.

        The cache is stored IN MEMORY only here — deliberately NOT a DB write. This runs
        inside the WS attach coroutine, which a client disconnect can cancel mid-write;
        a cancelled write can corrupt the SQLite connection it was using. Durability is
        folded into the next _persist_status write (a driver-loop callback that is safe
        to write from), so the catalog still reaches the DB row for cross-restart replay."""
        d = self._drivers.get(session_id)
        if d is None:
            return self._cached_command_catalog(session_id)
        try:
            info = await d.server_info()
        except Exception:
            log.warning("command_catalog: server_info failed session=%s", session_id, exc_info=True)
            return self._cached_command_catalog(session_id)
        catalog = [c.to_wire() for c in cmd_catalog.build_catalog(info)]
        if catalog:
            rec = self._records.get(session_id)
            if rec is not None:
                rec.command_catalog = catalog
        return catalog

    async def run_command(self, session_id, name, args,
                          identity: str = SYSTEM_IDENTITY) -> dict | None:
        """Dispatch a slash command. Raises KeyError if the command is unknown."""
        # /account is an orchestrator-level operation: it doesn't touch the driver
        # subprocess at all, so it must work even when no driver exists (e.g. the
        # session is driverless because the Copilot proxy died).
        if str(name).lstrip("/") == "account":
            acct = (args or "").strip()
            if not acct:
                return {"name": "account", "render": "notice",
                        "data": {"text": "Usage: /account <account>"}}
            try:
                await self.switch_account_manual(session_id, acct)
            except (InvalidAccountName, KeyError):
                return {"name": "account", "render": "notice",
                        "data": {"text": f"Unknown account: {acct}"}}
            return {"name": "account", "render": "notice",
                    "data": {"text": f"Account → {acct}"}}
        # /model is likewise orchestrator-level (record + DB, driver optional): it
        # must sit ABOVE the driver guard so a detached session persists the model
        # instead of getting "isn't live" — the picker was a silent no-op there.
        if str(name).lstrip("/") == "model":
            # `/model <alias> [--after-turn]`. The flag is how the picker's "switch
            # after this turn" choice rides the existing command frame: no
            # interrupt, the SDK applies the model on the next turn (see
            # set_model_for_session). Without it a busy session is stopped and
            # restarted on the new model.
            tokens = (args or "").split()
            after_turn = "--after-turn" in tokens
            arg = " ".join(t for t in tokens if t != "--after-turn").strip()
            # "default" is the picker's special row, not an alias the SDK knows:
            # it clears the override so the account default applies again
            # (mirrors create_session, which treats "default" as None).
            if arg == "default":
                arg = ""
            label = arg or "default"
            driver = self._drivers.get(session_id)
            was_busy = driver is not None and driver.turn_active
            restarted = await self.set_model_for_session(
                session_id, arg or None, when="after_turn" if after_turn else "now",
                identity=identity)
            # A model-gated switch is parked (pending_model set), not applied: the
            # session keeps running on its current model until approved. Say so
            # instead of the misleading "Model → <alias>" (which reads as "applied").
            rec = self._records.get(session_id)
            if rec is not None and rec.pending_model == (arg or None) and (arg or None) is not None:
                text = f"Model → {label} — needs approval; the session keeps its current model until approved"
                return {"name": "model", "render": "notice", "data": {"text": text}}
            if driver is None:
                text = f"Model → {label} — saved; applies when the session resumes"
            elif restarted:
                text = f"Model → {label} — stopped the current activity and restarted it on the new model"
            elif after_turn and was_busy:
                text = f"Model → {label} — applies when the current turn finishes"
            else:
                text = f"Model → {label}"
            return {"name": "model", "render": "notice", "data": {"text": text}}
        d = self._drivers.get(session_id)
        if d is None:
            return {"name": str(name).lstrip("/"), "render": "notice",
                    "data": {"text": "This session isn't live — open it and try again."}}
        if str(name).lstrip("/") == "goal":
            cond = (args or "").strip()
            cleared = (not cond) or cond.lower() == "clear"
            try:
                await self.set_goal_for_session(session_id, None if cleared else cond)
            except Exception:
                log.warning("goal capture/persist failed session=%s", session_id, exc_info=True)
            # fall through: forward /goal to the CLI regardless
        info = await d.server_info()
        by_name = {c.name: c for c in cmd_catalog.build_catalog(info)}
        cmd = by_name.get(str(name).lstrip("/"))
        if cmd is None:
            raise KeyError(name)
        return await cmd_dispatcher.dispatch(d, cmd, args or "")

    async def cancel_queued(self, session_id, index: int) -> bool:
        d = self._drivers.get(session_id)
        if d:
            return await d.cancel_queued(index)
        return False

    def transcript_path(self, session_id) -> str | None:
        """Locate the Claude transcript `<config_dir>/projects/*/<claude_session_id>.jsonl`.

        The transcript is found by GLOBBING for its globally-unique claude_session_id
        across all project dirs, rather than computing the project-dir name from cwd.
        Claude derives project-dir names by replacing BOTH '/' and '.' with '-' and
        KEEPS the leading slash (→ leading dash) — replicating that exactly is fragile
        (the old cwd.lstrip("/").replace("/","-") got the leading dash wrong, so replay
        found nothing → blank history for every session). Globbing by the unique UUID is
        robust to the naming convention AND to cwd/project mismatches (a session whose
        recorded cwd differs from where it actually ran still resolves).

        Falls back to the SessionRecord + account_cfg_dir(account) when the driver is
        absent or has no claude_session_id (e.g. rehydrated/detached sessions).

        Each account's per-account `projects` dir is itself a symlink to the one shared
        projects tree (configdir.SHARE_ALLOWLIST). That symlink can rot — point at itself
        or a sibling account — which makes the glob through `<config_dir>/projects` resolve
        nothing and silently blanks the transcript on attach (idle "Waiting for input"
        sessions only show replayed history, so they go blank; a live session masks it by
        streaming). Since the claude_session_id is globally unique and every transcript
        physically lives under the shared base, we also search shared_base()/projects so
        replay survives a broken per-account symlink.
        """
        d = self._drivers.get(session_id)
        if d and d.claude_session_id:
            config_dir, csid = d.config_dir, d.claude_session_id
            rec0 = self._records.get(session_id)
            cwd = getattr(rec0, "cwd", None) if rec0 is not None else None
        else:
            rec = self._records.get(session_id)
            if rec is None or not rec.claude_session_id:
                return None
            config_dir, csid = str(account_cfg_dir(rec.account)), rec.claude_session_id
            cwd = getattr(rec, "cwd", None)
        return self._find_transcript(config_dir, csid, cwd)

    @staticmethod
    def project_dir_name(cwd: str) -> str:
        """Claude Code's project-directory name for *cwd*: '/', '.' and '_' -> '-'.

        Verified against 12/12 real transcripts on 2026-08-21 by reading the cwd
        recorded inside each JSONL and comparing it to the containing directory.
        """
        import re as _re
        return _re.sub(r"[/._]", "-", cwd)

    def _find_transcript(self, config_dir: str, csid: str,
                         cwd: str | None = None) -> str | None:
        """Glob `<root>/projects/*/<csid>.jsonl` in the per-account config dir, then
        the canonical shared base as a backstop against a broken per-account symlink.
        Dedup by resolved path so a healthy symlink isn't globbed twice.

        Resolution is a READ: roots come from pure path helpers, never compose().
        compose() writes credentials and raises FileNotFoundError for an account
        whose store slot is gone (renamed/deleted account still referenced by old
        session rows) — which used to fail every caller fanning out over sessions,
        e.g. one dangling archived row 409'd the entire /sessions/search.
        """
        import glob as _glob

        # FAST PATH: derive the project dir from the cwd and stat ONE candidate.
        # The glob below is O(project dirs) — 1741 on this deployment — and every
        # entry is a network round-trip when the account store is a remote mount.
        # On rocky that made a single lookup take >200s, and _goal_sweep calls it
        # per session ON THE EVENT LOOP, so the daemon never finished starting
        # (2026-08-21 outage). One stat replaces the whole scan in the normal case.
        if cwd:
            enc = self.project_dir_name(cwd)
            for root in (Path(config_dir), Path(shared_base())):
                cand = root / "projects" / enc / f"{csid}.jsonl"
                try:
                    if cand.exists():
                        return str(cand)
                except OSError:
                    pass

        seen: set[str] = set()
        for root in (Path(config_dir), Path(shared_base())):
            projects = root / "projects"
            key = str(projects.resolve()) if projects.exists() else str(projects)
            if key in seen:
                continue
            seen.add(key)
            matches = _glob.glob(str(projects / "*" / f"{csid}.jsonl"))
            if matches:
                return matches[0]
        return None

    # ------------------------------------------------------------------
    # Stall watchdog (background task)
    # ------------------------------------------------------------------

    async def _check_stalls(self, now: float) -> None:
        # Wedge detection is DRIVER-SIDE: `last_progress_at` advances only when the SDK
        # stream yields a meaningful event (driver._note_progress), and the driver's output
        # queue is unbounded — so edge/IPC delivery (the replay-buffer overflow in #185) can
        # NEVER stall a turn or trip this watchdog. A trip here means the turn itself produced
        # no SDK progress for STALL_TIMEOUT; do not chase IPC drops for that symptom.
        quarantined = self._recovery.quarantined(now=now)
        for sid, driver in list(self._drivers.items()):
            # A live turn (`_turn_active`) is the obvious wedge candidate. But an
            # interrupt that the SDK ignored leaves a ZOMBIE: `interrupt()` flips
            # `_turn_active` False, yet the drain never completes (`drain_alive`) because
            # the subprocess is genuinely hung mid-compaction. Both need watching; only a
            # turn that is neither active nor still draining is truly idle.
            active = getattr(driver, "_turn_active", False)
            zombie = (not active) and getattr(driver, "drain_alive", False)
            if not active and not zombie:
                continue
            # A turn whose silence is attributable to API throttling — the driver is
            # backing off, or its account is in recovery quarantine (recently 429'd) —
            # is alive, not wedged. Give it the extended timeout before treating the
            # gap as a hang; past that longer window a genuine hang is still caught.
            # Issue #185: throttled turns were interrupted as "Turn failed" under load.
            throttled = getattr(driver, "in_api_backoff", False) or \
                getattr(driver, "account", None) in quarantined
            timeout = STALL_TIMEOUT_THROTTLED if throttled else STALL_TIMEOUT
            if now - driver.last_progress_at <= timeout:
                continue
            if getattr(driver, "awaiting_user_input", False):
                # Blocked on a permission / plan / question — waiting on the human,
                # not wedged. Interrupting here is the spurious "[Request interrupted
                # by user]" users see when they take a while to answer.
                continue
            if self._recovery.stall_cap_reached(sid, now=now):
                # Interrupts aren't landing (the cap of in-window attempts is spent and
                # the turn is still wedged) — escalate to a recompose-in-place rather
                # than abandon it. Bounded by crash_cap (shared with process-crash
                # recovery; both rebuild the driver on the same account, resume, and
                # resend the last prompt) so a turn that keeps wedging ultimately pauses
                # to needs_attention instead of looping forever.
                record = self._records.get(sid)
                if record is None:
                    continue
                if self._recovery.crash_cap_reached(sid, now=now):
                    log.warning("stall watchdog: recompose cap reached, pausing session=%s", sid)
                    await self._pause_session(record, reason="stall_cap")
                else:
                    self._recovery.record_crash(sid, now=now)
                    log.warning("stall watchdog: recomposing wedged turn session=%s", sid)
                    await self._reassign(record, record.account, reason="stall_recompose")
                continue
            self._recovery.record_stall(sid, now=now)
            log.warning("stall watchdog: interrupting wedged turn session=%s", sid)
            await driver.interrupt()

    async def _check_disabled_account_sessions(self) -> None:
        """Rotate any live sessions off accounts that have been disabled.

        Called from the stall loop so disabled accounts shed their sessions
        without requiring a daemon restart or manual rotation.
        """
        def _read_disabled() -> set[str]:
            from ccswitch import ledger as _ledger
            with _ledger.locked_ledger() as led:
                return {n for n, _ in ccswitch_accounts.all_accounts()
                        if not _ledger.is_account_enabled(led, n)}

        disabled = await asyncio.to_thread(_read_disabled)
        if not disabled:
            return
        enabled_hosts = await self._enabled_host_names()
        for record in list(self._records.values()):
            if record.account not in disabled:
                continue
            if record.status in INACTIVE_SESSION_STATUSES or record.status == "queued":
                continue
            # Host ownership (#852): same duplicate-actor shape as the stuck-session
            # sweep — the owning host's daemon runs this very loop for its rows.
            if not self._owns_record(record, enabled_hosts):
                continue
            log.info("disabled_account_sweep: rotating session=%s off disabled account=%s",
                     record.id, record.account)
            self._spawn(self._rotate_away(record, reason="account_disabled"),
                        name=f"rotate-disabled-{record.id}")

    async def _stall_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._check_stalls(self._clock())
                await self._check_disabled_account_sessions()
            except Exception:
                log.exception("stall watchdog tick failed")

    # ------------------------------------------------------------------
    # Rotation monitor (background task)
    # ------------------------------------------------------------------

    async def _enabled_host_names(self) -> frozenset[str] | None:
        """Enabled fleet-registry host names, cached for _OWNERSHIP_HOSTS_TTL_S.

        Ownership input for _owns_record (#852). Returns None when the registry is
        unreadable AND no prior snapshot exists — "unknown", which _owns_record
        treats as skip-foreign-stamps: delayed recovery of an orphaned row beats
        racing a live worker's _rotate_away over the same shared row.
        """
        now = time.monotonic()
        cached = self._ownership_hosts_cache
        if cached is not None and now - cached[0] < _OWNERSHIP_HOSTS_TTL_S:
            return cached[1]
        from sqlalchemy import select as sa_select
        from server.db.models import Host
        try:
            async with db_session() as db:
                names = frozenset((await db.execute(
                    sa_select(Host.name).where(Host.enabled.is_(True)))).scalars().all())
        except Exception:
            log.warning("ownership: enabled-hosts registry read failed", exc_info=True)
            return cached[1] if cached is not None else None
        self._ownership_hosts_cache = (now, names)
        return names

    def _owns_record(self, record: SessionRecord,
                     enabled_hosts: frozenset[str] | None) -> bool:
        """True iff THIS daemon is the recovery/rotation actor for *record* (#852).

        _records is host-partitioned on a worker (rehydrate filters), but the
        primary deliberately holds every row — the fleet dashboard is served from
        its _records — so its ACTING loops (stuck-session sweep, rotation monitor)
        must check per-row ownership or two daemons race duplicate _rotate_away
        spawns over the same shared rows (the reason the sweep was fleet-global
        before this).
        """
        if record.id in self._drivers:
            # The live driver is in this process — de facto ours regardless of
            # what the stamp says (e.g. a mid-flight registry rename).
            return True
        host = record.host
        me = self_host_name()
        if daemon_role() == "worker":
            return host == me
        # Primary owns: unstamped rows (pre-fleet legacy + its own automation
        # spawns, which never pass through the dispatcher), its own stamp, and
        # DEFAULT_HOST_NAME — a UDS registry entry is this host by definition
        # (same rule as the health check's split-brain guard, #844). A row
        # stamped for another ENABLED host has a live daemon responsible for it;
        # a stamp that has left the registry is an orphan the primary adopts.
        from server.sessions.fleet_router import DEFAULT_HOST_NAME
        if host is None or host == me or host == DEFAULT_HOST_NAME:
            return True
        if enabled_hosts is None:  # registry unknown — don't race a live worker
            return False
        return host not in enabled_hosts

    async def _rotation_monitor(self) -> None:
        """Subscribe to usage events; rotate any session whose account hits ceiling.

        Also handles accounts that become persistently unreachable (usage=None for
        _UNREACHABLE_ROTATION_THRESHOLD consecutive polls) — the canonical case is a
        Copilot proxy that exits when its premium-request quota is exhausted.
        """
        q = self._poller.subscribe()
        try:
            while True:
                event = await q.get()
                account: str = event.get("account", "")
                usage = event.get("usage")

                if usage is None:
                    # Transient fetch hiccup vs. persistently dead proxy — only act
                    # once enough consecutive failures rule out a brief network blip.
                    if (self._poller.consecutive_failures(account)
                            < _UNREACHABLE_ROTATION_THRESHOLD):
                        continue
                    reason = "account_unreachable"
                    session_pct = 0.0  # unknown; treat as ceiling for rotation logic
                else:
                    session_pct = getattr(usage, "session_pct", 0.0)
                    if session_pct < _ROTATION_CEILING:
                        continue
                    reason = "session limit reached"

                # Find all live sessions on this account and rotate them.
                enabled_hosts = await self._enabled_host_names()
                for record in list(self._records.values()):
                    if record.account != account:
                        continue
                    if record.status in INACTIVE_SESSION_STATUSES:
                        continue
                    # Queued sessions have no driver yet (intentionally) and will be
                    # started by _maybe_start_queued when a slot frees — skip them so
                    # the rotation monitor cannot bypass the cap.
                    if record.status == "queued":
                        continue
                    # Host ownership (#852): never rotate/restart a session whose
                    # driver lives on another fleet host — its own daemon (which
                    # also runs this monitor) is the actor; acting here would
                    # spawn a duplicate driver on the wrong host.
                    if not self._owns_record(record, enabled_hosts):
                        log.debug("rotation.skip_foreign session=%s host=%s",
                                  record.id, record.host)
                        continue
                    if record.id not in self._drivers:
                        # Driver died separately from any turn (e.g. proxy crash while
                        # session was idle).  Restart on a new account so the session
                        # is live again without requiring user interaction.
                        log.info(
                            "rotation.driverless session=%s account=%s reason=%s",
                            record.id, account, reason,
                        )
                        self._spawn(
                            self._restart_driverless(record, reason=reason),
                            name=f"restart-{record.id}",
                        )
                        continue
                    if record.account_pinned and usage is not None:
                        # Pinned + usage ceiling: mark limited so the user knows
                        # they need to unpin before the session can auto-rotate.
                        log.info("rotation.skip_pinned session=%s account=%s", record.id, account)
                        record.status = "limited"
                        self._spawn(
                            self._persist_status(record.id, "limited"),
                            name=f"limited-{record.id}",
                        )
                        self._publish_event({
                            "type": "status", "sessionId": record.id, "state": "limited",
                        })
                        continue
                    log.info(
                        "rotation.trigger session=%s account=%s pct=%.1f reason=%s",
                        record.id, account, session_pct, reason,
                    )
                    self._spawn(
                        self._do_rotate(record, reason=reason),
                        name=f"rotate-{record.id}",
                    )
        except asyncio.CancelledError:
            pass
        finally:
            self._poller.unsubscribe(q)

    async def _do_rotate(self, record: SessionRecord, *,
                         reason: str = "session limit reached") -> None:
        """Usage-ceiling rotation entrypoint (called by the poller monitor)."""
        await self._rotate_away(record, reason=reason)

    def _account_is_dead(self, account: str) -> bool:
        """True when the account is at usage ceiling OR persistently unreachable.

        Used by crash recovery to decide whether to rotate away rather than
        rebuilding the driver on the same broken account.
        """
        usage = self._poller.cached(account)
        if usage is not None:
            return getattr(usage, "session_pct", 0.0) >= _ROTATION_CEILING
        # usage=None: distinguish "not yet polled" from "persistently failing"
        return self._poller.consecutive_failures(account) >= _UNREACHABLE_ROTATION_THRESHOLD

    async def _restart_driverless(self, record: SessionRecord, *, reason: str) -> None:
        """Restart a session whose driver has died on a new account.

        Called by the rotation monitor when a live session has no driver (the
        subprocess died while the session was idle, e.g. Copilot proxy crash).
        Picks a fresh account and delegates to _rotate_away which rebuilds the
        driver and replays the last prompt.
        """
        log.info("rotation.restart_driverless session=%s reason=%s", record.id, reason)
        await self._rotate_away(record, reason=reason)

    async def _stuck_session_sweep(self) -> None:
        """Periodically retry rotation for sessions paused as needs_attention.

        The rotation monitor is event-driven — it fires when accounts *degrade*,
        not when they *recover*. A session paused by _pause_session() (reason:
        all_accounts_busy, crash_cap, rotation_cap, stall_cap, turn_dropped_cap)
        therefore stays stuck until the user intervenes, even when a healthy account
        frees up. This sweep fires every _STUCK_SESSION_SWEEP_INTERVAL seconds and
        calls _rotate_away for each needs_attention session. _rotate_away re-pauses
        gracefully if accounts are still busy, so the sweep is safe to run repeatedly.
        """
        while True:
            if not self._shutting_down:
                try:
                    await self._sweep_needs_attention()
                except Exception:
                    log.exception("stuck-session-sweep: sweep failed")
            await asyncio.sleep(_STUCK_SESSION_SWEEP_INTERVAL)

    async def _sweep_needs_attention(self) -> None:
        """One sweep pass: spawn a rotation attempt for every OWNED needs_attention
        session. The ownership check (#852) is what makes this sweep safe to run on
        every daemon: each recovers its own sessions, none races a duplicate
        _rotate_away over rows another live host owns."""
        enabled_hosts = await self._enabled_host_names()
        for record in list(self._records.values()):
            if record.status != "needs_attention":
                continue
            if not self._owns_record(record, enabled_hosts):
                log.debug("stuck-sweep.skip_foreign session=%s host=%s",
                          record.id, record.host)
                continue
            log.info(
                "stuck-sweep.retry session=%s account=%s", record.id, record.account,
            )
            self._spawn(
                self._rotate_away(record, reason="stuck_session_sweep"),
                name=f"sweep-rotate-{record.id}",
            )

    # ------------------------------------------------------------------
    # Failure-driven recovery (429 / 401 during a live turn)
    # ------------------------------------------------------------------

    async def _handle_recoverable_failure(self, session_id: str, kind: str,
                                          retry_after: float | None) -> None:
        """Driver callback: a turn failed with a recoverable kind. Recover off the
        event loop so the driver's drain is never blocked."""
        if self._shutting_down:
            # Daemon teardown (deploy / watchdog restart) is dropping this turn — not a
            # real failure. Skip recovery so we don't escalate to needs_attention; the
            # next boot's rehydrate() brings the session back as idle (resumable).
            log.info("recover.skip_shutdown session=%s kind=%s", session_id, kind)
            return
        record = self._records.get(session_id)
        if record is None or session_id not in self._drivers:
            return
        self._spawn(
            self._recover_session(record, kind, retry_after),
            name=f"recover-{session_id}",
        )

    async def _recover_session(self, record: SessionRecord, kind: str,
                               retry_after: float | None) -> None:
        """Apply the recovery policy for one recoverable turn failure."""
        now = self._clock()
        if kind == "turn_dropped":
            # A turn that dropped before the model committed any continuation. Escalate:
            # 1st drop → plain resend (cheap; absorbs a transient drop). Repeat drops in
            # the window → /compact first (the context is likely full and re-dropping the
            # resend). At the cap, stop and surface an explicit give-up — not silence.
            if self._recovery.resend_cap_reached(record.id, now=now):
                driver = self._drivers.get(record.id)
                if driver is not None:
                    await driver.emit_error("turn_dropped_cap", _TURN_DROPPED_GIVEUP)
                await self._pause_session(record, reason="turn_dropped_cap")
                return
            precompact = self._recovery.resend_count(record.id, now=now) >= 1
            self._recovery.record_resend(record.id, now=now)
            await self._replay_same_account(record, 0.0, precompact=precompact)
            return
        if self._recovery.rotation_cap_reached(record.id, now=now):
            await self._pause_session(record, reason="rotation_cap")
            return
        if kind == "process_crash":
            if self._recovery.crash_cap_reached(record.id, now=now):
                await self._pause_session(record, reason="crash_cap")
                return
            self._recovery.record_crash(record.id, now=now)
            # When the account is dead (usage ceiling or persistently unreachable
            # proxy — the Copilot quota-exhaustion case), retrying on the same
            # account just crashes again.  Quarantine it and rotate away instead.
            if self._account_is_dead(record.account):
                self._recovery.quarantine(record.account, until=now + QUARANTINE_429)
                await self._rotate_away(record, reason="process_crash_account_exhausted")
                return
            # Rebuild the driver in place on the same account, resuming the
            # conversation; _reassign re-sends the last prompt so the interrupted
            # turn continues. Same machinery rate-limit recovery uses.
            await self._reassign(record, record.account, reason="process_crash")
            return
        if kind == "rate_limit":
            if retry_after is not None and retry_after <= RETRY_AFTER_MAX:
                await self._replay_same_account(record, retry_after)
                return
            cooldown = min(
                retry_after if (retry_after and retry_after > 0) else QUARANTINE_429,
                QUARANTINE_MAX,
            )
            self._recovery.quarantine(record.account, until=now + cooldown)
            await self._rotate_away(record, reason="rate_limit")
            return
        if kind == "auth_401":
            if self._session_uses_setup_token(record):
                # A setup-token has no refresh grant — a 401 means the long-lived token is
                # dead. Skip the recompose detour; flag for regeneration and rotate.
                self._recompose_attempts.pop(record.id, None)
                self._recovery.quarantine(record.account, until=now + QUARANTINE_401)
                self._mark_needs_reauth(record.account)
                await self._rotate_away(record, reason="auth_401_setup_token")
                return
            if not self._recently_recomposed(record.id, now):
                self._recompose_attempts[record.id] = now
                await self._recompose_same_account(record)
                return
            # A 401 that survives a recompose means the stored creds are genuinely
            # bad — quarantine, propagate to the ledger (observability), then rotate.
            self._recompose_attempts.pop(record.id, None)
            self._recovery.quarantine(record.account, until=now + QUARANTINE_401)
            self._mark_needs_reauth(record.account)
            await self._rotate_away(record, reason="auth_401")
            return

    def _recently_recomposed(self, session_id: str, now: float) -> bool:
        ts = self._recompose_attempts.get(session_id)
        return ts is not None and (now - ts) <= self._recompose_ttl

    def _session_uses_setup_token(self, record: SessionRecord) -> bool:
        """True when this session authenticates via a stored long-lived setup-token
        (CLAUDE_CODE_OAUTH_TOKEN). A 401 on such a session is terminal — there is no
        OAuth grant to recompose — so it goes straight to needs-reauth."""
        return bool(crypto.read_oauth_token(record.account))

    async def _replay_same_account(self, record: SessionRecord, retry_after: float,
                                   *, precompact: bool = False) -> None:
        """Wait out a short Retry-After, then re-send the last prompt on the same account.
        When *precompact* is set, send /compact first; the driver's FIFO prompt queue runs
        the resend immediately after the compaction turn completes (no manual await)."""
        await asyncio.sleep(max(0.0, retry_after))
        driver = self._drivers.get(record.id)
        last = self._last_prompt.get(record.id)
        if driver is None or not last:
            return
        if precompact:
            await driver.send_prompt("/compact")
        await driver.send_prompt(last)

    async def _recompose_same_account(self, record: SessionRecord) -> None:
        """Heal a clobbered live .credentials.json. Force a token refresh FIRST so the
        relaunch picks up a fresh access token (the dominant in-session 401 cause — a stale
        stored token would otherwise 401 again immediately and falsely flip needs-reauth),
        then recompose from the store and relaunch the same account in place."""
        try:
            await asyncio.to_thread(ccswitch_accounts.ensure_valid_token, record.account)
        except Exception:
            log.warning("recompose.ensure_valid_token failed account=%s",
                        record.account, exc_info=True)
        await self._reassign(record, record.account, reason="recompose")

    async def _rotate_away(self, record: SessionRecord, *, reason: str) -> None:
        """Select a healthy account (excluding quarantined + current) and reassign."""
        now = self._clock()
        old = record.account
        exclude = self._recovery.quarantined(now=now) | frozenset({old})
        try:
            new_account = select_account(exclude=exclude, load=self.live_session_load()).name
        except AllAccountsBusy as e:
            await self._pause_session(record, reason="all_accounts_busy",
                                      earliest_reset=e.earliest_reset)
            return
        if record.account_pinned:
            record.account_pinned = False
            await self._persist_pin(record.id, False)
        self._recovery.record_rotation(record.id, now=now)
        await self._reassign(record, new_account, reason=reason)

    async def _pause_session(self, record: SessionRecord, *, reason: str,
                             earliest_reset=None) -> None:
        """No recovery possible (no healthy account / rotation cap). Flag the session
        for the user and publish a machine-readable paused event."""
        try:
            await self._persist_status(record.id, "needs_attention")
        except Exception:
            log.warning("pause.persist_status failed session=%s", record.id, exc_info=True)
        self._publish_event({
            "type": "paused", "sessionId": record.id, "account": record.account,
            "reason": reason,
            "earliestReset": iso_utc(earliest_reset),
        })
        log.warning("recover.paused session=%s reason=%s", record.id, reason)

    def _mark_needs_reauth(self, account: str) -> None:
        """Propagate a recompose-failed in-session 401 to the ledger so the dashboard
        stops reporting a falsely-healthy 'ok' for an account whose creds are dead.
        A transient 429 never reaches here — only a 401 that survives a recompose."""
        try:
            from ccswitch import ledger
            with ledger.locked_ledger() as led:
                ledger.set_status(led, account, "needs-reauth")
        except Exception:
            log.warning("recover.mark_needs_reauth failed account=%s", account, exc_info=True)
        with contextlib.suppress(RuntimeError):  # no running loop in some sync callers/tests
            self._spawn(self._mark_needs_reauth_audit(account),
                        name=f"mark-needs-reauth-{account}")

    async def _mark_needs_reauth_audit(self, account: str) -> None:
        """Write an account.needs_reauth audit row for the in-session 401 flip site."""
        from ccswitch.accounts import AuthFailure
        from server.audit import record_audit
        from server.usage.auth_failure_audit import build_needs_reauth_detail
        from server.db import db_session, _session_factory
        if _session_factory is None:
            return
        f = AuthFailure(account=account, status=401, body=None,
                        path="live-session", token_expires_at=ccswitch_accounts._slot_expires_at(account), attempts=1)
        try:
            async with db_session() as db:
                await record_audit(db, "account.needs_reauth", "system", build_needs_reauth_detail(f))
                await db.commit()
        except Exception:
            log.warning("mark_needs_reauth_audit failed account=%s", account, exc_info=True)

    async def switch_account_manual(self, session_id: str, new_account: str) -> None:
        """User-initiated account switch. Pins the session against usage-ceiling
        rotation, then resumes the conversation on new_account via the shared
        _reassign primitive (same path as auto-rotation / recovery)."""
        record = self._records.get(session_id)
        if record is None:
            raise KeyError(session_id)
        # Allow switching even when there is no live driver: the session may have
        # lost its driver because the account's proxy died (Copilot quota exhaustion).
        # _reassign now handles the driverless case by rebuilding from record state.
        # Validate: reject path-traversal / bad chars.
        assert_valid_account_name(new_account)
        # Validate: reject unknown accounts (must be in the configured ledger).
        known = {a["name"] for a in ccswitch_accounts.list_accounts()}
        if new_account not in known:
            raise KeyError(f"Unknown account: {new_account}")
        record.account_pinned = True
        await self._persist_pin(session_id, True)
        await self._reassign(record, new_account, reason="manual")

    async def _persist_pin(self, session_id: str, pinned: bool) -> None:
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id)
                .values(account_pinned=pinned)
            )

    async def set_model_for_session(
        self, session_id: str, model: str | None, *, when: str = "now",
        identity: str = SYSTEM_IDENTITY,
    ) -> bool:
        """Set the live model AND persist it, so it survives rehydrate and every
        driver rebuild (rotation/recovery re-applies record.model in _reassign).

        A busy driver only picks up set_model on FUTURE turns (verified against
        claude 2.1.251: a mid-turn set_model leaves the in-flight turn on the old
        model and the next turn runs on the new one). ``when`` picks how a
        mid-turn switch lands:

        * ``"now"`` — stop and restart: interrupt → set_model → re-send the stored
          last prompt. That's the same stop → switch → replay dance _reassign
          performs across drivers during rotation, here on the same driver — the
          driver's FIFO prompt queue runs the resend as a fresh turn once the
          interrupt unwinds. Returns True iff the turn was actually restarted; with
          no stored prompt (e.g. the daemon restarted mid-turn) the interrupt +
          switch still happen but nothing is resent.
        * ``"after_turn"`` — hand the model to the SDK without interrupting and let
          the in-flight turn finish on the old model. ``record.model_pending`` is
          set so the UI can say "switches after this turn"; _settle_model_pending
          clears it when the driver reports the turn over.

        Record update + persistence run in every case, including driverless
        (detached), so the model applies when the session resumes."""
        # Model-approval gate (mid-session). Mirrors create_session's launch gate,
        # the other point resolve_model_policy guards, so no path onto a session can
        # drift. A gated switch is PARKED here — pending_model recorded + persisted,
        # the driver NOT touched, the session keeps running on its current model —
        # rather than applied; a blocked one is refused. `identity` defaults to
        # SYSTEM_IDENTITY so rotation/recovery/automation callers are unaffected.
        # NB: rotation re-applies an already-approved model via the LOW-LEVEL
        # driver.set_model() in _reassign, NOT this method, so it never re-triggers
        # approval.
        record = self._records.get(session_id)
        _account = record.account if record is not None else None
        from server.sessions.model_policy import resolve_model_policy
        async with db_session() as _db:
            _policy = await resolve_model_policy(_db, model=model, account=_account)
        if _policy == "block":
            raise ModelBlockedError(model)
        if _policy == "require_approval":
            from datetime import datetime
            now = datetime.utcnow()
            if record is not None:
                record.pending_model = model
            async with db_session() as db:
                from sqlalchemy import update as sa_update
                await db.execute(
                    sa_update(DbSession).where(DbSession.id == session_id).values(
                        pending_model=model,
                        pending_model_requested_by=identity,
                        pending_model_requested_at=now))
                await record_audit(db, type="model_approval.requested", identity=identity,
                                   detail={"session_id": session_id, "model": model,
                                           "account": _account})
            # Live-push a status frame (unchanged status) so the dashboard reconciles
            # and picks up pendingModel without waiting for the poll — the same
            # lifecycle-frame mechanism _persist_status uses for a session-record change.
            if record is not None:
                self._publish_event({
                    "type": "status", "sessionId": session_id, "status": record.status,
                    "updatedAt": record.updated_at_iso, "revAt": record.rev_at})
            log.info("session.model_approval_requested id=%s model=%s by=%s",
                     session_id, model, identity)
            return False
        restarted = False
        pending = False
        driver = self._drivers.get(session_id)
        if driver is not None:
            if driver.turn_active and when == "after_turn":
                await driver.set_model(model)
                pending = True
            elif driver.turn_active:
                # Stamp BEFORE the interrupt: its status flap fires the autopilot nudge
                # tick, and autopilot must already see the swap as in flight by then.
                self._turn_restart_at[session_id] = self._clock()
                await driver.interrupt()
                await driver.set_model(model)
                last = self._last_prompt.get(session_id)
                if last:
                    await driver.send_prompt(last)
                    restarted = True
            else:
                await driver.set_model(model)
        record = self._records.get(session_id)
        if record is not None:
            record.model = model
            record.model_pending = pending
        await self._persist_model(session_id, model)
        return restarted

    def _settle_model_pending(self, session_id: str, status: str) -> None:
        """Clear ``record.model_pending`` once the turn it was waiting on is over.
        Called from the driver's on_status path with the derived status: any status
        outside the mid-turn set (in_progress / condensing / retrying) means the SDK
        has handed the turn back, so the next turn runs on ``record.model``."""
        from server.sessions.status import MID_TURN_STATUSES
        record = self._records.get(session_id)
        if record is None or not record.model_pending:
            return
        if status not in MID_TURN_STATUSES:
            record.model_pending = False

    async def _persist_model(self, session_id: str, model: str | None) -> None:
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id).values(model=model)
            )

    async def set_goal_for_session(self, session_id: str, goal: str | None) -> None:
        """Record the session's active /goal condition AND persist it, so the badge
        survives rehydrate. The /goal text is still forwarded to the CLI by run_command;
        this only mirrors the state for the UI (no SDK method exists for goals)."""
        record = self._records.get(session_id)
        if record is not None:
            record.active_goal = goal
        await self._persist_goal(session_id, goal)

    async def _persist_goal(self, session_id: str, goal: str | None) -> None:
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id).values(active_goal=goal)
            )

    def note_user_interaction(self, session_id: str) -> None:
        """Stamp the wall-clock time the user last steered this session (prompt/answer/permission/
        typing ping). Drives autopilot human-deference + the cooldown badge. In-memory ONLY — no DB
        write (a 120s window is meaningless across a restart), so this is safe to call on the WS
        receive path (never blocks on I/O, cf. the no-DB-write-on-attach rule)."""
        from datetime import datetime
        record = self._records.get(session_id)
        if record is not None:
            record.last_user_interaction_at = datetime.utcnow()

    async def set_autopilot_paused(self, session_id: str, paused: bool) -> None:
        """Pin a session to manual (paused=True) or return it to autopilot governance.
        Mirrors the in-memory record and persists so the opt-out survives rehydrate."""
        record = self._records.get(session_id)
        if record is not None:
            record.autopilot_paused = paused
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id)
                .values(autopilot_paused=paused)
            )

    def _install_driver(self, session_id: str, driver: SessionDriver) -> None:
        """Register *driver* as the live driver for *session_id* and notify the
        driver-replaced listener. Recovery swaps the driver with no RPC (see _reassign),
        yet the daemon's per-session pump is (re)started only inside the ensure_driver
        RPC — so a swap would leave the pump forwarding the DEAD driver's ended events()
        and every attached viewer's live transcript freezes until a reattach. Firing the
        hook lets the RPC layer restart the pump on the new driver. No-op when
        unregistered (edge/in-process mode has no daemon pump)."""
        self._drivers[session_id] = driver
        cb = self.on_driver_replaced
        if cb is not None:
            cb(session_id, driver)

    async def _reassign(self, record: SessionRecord, new_account: str, *, reason: str) -> None:
        """Resume the session's claude conversation on *new_account* (may equal the
        current account, for a 401 recompose). Relaunches the driver, replays the last
        prompt, updates the DB, and emits a rotated event.

        Works for both live sessions (closes the old driver first) and driverless ones
        (e.g. the driver died while the session was idle — in that case params are
        derived from the SessionRecord instead of the old driver).
        """
        # Validate BEFORE touching anything. switch_account_manual() validated its own
        # argument, but _rotate_away() did not — so a phantom account name out of
        # select_account() reached here, the working driver was closed, and the
        # relaunch then failed, leaving the session driverless and silent. See
        # docs/superpowers/fix-patterns/2026-08-31-empty-name-aliases-store-root.md
        # The guard belongs in the shared primitive, not in one of its callers.
        if not validate_account_name(new_account):
            log.error("reassign.invalid_account session=%s account=%r reason=%s — "
                      "keeping the current driver", record.id, new_account, reason)
            await self._pause_session(record, reason="invalid_rotation_target")
            return
        # Same defer contract as a model swap: the dance below flips the session
        # through actionable statuses autopilot would otherwise act on mid-restart.
        self._turn_restart_at[record.id] = self._clock()
        old_driver = self._drivers.get(record.id)
        old = record.account

        # Derive driver construction params from whichever source is available.
        if old_driver is not None:
            claude_sid = old_driver.claude_session_id
            cwd = old_driver.cwd
            mode = old_driver.mode
            identity = old_driver.identity
        else:
            # Driverless path: session lost its driver while idle (proxy crash, etc.).
            # Use stored record fields — cwd and claude_session_id are persisted on
            # every turn so they're reliable even after a daemon restart.
            claude_sid = record.claude_session_id
            cwd = record.cwd
            mode = getattr(record, "permission_mode", "interactive")
            identity = SYSTEM_IDENTITY

        try:
            if old_driver is not None:
                await old_driver.interrupt()
                await old_driver.aclose()
            cfg = compose(new_account)
            configdir.trust_project(new_account, cwd)
            Path(cwd).mkdir(parents=True, exist_ok=True)
            _copilot = getattr(self, "_copilot", None)
            if _copilot is not None and _provider_of(new_account) == _copilot_mod.PROVIDER:
                await _copilot.ensure(new_account)
            new_driver = SessionDriver(
                record.id, new_account, cwd, str(cfg),
                mode=mode, sdk_factory=self._session_factory(record.id),
                registry=self._registry, identity=identity,
                resume=claude_sid,
                on_session_id=self._persist_claude_session_id,
                on_sync_back=functools.partial(ccswitch_accounts.sync_back, new_account),
                on_status=self._persist_status,
                on_context_usage=self._persist_context_usage,
                on_record_condense_ms=self._record_condense_ms,
                get_condense_estimate=self._condense_estimate,
                on_recoverable_failure=self._handle_recoverable_failure,
                suggester=self._suggester,
            )
            # Set the record's account BEFORE start(): start() triggers the SDK factory →
            # _oat_env, which reads record.account to pick the setup-token. If left stale, a
            # cross-account reassign injects the old account's token into the new config (#346).
            record.account = new_account
            await new_driver.start()
            self._install_driver(record.id, new_driver)
            if record.model is not None:
                await new_driver.set_model(record.model)
            last = self._last_prompt.get(record.id)
            if last:
                await new_driver.send_prompt(last)
            async with db_session() as db:
                from sqlalchemy import update as sa_update
                await db.execute(
                    sa_update(DbSession)
                    .where(DbSession.id == record.id)
                    .values(account_name=new_account)
                )
            # Bump the in-memory rev token BEFORE publishing the event (mirrors
            # _persist_status's "row-write token" bump). Without this the `rotated`
            # WS patch (web/src/ws/client.ts _patchSessionAccount) carries no fresher
            # revAt than what a poll snapshotted just before the rotation persisted,
            # so reconcileSessions (web/src/session/reconcile.ts) can't tell the two
            # apart and the stale poll silently reverts the account badge.
            from datetime import datetime
            record.updated_at = datetime.utcnow()
            log.info("reassign.complete session=%s %s→%s reason=%s",
                     record.id, old, new_account, reason)
            self._publish_event({
                "type": "rotated", "sessionId": record.id, "fromAccount": old,
                "toAccount": new_account, "reason": reason,
                "turnRestarted": bool(last),
                "revAt": record.rev_at,
            })
            log_event(log, logging.INFO, "session.rotate",
                      session_id=record.id,
                      from_account=old, to_account=new_account, reason=reason)
        except Exception:
            log.exception("reassign.error session=%s", record.id)
            # The old driver is already closed by this point, so the session now has
            # NO driver. Logging alone left it silently stranded: the UI kept showing
            # a live-looking session and every prompt the user sent spun forever with
            # no reply. Flag it so it surfaces as needing attention instead.
            try:
                await self._pause_session(record, reason=f"reassign_failed:{reason}")
            except Exception:
                log.warning("reassign.pause_failed session=%s", record.id, exc_info=True)

    # ------------------------------------------------------------------
    # Archive / unarchive / permanent-delete
    # ------------------------------------------------------------------

    async def archive_session(self, session_id: str, *, archived_by: str = "user",
                              context: dict | None = None) -> None:
        """Archive a session: stop the driver and flip status='archived'. Nothing moves.

        The DB row is KEPT (status='archived') and the working directory STAYS IN
        PLACE — archiving is a pure state change, so it completes in milliseconds
        and cannot half-fail, time out the daemon RPC, or saturate the thread pool
        when auto-archive fires in bursts (the old in-request move did all three).
        Disk is reclaimed only when the max_archived_sessions cap purges the row
        (permanent_delete), which is also where the worktree is deleted and its
        git registration cleared.

        ``archive_root`` is stamped NULL: NULL marks a stay-in-place archive whose
        files are at ``cwd``; a non-NULL value marks a legacy row whose files were
        moved into an archive home (unarchive still restores those).

        ``archived_by`` records provenance ("user" | "autopilot" | "backlog-runner") and
        ``context`` snapshots the decision features for AUTO archives, so a later manual
        unarchive can tell an auto-archive was reversed and record a learning heuristic.
        """
        # Teardown the virtual X11 display — mirrors close_session.
        if self._x11 is not None:
            try:
                await self._x11.stop_display(session_id)
            except Exception:
                log.exception("session.display_cleanup_failed id=%s", session_id)

        # Stop the driver and clear in-memory state (mirrors close_session head).
        driver = self._drivers.pop(session_id, None)
        self._last_prompt.pop(session_id, None)
        self._turn_restart_at.pop(session_id, None)
        self._pending_initial_prompt.pop(session_id, None)

        # Remove from active records BEFORE driver.aclose() so the session
        # disappears from list_sessions() immediately and a background refetch
        # can never see it half-archived.
        record = self._records.pop(session_id, None)

        # Clear the in-memory worktree registration; the git registration stays
        # until the cap purge deletes the worktree (permanent_delete).
        self._worktrees.pop(session_id, None)

        # Close the driver in the background: aclose() takes up to
        # _CLOSE_GRACE_SEC (5 s) for live sessions and nothing below depends on
        # it, so the archive request must not wait on it.
        if driver is not None:
            self._spawn(self._close_archived_driver(driver, session_id),
                        name=f"archive-driver-close-{session_id}")

        # Remove from the session queue BEFORE persisting archived status so the
        # drain triggered below cannot resurrect a just-archived queued session.
        from server.sessions import session_queue as sq
        await sq.remove(session_id)

        # Mark archived in DB. archive_root=None also clears a stale root left by
        # a legacy archive→unarchive round trip, whose files are back at cwd.
        async with db_session() as db:
            from datetime import datetime
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession)
                .where(DbSession.id == session_id)
                .values(status="archived", archived_by=archived_by, archive_context=context,
                        archived_at=datetime.utcnow(), archive_root=None)
            )
        prev_status = record.status if record is not None else None
        log_event(log, logging.INFO, "session.terminal",
                  session_id=session_id,
                  **{"from": prev_status, "to": "archived", "reason": "archive"})
        log.info("session.archive id=%s done (worktree left in place)", session_id)

        # Enforce the configured archived-session cap off the request path — the
        # purge deletes worktrees (heavy I/O) and must never slow or fail the
        # archive that triggered it. _spawn logs any failure.
        self._spawn(self._enforce_archive_limit(), name="archive-limit-on-archive")

        # Drain the session queue: an archived session frees a slot so the next queued
        # session can start. Tracked via _spawn — mirrors close_session.
        self._spawn(self._maybe_start_queued(), name="queue-drain-on-archive")

    async def _close_archived_driver(self, driver: "SessionDriver", session_id: str) -> None:
        """Background half of archive_session: graceful driver shutdown (5 s grace)."""
        try:
            await driver.aclose()
        except Exception:
            log.warning("session.archive_driver_close_failed id=%s", session_id,
                        exc_info=True)

    async def _effective_repo_root(self) -> str:
        """DB AppSettings.repo_root → env/default fallback.

        Reads the runtime-settable repo root from the settings singleton so that
        changes made via the settings UI take effect without a server restart.
        """
        from sqlalchemy import select as sa_select

        from server.db.models import AppSettings
        try:
            async with db_session() as db:
                val = (await db.execute(
                    sa_select(AppSettings.repo_root)
                    .where(AppSettings.id == "singleton")
                )).scalar_one_or_none()
        except Exception:
            log.debug("settings: repo_root read failed", exc_info=True)
            return self._repo_root if hasattr(self, "_repo_root") else os.path.expanduser("~/GitHub")
        return val or (self._repo_root if hasattr(self, "_repo_root") else os.path.expanduser("~/GitHub"))

    async def _effective_archive_home(self) -> "Path":
        """DB AppSettings.archive_home → CCSWITCH_ARCHIVE_HOME env → default fallback.

        Reads the runtime-settable archive directory from the settings singleton.
        Falls back to the env-var/default path from archive_dir() if not set in DB.
        """
        from sqlalchemy import select as sa_select

        from ccswitch.paths import archive_dir
        from server.db.models import AppSettings
        try:
            async with db_session() as db:
                val = (await db.execute(
                    sa_select(AppSettings.archive_home)
                    .where(AppSettings.id == "singleton")
                )).scalar_one_or_none()
        except Exception:
            log.debug("settings: archive_home read failed", exc_info=True)
            return archive_dir()
        if val:
            p = Path(val)
            p.mkdir(parents=True, exist_ok=True)
            return p
        return archive_dir()

    async def _read_max_archived_sessions(self) -> int | None:
        """Read ``max_archived_sessions`` from the app-settings singleton.

        Returns the cap (>=1) or None for 'unlimited' — which is also the fallback if
        no settings row exists or the read fails."""
        from sqlalchemy import select as sa_select

        from server.db.models import AppSettings
        try:
            async with db_session() as db:
                val = (await db.execute(
                    sa_select(AppSettings.max_archived_sessions)
                    .where(AppSettings.id == "singleton")
                )).scalar_one_or_none()
        except Exception:
            log.debug("archive_limit: settings read failed", exc_info=True)
            return None
        if val is None or val < 1:
            return None
        return int(val)

    async def _read_auto_archive_enabled(self) -> bool:
        """Read the app-wide auto-archive kill switch from the app-settings singleton.

        Returns True (auto-archive on) when the column is NULL, no settings row exists,
        or the read fails — auto-archive stays on unless a user explicitly turned it off.
        Read from the shared DB on demand (not cached) so a toggle made on the edge is
        honored by the daemon in a split deployment, matching ``_read_max_archived_sessions``."""
        from sqlalchemy import select as sa_select

        from server.db.models import AppSettings
        try:
            async with db_session() as db:
                val = (await db.execute(
                    sa_select(AppSettings.auto_archive_enabled)
                    .where(AppSettings.id == "singleton")
                )).scalar_one_or_none()
        except Exception:
            log.debug("autoarchive_toggle: settings read failed", exc_info=True)
            return True
        return val is not False  # NULL/True → enabled; only an explicit False disables

    async def purge_oldest_archived(self, count: int) -> list[str]:
        """Permanently delete the ``count`` oldest archived sessions (oldest first by
        ``archived_at``). Returns the IDs actually deleted.

        Selection is null-safe — legacy rows with no ``archived_at`` fall back to
        ``created_at`` so they still sort as oldest. Best-effort: a failure on one
        session is logged and skipped, never aborting the rest. ``count`` <= 0 no-ops.
        """
        if count <= 0:
            return []
        from sqlalchemy import func as sa_func
        from sqlalchemy import select as sa_select

        async with db_session() as db:
            order_key = sa_func.coalesce(DbSession.archived_at, DbSession.created_at)
            stmt = (
                sa_select(DbSession.id)
                .where(DbSession.status == "archived")
                .order_by(order_key.asc())
                .limit(count)
            )
            # Skip sessions that have already exhausted their delete strikes, so one
            # un-deletable oldest archive (stale sshfs handle, containment rejection)
            # can't monopolise every selection and starve disk reclamation (#825).
            if self._archive_purge_poisoned:
                stmt = stmt.where(DbSession.id.notin_(self._archive_purge_poisoned))
            ids = list((await db.execute(stmt)).scalars().all())

        deleted: list[str] = []
        for sid in ids:
            try:
                ok = await self.permanent_delete(sid)
            except Exception:
                log.exception("archive_limit: permanent_delete failed id=%s", sid)
                self._note_purge_failure(sid)
                continue
            if ok:
                self._archive_purge_strikes.pop(sid, None)  # clean delete — reset strikes
                deleted.append(sid)
            else:
                # permanent_delete returned False (e.g. containment guard kept the row):
                # a soft failure that still re-selects the same session next sweep.
                self._note_purge_failure(sid)
        if deleted:
            log.info("archive_limit: purged %d oldest archived session(s)", len(deleted))
        return deleted

    def _note_purge_failure(self, sid: str) -> None:
        """Record a failed permanent_delete for *sid*; poison it (skip in future
        selections) once it crosses _ARCHIVE_PURGE_MAX_STRIKES, logging one visible
        WARNING at the transition so the stuck session is diagnosable (#825)."""
        strikes = self._archive_purge_strikes.get(sid, 0) + 1
        self._archive_purge_strikes[sid] = strikes
        if strikes >= _ARCHIVE_PURGE_MAX_STRIKES and sid not in self._archive_purge_poisoned:
            self._archive_purge_poisoned.add(sid)
            log.warning(
                "archive_limit: session id=%s failed permanent_delete %d times — "
                "marking poisoned and skipping it in future cap-purges so disk can still "
                "be reclaimed from other archived sessions; investigate its on-disk dir",
                sid, strikes,
            )

    async def _enforce_archive_limit(self) -> int:
        """Purge oldest archived sessions until the count is within
        ``max_archived_sessions``. Returns the number deleted (0 when the cap is unset
        or already satisfied)."""
        limit = await self._read_max_archived_sessions()
        if limit is None:
            return 0
        from sqlalchemy import func as sa_func
        from sqlalchemy import select as sa_select

        async with db_session() as db:
            total = (await db.execute(
                sa_select(sa_func.count())
                .select_from(DbSession)
                .where(DbSession.status == "archived")
            )).scalar_one()
        excess = int(total) - limit
        if excess <= 0:
            return 0
        return len(await self.purge_oldest_archived(excess))

    async def _archive_limit_loop(self) -> None:
        """Periodic sweep enforcing the archived-session cap (see start()). Sleeps first
        so it never races rehydrate() at boot; the on-archive path covers fresh archives."""
        while True:
            await asyncio.sleep(_ARCHIVE_LIMIT_SWEEP_INTERVAL)
            if self._shutting_down:
                continue
            try:
                await self._enforce_archive_limit()
            except Exception:
                log.exception("archive_limit: periodic sweep failed")

    async def _worktree_janitor_loop(self, interval: float) -> None:
        """Fires immediately on startup then every ``interval`` seconds (default 24 h).

        No leading sleep — the first pass reclaims orphans from the previous crash/
        incident before any new sessions start. Uses ``asyncio.to_thread`` so blocking
        git subprocesses don't stall the event loop.
        """
        while True:
            if not self._shutting_down:
                try:
                    await asyncio.to_thread(self._janitor_sweep_once)
                except Exception:
                    log.exception("worktree-janitor: sweep failed")
            await asyncio.sleep(interval)

    def _janitor_sweep_once(self) -> None:
        """Synchronous sweep — called inside ``asyncio.to_thread``."""
        from ccswitch.worktrees_janitor import sweep_orphaned_worktrees

        live_cwds: frozenset[str] = frozenset(
            os.path.abspath(rec.cwd)
            for rec in self._records.values()
            if rec.cwd
        ) | frozenset(
            os.path.abspath(wt.path)
            for wt in self._worktrees.values()
            if wt.path
        )
        counts = sweep_orphaned_worktrees(self._repo_root, live_cwds)
        log.info(
            "worktree-janitor: scanned=%d removed=%d kept_live=%d "
            "kept_young=%d kept_dirty=%d kept_unpushed=%d kept_error=%d evicted_dirty=%d",
            counts["scanned"], counts["removed"], counts["kept_live"],
            counts["kept_young"], counts["kept_dirty"],
            counts["kept_unpushed"], counts["kept_error"],
            counts.get("evicted_dirty", 0),
        )

    async def list_archived(self) -> list[dict]:
        """Return metadata dicts for all archived sessions (reads DB, not _records)."""
        from sqlalchemy import select as sa_select
        async with db_session() as db:
            rows = (await db.execute(
                sa_select(DbSession).where(DbSession.status == "archived")
            )).scalars().all()
        return [
            {
                "id": row.id,
                "name": row.name,
                "account": row.account_name,
                "cwd": row.cwd,
                "repo": row.repo,
                "claudeSessionId": row.claude_session_id,
                "permissionMode": row.permission_mode,
                "createdAt": iso_utc(row.created_at),
                "archivedAt": iso_utc(row.updated_at),
            }
            for row in rows
        ]

    @staticmethod
    def _compose_unarchive_seed(
        prev_archived_by: str | None,
        prev_context: dict | None,
        archived_at: "datetime | None",
        goal: str | None,
    ) -> str | None:
        """Compose the prompt that greets a restored session, or None to seed nothing.

        Only AUTOMATED archives (autopilot / backlog-runner) get a seed: a manual
        unarchive of one is direct evidence the auto-archive judgement was premature,
        and those are the only archives carrying an ``archive_context`` snapshot to
        draw the reason/goal from. A user who unarchives their OWN archive needs no
        "this was erroneous" narrative, so return None (also keeps the resume path a
        no-op for the common manual case)."""
        if prev_archived_by not in AUTO_ARCHIVE_PROVENANCE:
            return None
        ctx = prev_context or {}
        who = {"autopilot": "autopilot",
               "backlog-runner": "the backlog runner"}.get(prev_archived_by, prev_archived_by)
        when = f" on {archived_at.date().isoformat()}" if archived_at is not None else ""
        reason = (ctx.get("reason") or "").strip()
        premise = (f', on the reasoning: "{reason}".' if reason
                   else ", on the assumption its work was already complete.")
        lines = [
            f"This session was automatically archived{when} by {who}{premise}",
            "It has now been manually restored, which means that judgement was very "
            "likely premature or wrong.",
            "Re-examine the ACTUAL current state — working tree, tests, any open PR, and "
            "the transcript above — before doing anything, then continue the work.",
        ]
        if goal:
            lines.append(f"The goal this session was pursuing: {goal}")
        lines.append(
            "If, after checking, you find the work genuinely IS complete, say so "
            "explicitly and stop rather than redoing it.")
        return "\n".join(lines)

    async def unarchive(self, session_id: str) -> "SessionRecord | None":
        """Restore an archived session to idle.

        New-style (status-flip) archives leave the worktree at ``cwd``, so
        unarchiving is a pure status flip — instant, no disk I/O, cannot run out
        of space. Legacy rows (non-NULL ``archive_root``, or pre-archive_root
        rows whose cwd is gone) still have their files moved back from the
        archive home.

        Returns the restored SessionRecord, or None if not found / not archived.
        Adds the record to _records so it reappears in list_sessions() without restart.
        """
        from sqlalchemy import select as sa_select

        async with db_session() as db:
            row = (await db.execute(
                sa_select(DbSession)
                .where(DbSession.id == session_id)
                .where(DbSession.status == "archived")
            )).scalars().first()
        if row is None:
            return None

        stored_root = getattr(row, "archive_root", None)
        original_cwd = Path(row.cwd)

        if stored_root is None and original_cwd.exists():
            # New-style archive: the worktree never moved. NULL archive_root with
            # a present cwd is the marker (a pre-archive_root legacy row also has
            # NULL, but its cwd was moved away, so it falls through to restore).
            log.info("session.unarchive id=%s worktree already in place at %s",
                     session_id, original_cwd)
        else:
            await self._restore_legacy_archive(session_id, stored_root, original_cwd)

        # Capture the archive provenance BEFORE we clear it, so we can tell whether this
        # unarchive is REVERSING an automated archive (→ a learning heuristic).
        prev_archived_by = getattr(row, "archived_by", None)
        prev_context = getattr(row, "archive_context", None)

        # Seed the restored session with the "why you're back" context (AUTO archives
        # only — see _compose_unarchive_seed). Prefer the goal snapshotted at archive
        # time (what it was archived FOR), then the live goal fields.
        seed_goal = ((prev_context or {}).get("goal")
                     or getattr(row, "active_goal", None)
                     or getattr(row, "inferred_goal", None))
        seed = self._compose_unarchive_seed(
            prev_archived_by, prev_context, getattr(row, "archived_at", None), seed_goal)

        async with db_session() as db:
            from sqlalchemy import update as sa_update
            values: dict = dict(
                # Restore to idle AND pin the sticky opt-out: a manually-unarchived
                # session is never auto-archived again. Clear the now-consumed provenance
                # and the archive root — after a legacy restore the files are back at
                # cwd, so a stale root would misdescribe where they live.
                status="idle", auto_archive_opt_out_reason="manual-unarchive",
                archived_by=None, archive_context=None, archive_root=None)
            # Persist the seed so it survives a daemon restart between now and the next
            # attach (the in-memory _pending_initial_prompt below covers the no-restart
            # common case; the column is the durable copy + feeds completion judgement).
            if seed is not None:
                values["initial_prompt"] = seed
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id).values(**values)
            )

        # Heuristic capture: an unarchive that reverses an AUTO archive is a signal the
        # auto-archive judgement was wrong. Record it (with the captured decision features)
        # so the decider can grow more cautious — see _autopilot_reversal_hint.
        if prev_archived_by in AUTO_ARCHIVE_PROVENANCE:
            await self._record_archive_reversal(session_id, prev_archived_by, prev_context)

        record = self._record_from_row(
            row, status="idle", auto_archive_opt_out_reason="manual-unarchive")
        self._records[session_id] = record
        # Stage the seed for lazy delivery on the NEXT WS attach (same path as a new
        # session's deferred initial prompt — ws.py pops + sends it once the client is
        # watching, so its turn streams live rather than firing eagerly into a driver
        # nobody is looking at). setdefault: never clobber a genuinely queued prompt.
        if seed is not None:
            self._pending_initial_prompt.setdefault(session_id, seed)
        log.info("session.unarchive id=%s restored to %s", session_id, original_cwd)
        return record

    async def _record_archive_reversal(self, session_id: str, archived_by: str,
                                       context: dict | None) -> None:
        """Append an `archive_reversed` event to the autopilot feed when a human undoes an
        automated archive. This is the durable, append-only heuristic store the auto-archive
        algorithm learns from (queried repo-scoped by _autopilot_reversal_hint). Best-effort:
        a heuristic write must never fail the unarchive."""
        ctx = (context or {})
        detail = {
            "archived_by": archived_by,
            "repo": ctx.get("repo"),
            "goal": ctx.get("goal"),
            "goal_source": ctx.get("goal_source"),
            "pr_state": ctx.get("pr_state"),
            "reason": ctx.get("reason"),
        }
        repo = ctx.get("repo") or "?"
        await self._autopilot_ctx.emit(
            "info", f"auto-archive reversed ({archived_by}) — {repo}",
            kind="archive_reversed", session_id=session_id, target=ctx.get("repo"),
            detail=detail)

    async def _restore_legacy_archive(self, session_id: str, stored_root: str | None,
                                      original_cwd: Path) -> None:
        # @deprecated — legacy pre-status-flip archives (files moved into an archive
        # home); remove when no archived rows with a moved-away worktree remain.
        """Move a legacy archived cwd back into place (raises on failure).

        Resolves through the root recorded at archive time. The current archive home
        is only a fallback for rows predating archive_root — using it unconditionally
        is what silently restored empty trees after the archive root moved disks.
        """
        archive_base = Path(stored_root) if stored_root else (await self._effective_archive_home())
        archive_path = archive_base / session_id

        def _restore() -> str:
            if archive_path.exists():
                original_cwd.parent.mkdir(parents=True, exist_ok=True)
                return restore_from_archive(archive_path, original_cwd)
            else:
                # Do NOT fabricate an empty cwd here: that reports success while handing
                # the user an empty worktree, hiding a real misconfiguration.
                raise ArchiveMissingError(
                    f"Archived session {session_id} is not present at {archive_path}. "
                    f"Its recorded archive root is "
                    f"{stored_root or '(none — legacy row, used current archive home)'}; "
                    f"if the archive directory moved, restore it or correct the session's "
                    f"archive_root before unarchiving."
                )

        try:
            move_result = await asyncio.to_thread(_restore)
        except (ArchiveMissingError, InsufficientStorageError):
            raise
        except Exception as exc:
            log.exception("session.unarchive_move_failed id=%s archive=%s dest=%s",
                          session_id, archive_path, original_cwd)
            raise OSError(
                f"Failed to restore archived session {session_id} "
                f"from {archive_path} to {original_cwd}: {exc}"
            ) from exc
        if move_result == "skipped:nospace":
            raise InsufficientStorageError(
                f"Not enough disk space to restore session — the target filesystem "
                f"({mountpoint(original_cwd)}) is low on inodes or bytes"
            )

    async def permanent_delete(self, session_id: str) -> bool:
        """Permanently delete a session: remove its on-disk directory + DB row.

        This is where archived-session disk is actually reclaimed (archiving
        itself is a pure status flip). New-style rows (archive_root NULL) have
        their worktree deleted at ``cwd`` — guarded to ccswitch-managed paths —
        and its git worktree registration cleared. Legacy rows (non-NULL
        archive_root) have their moved-away archive folder deleted instead.

        Returns True if deleted, False if the session was not found in DB.

        A *live* session (still in _records/_drivers, or status != 'archived') is
        archived first: archive_session stops the driver, so deleting it can't
        leave a zombie driver running.
        """
        from sqlalchemy import select as sa_select

        async with db_session() as db:
            row = (await db.execute(
                sa_select(DbSession).where(DbSession.id == session_id)
            )).scalars().first()
        if row is None:
            return False

        # Archive-then-delete: a live session must be torn down before its row and
        # working dir are removed, or we leak a driver.
        if (
            session_id in self._records
            or session_id in self._drivers
            or row.status != "archived"
        ):
            await self.archive_session(session_id)

        # Re-read archive_root rather than reusing `row`: the archive-then-delete
        # above stamps it (to NULL), so the row loaded earlier predates it.
        async with db_session() as db:
            stored_root = (await db.execute(
                sa_select(DbSession.archive_root).where(DbSession.id == session_id)
            )).scalar_one_or_none()

        cwd_path = Path(row.cwd) if row.cwd else None
        if stored_root is None and cwd_path is not None and cwd_path.exists():
            # New-style archive: the worktree is still at cwd (same NULL-root +
            # present-cwd marker as unarchive).
            if not await self._delete_worktree_at_cwd(session_id, cwd_path):
                return False
        else:
            # Legacy archive (moved into an archive home) — including ancient
            # pre-archive_root rows, whose NULL root resolves to the current home.
            if not await self._delete_legacy_archive_dir(session_id, stored_root):
                return False

        async with db_session() as db:
            from sqlalchemy import delete as sa_delete
            # Remove the session's attachment rows + (dedup-aware) on-disk files
            # before the session row itself.
            n_att = await _attachments.delete_for_session(db, session_id)
            await db.execute(
                sa_delete(DbSession).where(DbSession.id == session_id)
            )
        if n_att:
            log.info("session.permanent_delete id=%s removed %d attachment(s)",
                     session_id, n_att)
        log.info("session.permanent_delete id=%s db row removed", session_id)
        return True

    async def _delete_worktree_at_cwd(self, session_id: str, cwd_path: Path) -> bool:
        """Delete a new-style archived session's worktree (still at its cwd).

        Returns True when it is safe for the caller to drop the DB row, False when the
        directory could not be removed and the row must be KEPT so a later sweep retries
        (e.g. a stale sshfs handle that recovers on remount — #825).

        Containment guard: sessions only ever run in ccswitch-created worktrees or
        scratch dirs (both under a ``.ccswitch`` segment — see the create-session
        isolation guarantee), so anything else is a misconfigured row and is left
        in place rather than risk deleting a user's real directory. Guarded by
        path segment, not the current worktrees base, so a repo_root move can't
        strand directories the way the 2026-08-21 archive-root incident did.
        """
        if ".ccswitch" not in cwd_path.parts:
            log.warning(
                "session.permanent_delete id=%s cwd=%s is not a ccswitch-managed "
                "directory — leaving it in place, deleting the row only",
                session_id, cwd_path,
            )
            return True  # nothing we should delete on disk; the row can still go
        # Resolve the shared clone BEFORE the delete, while the worktree is still a
        # valid git dir, so we can scope-deregister exactly this path afterward
        # (never a global prune — issue #177). Off the event loop: git subprocess
        # startup can spike under load.
        repo_top = await asyncio.to_thread(_resolve_git_common_dir, str(cwd_path))
        err = await asyncio.to_thread(_rmtree_best_effort, str(cwd_path))
        if err:
            # Keep the row: deleting it now would strand the worktree on disk (the
            # 2026-08-21 leak shape). The purge's strike counter retries and eventually
            # skips it, so one wedged mount can't stall reclamation of other sessions.
            log.warning("session.permanent_delete id=%s worktree rmtree incomplete at %s: %s "
                        "— keeping the row to retry on a later sweep",
                        session_id, cwd_path, err)
            return False
        if repo_top:
            try:
                await asyncio.to_thread(
                    worktrees.clear_stale_registration, repo_top, str(cwd_path)
                )
            except Exception:
                log.debug("session.permanent_delete_worktree_deregister_failed id=%s",
                          session_id, exc_info=True)
        log.info("session.permanent_delete id=%s worktree removed at %s",
                 session_id, cwd_path)
        return True

    async def _delete_legacy_archive_dir(self, session_id: str,
                                         stored_root: str | None) -> bool:
        # @deprecated — legacy pre-status-flip archives (files moved into an archive
        # home); remove together with _restore_legacy_archive.
        """Delete a legacy archived session's folder under its recorded archive root.

        Containment guard: only removes paths that are direct children of the
        resolved archive root. Returns False (caller must keep the DB row) on a
        guard violation, True otherwise.
        """
        base = Path(stored_root) if stored_root else await self._effective_archive_home()
        archive_base = base.resolve()
        candidate = (archive_base / session_id).resolve()
        try:
            candidate.relative_to(archive_base)
        except ValueError:
            log.error(
                "session.permanent_delete CONTAINMENT GUARD id=%s candidate=%s",
                session_id, candidate,
            )
            return False

        if candidate.exists():
            err = await asyncio.to_thread(_rmtree_best_effort, str(candidate))
            if err:
                # Keep the row so a later sweep retries once the mount recovers, rather
                # than deleting the row and stranding the archive dir on disk (#825).
                log.warning("session.permanent_delete id=%s archive rmtree incomplete at %s: %s "
                            "— keeping the row to retry on a later sweep",
                            session_id, candidate, err)
                return False
            log.info("session.permanent_delete id=%s archive_path=%s", session_id, candidate)
        else:
            # Never let this pass silently. This used to resolve against the CURRENT
            # archive home, so once that setting moved disks the rmtree targeted a path
            # under the new root that had never existed — and `ignore_errors=True` made
            # it "succeed". The row was deleted and the real directory stranded on the
            # old root forever: one leaked working tree per purged session (150 of them,
            # 28 GB, found 2026-08-21). Proceed with the row deletion, but say so.
            log.warning(
                "session.permanent_delete id=%s archive dir ABSENT at %s (archive_root=%s)"
                " — deleting the row anyway; check for a stranded directory under a"
                " previous archive root", session_id, candidate, stored_root or "<unset>",
            )
        return True

    async def resolve_transcript_path(self, session_id: str) -> str | None:
        """Resolve the on-disk JSONL path for a session's transcript.

        Works for both active and archived sessions. Returns None when not found.
        Active sessions are resolved via transcript_path(); archived sessions
        (not in _records) fall back to a DB lookup + glob.
        """
        from sqlalchemy import select as sa_select

        # transcript_path()/_find_transcript() stat + glob the account's projects tree,
        # which on the production host is an sshfs mount — a stalled mount turns this
        # into a multi-second (observed: 20s) block of the whole daemon loop (#826).
        # asyncio.wait_for in transcript_window bounds only the threaded replay AFTER
        # this, and can't interrupt synchronous work already running on the loop, so the
        # resolution itself must run off the loop. transcript_path only reads _drivers/
        # _records via .get() (no mutation, GIL-safe), so it is thread-safe to offload.
        path = await asyncio.to_thread(self.transcript_path, session_id)
        if path is None:
            # For archived sessions the record is not in _records; load from DB.
            async with db_session() as db:
                row = (await db.execute(
                    sa_select(DbSession).where(DbSession.id == session_id)
                )).scalars().first()
            if row and row.claude_session_id:
                path = await asyncio.to_thread(
                    self._find_transcript,
                    str(account_cfg_dir(row.account_name)), row.claude_session_id,
                    getattr(row, "cwd", None),
                )
        return path

    async def transcript_events(self, session_id: str, *, max_turns: int | None = 20) -> list:
        """Return replayed transcript events for a session (read-only review).

        Works for both active and archived sessions: transcripts live in the
        account's config dir (not the session cwd), so always accessible.
        Returns [] if no transcript is found or parsing fails.
        max_turns=None returns the FULL transcript — pass it whenever the result
        feeds a user-facing surface, or the default tail silently truncates it
        (the sub-agent summary bug: agents spawned before the last 20 turns
        vanished from the dashboard chips).
        """
        from server.sessions.replay import replay_events

        path = await self.resolve_transcript_path(session_id)
        if not path:
            return []
        try:
            return await replay_events(path, max_turns=max_turns)
        except Exception:
            log.warning("session.transcript_events id=%s replay failed", session_id, exc_info=True)
            return []

    async def transcript_window(self, session_id: str, *, before: int | None = None,
                                limit: int | None = None, timeout_s: float | None = None):
        """Windowed transcript replay. Returns a ReplayWindow, or None if no transcript.

        Use before/limit for lazy scrollback paging: returns up to `limit` turns
        ending before turn-ordinal `before` (or the last `limit` turns if before
        is None), plus earliest_turn/total_turns for pagination metadata.
        limit=None (the default) returns the FULL transcript — no silent cap.

        ``timeout_s`` overrides the interactive replay ceiling for callers that are
        legitimately slower and are not on the session-open critical path (the export
        download passes TRANSCRIPT_EXPORT_CEILING_S). Returns None on timeout — a caller
        that has already established the transcript EXISTS must treat that None as an
        error (504), not as an empty transcript.
        """
        from server.sessions.replay import replay_window

        ceiling = _TRANSCRIPT_WINDOW_CEILING_S if timeout_s is None else timeout_s
        path = await self.resolve_transcript_path(session_id)
        if not path:
            return None
        try:
            # Limitation: wait_for cancels the replay_window() coroutine, but if the
            # sub-agent scan inside it is blocked on a real asyncio.to_thread() worker
            # (e.g. a syscall wedged on a dead sshfs mount), CPython cannot stop that
            # underlying thread. The thread and its open fd leak, consuming a slot in
            # the daemon's shared default executor — the same pool every other
            # to_thread() call in the process draws from. Repeated timeouts against a
            # genuinely wedged mount can exhaust that pool and reintroduce hangs
            # elsewhere, not just here. There is no clean fix (a dedicated executor is
            # out of scope) — this comment exists so the next person debugging executor
            # starvation finds the explanation instead of rediscovering it.
            return await asyncio.wait_for(
                replay_window(path, before=before, limit=limit),
                timeout=ceiling,
            )
        except asyncio.TimeoutError:
            log.warning("session.transcript_window id=%s exceeded %.0fs — returning empty",
                        session_id, ceiling)
            return None
        except Exception:
            log.warning("session.transcript_window id=%s replay failed", session_id, exc_info=True)
            return None

    async def transcript_search_text(self, session_id: str) -> str:
        """Return the searchable transcript text for a session (read-only review).

        Resolves the path exactly like transcript_window/transcript_events (works for
        both active and archived sessions via resolve_transcript_path's DB-glob
        fallback) and extracts search-relevant text in-process. Returns "" when no
        transcript is found — never raises for a missing/unreadable file (mirrors
        search.transcript_text's own OSError handling).
        """
        from server.sessions.search import transcript_text

        path = await self.resolve_transcript_path(session_id)
        if not path:
            return ""
        return await asyncio.to_thread(transcript_text, path)

    def snapshot_deploy(self, *, path: str | None = None) -> dict:
        """SAVE step: record sessions with a live driver so a daemon-affecting restart
        can eagerly re-launch them (rehydrate alone is lazy + flattens 'working'→'idle').

        A mid-turn session also records its interrupted prompt: ``_last_prompt`` is
        in-memory and dies with this process, so the snapshot is the only way the
        post-restart daemon can re-issue the killed turn (issue #864)."""
        live = []
        for sid in list(self._drivers.keys()):
            status = getattr(self._records.get(sid), "status", "idle")
            entry = {"id": sid, "status": status}
            last = self._last_prompt.get(sid)
            if last and status in _REPLAY_STATUSES:
                entry["lastPrompt"] = last
            live.append(entry)
        _deploy_snapshot.write_snapshot(live, path=path)
        return {"live": live}

    async def restore_deploy(self, *, path: str | None = None) -> dict:
        """RESUME step: eagerly re-launch each snapshotted session in the fresh daemon.

        ensure_driver only resumes the transcript — it does NOT continue a turn the
        restart killed — so a session snapshotted mid-turn gets its recorded prompt
        re-issued on the resumed driver (``replayed``); without that the driver sits
        idle, is released again, and the session dangles at in_progress with no
        driver behind it. A session that fails this first attempt is enrolled in
        ``_resume_recovery`` so the daemon keeps retrying it (see
        _recover_resume_failures_once) instead of leaving it stuck behind a sticky
        'open to retry' banner. The outcome is logged here as well as returned:
        the controller's status object — previously the only surface for it — is
        wiped by the controller's own scheduled self-restart (issue #864)."""
        restored: list[str] = []
        failed: list[str] = []
        replayed: list[str] = []
        for entry in _deploy_snapshot.read_snapshot(path=path):
            sid = entry["id"]
            try:
                driver = await self.ensure_driver(sid, SYSTEM_IDENTITY)
            except Exception:
                driver = None
            if driver is None:
                failed.append(sid)
                self._resume_recovery[sid] = 0   # enroll for daemon-side retry
                continue
            restored.append(sid)
            last = entry.get("lastPrompt")
            if last and entry.get("status") in _REPLAY_STATUSES:
                # Seed this process's prompt memory BEFORE the send so the 429/401
                # replay machinery can re-drive the turn again later if needed.
                self._last_prompt[sid] = last
                try:
                    await driver.send_prompt(last)
                    replayed.append(sid)
                except Exception:
                    # Best-effort: the session is still restored (live driver);
                    # the user can re-send. Never fail the whole restore for it.
                    log.warning("deploy.restore: replay of interrupted turn failed "
                                "session=%s", sid, exc_info=True)
        _deploy_snapshot.clear_snapshot(path=path)
        log_event(log, logging.INFO, "deploy.restore",
                  restored=restored, failed=failed, replayed=replayed)
        return {"restored": restored, "failed": failed, "replayed": replayed}

    def _prune_recovered(self) -> None:
        """Drop any enrolled id that now has a live driver — covers a session a human
        revived via the manual 'open to retry' path before the recovery pass ran."""
        for sid in [s for s in self._resume_recovery if s in self._drivers]:
            self._resume_recovery.pop(sid, None)

    async def _recover_resume_failures_once(self) -> None:
        """One daemon-side pass over sessions that failed to resume after a deploy.

        Retries ensure_driver for each still-enrolled session; a success drops it
        (the banner clears), a failure charges an attempt. Runs unconditionally on
        the autopilot tick cadence — NOT gated on autopilot's governance mode — so
        every dropped session is recovered, not just governed ones. Capped at
        RESUME_RECOVERY_MAX_ATTEMPTS: a permanently-broken session stays enrolled
        (flagged gave-up by resume_recovery_status) but stops burning retries."""
        self._prune_recovered()
        for sid, attempts in list(self._resume_recovery.items()):
            if attempts >= RESUME_RECOVERY_MAX_ATTEMPTS:
                continue
            try:
                driver = await self.ensure_driver(sid, SYSTEM_IDENTITY)
            except Exception:
                driver = None
            if driver is not None:
                self._resume_recovery.pop(sid, None)
                log_event(log, logging.INFO, "deploy.resume.recovered", session_id=sid)
            else:
                self._resume_recovery[sid] = attempts + 1

    async def _existing_session_ids(self, ids: list[str]) -> set[str]:
        """Which of *ids* have an ACTIVE (non-archived, non-ended) row in sessions.

        Archived/ended rows are treated as absent for mirror-eviction purposes: once
        the owning daemon archives a session, the primary should drop its mirror copy
        so the session no longer appears in list_sessions (#898). Permanently deleted
        rows (no row at all) are also absent, as before.

        Split out as a seam so _reconcile_mirror_once's eviction logic is unit-
        testable without a live DB."""
        if not ids:
            return set()
        from sqlalchemy import select as sa_select
        async with db_session() as db:
            return set((await db.execute(
                sa_select(DbSession.id)
                .where(DbSession.id.in_(ids))
                .where(DbSession.status.notin_(INACTIVE_SESSION_STATUSES))
            )).scalars().all())

    async def _reconcile_mirror_once(self) -> None:
        """Evict _records entries for sessions deleted or archived on another fleet host.

        The primary holds a record for EVERY fleet session (its _records feeds the
        dashboard and its fleet-global automation). When a session is permanently
        deleted, that runs on the OWNING host — which pops its own _records and the
        shared DB row — but never touches the primary's mirror copy. Similarly, when a
        session is archived on the owning host (status='archived'), the primary's
        in-memory record stays stale, keeping the session visible in list_sessions (#898).

        _existing_session_ids now returns only ACTIVE (non-archived, non-ended) IDs, so
        both deleted and archived foreign sessions fall through to the two-strike eviction.

        Only FOREIGN entries (not owned by this daemon per _owns_record) are
        candidates: this daemon's own delete path pops _records synchronously, so an
        owned entry missing a row is a mid-creation race, never a real delete. A live
        driver here also means it's ours (ensure_driver adopted it) — _owns_record
        already treats that as owned. Two-strike (see _MIRROR_EVICT_STRIKES) so a
        session a worker is mid-creating (row not yet committed) is never dropped."""
        enabled_hosts = await self._enabled_host_names()
        foreign = [sid for sid, rec in list(self._records.items())
                   if not self._owns_record(rec, enabled_hosts)]
        if not foreign:
            self._mirror_missing_strikes.clear()
            return
        present = await self._existing_session_ids(foreign)
        for sid in foreign:
            if sid in present:
                self._mirror_missing_strikes.pop(sid, None)
                continue
            strikes = self._mirror_missing_strikes.get(sid, 0) + 1
            if strikes >= _MIRROR_EVICT_STRIKES:
                self._records.pop(sid, None)
                self._mirror_missing_strikes.pop(sid, None)
                log_event(log, logging.INFO, "mirror.evict_deleted", session_id=sid)
            else:
                self._mirror_missing_strikes[sid] = strikes
        # Drop strike bookkeeping for ids that are no longer tracked foreign records
        # (evicted above, or adopted as a live driver since the last pass).
        for sid in [s for s in self._mirror_missing_strikes if s not in self._records]:
            self._mirror_missing_strikes.pop(sid, None)

    def resume_recovery_status(self) -> dict:
        """Live view of unresolved deploy-resume failures for the banner.

        ``failed`` = still being retried; ``gaveUp`` = capped out (kept visible so a
        human sees a permanently-broken session). Prunes now-live ids first so the
        banner clears the moment a session comes back — by auto-retry or manual open."""
        self._prune_recovered()
        m = RESUME_RECOVERY_MAX_ATTEMPTS
        return {"failed": [s for s, a in self._resume_recovery.items() if a < m],
                "gaveUp": [s for s, a in self._resume_recovery.items() if a >= m]}

    def startup_report(self) -> dict:
        """This process's boot outcomes (rehydrate/rearm counts + any step error).

        Surfaced by /api/daemon/health so a boot where rehydrate() failed — which
        used to be a single swallowed log line while the dashboard showed zero
        sessions — is observable. Counts are None until the step has run."""
        return dict(getattr(self, "_startup_report", None) or {})

    def note_startup_failure(self, step: str, exc: BaseException) -> None:
        """Record a boot-step failure (called by the lifespans that catch them)."""
        self._startup_note(f"{step}Error", f"{type(exc).__name__}: {exc}"[:500])

    def _startup_note(self, key: str, value) -> None:
        """Write one startup-report entry. getattr-guarded because rehydrate-shaped
        tests build orchestrators via SessionOrchestrator.__new__ (no __init__, so
        no _startup_report) — recording is observability, never load-bearing."""
        rep = getattr(self, "_startup_report", None)
        if rep is not None:
            rep[key] = value


def _rmtree_best_effort(path: str) -> str | None:
    """Recursively delete *path*, guaranteeing the call never raises.

    ``shutil.rmtree(path, ignore_errors=True)`` is NOT sufficient on sshfs: CPython
    3.12's ``_rmtree_safe_fd`` calls ``os.fstat(dirfd)`` (and ``os.open``) OUTSIDE the
    region routed through the error handler, so an ``ESTALE`` (Errno 116) stale-handle
    error escapes anyway. On the production host the archive roots are sshfs mounts
    whose handles go stale on every reconnect, so this escaped and wedged the cap-purge
    (#825). Wrapping the whole call swallows that residual escape: a stale handle
    degrades to a logged skip instead of a raised exception.

    Returns None on success, or a short error string describing what stopped it (so the
    caller can log which session/path could not be reclaimed). Meant to run inside
    ``asyncio.to_thread`` — the underlying syscalls block.
    """
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError as exc:
        code = errno.errorcode.get(exc.errno, str(exc.errno)) if exc.errno is not None else "?"
        return f"{code}: {exc}"
    # ignore_errors=True routes per-file failures to a no-op, so a lingering path here
    # means something (the top-level fstat/open, or a genuinely undeletable entry) kept
    # it around without raising. os.path.exists itself swallows OSError -> False.
    if os.path.exists(path):
        return f"path still present after rmtree: {path}"
    return None


def _resolve_git_common_dir(path: str) -> str | None:
    """Return the absolute common git dir's parent for the worktree at *path*, or None.

    Combines the is-git-repo probe and the rev-parse into a single blocking call so
    callers can offload both to asyncio.to_thread in one step — git subprocess startup
    is non-trivial under load, and simultaneous archive of many sessions was blocking
    the event loop long enough to trip the health-check watchdog.
    """
    if not worktrees.is_git_repo(path):
        return None
    r = subprocess.run(
        ["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    return os.path.dirname(r.stdout.strip().rstrip("/"))
