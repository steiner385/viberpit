"""The backlog-runner automation: pulls high-value issues off the backlog and
spawns implementation sessions, handing finished PRs to PR Doctor."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

from ccswitch.worktrees import DISK_EXHAUSTION_MARKERS
from server.github.pr_status import PullRequestInfo

from .backlog_github import NEEDS_HUMAN_RETRY_DAYS_DEFAULT, GhRun, query_candidates
from .backlog_grouping import load_plan, mark_consumed, plan_to_batch
from .backlog_selection import SELECTION_STRATEGIES, Batch, should_free_slot
from .base import AutomationStatus, ConfigField
from .model_pick import AUTOMATION_MODEL_CHOICES
from .context import AutomationContext

log = logging.getLogger(__name__)

_DAY_SECONDS = 24 * 3600

# Spawn-failure damping (2026-07-21 incident: ENOSPC spawn retries every tick
# churned gigabytes of checkout page cache into the daemon cgroup's MemoryHigh
# throttle, wedging the control socket into a watchdog restart loop). Per-target
# cooldown ramps gently — a first failure is often a transient blip (a momentary
# worktree collision, a slot that frees a tick later), so the first retry is
# quick and the delay only stretches for a target that keeps failing (#666).
SPAWN_BACKOFF_SCHEDULE_S = (60.0, 300.0)    # cooldown after the 1st, 2nd consecutive failure
SPAWN_BACKOFF_MAX_DEFAULT_S = 900.0         # 3rd+ failure cooldown (CCSWITCH_BACKLOG_MAX_BACKOFF_S)
SPAWN_CIRCUIT_THRESHOLD = 3         # consecutive failures (any cause) that open the breaker
SPAWN_CIRCUIT_COOLDOWN_S = 1800.0   # breaker-open duration before a half-open probe
# Grouping-optimizer damping: after a failed plan refresh, retry after a SHORT
# fixed cooldown rather than a full grouping interval, so a transient model /
# connectivity blip recovers the plan within minutes — the underlying
# ``propose_groups`` call has its own per-account helper-layer circuit breaker,
# so a short cadence can't cause runaway model spend during a sustained outage.
GROUP_REFRESH_BACKOFF_S = 120.0


def _max_spawn_backoff_s() -> float:
    """The 3rd+-consecutive-failure per-target cooldown ceiling, overridable via
    ``CCSWITCH_BACKLOG_MAX_BACKOFF_S`` (seconds). A non-positive or unparseable
    value falls back to the default."""
    raw = os.environ.get("CCSWITCH_BACKLOG_MAX_BACKOFF_S")
    if raw:
        try:
            val = float(raw)
        except ValueError:
            return SPAWN_BACKOFF_MAX_DEFAULT_S
        if val > 0:
            return val
    return SPAWN_BACKOFF_MAX_DEFAULT_S


def _spawn_backoff_delay(consecutive_failures: int) -> float:
    """Per-target cooldown for the Nth consecutive spawn failure: 60s, 300s,
    then the (configurable) max for the 3rd and beyond. Every tier is clamped by
    the max so a low ``CCSWITCH_BACKLOG_MAX_BACKOFF_S`` still caps the ramp."""
    cap = _max_spawn_backoff_s()
    if 1 <= consecutive_failures <= len(SPAWN_BACKOFF_SCHEDULE_S):
        return min(SPAWN_BACKOFF_SCHEDULE_S[consecutive_failures - 1], cap)
    return cap


# --------------------------------------------------------------------------- #
# select_batch (plan consumption: query live candidates → pop a plan group)   #
# --------------------------------------------------------------------------- #

async def select_batch(
    config: dict,
    *,
    query: Callable[..., list[dict]] = query_candidates,
    gh_run: "GhRun | None" = None,
    repo_load: "dict[str, int] | None" = None,
    paused_repos: "set[str] | None" = None,
    load_plan: Callable[[], Awaitable[list[dict]]] = load_plan,
    mark_consumed: Callable[[int], Awaitable[None]] = mark_consumed,
) -> "Batch | None":
    """Pop the next precomputed group off the grouping plan as a ``Batch``.

    The runner never chooses or bundles issues itself: the background grouping
    optimizer (see :mod:`backlog_grouping`) writes the plan, and this function
    only intersects it with the LIVE candidate pool (so refs claimed or closed
    since the plan was computed are dropped) and returns the highest-priority
    surviving group. No live group → ``None`` — the runner WAITS and the loop
    nudges a plan refresh instead of inventing a selection.

    Parameters
    ----------
    config:
        Automation config dict.
    query:
        Injectable candidate-discovery callable (default: ``query_candidates``).
        Signature: ``(config, *, gh_run) -> list[dict]``.
    gh_run:
        Injectable GitHub API runner (forwarded to ``query``).
    repo_load:
        Optional ``{repo: active_run_count}`` map. When given, live plan groups
        are stable-reordered toward the repo with the FEWEST active runs
        (per-repo fairness) so a big backlog in one repo can't monopolise every
        slot; plan priority is preserved within a repo.
    paused_repos:
        Repos to skip this selection (stale git credentials, issue #670) — their
        clone can only fetch a stale ref, so branching a worktree off it would
        implement against ancient code. Other repos are still serviced.
    load_plan / mark_consumed:
        Injectable plan-store seams (default: the ``backlog_groups`` table).
        Tests pass canned functions so no DB or model is ever touched here.

    Returns
    -------
    ``Batch`` or ``None`` when there are no candidates or no live plan group.
    """
    from ccswitch import gitrepos

    _gh_run = gh_run if gh_run is not None else gitrepos._run_gh
    candidates = query(config, gh_run=_gh_run)
    if not candidates:
        return None

    plan = await load_plan()
    # The user-selectable per-repo strategy + its params ride on the config; the
    # ordered ``enabled_repos`` list doubles as the ``priority`` ranking.
    batch, group_id = plan_to_batch(
        plan, candidates, repo_load, paused_repos,
        strategy=config.get("selection_strategy") or "balanced",
        repo_order=config.get("enabled_repos") or [],
        weights=config.get("repo_weights") or {},
        caps=config.get("repo_caps") or {})
    if batch is not None and group_id is not None:
        try:
            await mark_consumed(group_id)
        except Exception:  # pragma: no cover — a UI stamp must never block a spawn
            log.warning("backlog_runner.mark_consumed_failed group=%s", group_id,
                        exc_info=True)
    return batch


class BacklogRunner:
    id = "backlog-runner"
    name = "Backlog Runner"
    description = "Continuously implements high-priority backlog issues into reviewed-ready PRs."
    modes: tuple[str, ...] = ("off", "propose", "auto")
    config_schema = [
        ConfigField(key="enabled_repos", label="Repositories", type="repo-multiselect",
                    default=[], help="Repos to draw issues from (empty = all)."),
        ConfigField(key="eligible_label", label="Eligible label", type="string",
                    default="ready",
                    help="Only issues with this label are candidates. Leave blank "
                         "to consider ALL open, unassigned issues (no label gate)."),
        ConfigField(key="selection_strategy", label="Repo selection strategy",
                    type="enum", default="balanced",
                    choices=list(SELECTION_STRATEGIES),
                    help="How a freed slot chooses among repos with work. "
                         "'balanced': fewest in-flight runs first (equal "
                         "rotation). 'priority': serve repos in the order listed "
                         "above, top first. 'weighted': bias by per-repo weight "
                         "(a freed slot favors higher-weighted repos; weight 0 "
                         "pauses a repo). 'concurrency-cap': never exceed a "
                         "repo's max in-flight runs."),
        ConfigField(key="repo_weights", label="Repo weights", type="repo-number-map",
                    default={},
                    help="Per-repo weight for the 'weighted' strategy (default 1). "
                         "Higher = more freed slots; 0 pauses the repo."),
        ConfigField(key="repo_caps", label="Repo concurrency caps",
                    type="repo-number-map", default={},
                    help="Per-repo max in-flight runs for the 'concurrency-cap' "
                         "strategy (a repo not listed is uncapped)."),
        ConfigField(key="max_concurrent", label="Max sessions in flight", type="int", default=1,
                    help="Parallel implementing sessions allowed at once."),
        ConfigField(key="max_spawns_per_day", label="Max spawns per day", type="int", default=10,
                    help="Circuit-breaker: pause spawning past this many per day."),
        ConfigField(key="max_attempts", label="Max attempts per issue", type="int", default=2),
        ConfigField(key="max_issues_per_pr", label="Max issues per PR", type="int", default=1,
                    help="Bundle up to this many related issues into one PR. The "
                         "background grouping optimizer treats this as a hard cap, "
                         "not a target — only genuinely related issues share a PR."),
        ConfigField(key="grouping_interval_minutes", label="Grouping refresh (minutes)",
                    type="int", default=30,
                    help="How often the background optimizer recomputes the group "
                         "plan. It also refreshes immediately when the runner "
                         "needs work and no plan group is live."),
        ConfigField(key="needs_human_retry_days", label="Retry needs-human after (days)",
                    type="int", default=NEEDS_HUMAN_RETRY_DAYS_DEFAULT,
                    help="Reconsider escalated (needs-human) issues once they sit "
                         "untouched this many days. 0 = never retry (permanent "
                         "handoff). Any activity on the issue restarts the clock."),
        ConfigField(key="default_model", label="Session model", type="enum",
                    default="auto", choices=AUTOMATION_MODEL_CHOICES,
                    help="Model for spawned sessions. 'auto' assesses each job's "
                         "complexity and picks a tier; 'default' uses the "
                         "account's default model."),
        ConfigField(key="respect_dependencies", label="Respect issue dependencies",
                    type="bool", default=True,
                    help="Skip issues whose dependencies are still open: native "
                         "blocked-by relationships, 'Depends on #N' in the body, "
                         "and epic list order (sub-issues / task-list checklists "
                         "— list order is execution order). Skips are logged and "
                         "counted; the issue becomes eligible the moment its "
                         "blockers close."),
    ]

    def status(self, ctx) -> AutomationStatus:
        counts = getattr(ctx, "backlog_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        n = counts.get("in_flight", 0)
        return AutomationStatus(
            text=f"{n} implementing, {counts.get('eligible', 0)} eligible"
                 + (f", {counts['blocked']} blocked" if counts.get("blocked") else "")
                 + (f", {counts['needs_human']} needs-human" if counts.get("needs_human") else ""),
            level="active" if n else ("warn" if counts.get("needs_human") else "idle"),
            counts=counts)

    async def on_state_change(self, *, enabled, mode, config, ctx) -> None:
        # The orchestrator's loop reads mode/config each tick; nothing to start here.
        ...


# --------------------------------------------------------------------------- #
# Control loop                                                                 #
# --------------------------------------------------------------------------- #

# Injected seam type aliases (the exact contract Plan B implements).
SelectBatch = Callable[[dict], Awaitable["Batch | None"]]
Claim = Callable[["Batch"], Awaitable[bool]]
Spawn = Callable[["Batch", str], Awaitable[Any]]
CountInFlight = Callable[[], Awaitable[int]]
# (eligible, blocked): the true count of eligible, unclaimed OPEN issues — the
# honest backlog gauge for the Automation card, independent of how many runner
# slots are free — plus how many otherwise-eligible issues were skipped as
# dependency-blocked this sweep (the "why isn't the backlog draining" note).
CountCandidates = Callable[[], Awaitable["tuple[int, int]"]]
HealthyAccount = Callable[[], Awaitable["str | None"]]
PrStatusFor = Callable[[str], Awaitable["PullRequestInfo | None"]]
PrDoctorActive = Callable[[str], Awaitable[bool]]
ReadConfig = Callable[[], Awaitable["tuple[str, dict]"]]
# RELEASE-only path: remove ``in-progress``, return the batch to the pool (no
# ``needs-human``). Used on a transient spawn failure and by reconciliation.
Release = Callable[[str], Awaitable[None]]
# GIVE-UP path: add ``needs-human`` + remove ``in-progress``. Used only when a
# target has exhausted ``max_attempts`` or PR Doctor gave up on it.
NeedsHuman = Callable[[str], Awaitable[None]]
# MERGED path: the work landed but the PR may not have carried a closing
# keyword — comment + close every issue in the batch so it can't stay open
# (and label-excluded) forever. Returns the close-out outcome: "closed" (≥1
# issue verifiably linked to the PR — proceed), "unlinked" (the PR references
# NO batch issue: a shared-clone misattribution, #642 — ignore + denylist it),
# or "retry" (linkage unverifiable — try again next sweep). ``None`` (legacy
# stubs) reads as "closed".
CloseOut = Callable[[str, "PullRequestInfo"], Awaitable["str | None"]]
SessionEnded = Callable[[str], Awaitable[bool]]
# Did the session ever run a turn? True once it recorded a ``claude_session_id``
# (the SDK assigns one when turn ONE starts), so False proves the batch was never
# actually attempted. Gates the give-up path: a session that never ran must not
# escalate its issue to ``needs-human`` (#1100).
SessionRan = Callable[[str], Awaitable[bool]]
# Reconciliation seam: given the durably-claimed refs and the live origin_refs
# (non-terminal runner sessions), release any dangling claim to the pool.
Reconcile = Callable[[list[str], "set[str]"], Awaitable[None]]
# GitHub-truth orphan healing: (config, tracked_refs) -> [(ref, action), ...]
# where action is "closed" (merged PR found → issue closed), "escalated" (merged
# PR found, but the human gate blocked auto-close → handed to needs-human, #784),
# or "reclaimed" (stale lock released back to the pool).
HealOrphans = Callable[[dict, "set[str]"], Awaitable["list[tuple[str, str]]"]]
# Grouping-optimizer seam: recompute + persist the group plan from the live
# backlog; returns the number of groups stored. MUST raise on a model/plan
# failure (rather than storing an empty plan over a real backlog) so the loop
# backs off a full grouping interval instead of hammering the model every tick.
RefreshGroups = Callable[[dict], Awaitable[int]]
# Stale-credential seam (issue #670): the current list of repos whose last git
# fetch failed with an auth-class error (each dict: repo/since/error/settingsUrl).
# The loop uses it to notify the dashboard; selection pauses those repos
# separately via ``select_batch(paused_repos=...)``.
CredentialAlerts = Callable[[], "list[dict]"]

# The heal pass costs a gh search plus up to `limit` cross-ref lookups, so it
# runs at most once per interval — orphans are a slow leak, not a hot path.
_ORPHAN_SWEEP_INTERVAL = 3600.0


async def _stub_select_batch(_config: dict) -> Batch | None:
    # Fallback when no selection seam is wired: the loop runs but never selects
    # a candidate, so no session is ever spawned.
    return None


async def _stub_claim(_batch: Batch) -> bool:
    # TODO(Plan B): atomic label-based claim + reconciliation. No claim → no spawn.
    return False


async def _stub_release(_target: str) -> None:
    # RELEASE-only fallback: remove the claim, no needs-human.
    return None


async def _stub_needs_human(_target: str) -> None:
    # GIVE-UP fallback: needs-human handoff.
    return None


class BacklogRunnerLoop:
    """A restart-safe, non-reentrant control loop that keeps up to
    ``max_concurrent`` implementation sessions in flight.

    Every I/O dependency is INJECTED (selection/claim/spawn/counts/accounts/PR
    status/config/clock/session-probe) so the whole refill+sweep body is testable
    with no network and no real sessions. ``tick()`` runs the entire body under a
    single ``asyncio.Lock`` — an interval tick and an end-of-session nudge must
    never overlap, or a slot could be double-filled (in-flight is always
    re-derived from the DB via ``count_in_flight`` inside the lock, never cached).

    The circuit-breaker cap (``max_spawns_per_day``) is a ROLLING 24-hour window
    over ``time.monotonic`` spawn timestamps, not a calendar day — a burst is
    throttled for a full day after it happens, regardless of midnight.
    """

    def __init__(
        self,
        ctx: AutomationContext,
        *,
        select_batch: SelectBatch,
        claim: Claim,
        spawn: Spawn,
        count_in_flight: CountInFlight,
        healthy_account: HealthyAccount,
        pr_status_for: PrStatusFor,
        pr_doctor_active: PrDoctorActive,
        read_config: ReadConfig,
        clock: Callable[[], float],
        release: Release | None = None,
        needs_human: NeedsHuman | None = None,
        session_ended: SessionEnded | None = None,
        session_ran: SessionRan | None = None,
        reconcile: Reconcile | None = None,
        count_candidates: CountCandidates | None = None,
        close_out: CloseOut | None = None,
        heal_orphans: HealOrphans | None = None,
        refresh_groups: RefreshGroups | None = None,
        credential_alerts: CredentialAlerts | None = None,
    ) -> None:
        self._ctx = ctx
        self._select_batch = select_batch          # Plan B: -> Batch | None
        self._claim = claim                         # Plan B: (batch) -> bool
        self._spawn = spawn                         # (batch, account) -> SessionRecord
        self._count_in_flight = count_in_flight     # DB: origin==id, status!=ended
        self._healthy_account = healthy_account     # -> account name | None
        self._pr_status_for = pr_status_for         # (primary) -> PullRequestInfo | None
        self._pr_doctor_active = pr_doctor_active   # (repo) -> bool
        self._read_config = read_config             # -> (mode, config)
        self._clock = clock                         # injected; no wall-clock in tests
        self._release = release or _stub_release    # remove in-progress, back to pool
        self._needs_human = needs_human or _stub_needs_human  # add needs-human + remove
        self._session_ended = session_ended         # (session_id) -> bool | None
        self._session_ran = session_ran             # (session_id) -> bool | None
        self._reconcile = reconcile                 # (claimed_refs, live_refs) -> None
        self._count_candidates = count_candidates    # -> int (true eligible backlog size)
        self._close_out = close_out                 # (target, info) -> close merged batch issues
        self._heal_orphans = heal_orphans               # GitHub-truth orphan healing
        self._refresh_groups = refresh_groups           # grouping optimizer (plan writer)
        self._credential_alerts = credential_alerts     # -> [{repo,since,error,settingsUrl}]
        self._cred_alerted: set[str] = set()            # repos with a live credential alert
        self._last_orphan_sweep: float | None = None    # monotonic; throttles the heal pass
        self._last_group_refresh: float | None = None   # monotonic; throttles the optimizer
        self._group_nudge = False                       # refill starved → refresh next tick
        self._group_refresh_retry_at: float | None = None  # failed → gate nudges until this clock time
        self._group_refresh_failed_emitted = False      # debounce the failure event
        self._last_groups_emitted: int | None = None    # debounce the refreshed event
        self._lock = asyncio.Lock()
        self._spawns_today: list[float] = []        # spawn timestamps within the rolling day
        self._circuit_open_emitted = False          # debounce the circuit_open event
        self._spawn_backoff: dict[str, tuple[int, float]] = {}  # target → (fails, retry_at)
        self._spawn_circuit_until: float | None = None  # breaker open until this clock time
        self._consec_spawn_failures = 0             # resets on any successful spawn
        self._no_account_emitted = False            # debounce the skipped_no_account event

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._refill_and_sweep()
            except Exception:  # pragma: no cover — defensive: a tick must never crash the loop
                log.warning("backlog_runner.tick_failed", exc_info=True)

    # -- refill ------------------------------------------------------------- #

    async def _refill_and_sweep(self) -> None:
        mode, config = await self._read_config()
        # Surface stale-credential alerts every tick, before refill — a paused
        # repo must both notify the dashboard and be excluded from selection.
        await self._check_credentials()
        if mode != "off":
            # The grouping optimizer runs BEFORE refill so a cold start (or a
            # just-consumed plan) can produce groups and spawn in the same tick.
            await self._maybe_refresh_groups(config)
            self._prune_day()
            if len(self._spawns_today) < config["max_spawns_per_day"]:
                self._circuit_open_emitted = False
                await self._refill(mode, config)
            elif not self._circuit_open_emitted:
                self._circuit_open_emitted = True
                await self._ctx.emit("warn", "daily spawn cap reached — pausing",
                                     kind="circuit_open")
        # handoff + terminal-watch runs every tick, regardless of mode.
        await self._sweep(config)
        # Return orphaned claims to the pool (crash-between-claim-and-spawn leak).
        await self._reconcile_dangling()
        # GitHub-truth healing: claim labels NO state row tracks (crash, DB
        # loss, or pre-handed_off handoffs) are invisible to the row-derived
        # reconciliation above and lock issues out of the pools forever.
        # Propose mode is now included: propose-mode claims write a state row
        # (proposed=True) before the gh label write, so the heal sees them as
        # tracked (not orphans) and leaves their labels alone.
        if mode != "off":
            await self._orphan_sweep(config)
        # Stash live counts on the ctx so ``BacklogRunner.status(ctx)`` renders
        # "N implementing, M eligible, K needs-human" instead of always "idle".
        await self._stash_counts(config)

    async def _check_credentials(self) -> None:
        """Surface stale-credential alerts (issue #670) to the dashboard.

        Reads the current set of repos whose last git fetch failed with an
        auth-class error and emits a dashboard event the FIRST time each repo
        goes stale, and a "restored" event once it clears — both debounced via
        ``_cred_alerted`` so a persistent failure doesn't flood the activity feed
        every tick. The error-level emit also routes through the notification
        rules (see :meth:`AutomationContext.emit`), so a configured push/webhook
        fires too. Selection pausing is handled separately
        (``select_batch(paused_repos=...)``); this method only notifies.
        Best-effort: an alert-read or emit failure must never stall a tick."""
        if self._credential_alerts is None:
            return
        try:
            alerts = self._credential_alerts() or []
        except Exception:  # pragma: no cover — an alert read must not stall the tick
            log.warning("backlog_runner.credential_read_failed", exc_info=True)
            return
        by_repo = {a["repo"]: a for a in alerts if a.get("repo")}
        current = set(by_repo)
        # Newly stale → error event (once per repo until it clears).
        for repo in sorted(current - self._cred_alerted):
            self._cred_alerted.add(repo)
            url = by_repo[repo].get("settingsUrl")
            await self._ctx.emit(
                "error",
                f"Git credential refresh needed for {repo} — backlog paused for "
                f"this repo until a fetch succeeds",
                kind="credential_refresh_needed", target=repo,
                detail={"repo": repo, "settingsUrl": url,
                        "error": by_repo[repo].get("error")})
        # Recovered → restored event + drop the debounce so a future failure re-alerts.
        for repo in sorted(self._cred_alerted - current):
            self._cred_alerted.discard(repo)
            await self._ctx.emit(
                "info",
                f"Git credentials for {repo} working again — backlog resumed",
                kind="credential_restored", target=repo, detail={"repo": repo})

    async def _stash_counts(self, config: dict) -> None:
        """Cache the current in-flight / eligible / needs-human counts on the ctx
        so ``status(ctx)`` can render non-idle text. Best-effort — a count read
        failure must never break a tick."""
        try:
            in_flight = await self._count_in_flight()
        except Exception:  # pragma: no cover — count read must not stall the tick
            in_flight = 0
        needs_human = 0
        try:
            states = await self._ctx.list_target_states()
            needs_human = sum(1 for s in states.values() if s.get("gave_up"))
        except Exception:  # pragma: no cover — defensive
            states = {}
        # The honest backlog gauge: the true count of eligible, unclaimed issues,
        # independent of how many runner slots are free (a saturated runner still
        # reports real depth), plus how many were skipped as dependency-blocked
        # this sweep. Best-effort — a query failure reports 0.
        eligible = 0
        blocked = 0
        if self._count_candidates is not None:
            try:
                eligible, blocked = await self._count_candidates()
            except Exception:  # pragma: no cover — a count read must not stall the tick
                eligible = blocked = 0
        setattr(self._ctx, "backlog_counts", {
            "in_flight": in_flight,
            "eligible": eligible,
            "blocked": blocked,
            "needs_human": needs_human,
        })

    async def _reconcile_dangling(self) -> None:
        """Release claims that leaked (label written, session never came up).

        Derives the durably-claimed refs from the target-state ``claimed`` sets
        and the live origin_refs from non-terminal runner sessions, then hands
        both to the injected ``reconcile`` seam. Deriving danglers from the
        durable ``claimed`` state (rather than a fresh gh ``in-progress`` query)
        keeps this cheap and network-light for v1: a claimed row that never
        recorded a ``session_id`` (the crash-between-claim-and-spawn leak) is a
        dangler; a row whose spawn succeeded (has a ``session_id``) is owned by
        the sweep's give-up / handoff bookkeeping and is carried as "live" so the
        seam leaves its labels intact and never races the attempt counting.
        A ``handed_off`` row is likewise sweep-owned — its session is gone by design
        (PR Doctor carries the PR), so it must never read as a dangler."""
        if self._reconcile is None:
            return
        try:
            states = await self._ctx.list_target_states()
        except Exception:  # pragma: no cover — a state read must not stall the tick
            return
        claimed_refs: list[str] = []
        live_refs: set[str] = set()
        danglers: list[str] = []
        for target, st in states.items():
            if not st.get("claimed"):
                continue
            issues = st.get("issues") or [target]
            claimed_refs.extend(issues)
            # A row that ever recorded a session (spawn succeeded) is owned by the
            # sweep, not reconciliation — treat it as live. Handed-off rows (PR
            # Doctor has taken over) are also live. A proposed-mode row (claimed,
            # waiting for the user tap) is intentional: its ``proposed`` flag
            # distinguishes it from the crash-between-claim-and-spawn leak (no
            # session_id, no proposed flag, no handed_off), which IS a dangler.
            if st.get("session_id") is not None or st.get("handed_off") or st.get("proposed"):
                live_refs.update(issues)
            else:
                danglers.append(target)
        if claimed_refs:
            try:
                await self._reconcile(claimed_refs, live_refs)
            except Exception:  # pragma: no cover — best-effort reconciliation
                log.warning("backlog_runner.reconcile_failed", exc_info=True)
                return
            # The labels are released — drop the leaked rows too, or they hold
            # a concurrency slot forever (count_in_flight counts active rows).
            for target in danglers:
                await self._ctx.delete_target_state(target)

    async def _orphan_sweep(self, config: dict) -> None:
        """Heal orphaned claim labels from GitHub truth, at most once per
        ``_ORPHAN_SWEEP_INTERVAL``.

        Tracked refs are the union of every state row's ``issues`` — anything
        else wearing ``in-progress`` in the enabled repos is an orphan the
        injected seam heals (close if its PR merged, release if nothing carries
        it). The window stamp is taken up front so a failing pass also waits a
        full interval instead of hammering gh every tick."""
        if self._heal_orphans is None:
            return
        now = self._clock()
        if (self._last_orphan_sweep is not None
                and now - self._last_orphan_sweep < _ORPHAN_SWEEP_INTERVAL):
            return
        self._last_orphan_sweep = now
        try:
            states = await self._ctx.list_target_states()
            tracked: set[str] = set()
            for target, st in states.items():
                tracked.update(st.get("issues") or [target])
            healed = await self._heal_orphans(config, tracked)
        except Exception:  # pragma: no cover — healing must never break a tick
            log.warning("backlog_runner.orphan_sweep_failed", exc_info=True)
            return
        for ref, action in healed or []:
            if action == "closed":
                await self._ctx.emit(
                    "info", f"{ref}: orphaned claim — merged PR found, issue closed",
                    kind="orphan_closed", target=ref)
            elif action == "escalated":
                await self._ctx.emit(
                    "info", f"{ref}: orphaned claim — merged PR found, but auto-close "
                    "blocked (action/ops or docs-only); escalated to a human",
                    kind="orphan_escalated", target=ref)
            else:
                await self._ctx.emit(
                    "info", f"{ref}: orphaned claim released back to the pool",
                    kind="orphan_reclaimed", target=ref)

    async def _maybe_refresh_groups(self, config: dict) -> None:
        """Run the grouping optimizer when due: on the first enabled tick, every
        ``grouping_interval_minutes``, or on the nudge a starved refill leaves
        behind (``select_batch`` found no live plan group).

        The window stamp is taken (and the nudge cleared) UP FRONT so a failing
        refresh backs off a SHORT fixed cooldown instead of hammering the model
        every tick — the seam raises on failure rather than storing an empty
        plan over a real backlog. A 2-minute failure cooldown (rather than a
        full grouping interval) lets a transient model / connectivity blip
        recover the plan quickly while still never retrying per-tick (#666). The
        refreshed event is emitted only when the group count CHANGES, so an idle
        empty backlog doesn't flood the activity feed."""
        if self._refresh_groups is None:
            return
        now = self._clock()
        interval = max(1, int(config.get("grouping_interval_minutes") or 30)) * 60.0
        elapsed = (self._last_group_refresh is None
                   or now - self._last_group_refresh >= interval)
        # The nudge is ignored while backing off from a failure: a starved refill
        # re-nudges EVERY tick, and without this gate a persistently broken model
        # would be re-called once per tick. The backoff is a short cooldown, not a
        # full interval, so recovery is fast — a scheduled (elapsed) refresh always
        # runs regardless, clearing the gate below on success.
        backing_off = (self._group_refresh_retry_at is not None
                       and now < self._group_refresh_retry_at)
        if not (elapsed or (self._group_nudge and not backing_off)):
            return
        self._last_group_refresh = now
        self._group_nudge = False
        try:
            n = await self._refresh_groups(config)
        except Exception:
            log.warning("backlog_runner.group_refresh_failed", exc_info=True)
            self._group_refresh_retry_at = now + GROUP_REFRESH_BACKOFF_S
            if not self._group_refresh_failed_emitted:
                self._group_refresh_failed_emitted = True
                await self._ctx.emit(
                    "warn", "group plan refresh failed — retrying shortly",
                    kind="groups_refresh_failed")
            return
        self._group_refresh_retry_at = None
        self._group_refresh_failed_emitted = False
        if n != self._last_groups_emitted:
            self._last_groups_emitted = n
            await self._ctx.emit(
                "info", f"group plan refreshed — {n} group(s) planned",
                kind="groups_refreshed", detail={"groups": n})

    async def _refill(self, mode: str, config: dict) -> None:
        if self._spawn_circuit_until is not None:
            if self._clock() < self._spawn_circuit_until:
                return                      # breaker open — no spawn attempts at all
            self._spawn_circuit_until = None    # cooldown over → half-open probe
        in_flight = await self._count_in_flight()
        free = config["max_concurrent"] - in_flight
        for _ in range(max(0, free)):
            if len(self._spawns_today) >= config["max_spawns_per_day"]:
                if not self._circuit_open_emitted:
                    self._circuit_open_emitted = True
                    await self._ctx.emit("warn", "daily spawn cap reached — pausing",
                                         kind="circuit_open")
                break
            account = await self._healthy_account()
            if account is None:
                if not self._no_account_emitted:
                    self._no_account_emitted = True
                    await self._ctx.emit("info", "no healthy account — pausing spawns",
                                         kind="skipped_no_account")
                return
            self._no_account_emitted = False  # an account is available again
            batch = await self._select_batch(config)
            if batch is None:
                # Starved: empty backlog OR no live plan group. Either way the
                # grouping optimizer is the answer — nudge it so the next tick
                # refreshes the plan (WAIT-for-the-grouper; the runner never
                # falls back to choosing issues itself).
                self._group_nudge = True
                return
            fails_retry = self._spawn_backoff.get(batch.primary)
            if fails_retry is not None and self._clock() < fails_retry[1]:
                # The selected target is cooling down after a failed spawn.
                # break, not continue: _select_batch would hand us the same
                # highest-priority batch again and spin this for-loop.
                break
            st = await self._ctx.get_target_state(batch.primary)
            if st.get("gave_up"):
                continue
            if mode == "propose":
                # RECORDS-BEFORE-LABELS: write the durable claim BEFORE the gh
                # label write so a crash after the label lands still leaves a
                # recoverable row. The ``proposed`` flag marks this as an
                # intentional pending-tap claim, distinguishing it from the
                # crash-between-claim-and-spawn leak (no session_id, no proposed)
                # which reconciliation treats as a dangler and releases.
                await self._ctx.set_target_state(batch.primary, {
                    "repo": batch.repo,
                    "attempts": st.get("attempts", 0),
                    "issues": batch.issues,
                    "claimed": True,
                    "proposed": True,
                })
                if not await self._claim(batch):
                    # Never actually held the claim → drop the speculative row.
                    await self._ctx.delete_target_state(batch.primary)
                    continue  # lost the race / already claimed
                await self._ctx.emit("info", f"claimed {batch.primary}",
                                     kind="claimed", target=batch.primary,
                                     detail={"issues": batch.issues})
                await self._ctx.emit("info", f"ready to implement {batch.primary}",
                                     kind="selected", target=batch.primary,
                                     detail={"issues": batch.issues})
                continue  # spawn happens on a user tap (Plan B endpoint)

            # RECORDS-BEFORE-LABELS: write the durable claim (issues + a
            # ``claimed`` marker) BEFORE the gh label write, so a crash after the
            # label lands still leaves a recoverable state row (reconciliation
            # sees a claimed ref with a state row and leaves it; a claim with no
            # row and no live session is the dangling case it clears).
            await self._ctx.set_target_state(batch.primary, {
                "repo": batch.repo,
                "attempts": st.get("attempts", 0),
                "issues": batch.issues,
                "claimed": True,
            })
            if not await self._claim(batch):
                # Never actually held the claim → drop the speculative state row.
                await self._ctx.delete_target_state(batch.primary)
                continue  # lost the race / already claimed
            await self._ctx.emit("info", f"claimed {batch.primary}",
                                 kind="claimed", target=batch.primary,
                                 detail={"issues": batch.issues})
            try:
                rec = await self._spawn(batch, account)
            except Exception as exc:
                # Spawn/clone failed → RELEASE the claim (remove in-progress only,
                # NO needs-human) and do NOT consume an attempt or a daily-cap slot
                # (the batch re-enters the pool for a later tick). A claimed batch
                # must never leak on failure — but it must also not be retried
                # every tick: back off per target, and open the global breaker on
                # disk exhaustion (retrying ENOSPC is guaranteed-futile churn) or
                # repeated consecutive failures.
                log.warning("backlog_runner.spawn_failed target=%s", batch.primary,
                            exc_info=True)
                fails = self._spawn_backoff.get(batch.primary, (0, 0.0))[0] + 1
                delay = _spawn_backoff_delay(fails)
                self._spawn_backoff[batch.primary] = (fails, self._clock() + delay)
                self._consec_spawn_failures += 1
                msg = str(exc)
                disk = any(m in msg for m in DISK_EXHAUSTION_MARKERS)
                if disk or self._consec_spawn_failures >= SPAWN_CIRCUIT_THRESHOLD:
                    self._spawn_circuit_until = self._clock() + SPAWN_CIRCUIT_COOLDOWN_S
                    reason = ("disk exhaustion" if disk else
                              f"{self._consec_spawn_failures} consecutive spawn failures")
                    await self._ctx.emit(
                        "warn",
                        f"spawn circuit open ({reason}) — pausing spawns "
                        f"{int(SPAWN_CIRCUIT_COOLDOWN_S // 60)}m: {msg.splitlines()[0][:200]}",
                        kind="spawn_circuit_open")
                await self._release(batch.primary)
                await self._ctx.emit(
                    "info",
                    f"released {batch.primary} (spawn failed; retry in {int(delay)}s)",
                    kind="unclaimed", target=batch.primary)
                await self._ctx.delete_target_state(batch.primary)
                if self._spawn_circuit_until is not None:
                    break
                continue
            self._spawn_backoff.pop(batch.primary, None)
            self._consec_spawn_failures = 0
            self._spawns_today.append(self._clock())
            # NB: ``attempts`` is charged when a session ENDS without a mergeable
            # PR (see ``_sweep_one``), never at spawn time — a successful handoff
            # deletes the state row, so success costs no attempt. Update the
            # earlier-written claim row with the spawned session id/branch.
            await self._ctx.set_target_state(batch.primary, {
                "session_id": getattr(rec, "id", None),
                "repo": batch.repo,
                "branch": getattr(rec, "branch", None),
                "attempts": st.get("attempts", 0),
                "issues": batch.issues,
                "claimed": True,
            })

    def _prune_day(self) -> None:
        """Drop spawn timestamps older than one rolling day (injected clock)."""
        cutoff = self._clock() - _DAY_SECONDS
        self._spawns_today = [t for t in self._spawns_today if t > cutoff]

    # -- sweep (handoff + terminal-watch) ----------------------------------- #

    async def _sweep(self, config: dict) -> None:
        states = await self._ctx.list_target_states()
        gave_up_targets = await self._pr_doctor_gave_up_targets()
        for target, st in states.items():
            if st.get("gave_up"):
                continue  # terminal — leave the guard row until re-armed
            try:
                await self._sweep_one(target, st, config, gave_up_targets)
            except Exception:  # pragma: no cover — one target must not stall the rest
                log.warning("backlog_runner.sweep_target_failed target=%s", target,
                            exc_info=True)

    async def _sweep_one(self, target: str, st: dict, config: dict,
                         gave_up_targets: set[str]) -> None:
        repo = target.split("#", 1)[0]
        info = await self._pr_status_for(target)

        # A PR that already failed close-out linkage verification (#642): the
        # branch→PR association is a shared-clone misattribution, so the PR is
        # NOT this batch's — treat it as "no PR at all" for every decision
        # below (close-out, give-up, handoff).
        if info is not None and info.number in (st.get("unlinked_prs") or []):
            info = None

        # PR merged → the work landed; close out the batch's issues (the PR may
        # not have carried a `Fixes #N`, leaving them open and label-excluded
        # forever), then free the slot and drop the guard row. The close-out
        # verifies PR↔issue linkage and FAILS CLOSED (#642).
        if info is not None and info.state == "merged":
            outcome = "closed"
            if self._close_out is not None:
                outcome = await self._close_out(target, info) or "closed"
            if outcome == "retry":
                return  # linkage unverifiable this sweep — try again next tick
            if outcome == "unlinked":
                # Verified misattribution: purge the poisoned branch snapshot
                # and denylist the PR number, or the stale association would
                # re-trigger this bogus close-out on every sweep and re-claim
                # (the #642 30-second re-close loop). The row survives — the
                # batch is not done.
                new_st = {k: v for k, v in st.items() if k != "branch"}
                bad = list(new_st.get("unlinked_prs") or [])
                if info.number not in bad:
                    bad.append(info.number)
                new_st["unlinked_prs"] = bad
                await self._ctx.set_target_state(target, new_st)
                await self._ctx.emit(
                    "warn",
                    f"{target}: merged PR #{info.number} has no linkage to the "
                    f"batch — ignored (misattributed branch)",
                    kind="unlinked_pr", target=target)
                return
            await self._ctx.emit("info", f"{target} merged", kind="merged", target=target)
            await self._ctx.delete_target_state(target)
            return

        # PR-Doctor gave up on this target, or the PR closed unmerged → give up.
        closed_unmerged = info is not None and info.state == "closed"
        if target in gave_up_targets or closed_unmerged:
            reason = "pr-doctor gave up" if target in gave_up_targets else "PR closed unmerged"
            await self._ctx.emit("warn", f"{target}: {reason} — needs human",
                                 kind="gave_up", target=target)
            await self._needs_human(target)
            await self._ctx.delete_target_state(target)
            return

        # PR reached the handoff point (PR Doctor carries it to auto-merge).
        # Mark the row handed_off instead of deleting it: the merged close-out
        # above must still run when the PR lands. Deleting here orphaned the
        # batch's ``in-progress`` labels whenever the merged PR lacked a
        # closing keyword — the labels never came off and the issues were
        # excluded from the candidate pools forever. The slot still frees now:
        # the orchestrator's in-flight count skips handed_off rows.
        if info is not None:
            active = await self._pr_doctor_active(repo)
            if should_free_slot(info, active):
                if not st.get("handed_off"):
                    new_st = dict(st)
                    new_st["handed_off"] = True
                    await self._ctx.set_target_state(target, new_st)
                    await self._ctx.emit("info", f"{target} handed off to PR Doctor",
                                         kind="handoff", target=target)
                return

        # No mergeable PR and the runner session ended → count an attempt; give up
        # once we've burned through max_attempts. The attempt is charged HERE (on
        # a session ending without a mergeable PR), never at spawn time. A
        # handed_off row is exempt: its session is long gone by design, and a
        # transient PR-lookup miss must not read as "ended with no PR".
        if info is None and not st.get("handed_off") and not await self._session_alive(st):
            attempts = st.get("attempts", 0) + 1
            if attempts >= config["max_attempts"]:
                if not await self._session_ever_ran(st):
                    # The session never ran a single turn (no claude_session_id) —
                    # its queue entry was lost and the reaper ended it, or it died
                    # before turn one (#1100). Escalating that to ``needs-human``
                    # parks a perfectly good issue for ``needs_human_retry_days`` on
                    # evidence of nothing: no diff was attempted, no failure was
                    # observed. RELEASE the claim instead so the issue returns to the
                    # fresh pool and gets a real attempt.
                    await self._release(target)
                    await self._ctx.emit(
                        "info",
                        f"released {target} (session never ran a turn — not escalating)",
                        kind="unclaimed", target=target)
                    await self._ctx.delete_target_state(target)
                    return
                await self._ctx.emit("warn", f"{target}: {attempts} attempts, no PR — giving up",
                                     kind="gave_up", target=target)
                await self._needs_human(target)
                await self._ctx.delete_target_state(target)
            else:
                # Charge the attempt and mark this session consumed so we don't
                # re-count the SAME ended session on every subsequent sweep; a
                # later re-arm / retry path re-spawns and clears ``_ended``.
                new_st = dict(st)
                new_st["attempts"] = attempts
                new_st["_ended"] = True
                await self._ctx.set_target_state(target, new_st)

    async def _session_ever_ran(self, st: dict) -> bool:
        """Whether this target's session ever started turn one.

        FAILS SAFE to True — "we could not prove it never ran" must keep the normal
        give-up path, so a missing probe, a vanished row or a probe error can never
        silently disable escalation. Only a probe that positively answers False (a
        live row with a NULL ``claude_session_id``) diverts to the release path."""
        sid = st.get("session_id")
        if sid is None or self._session_ran is None:
            return True
        try:
            return bool(await self._session_ran(sid))
        except Exception:  # pragma: no cover — a probe failure must not stall the sweep
            log.warning("backlog_runner.session_ran_probe_failed sid=%s", sid,
                        exc_info=True)
            return True

    async def _session_alive(self, st: dict) -> bool:
        """Whether the spawned session for this target is still in flight.

        Prefers the injected ``session_ended`` probe (looks the session up by its
        recorded ``session_id`` and returns True once its status is ``ended``);
        the loop treats a target whose session is ended, or one already marked
        ``_ended`` in a prior sweep, as no longer alive. Without a probe wired we
        fall back to the durable ``_ended`` flag only."""
        sid = st.get("session_id")
        if st.get("_ended") is True:
            return False
        if sid is None:
            # No recorded session (e.g. de-dup returned an existing row without an
            # id) → conservatively alive so we don't charge a phantom attempt.
            return True
        if self._session_ended is not None:
            try:
                if await self._session_ended(sid):
                    return False
            except Exception:  # pragma: no cover — a probe failure must not stall
                log.warning("backlog_runner.session_probe_failed sid=%s", sid,
                            exc_info=True)
        return True

    async def _pr_doctor_gave_up_targets(self) -> set[str]:
        """Targets PR Doctor has emitted a ``gave_up`` event for (matched by the
        Runner's ``origin_ref`` carried in ``detail.origin_ref``)."""
        from sqlalchemy import select

        from server.db import db_session
        from server.db.models import AutomationEvent
        out: set[str] = set()
        try:
            async with db_session() as db:
                rows = (await db.execute(select(AutomationEvent).where(
                    AutomationEvent.automation_id == "pr-doctor",
                    AutomationEvent.kind == "gave_up"))).scalars().all()
            for r in rows:
                ref = (r.detail or {}).get("origin_ref") if r.detail else None
                key = ref or r.target
                if key:
                    out.add(key)
        except Exception:  # pragma: no cover — a feed read must not break the sweep
            log.warning("backlog_runner.gave_up_query_failed", exc_info=True)
        return out
