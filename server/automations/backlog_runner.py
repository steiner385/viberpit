"""The backlog-runner automation: pulls high-value issues off the backlog and
spawns implementation sessions, handing finished PRs to PR Doctor."""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
from typing import Any, Awaitable, Callable

from server.github.pr_status import PullRequestInfo

from .backlog_github import NEEDS_HUMAN_RETRY_DAYS_DEFAULT, GhRun, query_candidates
from .backlog_selection import Batch, rank_and_batch, should_free_slot
from .base import AutomationStatus, ConfigField
from .model_pick import AUTOMATION_MODEL_CHOICES
from .context import AutomationContext

log = logging.getLogger(__name__)

_DAY_SECONDS = 24 * 3600

# --------------------------------------------------------------------------- #
# One-shot LLM ranking seam                                                   #
# --------------------------------------------------------------------------- #

_RANK_SYSTEM = (
    "You are a backlog prioritizer. You receive a list of GitHub issues and "
    "return them ordered by implementation priority — highest value, lowest "
    "risk first. When asked for a coherent group you also pick issues that "
    "naturally belong in the same pull request."
)


def _default_sdk_query_fn():
    from claude_agent_sdk import query  # lazy import — not available in tests
    return query


async def rank_issues(
    candidates: list[dict],
    config: dict,
    *,
    _query_fn=None,
    _config_dir: str | None = None,
    _cwd: str | None = None,
) -> list[str]:
    """One-shot Haiku query that returns candidate refs ordered by priority.

    When ``config["max_issues_per_pr"] > 1`` the prompt asks the model to
    return a COHERENT group (the best issue plus genuinely related ones) first,
    so ``rank_and_batch`` can slice off the top-K for a bundled PR.

    Parameters
    ----------
    candidates:
        List of dicts with ``"ref"`` (str), ``"number"`` (int), and
        ``"title"`` (str | None).
    config:
        Automation config dict (needs ``"max_issues_per_pr"``).
    _query_fn:
        Injected claude-agent-sdk ``query`` callable — tests pass a canned
        function here so the real model is NEVER called in tests.
    _config_dir:
        ``CLAUDE_CONFIG_DIR`` for the one-shot session.  Defaults to the
        current process env (fine for production; tests inject a canned fn).
    _cwd:
        Working directory for the one-shot session.  Defaults to ``os.getcwd()``.

    Returns
    -------
    list[str]
        Ordered list of refs.  On any failure returns ``[]`` (rank_and_batch
        falls back to lowest-number ordering).
    """
    if not candidates:
        return []

    max_k = max(1, int(config.get("max_issues_per_pr") or 1))

    lines = "\n".join(
        f"{c['ref']}  {c.get('title') or '(no title)'}" for c in candidates
    )
    if max_k > 1:
        body = (
            f"Return the {max_k} most coherent issues to implement together "
            f"(highest-value primary issue first, then genuinely related ones), "
            f"then the remaining issues in priority order.\n\n"
            f"Issues:\n{lines}\n\n"
            f"Reply with ONLY the issue refs, one per line, in order. "
            f"No explanation, no preamble."
        )
    else:
        body = (
            f"Order these issues by implementation priority "
            f"(highest value / lowest risk first).\n\n"
            f"Issues:\n{lines}\n\n"
            f"Reply with ONLY the issue refs, one per line, in order. "
            f"No explanation, no preamble."
        )

    query_fn = _query_fn or _default_sdk_query_fn()
    config_dir = _config_dir or os.environ.get("CLAUDE_CONFIG_DIR", "")
    cwd = _cwd or os.getcwd()

    # rank_issues is awaited directly by ``select_batch`` (itself awaited by the
    # loop's ``tick()``), so we run inside the caller's event loop — no
    # ``asyncio.run`` / ThreadPoolExecutor nesting.
    try:
        from claude_agent_sdk import ClaudeAgentOptions

        options = ClaudeAgentOptions(
            model="haiku",
            system_prompt=_RANK_SYSTEM,
            cwd=cwd,
            env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir},
            allowed_tools=[],
            setting_sources=[],
        )
        text_parts: list[str] = []
        async for msg in query_fn(prompt=body, options=options):
            content = getattr(msg, "content", None)
            if not isinstance(content, list):
                continue
            for block in content:
                t = getattr(block, "text", None)
                if isinstance(t, str):
                    text_parts.append(t)
        raw = "".join(text_parts).strip()
    except Exception as exc:
        log.warning("rank_issues: model call failed: %s", exc)
        return []

    known = {c["ref"] for c in candidates}
    refs: list[str] = []
    for line in raw.splitlines():
        ref = line.strip()
        if ref in known:
            refs.append(ref)
    return refs


# --------------------------------------------------------------------------- #
# select_batch assembly (query → rank → batch)                               #
# --------------------------------------------------------------------------- #

async def select_batch(
    config: dict,
    *,
    query: Callable[..., list[dict]] = query_candidates,
    rank: Callable[..., Any] = rank_issues,
    gh_run: "GhRun | None" = None,
    repo_load: "dict[str, int] | None" = None,
) -> "Batch | None":
    """Assemble a ``Batch`` from live GitHub candidates ranked by the LLM.

    Parameters
    ----------
    config:
        Automation config dict.
    query:
        Injectable candidate-discovery callable (default: ``query_candidates``).
        Signature: ``(config, *, gh_run) -> list[dict]``.
    rank:
        Injectable ranking callable (default: ``rank_issues``).
        Signature: ``(candidates, config) -> list[str]`` and may be async
        (``rank_issues`` is a coroutine); the result is awaited inside the
        caller's event loop — no nested loops.  Tests inject a canned (sync or
        async) function so the real model is NEVER called.
    gh_run:
        Injectable GitHub API runner (forwarded to ``query``).
    repo_load:
        Optional ``{repo: active_run_count}`` map. When given, the primary is
        biased toward the repo with the FEWEST active runs (per-repo fairness) so
        a big backlog in one repo can't monopolise every slot. Absent/empty →
        selection is exactly the LLM ranking, unchanged.

    Returns
    -------
    ``Batch`` or ``None`` when there are no candidates.
    """
    from ccswitch import gitrepos

    _gh_run = gh_run if gh_run is not None else gitrepos._run_gh
    candidates = query(config, gh_run=_gh_run)
    if not candidates:
        return None

    # Pre-resolve the ranking (awaiting an async seam like ``rank_issues``) so
    # the pure ``rank_and_batch`` stays sync.  ``rank_and_batch`` skips ranking
    # entirely for a single candidate, so only invoke ``rank`` when it matters.
    ranked: list[str] = []
    if len(candidates) > 1:
        result = rank(candidates, config)
        if inspect.isawaitable(result):
            result = await result
        ranked = list(result or [])

    ranking: Callable[..., list[str]] = lambda cs: ranked  # noqa: E731
    if repo_load:
        # Per-repo fairness: STABLE-reorder the effective priority order (ranked,
        # else lowest-number fallback) by ascending active-run count, so the
        # lightest repo's highest-priority issue becomes the primary while
        # priority WITHIN a repo is preserved. Resolve the fallback here (not in
        # rank_and_batch) so fairness also applies when the LLM ranking is empty.
        by_ref = {c["ref"]: c for c in candidates}
        order = [r for r in ranked if r in by_ref] or [
            c["ref"] for c in sorted(candidates, key=lambda c: c["number"])]
        order = sorted(order, key=lambda ref: repo_load.get(by_ref[ref]["repo"], 0))
        ranking = lambda cs, _o=order: _o  # noqa: E731

    return rank_and_batch(
        candidates,
        max(1, int(config.get("max_issues_per_pr") or 1)),
        ranking,
    )


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
        ConfigField(key="max_concurrent", label="Max sessions in flight", type="int", default=1,
                    help="Parallel implementing sessions allowed at once."),
        ConfigField(key="max_spawns_per_day", label="Max spawns per day", type="int", default=10,
                    help="Circuit-breaker: pause spawning past this many per day."),
        ConfigField(key="max_attempts", label="Max attempts per issue", type="int", default=2),
        ConfigField(key="max_issues_per_pr", label="Max issues per PR", type="int", default=1,
                    help="Bundle up to this many related issues into one PR."),
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
    ]

    def status(self, ctx) -> AutomationStatus:
        counts = getattr(ctx, "backlog_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        n = counts.get("in_flight", 0)
        return AutomationStatus(
            text=f"{n} implementing, {counts.get('eligible', 0)} eligible"
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
# True count of eligible, unclaimed OPEN issues — the honest backlog gauge for
# the Automation card, independent of how many runner slots are free.
CountCandidates = Callable[[], Awaitable[int]]
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
# (and label-excluded) forever.
CloseOut = Callable[[str, "PullRequestInfo"], Awaitable[None]]
SessionEnded = Callable[[str], Awaitable[bool]]
# Reconciliation seam: given the durably-claimed refs and the live origin_refs
# (non-terminal runner sessions), release any dangling claim to the pool.
Reconcile = Callable[[list[str], "set[str]"], Awaitable[None]]
# GitHub-truth orphan healing: (config, tracked_refs) -> [(ref, action), ...]
# where action is "closed" (merged PR found → issue closed) or "reclaimed"
# (stale lock released back to the pool).
HealOrphans = Callable[[dict, "set[str]"], Awaitable["list[tuple[str, str]]"]]

# The heal pass costs a gh search plus up to `limit` cross-ref lookups, so it
# runs at most once per interval — orphans are a slow leak, not a hot path.
_ORPHAN_SWEEP_INTERVAL = 3600.0


async def _stub_select_batch(_config: dict) -> Batch | None:
    # TODO(Plan B): label-gated `gh` query → rank_and_batch. Until then the loop
    # is wired but never selects a candidate, so no session is ever spawned.
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
        reconcile: Reconcile | None = None,
        count_candidates: CountCandidates | None = None,
        close_out: CloseOut | None = None,
        heal_orphans: HealOrphans | None = None,
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
        self._reconcile = reconcile                 # (claimed_refs, live_refs) -> None
        self._count_candidates = count_candidates    # -> int (true eligible backlog size)
        self._close_out = close_out                 # (target, info) -> close merged batch issues
        self._heal_orphans = heal_orphans               # GitHub-truth orphan healing
        self._last_orphan_sweep: float | None = None    # monotonic; throttles the heal pass
        self._lock = asyncio.Lock()
        self._spawns_today: list[float] = []        # spawn timestamps within the rolling day
        self._circuit_open_emitted = False          # debounce the circuit_open event
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
        if mode != "off":
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
        # reports real depth). Best-effort — a query failure reports 0.
        eligible = 0
        if self._count_candidates is not None:
            try:
                eligible = await self._count_candidates()
            except Exception:  # pragma: no cover — a count read must not stall the tick
                eligible = 0
        setattr(self._ctx, "backlog_counts", {
            "in_flight": in_flight,
            "eligible": eligible,
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
            else:
                await self._ctx.emit(
                    "info", f"{ref}: orphaned claim released back to the pool",
                    kind="orphan_reclaimed", target=ref)

    async def _refill(self, mode: str, config: dict) -> None:
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
                return  # no candidates
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
            except Exception:
                # Spawn/clone failed → RELEASE the claim (remove in-progress only,
                # NO needs-human) and do NOT consume an attempt or a daily-cap slot
                # (the batch re-enters the pool for a later tick). A claimed batch
                # must never leak on failure.
                log.warning("backlog_runner.spawn_failed target=%s", batch.primary,
                            exc_info=True)
                await self._release(batch.primary)
                await self._ctx.emit("info", f"released {batch.primary} (spawn failed)",
                                     kind="unclaimed", target=batch.primary)
                await self._ctx.delete_target_state(batch.primary)
                continue
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

        # PR merged → the work landed; close out the batch's issues (the PR may
        # not have carried a `Fixes #N`, leaving them open and label-excluded
        # forever), then free the slot and drop the guard row.
        if info is not None and info.state == "merged":
            if self._close_out is not None:
                await self._close_out(target, info)
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
