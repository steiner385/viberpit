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
    from server.automations.completion_bar import PrFacts
    from server.github.merge_queue import QueueEntry
    from server.github.pr_status import PipelinePr
    from server.pipeline.merge_train import MergeTrainState
    from server.pipeline.track import StageModel

from ccswitch import accounts as ccswitch_accounts
from ccswitch import configdir, copilot as _copilot_mod, crypto, gitrepos, worktrees
from ccswitch.accounts import provider_of as _provider_of
from ccswitch.account_names import assert_valid_account_name, InvalidAccountName
from ccswitch.config import load_config
from ccswitch.configdir import compose
from ccswitch.paths import account_cfg_dir, shared_base
from server.audit import record_audit
from server.db import db_session
from server.db.models import INACTIVE_SESSION_STATUSES, NON_ACTIVE_SESSION_STATUSES, Session as DbSession
from server.sessions import adopt as _adopt
from server.sessions import deploy_snapshot as _deploy_snapshot
from server.sessions.archive_move import mountpoint, move_into_archive
from server.sessions import attachments as _attachments
from server.sessions import dispatch
from server.sessions import events as ev
from server.sessions import push as push_mod
from server.sessions.commands import catalog as cmd_catalog
from server.sessions.commands import dispatcher as cmd_dispatcher
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.sdk_factory import real_sdk_factory, real_suggestion_factory, real_summary_factory
from server.sessions.replay import replay_events
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
from server.time_iso import iso_utc
from server.usage.poller import UsagePoller
from server.github.pr_status import PullRequestInfo
from server.sessions.pr_fix import build_conflict_fix_prompt, build_pr_fix_prompt

log = logging.getLogger(__name__)


# Statuses that mean a session is mid-turn or awaiting user input — restarting now
# would lose in-flight work. All other statuses (idle / ready_review / done /
# limited / ended / archived) are restart-safe: resume + transcript replay recover them.
_ACTIVE_STATUSES = frozenset({"in_progress", "working", "waiting_input", "condensing"})

# Message surfaced to the user when auto-recovery gives up on a repeatedly-dropped turn.
_TURN_DROPPED_GIVEUP = ("Auto-recovery gave up after repeated empty turns — the context "
                        "may be full. Send a message to retry, or /compact to free space.")

# Bounded auto-recovery attempts for a session that failed to resume after a
# daemon-affecting deploy. Past this the session is kept visible (flagged gave-up)
# but no longer retried — retrying a worktree-deleted / repo-gone session is futile.
RESUME_RECOVERY_MAX_ATTEMPTS = 3

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


# TTL for cached issue proposals (seconds).
_ISSUE_TTL_S: float = 300.0


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


class BranchSwitchError(Exception):
    """Raised when a git branch checkout fails."""


class InsufficientStorageError(Exception):
    """Raised when unarchiving cannot proceed due to inode or byte exhaustion."""


# Session-window utilisation ceiling that triggers rotation (100 = hard ceiling per spec)
_ROTATION_CEILING: float = 100.0

# Consecutive poller failures before treating an account as persistently dead (e.g. Copilot
# proxy crashed at quota exhaustion).  1–2 failures are transient; ≥3 warrants rotation.
_UNREACHABLE_ROTATION_THRESHOLD: int = 3

# How often the stuck-session sweep retries rotation for needs_attention sessions.
# _pause_session() is a dead-end: the rotation monitor only fires on degradation
# events, not recovery, so paused sessions need a separate periodic retry.
_STUCK_SESSION_SWEEP_INTERVAL: float = 300.0

# Periodic re-check of the archived-session cap, so a limit LOWERED via settings takes
# effect even with no new archive event (the on-archive path covers the fresh-archive case).
_ARCHIVE_LIMIT_SWEEP_INTERVAL: float = 300.0


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
        # Sessions that failed to re-launch after a daemon-affecting deploy:
        # session_id -> attempts so far. Seeded by restore_deploy, drained by the
        # daemon's recovery pass (_recover_resume_failures_once). Deploy-scoped and
        # in-memory by design — a fresh deploy rebuilds it; a crash loses it exactly
        # as the deploy controller loses its own frozen 'failed' list.
        self._resume_recovery: dict[str, int] = {}
        # Detached-session next-prompt suggestions: session_id -> (transcript mtime, items).
        # Generated from the on-disk transcript without resuming the driver (see
        # latest_suggestions / _generate_detached_suggestions). Cached even when empty so
        # the same transcript state is not re-generated on every /sessions poll.
        self._detached_suggestions: dict[str, tuple[float, list[str]]] = {}
        self._suggest_inflight: set[str] = set()
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

        # Build a live-session-aware usage_fn: when an account has a live session
        # the SDK owns its token family, so the poller must not independently
        # refresh.  Pass allow_refresh=False for those accounts so the poller
        # calls sync_back first and returns None on an expired-but-owned token
        # (the SDK will refresh on its next request).
        if usage_fn is not None:
            _poller_usage_fn = usage_fn
        else:
            def _poller_usage_fn(name: str):
                return ccswitch_accounts.usage_for(
                    name,
                    allow_refresh=not self.has_live_session(name),
                    path="poller-idle",
                )

        self._poll_interval = poll_interval
        self._poller: UsagePoller = UsagePoller(
            poll_interval=poll_interval,
            usage_fn=_poller_usage_fn,
        )
        from server.github.pr_poller import PrStatusPoller
        self._pr_poller: PrStatusPoller = PrStatusPoller()
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
        # Sessions whose cwd has vanished — warned once so the titling sweep doesn't
        # spam "Working directory does not exist" every interval (see _generate_fallback_title).
        self._missing_cwd_warned: set[str] = set()
        # Sessions whose background tasks have been re-armed this process (dedup the
        # auto pass against retry-on-attach). Cleared by a fresh process start.
        self._rearmed: set[str] = set()
        # Session-ids currently in a push-worthy state (AUT-02 debounce): push fires once
        # on entry; cleared when the session transitions out so re-entry fires again.
        self._pushed: set[str] = set()
        # Backlog-runner session-ids we've scheduled a completion auto-archive for,
        # so repeated ``complete`` status emits schedule the archive exactly once.
        self._auto_archiving: set[str] = set()
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
        )
        self._backlog_task: asyncio.Task | None = None
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
        self._pr_branch_task: asyncio.Task | None = None
        # Event bus — WS bridge subscribes here for lifecycle events
        self._event_subscribers: list[asyncio.Queue] = []
        # Serialises the check-build-register critical section in ensure_driver
        # so that two concurrent WS attaches to the same detached session cannot
        # each see no driver, each build+start one, and both register → two SDK
        # processes attached to the same Claude conversation (TOCTOU fix).
        self._ensure_lock: asyncio.Lock = asyncio.Lock()
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
        tok = crypto.read_oauth_token(rec.account)
        return {"CLAUDE_CODE_OAUTH_TOKEN": tok} if tok else {}

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
        return task

    async def start(self) -> None:
        """Start the usage poller, the rotation monitor and the titling sweep."""
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
        """Stop the titling sweep, rotation monitor and usage poller."""
        # Mark teardown FIRST, before any driver is closed below: a turn dropped by
        # our own aclose() must not trip failure recovery → needs_attention. Leave the
        # row for rehydrate() to normalize to idle (resumable) on the next boot.
        self._shutting_down = True
        if self._pr_branch_task is not None:
            self._pr_branch_task.cancel()
            try:
                await self._pr_branch_task
            except asyncio.CancelledError:
                pass
            self._pr_branch_task = None
        for attr in ("_titling_task", "_goal_task", "_rotation_task", "_stall_task",
                     "_token_refresh_task", "_backlog_task", "_autopilot_task",
                     "_cicd_task", "_archaudit_task", "_sweep_task",
                     "_archive_limit_task", "_drain_timer"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, attr, None)
        # Cancel+await every tracked fire-and-forget task (#405) so none survives to
        # write into a later app/test's DB. Snapshot first: a task's done-callback
        # mutates ``_bg_tasks`` as it settles.
        pending = [t for t in self._bg_tasks if not t.done()]
        for t in pending:
            t.cancel()
        for t in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._bg_tasks.clear()
        # Close any still-live driver so its stream/pump (which invokes the persist
        # callbacks that write to the DB) cannot outlive teardown. In production the
        # app shutdown closes sessions before stop(); when it doesn't (e.g. the X11
        # manager is absent, as in tests), this is the backstop.
        for sid in list(self._drivers.keys()):
            driver = self._drivers.pop(sid, None)
            if driver is not None:
                with contextlib.suppress(Exception):
                    await driver.aclose()
        await self._poller.stop()
        await self._pr_poller.stop()
        await self._pr_population.stop()
        await self._deploy_watcher.stop()

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
        """
        # Resolve cwd: if given, it MUST be an existing directory
        if cwd is not None:
            if not os.path.isdir(cwd):
                raise ValueError(f"cwd does not exist: {cwd}")

        session_id = str(uuid.uuid4())

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
        return sum(1 for r in self._records.values()
                   if r.status not in NON_ACTIVE_SESSION_STATUSES)

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
        async with db_session() as db:
            db.add(DbSession(
                id=session_id, name=name, account_name=account or "",
                claude_session_id=None, status="queued", pid=None,
                cwd=cwd or "", repo=repo, branch=None,
                permission_mode=permission_mode, name_is_auto=name_is_auto,
                origin=origin, origin_ref=origin_ref, model=model,
                initial_prompt=initial_prompt))
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
            updated_at=datetime.utcnow(), origin=origin, origin_ref=origin_ref,
            model=model)
        self._records[session_id] = rec
        log.info("session.queued id=%s priority=%d origin=%s",
                 session_id, priority, origin)
        return rec

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
        _pre_record = SessionRecord(
            id=session_id, name=name, account=account, claude_session_id="",
            cols=cols, rows=rows, cwd=cwd, repo=repo, branch=branch_name,
            status="idle", name_is_auto=name_is_auto,
            updated_at=datetime.utcnow(), usage_unverified=usage_unverified,
            origin=origin, origin_ref=origin_ref, model=None,
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
        if rec is not None:
            rec.status = status
            rec.updated_at = datetime.utcnow()   # row-write token; bumps on EVERY persist (see rev_at)
            if turn_at is not None:
                rec.last_turn_at = turn_at
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
        await self._persist_sub_agents(session_id)
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

    def _maybe_autoarchive_completed(self, session_id: str, status: str, rec) -> None:
        """Schedule a completion auto-archive for a finished backlog-runner session.

        When the agent signals it's done, ``derive_status`` yields ``complete``
        (distinct from a plain ``done`` turn). A spawned session otherwise never
        reaches a terminal DB state, so its driver/worktree would linger and its
        slot stay occupied. Archiving frees all of that; the sweep then reclaims
        the slot. Scheduled (not awaited) because archive stops THIS driver while
        we're inside its ``on_status`` callback; idempotent via ``_auto_archiving``.
        Scoped to backlog-runner (pr-doctor fix sessions are short and re-spawn on
        demand, so they don't need this)."""
        if status != "complete":
            return
        if rec is None or getattr(rec, "origin", None) != "backlog-runner":
            return
        # A human unarchived this run → honor the sticky opt-out and never re-archive it.
        if getattr(rec, "auto_archive_opt_out_reason", None):
            return
        if session_id in self._auto_archiving:
            return
        self._auto_archiving.add(session_id)
        asyncio.create_task(self._auto_archive_completed(session_id),
                            name="backlog-autoarchive")

    async def _auto_archive_completed(self, session_id: str) -> None:
        """Archive a completed backlog-runner session + log it to the activity feed.
        A failure clears the guard so a later ``complete`` emit can retry.

        Snapshots decision-time evidence (goal, repo, PR state) so the retro archive
        audit can evaluate whether the archive was premature. Mirrors ``_autopilot_archive``."""
        rec = self._records.get(session_id)
        if rec is not None:
            from server.sessions.goals import effective_goal
            goal_text, goal_source = effective_goal(rec)
            pr = self._autopilot_pr_facts(rec)
            context = {
                "repo": rec.repo, "goal": goal_text, "goal_source": goal_source,
                "pr_state": pr.to_prompt() if pr is not None else None,
                "reason": "backlog-runner: run complete",
            }
        else:
            context = {
                "repo": None,
                "goal": None,
                "goal_source": "backlog-runner",
                "reason": "backlog-runner: run complete",
            }
        try:
            await self.archive_session(session_id, archived_by="backlog-runner", context=context)
        except Exception:
            log.warning("backlog_runner.autoarchive_failed id=%s", session_id, exc_info=True)
            self._auto_archiving.discard(session_id)
            return
        await self._backlog_ctx.emit(
            "info", f"archived completed run {session_id}",
            kind="archived", session_id=session_id)

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
        return await select_batch(config, gh_run=self._gh_run, repo_load=repo_load)

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
        """Active runs per repo, derived from durable target states — the input to
        the runner's per-repo fairness. Mirrors ``_backlog_count_in_flight``'s
        active-state basis (skips ``gave_up`` guard rows) but split by repo, so a
        freed slot goes to the repo currently carrying the fewest runs."""
        states = await self._backlog_ctx.list_target_states()
        load: dict[str, int] = {}
        for st in states.values():
            if st.get("gave_up") or st.get("handed_off"):
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

    async def _backlog_close_out(self, target: str, info) -> None:
        """MERGED path: close every issue in the batch with a comment linking
        the merged PR — the PR body may have lacked ``Fixes #N``, and an issue
        left open under ``in-progress``/``needs-human`` is excluded from the
        candidate pools forever. Best-effort, off the event loop."""
        from server.automations import backlog_github
        batch = await self._backlog_batch_for(target)
        await asyncio.to_thread(
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
            # A completed backlog-runner session is owned by the dedicated auto-archive
            # path (`_maybe_autoarchive_completed`); governing it here too would let two
            # paths race to archive the same session. Autopilot owns every OTHER
            # completed session (human-created, pr-doctor, etc.).
            if rec.status == "complete" and getattr(rec, "origin", None) == "backlog-runner":
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
        return DecisionContext(
            session_id=session_id, status=rec.status, account=rec.account, repo=rec.repo,
            branch=getattr(rec, "branch", None), effective_goal=goal_text,
            goal_source=goal_source, stuck=stuck,
            transcript_tail=await self._autopilot_transcript_tail(session_id), ledger={},
            pr_facts=self._autopilot_pr_facts(rec),
            no_auto_archive=bool(getattr(rec, "auto_archive_opt_out_reason", None)),
            reversal_hint=await self._autopilot_reversal_hint(rec.repo),
            seconds_since_human=_secs_since_human,
            fleet_context=await self._autopilot_fleet_context(session_id))

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
        """Verified PR ground-truth for a governed session, from the poller cache
        only (no new I/O). Mirrors request_pr_fix: check every authored branch plus
        the scratch branch, dedupe by PR number."""
        from server.automations.completion_bar import PrFact, PrFacts
        seen: dict[int, PrFact] = {}
        for br in self._session_pr_branches(rec):
            info = self.pr_for(rec.repo, br) if rec.repo else None
            if info is not None and info.number not in seen:
                seen[info.number] = PrFact(
                    number=info.number, state=info.state, is_draft=info.is_draft,
                    auto_merge_enabled=info.auto_merge_enabled,
                    in_merge_queue=info.in_merge_queue, url=info.url)
        return PrFacts(has_repo=bool(rec.repo), prs=tuple(seen.values()))

    async def _autopilot_transcript_tail(self, session_id: str,
                                         *, turns: int = 6, max_entries: int = 12,
                                         max_chars: int = 400) -> list[dict]:
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
                    arg_head = _json.dumps(e.input, default=str)[:160]
                except Exception:
                    arg_head = str(e.input)[:160]
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
            context = {
                "repo": rec.repo, "goal": goal_text, "goal_source": goal_source,
                "pr_state": pr.to_prompt() if pr is not None else None,
                "reason": "autopilot: goal met",
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
        self, repo: str, branch: str, info: PullRequestInfo, kind: str, rec
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
        """
        target = f"{repo}#{info.number}"
        # Select the branch-aware first prompt by failure kind. Single source of
        # truth for both the live-driver inject and the fresh-spawn seed.
        if kind == "merge_conflict":
            prompt = build_conflict_fix_prompt(repo, info)
        else:  # "failing_ci"
            prompt = build_pr_fix_prompt(repo, info)

        live_rec = rec if (rec is not None and rec.id in self._drivers) else None
        if live_rec is not None:
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
        failing = ", ".join(
            d.name for d in info.checks_detail if d.conclusion == "failure")
        model, model_source = await resolve_model(
            await self._pr_doctor_config(),
            f"{kind} fix for PR {target}: {info.title}"
            + (f"\nfailing checks: {failing}" if failing else ""))
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
        except AllAccountsBusy:
            # Every account is at/above the usage ceiling → defer, not fail. Attempt
            # NOT consumed; surfaced to the user as a warn, not a silent no-op.
            await self._pr_doctor_ctx.emit(
                "warn", f"spawn deferred for {target}: all accounts at/above usage ceiling",
                kind="spawn_failed", target=target)
            return "ceiling", None
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
                await self._pr_doctor_ctx.emit(
                    "warn", f"update-branch failed (rc={proc.returncode}) for {target}",
                    kind="mechanical_failed", target=target)
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
        """Default fallback title generator: a one-shot Haiku query under the account."""
        from ccswitch.oauth import AuthError
        try:
            await asyncio.to_thread(ccswitch_accounts.acquire_valid_token, account)
        except (AuthError, FileNotFoundError) as exc:
            log.debug("title gen skipped — token refresh failed account=%s: %s", account, exc)
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
            account_pinned=bool(getattr(row, "account_pinned", False)),
            active_goal=getattr(row, "active_goal", None),
            inferred_goal=getattr(row, "inferred_goal", None),
            autopilot_paused=bool(getattr(row, "autopilot_paused", False)),
            auto_archive_opt_out_reason=getattr(row, "auto_archive_opt_out_reason", None),
            authored_branches=list(getattr(row, "authored_branches", None) or []),
            context_used_tokens=getattr(row, "context_used_tokens", None),
            context_max_tokens=getattr(row, "context_max_tokens", None),
            command_catalog=getattr(row, "command_catalog", None),
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
        from .background_tasks import is_parked
        added = 0
        backfilled: dict[str, str] = {}
        healed: dict[str, str] = {}
        for row in rows:
            if row.id in self._records:
                continue
            # A pre-crash live status has no driver anymore → normalize to idle. When we
            # DO normalize (the row was mid-turn), the daemon restart dropped an in-flight
            # turn — flag it (issue #434) so the dashboard says "interrupted by daemon
            # restart — re-send to resume" instead of a generic idle card. Transient: the
            # flag lives only on this in-memory record and clears on the next turn.
            was_midturn = row.status in ("working", "in_progress", "condensing")
            status = "idle" if was_midturn else row.status
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
                row, status=status, branch=branch, interrupted_by_restart=was_midturn)
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

    def all_repos_stage_models(self) -> "dict[str, StageModel]":
        """Per-repo StageModel for every watched repo (derived from CI/CD facts)."""
        from server.pipeline.track import build_stage_model, order_deploy_envs
        models: dict = {}
        pop = self._pr_population.all()
        history = self._pr_population.history_store()
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
        from sqlalchemy.dialects.sqlite import insert as _sqlite_insert
        from server.db import db_session as _db_session
        from server.db.models import RepoDeployConfig as _Cfg

        stmt = _sqlite_insert(_Cfg).values(
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

    def check_insights(self, repo: str, window: str, now=None) -> dict:
        """Per-repo Check Health: slowest + flakiest checks, duration regressions,
        and runner-wait health over a time window.
        Reads the read-only HistoryStore; returns a JSON-safe payload. Empty lists
        when history is unavailable or the repo has no rows (never raises)."""
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

    def all_prs(self) -> "list[PipelinePr]":
        """Full PR population (all watched repos) joined to authoring sessions.

        Session join reuses ``prs_for`` (the session-branch poller's knowledge),
        so a PR is linked iff some session authored its branch. Sorted by stage
        urgency (parked→ci→ready→queue→merged), then PR number ascending.
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
        models = self.all_repos_stage_models()
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
            await self._maybe_rearm_on_attach(session_id)
            log.info("session.resume id=%s resume=%s", session_id, resume or "<fresh>")
            return driver

    # ------------------------------------------------------------------
    # Adoption — bring an untracked live claude session under management
    # ------------------------------------------------------------------

    def _resolve_candidate(self, claude_session_id: str):
        """Re-run discovery and return the AdoptionCandidate for this sid, or None.

        Re-resolving (rather than trusting a stale list) is deliberate: processes
        churn fast, so the candidate may have died or changed between the dashboard
        scan and the adopt call. Anchored to the transcript, which persists.
        """
        known = [a["name"] for a in ccswitch_accounts.list_accounts()]
        tracked = {r.claude_session_id for r in self._records.values() if r.claude_session_id}
        for cand in _adopt.discover_candidates(known, tracked):
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

        def _build(resume_arg):
            return SessionDriver(
                session_id, cand.account, cand.cwd, str(cfg_dir),
                mode="interactive", sdk_factory=self._session_factory(session_id), registry=self._registry,
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
        async with db_session() as db:
            db.add(DbSession(
                id=session_id, name=cand.name, account_name=cand.account,
                claude_session_id=claude_session_id, status="idle", pid=None,
                cwd=cand.cwd, repo=None, permission_mode="interactive",
                name_is_auto=True, adopted_in_place=True, last_turn_at=adopted_last_turn,
            ))
            await record_audit(
                db, "session.adopt", identity,
                {"sessionId": session_id, "account": cand.account,
                 "claudeSessionId": claude_session_id, "resumed": resumed},
            )

        from datetime import datetime
        rec = SessionRecord(
            id=session_id, name=cand.name, account=cand.account,
            claude_session_id=claude_session_id, cols=80, rows=24,
            cwd=cand.cwd, repo=None, status="idle", adopted_in_place=True,
            updated_at=datetime.utcnow(),  # row-write time
            last_turn_at=adopted_last_turn,  # real last-response time from the transcript
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

        # Only schedule a background suggestion refresh when a suggester is available.
        if self._suggester is not None and session_id not in self._suggest_inflight:
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

        Caches (signature, items) even when items is empty, so the same transcript
        state is not re-generated on the next poll. Always clears the in-flight flag.
        """
        try:
            if self._suggester is None:
                self._detached_suggestions[session_id] = (signature, [])
                return
            from ccswitch.oauth import AuthError
            try:
                await asyncio.to_thread(ccswitch_accounts.acquire_valid_token, rec.account)
            except (AuthError, FileNotFoundError):
                self._detached_suggestions[session_id] = (signature, [])
                return
            events = await replay_events(path)
            user_text, assistant_text = last_turn_context(events)
            context = f"User: {user_text}\n\nAssistant: {assistant_text}"
            items = await self._suggester.generate(rec.cwd, str(compose(rec.account)), context)
            self._detached_suggestions[session_id] = (signature, list(items or []))
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
            items = await self._suggester.generate(rec.cwd, str(compose(rec.account)), context)
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
            text = await self._summarizer.generate(rec.cwd, str(compose(rec.account)), context)
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
        a cancelled write corrupts the shared (StaticPool) connection. Durability is
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

    async def run_command(self, session_id, name, args) -> dict | None:
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
            arg = (args or "").strip()
            label = arg or "default"
            has_driver = self._drivers.get(session_id) is not None
            restarted = await self.set_model_for_session(session_id, arg or None)
            if not has_driver:
                text = f"Model → {label} — saved; applies when the session resumes"
            elif restarted:
                text = f"Model → {label} — stopped the current activity and restarted it on the new model"
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
        else:
            rec = self._records.get(session_id)
            if rec is None or not rec.claude_session_id:
                return None
            config_dir, csid = str(account_cfg_dir(rec.account)), rec.claude_session_id
        return self._find_transcript(config_dir, csid)

    def _find_transcript(self, config_dir: str, csid: str) -> str | None:
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

    async def _stall_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._check_stalls(self._clock())
            except Exception:
                log.exception("stall watchdog tick failed")

    # ------------------------------------------------------------------
    # Rotation monitor (background task)
    # ------------------------------------------------------------------

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
                for record in list(self._records.values()):
                    if record.account != account:
                        continue
                    if record.status in INACTIVE_SESSION_STATUSES:
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
        """One sweep pass: spawn a rotation attempt for every needs_attention session."""
        for record in list(self._records.values()):
            if record.status != "needs_attention":
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

    async def set_model_for_session(self, session_id: str, model: str | None) -> bool:
        """Set the live model AND persist it, so it survives rehydrate and every
        driver rebuild (rotation/recovery re-applies record.model in _reassign).

        A busy driver only picks up set_model on FUTURE turns, so switching mid-turn
        would leave the in-flight activity running on the old model with no feedback.
        When the driver is mid-turn (turn_active) the turn is stopped and restarted:
        interrupt → set_model → re-send the stored last prompt. That's the same
        stop → switch → replay dance _reassign performs across drivers during
        rotation, here on the same driver — the driver's FIFO prompt queue runs the
        resend as a fresh turn once the interrupt unwinds. Returns True iff the turn
        was actually restarted; with no stored prompt (e.g. the daemon restarted
        mid-turn) the interrupt + switch still happen but nothing is resent. Record
        update + persistence run in every case, including driverless (detached),
        so the model applies when the session resumes."""
        restarted = False
        driver = self._drivers.get(session_id)
        if driver is not None:
            # Stamp BEFORE the interrupt: its status flap fires the autopilot nudge
            # tick, and autopilot must already see the swap as in flight by then.
            self._turn_restart_at[session_id] = self._clock()
            if driver.turn_active:
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
        await self._persist_model(session_id, model)
        return restarted

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

    # ------------------------------------------------------------------
    # Archive / unarchive / permanent-delete
    # ------------------------------------------------------------------

    async def archive_session(self, session_id: str, *, archived_by: str = "user",
                              context: dict | None = None) -> None:
        """Archive a session: stop driver, move cwd into archive_dir, status='archived'.

        The DB row is KEPT (status='archived'). The working directory is moved to
        archive_dir()/<session_id>/ so it can be reviewed or unarchived later.
        If the cwd is missing (already gone), skip the move but still mark archived.
        Git worktree registrations are pruned best-effort after the move.

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
        if driver is not None:
            await driver.aclose()

        # Remove from active records — archived sessions are NOT in _records.
        record = self._records.pop(session_id, None)

        # Clear in-memory worktree registration (we move the dir, not remove via git).
        self._worktrees.pop(session_id, None)

        # Move the cwd into the archive directory.
        dest = (await self._effective_archive_home()) / session_id
        cwd_path: Path | None = Path(record.cwd) if (record and record.cwd) else None

        # Resolve the shared clone BEFORE the move, while the worktree is still a
        # valid git dir, so we can scope-deregister exactly this path afterward
        # (never a global prune — issue #177).
        # Both git calls run off the event loop (asyncio.to_thread) — git subprocess
        # startup can spike under load, and simultaneous archive of many sessions was
        # blocking the event loop long enough to trip the health-check watchdog.
        repo_top: str | None = None
        if cwd_path and cwd_path.exists():
            repo_top = await asyncio.to_thread(_resolve_git_common_dir, str(cwd_path))

        if cwd_path and cwd_path.exists():
            try:
                outcome = await asyncio.to_thread(move_into_archive, cwd_path, dest)
                if outcome == "skipped:nospace":
                    log.warning(
                        "session.archive_move_skipped id=%s src=%s dest=%s — "
                        "insufficient free space on archive disk; worktree left in place",
                        session_id, cwd_path, dest,
                    )
                else:
                    log.info(
                        "session.archive id=%s moved cwd=%s to archive=%s (%s)",
                        session_id, cwd_path, dest, outcome,
                    )
            except Exception:
                log.exception(
                    "session.archive_move_failed id=%s src=%s dest=%s",
                    session_id, cwd_path, dest,
                )
        else:
            log.info("session.archive id=%s cwd missing or unknown, skip move", session_id)

        # Best-effort: drop THIS session's now-stale worktree registration, scoped
        # to its own path. NOT a global `git worktree prune`: that reaped a sibling
        # session's live worktree (issue #177), and it can no longer clean up our
        # own record anyway now that session worktrees are locked on create.
        if repo_top and cwd_path:
            try:
                await asyncio.to_thread(
                    worktrees.clear_stale_registration, repo_top, str(cwd_path)
                )
            except Exception:
                log.debug("session.archive_worktree_deregister_failed id=%s",
                          session_id, exc_info=True)

        # Mark archived in DB.
        async with db_session() as db:
            from datetime import datetime
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession)
                .where(DbSession.id == session_id)
                .values(status="archived", archived_by=archived_by, archive_context=context,
                        archived_at=datetime.utcnow())
            )
        prev_status = record.status if record is not None else None
        log_event(log, logging.INFO, "session.terminal",
                  session_id=session_id,
                  **{"from": prev_status, "to": "archived", "reason": "archive"})
        log.info("session.archive id=%s done", session_id)

        # Enforce the configured archived-session cap. Best-effort: a purge failure
        # must never fail the archive that triggered it.
        try:
            await self._enforce_archive_limit()
        except Exception:
            log.exception("archive_limit: enforcement after archive failed id=%s", session_id)

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
            ids = list((await db.execute(
                sa_select(DbSession.id)
                .where(DbSession.status == "archived")
                .order_by(order_key.asc())
                .limit(count)
            )).scalars().all())

        deleted: list[str] = []
        for sid in ids:
            try:
                if await self.permanent_delete(sid):
                    deleted.append(sid)
            except Exception:
                log.exception("archive_limit: permanent_delete failed id=%s", sid)
        if deleted:
            log.info("archive_limit: purged %d oldest archived session(s)", len(deleted))
        return deleted

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
                "claudeSessionId": row.claude_session_id,
                "permissionMode": row.permission_mode,
                "createdAt": iso_utc(row.created_at),
                "archivedAt": iso_utc(row.updated_at),
            }
            for row in rows
        ]

    async def unarchive(self, session_id: str) -> "SessionRecord | None":
        """Move the archived cwd back and restore the session to idle.

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

        archive_path = (await self._effective_archive_home()) / session_id
        original_cwd = Path(row.cwd)

        def _restore() -> str:
            if archive_path.exists():
                original_cwd.parent.mkdir(parents=True, exist_ok=True)
                # Same cross-device safety as archiving, but keep the whole tree
                # (prune=()): unarchiving restores the worktree, it does not shed deps.
                return move_into_archive(archive_path, original_cwd, prune=())
            else:
                # Archive folder missing — recreate an empty cwd so SDK can start.
                original_cwd.mkdir(parents=True, exist_ok=True)
                log.warning(
                    "session.unarchive id=%s archive folder missing, recreating empty cwd",
                    session_id,
                )
                return "created_empty"

        move_result = await asyncio.to_thread(_restore)
        if move_result == "skipped:nospace":
            raise InsufficientStorageError(
                f"Not enough disk space to restore session — the target filesystem "
                f"({mountpoint(original_cwd)}) is low on inodes or bytes"
            )

        # Capture the archive provenance BEFORE we clear it, so we can tell whether this
        # unarchive is REVERSING an automated archive (→ a learning heuristic).
        prev_archived_by = getattr(row, "archived_by", None)
        prev_context = getattr(row, "archive_context", None)

        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession)
                .where(DbSession.id == session_id)
                # Restore to idle AND pin the sticky opt-out: a manually-unarchived
                # session is never auto-archived again. Clear the now-consumed provenance.
                .values(status="idle", auto_archive_opt_out_reason="manual-unarchive",
                        archived_by=None, archive_context=None)
            )

        # Heuristic capture: an unarchive that reverses an AUTO archive is a signal the
        # auto-archive judgement was wrong. Record it (with the captured decision features)
        # so the decider can grow more cautious — see _autopilot_reversal_hint.
        if prev_archived_by in ("autopilot", "backlog-runner"):
            await self._record_archive_reversal(session_id, prev_archived_by, prev_context)

        record = self._record_from_row(
            row, status="idle", auto_archive_opt_out_reason="manual-unarchive")
        self._records[session_id] = record
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

    async def permanent_delete(self, session_id: str) -> bool:
        """Permanently delete a session: remove archive folder + DB row.

        Returns True if deleted, False if the session was not found in DB.
        Containment guard: only removes paths that are direct children of archive_dir().

        A *live* session (still in _records/_drivers, or status != 'archived') is
        archived first: archive_session stops the driver and moves the cwd into the
        archive. Deleting a live session directly would otherwise leave a zombie
        driver running and orphan the working directory outside archive_dir().
        """
        from ccswitch.paths import archive_dir
        from sqlalchemy import select as sa_select

        async with db_session() as db:
            row = (await db.execute(
                sa_select(DbSession).where(DbSession.id == session_id)
            )).scalars().first()
        if row is None:
            return False

        # Archive-then-delete: a live session must be torn down before its row and
        # working dir are removed, or we leak a driver and orphan the cwd.
        if (
            session_id in self._records
            or session_id in self._drivers
            or row.status != "archived"
        ):
            await self.archive_session(session_id)

        # Containment guard: candidate must be a direct child of archive_dir().
        archive_base = archive_dir().resolve()
        candidate = (archive_base / session_id).resolve()
        try:
            candidate.relative_to(archive_base)
        except ValueError:
            log.error(
                "session.permanent_delete CONTAINMENT GUARD id=%s candidate=%s",
                session_id, candidate,
            )
            return False

        await asyncio.to_thread(shutil.rmtree, str(candidate), True)
        log.info("session.permanent_delete id=%s archive_path=%s", session_id, candidate)

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

    async def resolve_transcript_path(self, session_id: str) -> str | None:
        """Resolve the on-disk JSONL path for a session's transcript.

        Works for both active and archived sessions. Returns None when not found.
        Active sessions are resolved via transcript_path(); archived sessions
        (not in _records) fall back to a DB lookup + glob.
        """
        from sqlalchemy import select as sa_select

        path = self.transcript_path(session_id)
        if path is None:
            # For archived sessions the record is not in _records; load from DB.
            async with db_session() as db:
                row = (await db.execute(
                    sa_select(DbSession).where(DbSession.id == session_id)
                )).scalars().first()
            if row and row.claude_session_id:
                path = self._find_transcript(
                    str(account_cfg_dir(row.account_name)), row.claude_session_id
                )
        return path

    async def transcript_events(self, session_id: str, *, max_turns: int | None = 20) -> list:
        """Return replayed transcript events for a session (read-only review).

        Works for both active and archived sessions: transcripts live in the
        account's config dir (not the moved cwd), so always accessible.
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
                                limit: int | None = None):
        """Windowed transcript replay. Returns a ReplayWindow, or None if no transcript.

        Use before/limit for lazy scrollback paging: returns up to `limit` turns
        ending before turn-ordinal `before` (or the last `limit` turns if before
        is None), plus earliest_turn/total_turns for pagination metadata.
        limit=None (the default) returns the FULL transcript — no silent cap.
        """
        from server.sessions.replay import replay_window

        path = await self.resolve_transcript_path(session_id)
        if not path:
            return None
        try:
            return await replay_window(path, before=before, limit=limit)
        except Exception:
            log.warning("session.transcript_window id=%s replay failed", session_id, exc_info=True)
            return None

    def snapshot_deploy(self, *, path: str | None = None) -> dict:
        """SAVE step: record sessions with a live driver so a daemon-affecting restart
        can eagerly re-launch them (rehydrate alone is lazy + flattens 'working'→'idle')."""
        live = [{"id": sid, "status": getattr(self._records.get(sid), "status", "idle")}
                for sid in list(self._drivers.keys())]
        _deploy_snapshot.write_snapshot(live, path=path)
        return {"live": live}

    async def restore_deploy(self, *, path: str | None = None) -> dict:
        """RESUME step: eagerly re-launch each snapshotted session in the fresh daemon.

        A session that fails this first attempt is enrolled in ``_resume_recovery``
        so the daemon keeps retrying it (see _recover_resume_failures_once) instead
        of leaving it stuck behind a sticky 'open to retry' banner."""
        restored: list[str] = []
        failed: list[str] = []
        for entry in _deploy_snapshot.read_snapshot(path=path):
            sid = entry["id"]
            try:
                driver = await self.ensure_driver(sid, SYSTEM_IDENTITY)
            except Exception:
                driver = None
            if driver is not None:
                restored.append(sid)
            else:
                failed.append(sid)
                self._resume_recovery[sid] = 0   # enroll for daemon-side retry
        _deploy_snapshot.clear_snapshot(path=path)
        return {"restored": restored, "failed": failed}

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

    def resume_recovery_status(self) -> dict:
        """Live view of unresolved deploy-resume failures for the banner.

        ``failed`` = still being retried; ``gaveUp`` = capped out (kept visible so a
        human sees a permanently-broken session). Prunes now-live ids first so the
        banner clears the moment a session comes back — by auto-retry or manual open."""
        self._prune_recovered()
        m = RESUME_RECOVERY_MAX_ATTEMPTS
        return {"failed": [s for s, a in self._resume_recovery.items() if a < m],
                "gaveUp": [s for s, a in self._resume_recovery.items() if a >= m]}


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
