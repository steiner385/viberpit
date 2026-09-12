"""The backlog-runner automation: pulls high-value issues off the backlog and
spawns implementation sessions, handing finished PRs to PR Doctor."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Protocol, cast

from ccswitch.worktrees import DISK_EXHAUSTION_MARKERS
from server.github.pr_status import PullRequestInfo
from server.pm.queue import NEEDS_HUMAN_RETRY_DAYS_DEFAULT
from server.pm.queue_store import parse_iso_utc

from .backlog_grouping import load_plan, mark_consumed, plan_to_batch
from .backlog_selection import SELECTION_STRATEGIES, Batch, should_free_slot
from .base import AutomationStatus, ConfigField
from .context import AutomationContext
from .engine_policy import EnginePolicy
from .product_policy import effective_policy, is_on

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
# How long a freshly written claim is trusted to be MID-SPAWN rather than leaked.
# ``_reconcile_dangling`` reads "claimed, no session_id" as the
# crash-between-claim-and-spawn leak, which is sound for this loop (claim and
# spawn happen in one tick that cannot interleave with itself) but NOT for
# ``backlog_runner_dispatch``: a manual dispatch is a separate task, so a tick
# firing during its spawn would reap a live claim — the dispatch's own state
# UPDATE then matches 0 rows (``StaleDataError`` → 500) or, worse, the issue
# returns to the pool and a later tick double-spawns it. The window only has to
# cover one spawn (account health check + worktree setup), and its whole cost is
# that a genuinely crashed claim holds its concurrency slot this much longer.
CLAIM_GRACE_S = 120.0


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
# select_batch (plan consumption: read the priority queue → pop a plan group) #
# --------------------------------------------------------------------------- #

async def select_batch(
    config: dict,
    *,
    query: Callable[[dict], Awaitable[list[dict]]],
    repo_load: "dict[str, int] | None" = None,
    paused_repos: "set[str] | None" = None,
    weights: "dict[str, float] | None" = None,
    caps: "dict[str, int] | None" = None,
    repo_order: "list[str] | None" = None,
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
        The candidate pool, awaited: ``async (config) -> list[dict]``. The
        orchestrator injects the global priority queue
        (``_backlog_query_queue``), so the pool is one ranking over the mirrored
        Projects v2 board rather than a live GitHub label search (spec 003 §7a).
    repo_load:
        Optional ``{repo: active_run_count}`` map. When given, live plan groups
        are stable-reordered toward the repo with the FEWEST active runs
        (per-repo fairness) so a big backlog in one repo can't monopolise every
        slot; plan priority is preserved within a repo.
    paused_repos:
        Repos to skip this selection (stale git credentials, issue #670) — their
        clone can only fetch a stale ref, so branching a worktree off it would
        implement against ancient code. Other repos are still serviced.
    weights / caps / repo_order:
        The per-repo strategy inputs — explicit parameters, never config reads
        (the config keys that used to carry them are retired). The orchestrator
        derives ``weights``/``repo_order`` from the PRODUCT policies
        (``_backlog_select_batch``: each ON product's weight, applied to its repos,
        ordered by product age). It passes no ``caps``: a product's
        ``max_in_flight`` is enforced per PRODUCT in the loop's ``_refill``, where
        it is exact and strategy-independent, rather than aliased onto each of its
        repos. ``caps`` remains part of this pure selection contract for the
        ``concurrency-cap`` strategy.
    load_plan / mark_consumed:
        Injectable plan-store seams (default: the ``backlog_groups`` table).
        Tests pass canned functions so no DB or model is ever touched here.

    Returns
    -------
    ``Batch`` or ``None`` when there are no candidates or no live plan group.
    """
    candidates = await query(config)
    if not candidates:
        return None

    plan = await load_plan()
    # The user-selectable per-repo strategy rides on the (runner-wide) config; its
    # per-repo params come from the product policies via the caller.
    batch, group_id = plan_to_batch(
        plan, candidates, repo_load, paused_repos,
        strategy=config.get("selection_strategy") or "balanced",
        repo_order=repo_order or [], weights=weights or {}, caps=caps or {})
    if batch is not None and group_id is not None:
        try:
            await mark_consumed(group_id)
        except Exception:  # pragma: no cover — a UI stamp must never block a spawn
            log.warning("backlog_runner.mark_consumed_failed group=%s", group_id,
                        exc_info=True)
    return batch


def _on_products(products: dict) -> list[tuple[str, dict]]:
    """The per-product count rows whose dispatch is on, slug-sorted.

    ONE reading of "which products is this runner working" for the card — the
    shared ``product_policy.is_on`` the queue scope and the selection knobs use."""
    return sorted((slug, p) for slug, p in products.items() if is_on(p))


# "this tick never reached the count seam" — a THIRD state, distinct from the two
# the seam itself can report: ``None`` ("the read failed, we don't know") and ``{}``
# ("the read worked and no product is opted in"). Three different sentences on the
# card, so the absent case needs its own marker rather than sharing ``None``.
_NO_COUNT_YET = object()


#: The global runner modes that existed BEFORE policy phase 1, when propose-vs-auto
#: was one fleet-wide switch. The vocabulary is ``off | on`` now (``modes`` below)
#: and these two fold to ``on``; the pre-fold value is what the one-shot seed turns
#: into each product's ``dispatch``.
LEGACY_GLOBAL_MODES: tuple[str, ...] = ("propose", "auto")


def fold_global_mode(mode: str) -> str:
    """A stored global mode in the phase-1 vocabulary (``off | on``).

    ONE fold, shared by the read path (``_backlog_read_config``, so an enabled
    pre-flip row keeps running) and the seed's row migration (which rewrites the
    stored value so the fold stops being load-bearing). A stored ``off`` survives
    verbatim: it is a human's explicit "parked"."""
    return "on" if mode in LEGACY_GLOBAL_MODES else mode


def is_in_flight(state: dict) -> bool:
    """Whether one durable target state still holds a runner slot.

    THE predicate for "this row occupies a slot", shared by the fleet-wide count
    (``SessionOrchestrator._backlog_count_in_flight``, the ``max_concurrent``
    gate) and the per-product one below (the ``max_in_flight`` gate and the
    card's per-product gauge). Two ledgers over the same rows with two different
    row filters is the shape where a product reads "2/2 in flight" while the
    fleet counts 3 and neither number is wrong on its own terms.

    A claim counts from the moment the row is written — a ``proposed`` row
    awaiting a human tap holds the slot exactly like a spawned one — until the
    sweep frees it: a ``gave_up`` row waits on a human and a ``handed_off`` one
    belongs to PR Doctor, so neither holds a slot. Nothing else is asked of the
    row: the row EXISTING is the claim (Phase D), so an extra ``claimed`` test
    would only restate that while quietly dropping any row a future write path
    forgets the flag on — under-counting a ceiling, i.e. over-spawning.
    """
    return not state.get("gave_up") and not state.get("handed_off")


def in_flight_by_product(states: dict) -> dict[str, int]:
    """``{product slug: live claims}`` over the runner's durable target states.

    ``is_in_flight`` per row, bucketed by the product recorded ON the claim. A
    row with no product (a pre-flip claim) counts toward the fleet-wide budget
    but against no product's ceiling — there is no product to charge it to.
    """
    out: dict[str, int] = {}
    for st in states.values():
        if not is_in_flight(st):
            continue
        slug = st.get("product")
        if slug:
            out[slug] = out.get(slug, 0) + 1
    return out


class BacklogRunner:
    id = "backlog-runner"
    name = "Backlog Runner"
    work_class = "development"
    description = "Continuously implements high-priority backlog issues into reviewed-ready PRs."
    # Phase 1 of the product automation policy (spec §5): propose-vs-auto is a
    # PRODUCT decision now (``Project.automation_policy["dispatch"]``), so the
    # global knob only says whether the runner runs at all. A stored ``propose`` /
    # ``auto`` row folds to ``on`` in ``_backlog_read_config``.
    modes: tuple[str, ...] = ("off", "on")
    # Audited engine ceiling (Phase 7, Task 7.1; widened Task 7.2/#1264). The
    # offline acceptance suite (server/tests/test_backlog_runner_codex_acceptance.py)
    # passes: prompt contract, claim/spawn/reconcile state machine, refusal
    # handling, and shared-account concurrency are all covered. The suite's own
    # last item — a live Codex session landing a real PR in a disposable repo —
    # is explicitly NOT covered here; the owner reviewed that gap and accepted
    # it to widen this ceiling now rather than block on it (#1264). Two
    # independent gates still stand between this and a live Codex launch:
    # CCSWITCH_CODEX_AUTOMATIONS_ENABLED (default false, global kill switch),
    # this automation's own `engine` config opt-in (default "claude").
    engine_policy: EnginePolicy = EnginePolicy.CODEX_ALLOWED
    config_schema = [
        ConfigField(key="selection_strategy", label="Repo selection strategy",
                    type="enum", default="balanced",
                    choices=list(SELECTION_STRATEGIES),
                    help="How a freed slot chooses among the repos of the products "
                         "with work. 'balanced': fewest in-flight runs first (equal "
                         "rotation). 'priority': serve products in the order they "
                         "were onboarded, oldest first. 'weighted': bias by the "
                         "product's policy weight (a freed slot favors "
                         "higher-weighted products; weight 0 pauses one). "
                         "'concurrency-cap': never exceed a product's max in "
                         "flight."),
        ConfigField(key="max_concurrent", label="Max sessions in flight", type="int", default=1,
                    help="Parallel implementing sessions allowed at once, across "
                         "every product."),
        ConfigField(key="max_spawns_per_day", label="Max spawns per day", type="int", default=10,
                    help="Circuit-breaker: pause spawning past this many per day."),
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
                    help="Reconsider escalated (needs-human) issues this many days "
                         "after the runner gave up on them. 0 = never retry "
                         "(permanent handoff)."),
    ]
    # ``eligible_label`` gated the old GitHub label search; the runner now selects
    # from the board-mirror queue (spec 003 §7a), where board status — not a label
    # — says what is workable, so the key has no equivalent to migrate to. The rest
    # moved to the PRODUCT policy (``server/automations/product_policy.py``): which
    # products to work, each one's board reach, in-flight cap, weight, attempt cap,
    # session model and dependency rule. Listed here so the rows that still carry
    # them keep the rest of their settings (and so the one-shot seed can read the
    # old values off the raw blob once).
    retired_config_keys: tuple[str, ...] = (
        "eligible_label", "enabled_repos", "repo_weights", "repo_caps",
        "queue_min_status", "max_attempts", "default_model", "respect_dependencies")

    def status(self, ctx) -> AutomationStatus:
        counts = getattr(ctx, "backlog_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        # Per-product rows (``_backlog_count_candidates``), absent on a tick that
        # never reached the count seam. An empty map is the real, reportable state:
        # the runner is enabled but no product opted in, which as a bare
        # "0 eligible" reads like an empty backlog rather than a policy nobody set.
        products = getattr(ctx, "backlog_products", _NO_COUNT_YET)
        n = counts.get("in_flight", 0)
        if products is None:
            # The read FAILED. "We don't know" and "no product is opted in" must
            # not share a sentence: an operator debugging a broken mirror read
            # would otherwise be told their policies are unset. The in-flight
            # count comes from a different seam and is still true, so it stays.
            return AutomationStatus(
                text=f"{n} implementing · product counts unavailable "
                     f"(the queue read failed)",
                level="warn", counts=counts)
        counted = products is not _NO_COUNT_YET
        rows: dict = products if isinstance(products, dict) else {}
        if counted and not _on_products(rows):
            # ``products=[]``, never ``None``: this automation HAS products and
            # none is on. ``None`` is reserved for an automation with no concept
            # of products at all (PR Doctor), which the web renders differently —
            # an empty per-product table vs no table at all.
            return AutomationStatus(text="idle — no product has dispatch on",
                                    level="idle", counts=counts, products=[])
        level = "active" if n else ("warn" if counts.get("needs_human") else "idle")
        # ``untriaged`` is the below-min-status skip: in the default Ready mode a
        # board full of untriaged items reads as "0 eligible", which looks like an
        # empty backlog rather than one waiting on triage.
        text = (f"{n} implementing, {counts.get('eligible', 0)} eligible"
                + (f", {counts['blocked']} blocked" if counts.get("blocked") else "")
                + (f", {counts['untriaged']} untriaged" if counts.get("untriaged") else "")
                + (f", {counts['needs_human']} needs-human" if counts.get("needs_human") else ""))
        # One segment per ON product after the summary: which products this runner
        # is working, how, and how hard. Joined with " · " on ONE line because the
        # web renders ``AutomationStatus.text`` without honouring newlines.
        # ``products`` (spec §9/§10) carries the SAME rows, structured, for the
        # web's per-product table (Task 5) — the text is the human summary, this
        # is the machine one, built from one loop so they can never disagree.
        on = _on_products(rows)
        products_out = []
        for slug, p in on:
            text += (f" · {slug}: {p.get('dispatch')} · "
                     f"{p.get('in_flight', 0)}/{p.get('cap', 0)} in flight · "
                     f"{p.get('eligible', 0)} eligible")
            bounced = p.get("bounced", 0)
            if bounced:
                text += f" · {bounced} bounced"
            products_out.append({"slug": slug, "dispatch": p.get("dispatch"),
                                 "inFlight": p.get("in_flight", 0), "cap": p.get("cap", 0),
                                 "eligible": p.get("eligible", 0), "bounced": bounced})
        # Queue provenance (#990): which ranked candidate this came from, and
        # what it was picked over — same formatter the ``picked`` event uses,
        # so the card and the activity feed can never say different things.
        # Suppressed while idle: a stale pick from a prior spawn shouldn't
        # clutter a card reporting nothing in flight.
        pick = getattr(ctx, "backlog_last_pick", None)
        if pick and level != "idle":
            text += f" · {format_pick(pick)}"
        return AutomationStatus(text=text, level=level, counts=counts, products=products_out)

    async def on_state_change(self, *, enabled, mode, config, ctx) -> None:
        # The orchestrator's loop reads mode/config each tick; nothing to start here.
        ...


# --------------------------------------------------------------------------- #
# Control loop                                                                 #
# --------------------------------------------------------------------------- #

# Injected seam type aliases (the exact contract Plan B implements).
SelectBatch = Callable[[dict], Awaitable["Batch | None"]]
# Spec 003 §7a safety net: re-check the selected batch's issues against live
# GitHub right before the claim (the mirror the queue ranks from can lag GitHub
# by a sync interval). ``True`` = every issue still open, unassigned, unblocked;
# ``False`` = at least one is not; ``None`` = the check itself failed. §10: the
# loop treats ``None`` like ``False`` (fails closed) — never "ready".
VerifyReady = Callable[["Batch"], Awaitable["bool | None"]]
# Status-card queue provenance (#990): a SYNC, cheap seam returning a COPY of
# the last ranked candidate pool ``_backlog_query_queue`` produced (the
# orchestrator owns the cache — this never re-queries). ``None`` seam = no
# ``picked`` event and ``status()`` renders exactly as before.
QueueTail = Callable[[], "list[dict]"]
# Human-visible trace of a claim: one comment per issue (the claim ITSELF is the
# durable target-state row this loop writes first). Best-effort by contract —
# ``_refill`` swallows a failure rather than losing the spawn it already earned.
Announce = Callable[["Batch"], Awaitable[None]]


class Spawn(Protocol):
    """Spawn the session for a claimed batch.

    ``default_model`` is the PRODUCT's session model, taken from the SAME
    per-tick policy snapshot every other per-product decision in this refill
    used. Passed rather than re-read at the spawn site: a second read is a second
    answer (policies can change between the dispatch decision and the spawn, and
    a failed re-read silently downgraded the model to ``auto``), and the loop is
    the only place that knows which snapshot governed this claim."""

    def __call__(self, batch: "Batch", account: str, *,
                 default_model: str) -> Awaitable[Any]: ...


CountInFlight = Callable[[], Awaitable[int]]
# (eligible, blocked, untriaged, per_product): the true count of eligible,
# unclaimed OPEN issues — the honest backlog gauge for the Automation card,
# independent of how many runner slots are free — plus how many otherwise-eligible
# issues were skipped as dependency-blocked this sweep (the "why isn't the backlog
# draining" note) and how many were skipped for sitting below the product's
# ``queue_min_status`` (in the default Ready mode, an untriaged board would
# otherwise read as "0 eligible"). ``per_product`` is one row per ON product —
# ``{slug: {dispatch, eligible, blocked, untriaged, in_flight, cap}}`` — which
# ``status()`` renders as the per-product segments, or ``None`` when the read
# FAILED: "we don't know" and "no product is opted in" are different sentences on
# the card, and an empty dict would say the second.
CountCandidates = Callable[[], Awaitable["tuple[int, int, int, dict | None]"]]
HealthyAccount = Callable[[], Awaitable["str | None"]]
PrStatusFor = Callable[[str], Awaitable["PullRequestInfo | None"]]
PrDoctorActive = Callable[[str], Awaitable[bool]]
# How many session fixes PR Doctor has injected into a PR (``repo#N``), or None
# when that is UNKNOWN — a PR it never tracked, or one whose per-target row it
# already cleared at terminal. ``first_pass_merge`` is derived from it and stays
# None on an unknown count: "PR Doctor never had to act" and "we cannot tell" are
# different claims, and only the first one may be recorded as a first-pass merge.
PrDoctorAttemptsFor = Callable[[str], Awaitable["int | None"]]
ReadConfig = Callable[[], Awaitable["tuple[str, dict]"]]
# slug → the validated product policy (``effective_policy``). Read once per tick:
# the loop looks a batch's/claim's product up in it for dispatch (propose vs auto),
# the attempt cap and the session model. An unwired seam (or a product missing from
# the map) falls back to the pure schema defaults (``effective_policy(None)``) — see
# ``_policy_for`` for why that is the safe answer for the dispatch decision.
ReadPolicies = Callable[[], Awaitable["dict[str, dict]"]]
# D10 one-shot migration: carry the runner's retired global repo knobs onto the
# products that owned those repos, returning how many policies were written. Called
# ONCE per process, on the first tick, best-effort — it is a convenience, never a
# gate on the sweep that frees slots and escalates stuck work.
SeedPolicies = Callable[[], Awaitable[int]]
# GIVE-UP path: announce the handoff (the reason is what a human needs in order
# to pick the target up). Used only when a target has exhausted ``max_attempts``
# or PR Doctor gave up on it. The SIGNAL itself is the durable ``gave_up`` row
# ``_give_up`` writes — this seam only tells the outside world about it.
NeedsHuman = Callable[[str, str], Awaitable[None]]
# MERGED path: the work landed but the PR may not have carried a closing
# keyword — comment + close every issue in the batch so it can't stay open
# forever. Returns the close-out outcome: "closed" (≥1 issue verifiably linked to
# the PR and closed — proceed), "escalated" (the human gate fired and nothing was
# closed: a human must finish it, #784), "unlinked" (the PR references NO batch
# issue: a shared-clone misattribution, #642 — ignore + denylist it), or "retry"
# (linkage unverifiable — try again next sweep). ``None`` (legacy stubs) reads as
# "closed".
CloseOut = Callable[[str, "PullRequestInfo"], Awaitable["str | None"]]
SessionEnded = Callable[[str], Awaitable[bool]]
# Did the session ever run a turn? True once it recorded a ``engine_session_id``
# (the SDK assigns one when turn ONE starts), so False proves the batch was never
# actually attempted. Gates the give-up path: a session that never ran must not
# escalate its issue to ``needs-human`` (#1100).
SessionRan = Callable[[str], Awaitable[bool]]
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


def format_pick(pick: dict) -> str:
    """Render a queue-provenance pick (``{ref, score, product, over}``) as the
    ONE string both the ``picked`` event summary and ``status()`` show, so the
    two can never drift (#990). ``score``/``product`` are omitted from the
    parenthetical when ``None`` (or, for ``product``, empty); the "over ..."
    clause is omitted entirely when ``over`` is empty."""
    parts = []
    score = pick.get("score")
    if score is not None:
        parts.append(f"score {score:g}")
    product = pick.get("product")
    if product:
        parts.append(f"product {product}")
    paren = f" ({', '.join(parts)})" if parts else ""
    over = pick.get("over") or []
    over_str = f" over {', '.join(over)}" if over else ""
    return f"picked {pick.get('ref')}{paren}{over_str}"


async def _stub_select_batch(_config: dict) -> Batch | None:
    # Fallback when no selection seam is wired: the loop runs but never selects
    # a candidate, so no session is ever spawned.
    return None


async def _stub_needs_human(_target: str, _reason: str) -> None:
    # GIVE-UP fallback: announce nothing; the durable row still carries the handoff.
    return None


class BacklogRunnerLoop:
    """A restart-safe, non-reentrant control loop that keeps up to
    ``max_concurrent`` implementation sessions in flight.

    Every I/O dependency is INJECTED (selection/announce/spawn/counts/accounts/PR
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
        spawn: Spawn,
        count_in_flight: CountInFlight,
        healthy_account: HealthyAccount,
        pr_status_for: PrStatusFor,
        pr_doctor_active: PrDoctorActive,
        read_config: ReadConfig,
        clock: Callable[[], float],
        read_policies: ReadPolicies | None = None,
        seed_policies: SeedPolicies | None = None,
        announce: Announce | None = None,
        needs_human: NeedsHuman | None = None,
        session_ended: SessionEnded | None = None,
        session_ran: SessionRan | None = None,
        pr_doctor_attempts_for: PrDoctorAttemptsFor | None = None,
        count_candidates: CountCandidates | None = None,
        close_out: CloseOut | None = None,
        refresh_groups: RefreshGroups | None = None,
        credential_alerts: CredentialAlerts | None = None,
        verify_ready: VerifyReady | None = None,
        queue_tail: QueueTail | None = None,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ctx = ctx
        self._select_batch = select_batch          # Plan B: -> Batch | None
        self._verify_ready = verify_ready           # (batch) -> bool | None; None seam = no check
        self._queue_tail = queue_tail               # sync: () -> cached ranked pool copy; None = no picked event
        self._announce = announce                   # (batch) -> None; None seam = no announce
        self._spawn = spawn                         # (batch, account) -> SessionRecord
        self._count_in_flight = count_in_flight     # DB: origin==id, status!=ended
        self._healthy_account = healthy_account     # -> account name | None
        self._pr_status_for = pr_status_for         # (primary) -> PullRequestInfo | None
        self._pr_doctor_active = pr_doctor_active   # (repo) -> bool
        self._read_config = read_config             # -> (mode, config)
        self._read_policies = read_policies         # -> {slug: policy}; None = none wired
        self._seed_policies = seed_policies         # D10 one-shot migration seam
        self._seeded = False                        # the seed has had its one chance
        self._policies: dict[str, dict] = {}        # this tick's product policies
        self._clock = clock                         # injected; no wall-clock in tests
        # WALL clock (aware UTC), distinct from ``_clock``: the give-up stamp and
        # its expiry window are absolute times that must survive a restart, so
        # they cannot ride the monotonic clock the backoffs use.
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._needs_human = needs_human or _stub_needs_human  # announce the handoff
        self._session_ended = session_ended         # (session_id) -> bool | None
        self._session_ran = session_ran             # (session_id) -> bool | None
        self._pr_doctor_attempts_for = pr_doctor_attempts_for  # (repo#N) -> int | None
        self._count_candidates = count_candidates    # -> int (true eligible backlog size)
        self._close_out = close_out                 # (target, info) -> close merged batch issues
        self._refresh_groups = refresh_groups           # grouping optimizer (plan writer)
        self._credential_alerts = credential_alerts     # -> [{repo,since,error,settingsUrl}]
        self._cred_alerted: set[str] = set()            # repos with a live credential alert
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
        self._last_pick: dict | None = None          # queue provenance of the last successful spawn (#990)

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._refill_and_sweep()
            except Exception:  # pragma: no cover — defensive: a tick must never crash the loop
                log.warning("backlog_runner.tick_failed", exc_info=True)

    # -- refill ------------------------------------------------------------- #

    def _policy_for(self, product: "str | None") -> dict:
        """The policy this batch / claim is governed by.

        ONE lookup for every per-product decision in the loop (dispatch, attempt
        cap, model), so they cannot disagree about which product owns a target.

        A product the map does not know falls back to the pure schema defaults
        (``effective_policy(None)``), i.e. dispatch ``off``. That is deliberately
        fail-SAFE on the one decision where being wrong inverts an operator's
        instruction: ``propose`` means "do not start a session without me", and a
        fallback of ``auto`` would spawn for every proposing product the moment a
        policy read hiccuped. Nothing legitimate is stranded by it — the QUEUE is
        the off-gate, so a refill batch always carries a real ON slug — and the
        two OTHER decisions this feeds keep working for a pre-flip claim that
        carries no product: the defaults ARE the old global ``max_attempts`` (2)
        and ``default_model`` (``auto``) those claims used to run under.
        """
        if product:
            policy = self._policies.get(product)
            if policy is not None:
                return policy
        return effective_policy(None)

    async def _maybe_seed_policies(self) -> None:
        """Run the D10 migration once per process, on the first tick.

        The flag is set BEFORE the await: a seed that raises must not be retried
        every tick (it would re-read the same row and hit the same failure), and a
        seed that half-succeeded must not be run twice over the products it already
        wrote. Best-effort by contract — this never stops a tick."""
        if self._seeded or self._seed_policies is None:
            return
        self._seeded = True
        try:
            n = await self._seed_policies()
        except Exception:
            log.warning("backlog_runner.policy_seed_failed", exc_info=True)
            return
        if n:
            log.info("backlog_runner.policies_seeded count=%d", n)

    async def _refill_and_sweep(self) -> None:
        await self._maybe_seed_policies()
        mode, config = await self._read_config()
        # Every per-product decision below (dispatch, attempt cap, model) reads
        # from this one snapshot, taken once per tick so a policy edit mid-tick
        # cannot make refill and sweep act on different answers.
        #
        # A FAILED read must never reach refill on guessed policies: refill decides
        # propose-vs-auto, and guessing there either spawns for a product whose
        # operator said "ask me first" or refuses one that said "go". So the last
        # good snapshot is kept if there is one (policies change rarely; a snapshot
        # one tick old is far better than none), and with no snapshot at all refill
        # is skipped for this tick. The sweep still runs either way — its work
        # (handoffs, give-ups, reconciliation) frees slots and escalates stuck work,
        # and stalling THAT on a DB blip is its own outage.
        policies_known = True
        if self._read_policies is not None:
            try:
                self._policies = await self._read_policies()
            except Exception:
                policies_known = bool(self._policies)
                log.warning("backlog_runner.policy_read_failed (refill %s)",
                            "on the previous snapshot" if policies_known else "skipped",
                            exc_info=True)
        # Surface stale-credential alerts every tick, before refill — a paused
        # repo must both notify the dashboard and be excluded from selection.
        await self._check_credentials()
        # Expire spent give-ups BEFORE refill, not as part of the sweep that
        # follows it: on the very tick a cooldown passes, the queue already
        # offers the ref, so ``select_batch`` would consume its plan group only
        # for ``_refill``'s ``gave_up`` guard to discard the pick — burning the
        # group until the next grouping refresh. Expiring first lets the same
        # tick spawn it.
        await self._expire_give_ups(config)
        if mode != "off" and policies_known:
            # The grouping optimizer runs BEFORE refill so a cold start (or a
            # just-consumed plan) can produce groups and spawn in the same tick.
            await self._maybe_refresh_groups(config)
            self._prune_day()
            if len(self._spawns_today) < config["max_spawns_per_day"]:
                self._circuit_open_emitted = False
                await self._refill(config)
            elif not self._circuit_open_emitted:
                self._circuit_open_emitted = True
                await self._ctx.emit("warn", "daily spawn cap reached — pausing",
                                     kind="circuit_open")
        # handoff + terminal-watch runs every tick, regardless of mode.
        await self._sweep()
        # Return orphaned claims to the pool (crash-between-claim-and-spawn leak).
        await self._reconcile_dangling()
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
        # and how many sit below the configured board status (untriaged).
        # Best-effort — a query failure reports 0.
        eligible = 0
        blocked = 0
        untriaged = 0
        per_product: dict | None = None
        if self._count_candidates is not None:
            try:
                eligible, blocked, untriaged, per_product = await self._count_candidates()
            except Exception:  # pragma: no cover — a count read must not stall the tick
                eligible = blocked = untriaged = 0
                per_product = None
        setattr(self._ctx, "backlog_counts", {
            "in_flight": in_flight,
            "eligible": eligible,
            "blocked": blocked,
            "untriaged": untriaged,
            "needs_human": needs_human,
        })
        # The per-product rows behind the card's per-product segments. Kept OFF
        # ``backlog_counts`` (which stays int-only: the web types
        # ``AutomationStatus.counts`` as ``Record<string, number>``). ``None`` is
        # published DELIBERATELY when the seam reported a failed read — "we don't
        # know" is its own sentence on the card, and must not render as "no product
        # has dispatch on". With no seam wired at all the attribute stays absent,
        # which is the third state: the card keeps rendering its plain summary.
        if self._count_candidates is not None:
            setattr(self._ctx, "backlog_products", per_product)
        # Queue provenance (#990): the last successful spawn's pick, if any —
        # a plain dict, for the same typing reason.
        setattr(self._ctx, "backlog_last_pick", self._last_pick)

    async def _reconcile_dangling(self) -> None:
        """Delete claim rows that leaked (row written, session never came up).

        The claim IS the row (Phase D): deleting it is what returns the issue to
        the pool, because the priority queue excludes every ref a claim row
        holds. A claimed row that never recorded a ``session_id`` (the
        crash-between-claim-and-spawn leak) is a dangler; a row whose spawn
        succeeded (has a ``session_id``) is owned by the sweep's give-up /
        handoff bookkeeping and must be left alone so reconciliation never races
        the attempt counting. A ``handed_off`` row is likewise sweep-owned — its
        session is gone by design (PR Doctor carries the PR), so it must never
        read as a dangler; nor may a propose-mode ``proposed`` row, which is an
        intentional pending-tap claim.

        Nor, finally, may a claim written within ``CLAIM_GRACE_S``: that one is
        mid-spawn, and the spawn racing it may belong to another task entirely
        (``backlog_runner_dispatch``). A MISSING or unreadable ``claimed_at`` is
        reaped exactly as before — rows predating the stamp must stay
        collectable, or a single legacy leak would hold its slot forever."""
        try:
            states = await self._ctx.list_target_states()
        except Exception:  # pragma: no cover — a state read must not stall the tick
            return
        for target, st in states.items():
            if not st.get("claimed"):
                continue
            if (st.get("session_id") is not None or st.get("handed_off")
                    or st.get("proposed")):
                continue
            claimed_at = parse_iso_utc(st.get("claimed_at"))
            if (claimed_at is not None
                    and self._wall_clock() - claimed_at
                    < timedelta(seconds=CLAIM_GRACE_S)):
                continue
            # A leaked row holds a concurrency slot forever (count_in_flight
            # counts active rows), so dropping it is the whole release.
            await self._ctx.delete_target_state(target)

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

    async def _live_check(self, batch: "Batch") -> bool:
        """Spec 003 §7a safety net, before ANY claim (propose mode claims too):
        re-verify the batch live. Anything but ``True`` — not ready, the check
        failed, the seam raised — is a skip (§10: fails closed). The target
        cools down on the same schedule as a failed spawn so a persistently
        unverifiable pick cannot spin the loop; a later successful spawn clears
        the entry as usual."""
        assert self._verify_ready is not None
        try:
            ok = await self._verify_ready(batch)
        except Exception:
            log.warning("backlog_runner.live_check_raised target=%s", batch.primary,
                        exc_info=True)
            ok = None
        if ok is True:
            return True
        result, why = (("not_ready", "no longer ready on GitHub") if ok is False
                       else ("check_failed", "live check failed"))
        fails = self._spawn_backoff.get(batch.primary, (0, 0.0))[0] + 1
        delay = _spawn_backoff_delay(fails)
        self._spawn_backoff[batch.primary] = (fails, self._clock() + delay)
        await self._ctx.emit(
            "info" if ok is False else "warn",
            f"skipped {batch.primary}: {why} (retry in {int(delay)}s)",
            kind="skipped_live_check", target=batch.primary,
            detail={"result": result, "issues": batch.issues})
        return False

    async def _emit_pick(self, batch: "Batch") -> None:
        """Queue provenance for a successful spawn (#990): which ranked
        candidate the batch's primary was, its score/product, and the refs it
        was picked over (the next 3 tail entries not already in this batch).
        The ranked pool can have been refreshed since selection, so a primary
        missing from the tail reports score/product as ``None`` — provenance
        is best-effort and never a reason to skip or undo the spawn. Runs
        AFTER the spawn already succeeded but BEFORE the state row records
        the session id, so a raising ``_queue_tail`` (or a bad event write)
        must never escape here: it would abort ``_refill`` mid-spawn, leaving
        a live session whose state row was never finalized — exactly the
        crash-between-claim-and-spawn shape ``_reconcile_dangling`` releases
        or re-spawns. Callers guard on ``self._queue_tail`` being set."""
        try:
            # The call site already checked ``self._queue_tail is not None``;
            # ``cast`` tells mypy the same thing without a second runtime
            # check that would just duplicate the caller's guard.
            tail = cast("QueueTail", self._queue_tail)()
            entry = next((e for e in tail if e.get("ref") == batch.primary), None)
            over = [e.get("ref") for e in tail if e.get("ref") not in batch.issues][:3]
            pick = {
                "ref": batch.primary,
                "score": entry.get("score") if entry else None,
                "product": entry.get("product") if entry else None,
                "over": over,
            }
            self._last_pick = pick
            await self._ctx.emit("info", format_pick(pick), kind="picked",
                                 target=batch.primary, detail=pick)
        except Exception:  # provenance must never stall a spawn
            log.warning("backlog_runner.pick_provenance_failed target=%s", batch.primary,
                        exc_info=True)

    async def _announce_claim(self, batch: "Batch") -> None:
        """Post the claim's human-visible trace, swallowing any failure.

        The claim is already durable (the row was written just above) and the
        spawn is about to happen; a ``gh`` comment that will not post is worth a
        log line, never a lost spawn. Without this guard an announce raising here
        would abort ``_refill`` mid-claim, leaving a claimed row with no session —
        exactly the dangling shape ``_reconcile_dangling`` has to clean up."""
        if self._announce is None:
            return
        try:
            await self._announce(batch)
        except Exception:
            log.warning("backlog_runner.claim_announce_failed target=%s", batch.primary,
                        exc_info=True)

    async def _refill(self, config: dict) -> None:
        if self._spawn_circuit_until is not None:
            if self._clock() < self._spawn_circuit_until:
                return                      # breaker open — no spawn attempts at all
            self._spawn_circuit_until = None    # cooldown over → half-open probe
        in_flight = await self._count_in_flight()
        # The per-PRODUCT ceiling (``max_in_flight``), counted once up front and
        # then kept in step with every claim this refill takes. ``max_concurrent``
        # above is the fleet-wide slot budget; this is each product's share of it,
        # enforced here rather than aliased onto the product's repos — a product
        # owning N repos would otherwise run N × its cap, and the card's
        # (product-aggregated) "4/2 in flight" would be a number nothing enforced.
        try:
            per_product = in_flight_by_product(await self._ctx.list_target_states())
        except Exception:  # pragma: no cover — a state read must not stall the tick
            log.warning("backlog_runner.in_flight_by_product_failed", exc_info=True)
            per_product = {}
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
            policy = self._policy_for(batch.product)
            dispatch = policy["dispatch"]
            if dispatch == "off":
                # Flipped off (or unreadable) since the queue read that produced
                # this batch — take no claim. ``continue``, not ``break``: another
                # product's batch is still perfectly workable this tick.
                #
                # COST: ``select_batch`` already consumed this pick's plan group
                # (``mark_consumed``), so the group is burnt until the next
                # grouping refresh even though nothing was claimed. Accepted
                # deliberately: the alternative — ``break`` — parks every OTHER
                # product's work for the tick because one product was flipped off,
                # which is the worse trade on a multi-product board. A dispatch
                # flip is rare and operator-driven, so the burn is bounded.
                await self._ctx.emit(
                    "info",
                    f"skipped {batch.primary}: {batch.product or 'unknown product'} "
                    f"dispatch off",
                    kind="skipped_dispatch_off", target=batch.primary,
                    detail={"product": batch.product, "issues": batch.issues})
                continue
            # Every cap is read LITERALLY, 0 included: ``max_in_flight: 0`` is the
            # PAUSED setting (schema ``min=0``), not an "unlimited" sentinel — a
            # product an operator parked at 0 must take no claim while keeping the
            # rest of its policy. Negative values cannot be stored (validation).
            cap = int(policy["max_in_flight"])
            if batch.product and per_product.get(batch.product, 0) >= cap:
                # Same plan-group burn as the dispatch-off skip above, and the same
                # reason for ``continue``: one product at its ceiling must not stop
                # the tick from serving the products that are not.
                await self._ctx.emit(
                    "info",
                    f"skipped {batch.primary}: {batch.product} is at its in-flight "
                    f"cap ({cap})",
                    kind="skipped_product_cap", target=batch.primary,
                    detail={"product": batch.product, "cap": cap,
                            "inFlight": per_product.get(batch.product, 0)})
                continue
            if self._verify_ready is not None and not await self._live_check(batch):
                # break, not continue: ``select_batch`` already consumed this
                # pick's plan group, so continuing would burn the REST of the
                # plan on what is usually one GitHub hiccup. The slot waits a
                # tick instead; the group returns at the next grouping refresh.
                break
            # Propose-vs-auto is the PRODUCT's call now (spec §5): one enabled
            # runner can announce for a product still being trusted and spawn for
            # one that is, on the same tick. Spawning is the branch that needs an
            # explicit ``auto``, never the fallthrough: announcing where a spawn
            # was wanted costs a tap, spawning where an announce was wanted is the
            # unattended session the operator asked not to have.
            if dispatch != "auto":
                # The row IS the claim (Phase D): writing it is what takes the
                # issue out of the pool, and the announce that follows is only its
                # human-visible trace — it cannot fail the claim, so there is no
                # race to lose here. The ``proposed`` flag marks this as an
                # intentional pending-tap claim, distinguishing it from the
                # crash-between-claim-and-spawn leak (no session_id, no proposed)
                # which reconciliation treats as a dangler and deletes.
                await self._ctx.set_target_state(batch.primary, {
                    "repo": batch.repo,
                    "product": batch.product,
                    "attempts": st.get("attempts", 0),
                    "issues": batch.issues,
                    "claimed": True,
                    "proposed": True,
                })
                if batch.product:
                    per_product[batch.product] = per_product.get(batch.product, 0) + 1
                await self._announce_claim(batch)
                await self._ctx.emit("info", f"claimed {batch.primary}",
                                     kind="claimed", target=batch.primary,
                                     detail={"issues": batch.issues})
                await self._ctx.emit("info", f"ready to implement {batch.primary}",
                                     kind="selected", target=batch.primary,
                                     detail={"issues": batch.issues})
                continue  # spawn happens on a user tap (Plan B endpoint)

            # RECORDS-BEFORE-ANYTHING: the durable row (issues + a ``claimed``
            # marker) IS the claim, so it is written before the announce and
            # before the spawn — a crash anywhere after this leaves a recoverable
            # row, and a row with no live session is the dangling case
            # reconciliation clears.
            await self._ctx.set_target_state(batch.primary, {
                "repo": batch.repo,
                # The product is recorded ON THE CLAIM so the sweep can read this
                # target's policy back (its attempt cap) long after the queue read
                # that produced the batch is gone.
                "product": batch.product,
                "attempts": st.get("attempts", 0),
                "issues": batch.issues,
                "claimed": True,
                "claimed_at": self._wall_clock().isoformat(),
            })
            await self._announce_claim(batch)
            await self._ctx.emit("info", f"claimed {batch.primary}",
                                 kind="claimed", target=batch.primary,
                                 detail={"issues": batch.issues})
            try:
                # The product's session model rides along from THIS tick's policy
                # snapshot — the same one that just decided this batch may spawn.
                rec = await self._spawn(batch, account,
                                        default_model=policy["default_model"])
            except Exception as exc:
                # Spawn/clone failed → RELEASE the claim (delete the row — that IS
                # the release; NO give-up row) and do NOT consume an attempt or a
                # daily-cap slot (the batch re-enters the pool for a later tick).
                # A claimed batch must never leak on failure — but it must also not be retried
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
                await self._ctx.emit(
                    "info",
                    f"released {batch.primary} (spawn failed; retry in {int(delay)}s)",
                    kind="unclaimed", target=batch.primary)
                await self._ctx.delete_target_state(batch.primary)
                if self._spawn_circuit_until is not None:
                    break
                continue
            if rec is None:
                # REFUSED, not failed: the spawn helper declined before creating
                # anything (engine policy, or an engine/permission-mode pair that
                # could never push a branch — see
                # ``engine_policy.unattended_launch_blocker``). The context has
                # already recorded why, so the only thing owed here is to undo the
                # claim — otherwise the state row holds a concurrency slot forever
                # and a later sweep escalates the target to ``needs-human`` on the
                # evidence of a session that never existed.
                #
                # A refusal is a configuration answer, not a fault: it consumes no
                # attempt and no daily-cap slot, and it does NOT count toward the
                # consecutive-failure breaker (which would also block the Claude
                # fallback an operator reaches for to fix it). It IS origin-wide,
                # so the remaining free slots would refuse identically — back the
                # target off and stop refilling this tick.
                fails = max(1, self._spawn_backoff.get(batch.primary, (0, 0.0))[0])
                self._spawn_backoff[batch.primary] = (
                    fails, self._clock() + _spawn_backoff_delay(fails))
                await self._ctx.emit(
                    "info", f"released {batch.primary} (launch refused)",
                    kind="unclaimed", target=batch.primary)
                await self._ctx.delete_target_state(batch.primary)
                break
            self._spawn_backoff.pop(batch.primary, None)
            if self._queue_tail is not None:
                await self._emit_pick(batch)
            self._consec_spawn_failures = 0
            self._spawns_today.append(self._clock())
            if batch.product:
                per_product[batch.product] = per_product.get(batch.product, 0) + 1
            # The dispatch is real now, so it gets its durable ``running`` row
            # (spec §6). Written HERE — after the spawn succeeded, before the
            # claim row is finalised — so a refusal/failure above (which costs no
            # attempt) leaves no history, and a crash below still leaves a row the
            # sweep can finalise. The model recorded is the one the session will
            # actually run under: the record's own, else this tick's policy.
            await self._record_outcome(
                batch.primary, product_slug=batch.product,
                session_id=getattr(rec, "id", None),
                model=getattr(rec, "model", None) or policy["default_model"],
                attempt_no=st.get("attempts", 0))
            # NB: ``attempts`` is charged when a session ENDS without a mergeable
            # PR (see ``_sweep_one``), never at spawn time — a successful handoff
            # deletes the state row, so success costs no attempt. Update the
            # earlier-written claim row with the spawned session id/branch.
            await self._ctx.set_target_state(batch.primary, {
                "session_id": getattr(rec, "id", None),
                "repo": batch.repo,
                "product": batch.product,
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

    async def _expire_give_ups(self, config: dict) -> None:
        """Drop ``gave_up`` rows whose needs-human window has passed.

        The row IS the needs-human handoff (Phase D), so it stays put until the
        retry window passes and DELETING it is what returns the issue to the
        pool: the queue's cooldown reads ``needs_human_at`` off this row, and
        nothing else does. ``needs_human_retry_days`` = 0 is a permanent handoff,
        never expired. An UNDATED (or unreadable) stamp expires immediately: the
        queue already treats it as no cooldown, and a row kept past that would
        park the issue in the card's needs-human gauge while it was being picked
        again.

        Runs every tick regardless of mode (like the sweep it used to live in),
        but BEFORE ``_refill`` — see the call site for why the ordering matters.
        """
        raw_days = config.get("needs_human_retry_days")
        retry_days = (NEEDS_HUMAN_RETRY_DAYS_DEFAULT if raw_days is None
                      else max(0, int(raw_days)))
        if retry_days <= 0:
            return
        states = await self._ctx.list_target_states()
        for target, st in states.items():
            if not st.get("gave_up"):
                continue
            at = parse_iso_utc(st.get("needs_human_at"))
            if at is None or self._wall_clock() - at >= timedelta(days=retry_days):
                await self._ctx.delete_target_state(target)
                await self._ctx.emit(
                    "info", f"{target}: needs-human window passed — back in the pool",
                    kind="needs_human_expired", target=target)

    async def _sweep(self) -> None:
        states = await self._ctx.list_target_states()
        gave_up_targets = await self._pr_doctor_gave_up_targets()
        for target, st in states.items():
            if st.get("gave_up"):
                # Terminal: a row still here survived ``_expire_give_ups`` this
                # tick, so it is still inside its window. Never fall through to
                # the claim bookkeeping below — it holds no claim and no slot.
                continue
            try:
                await self._sweep_one(target, st, gave_up_targets)
            except Exception:  # pragma: no cover — one target must not stall the rest
                log.warning("backlog_runner.sweep_target_failed target=%s", target,
                            exc_info=True)

    # -- dispatch history (spec §6, §13) ------------------------------------ #
    #
    # Every dispatch leaves ONE ``dispatch_outcomes`` row: ``running`` at spawn,
    # finalised at the terminal branch of the sweep that ends it. Both writers are
    # the ``AutomationContext`` seams — the loop owns WHEN and WITH WHAT, never the
    # SQL. History is best-effort and dispatch is not (§13): the context already
    # swallows its own DB failures, and these wrappers guard the seam itself, so a
    # broken history writer can never cost a spawn or strand a claim mid-sweep.

    async def _record_outcome(self, target: str, *, product_slug: str | None,
                              session_id: str | None, model: str | None,
                              attempt_no: int) -> None:
        try:
            await self._ctx.record_outcome(
                target, product_slug=product_slug, session_id=session_id,
                model=model, attempt_no=attempt_no)
        except Exception:
            log.warning("backlog_runner.record_outcome_failed target=%s", target,
                        exc_info=True)

    async def _finalize_outcome(self, target: str, *, outcome: str,
                                cause: str | None = None, pr_number: int | None = None,
                                pr_url: str | None = None,
                                first_pass_merge: bool | None = None,
                                upgrade_from: tuple[str, ...] = ()) -> None:
        try:
            await self._ctx.finalize_outcome(
                target, outcome=outcome, cause=cause, pr_number=pr_number,
                pr_url=pr_url, first_pass_merge=first_pass_merge,
                upgrade_from=upgrade_from)
        except Exception:
            log.warning("backlog_runner.finalize_outcome_failed target=%s", target,
                        exc_info=True)

    async def _pr_doctor_attempts(self, pr_ref: str) -> int | None:
        """PR Doctor's session-fix count for *pr_ref*, or None when unknown.

        An UNWIRED seam and a failing one both answer "unknown" — never 0, which
        would record an unknown merge as a first-pass merge."""
        if self._pr_doctor_attempts_for is None:
            return None
        try:
            return await self._pr_doctor_attempts_for(pr_ref)
        except Exception:  # pragma: no cover — a history read must not stall the sweep
            log.warning("backlog_runner.pr_doctor_attempts_failed ref=%s", pr_ref,
                        exc_info=True)
            return None

    async def _give_up(self, target: str, st: dict, reason: str) -> None:
        """Hand the target to a human: announce it (comment, best-effort) and keep a
        durable ``gave_up`` row stamped ``needs_human_at``. The row is the ONLY carrier
        of the signal now (Phase D): the reconciler turns it into board Status
        ``Needs human`` and the queue into a cooldown; ``_sweep`` deletes it once
        ``needs_human_retry_days`` has passed so the issue is retried (never, when 0).

        The row REPLACES the claim row — a target waiting on a human holds no claim
        and no concurrency slot.

        ROW BEFORE ANNOUNCE, and the announce swallows its own failures: the
        announce is a best-effort side channel (a ``gh`` comment), while the row is
        the signal. Announcing first, unguarded, would let a GitHub hiccup abort
        this into ``_sweep``'s per-target guard with the claim row still in place —
        the target would sit claimed at max attempts, hold a concurrency slot and
        re-emit ``gave_up`` on every sweep, forever."""
        await self._ctx.set_target_state(target, {
            "repo": st.get("repo"),
            "issues": st.get("issues") or [target],
            "gave_up": True,
            "needs_human_at": self._wall_clock().isoformat(),
            "reason": reason,
        })
        try:
            await self._needs_human(target, reason)
        except Exception:
            log.warning("backlog_runner.needs_human_announce_failed target=%s", target,
                        exc_info=True)

    async def _sweep_one(self, target: str, st: dict,
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
        # not have carried a `Fixes #N`, leaving them open forever), then free the
        # slot and drop the guard row. The close-out verifies PR↔issue linkage and
        # FAILS CLOSED (#642).
        if info is not None and info.state == "merged":
            outcome = "closed"
            if self._close_out is not None:
                outcome = await self._close_out(target, info) or "closed"
            if outcome == "retry":
                return  # linkage unverifiable this sweep — try again next tick
            if outcome == "escalated":
                # The human gate fired for every issue (#784): the PR merged but a
                # human has to finish and close the work. That is a handoff, not a
                # completion — keep a stamped ``gave_up`` row instead of deleting
                # the row as if the batch were done.
                reason = "human gate: merged PR needs a human close-out"
                await self._ctx.emit("warn", f"{target}: {reason} — needs human",
                                     kind="gave_up", target=target)
                await self._finalize_outcome(target, outcome="needs_human",
                                             cause=reason, pr_number=info.number,
                                             pr_url=info.url)
                await self._give_up(target, st, reason)
                return
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
            # ``first_pass_merge`` is only knowable while PR Doctor still has a row
            # for this PR; an unknown count stays None rather than claiming a clean
            # first pass (R3-4).
            attempts = await self._pr_doctor_attempts(f"{repo}#{info.number}")
            await self._finalize_outcome(
                target, outcome="merged", pr_number=info.number, pr_url=info.url,
                first_pass_merge=(attempts == 0) if attempts is not None else None)
            await self._ctx.delete_target_state(target)
            return

        # PR-Doctor gave up on this target, or the PR closed unmerged → give up.
        closed_unmerged = info is not None and info.state == "closed"
        if target in gave_up_targets or closed_unmerged:
            doctor_gave_up = target in gave_up_targets
            reason = "pr-doctor gave up" if doctor_gave_up else "PR closed unmerged"
            await self._ctx.emit("warn", f"{target}: {reason} — needs human",
                                 kind="gave_up", target=target)
            await self._finalize_outcome(
                target, outcome="pr_gave_up" if doctor_gave_up else "abandoned",
                cause=reason,
                pr_number=info.number if info is not None else None,
                pr_url=info.url if info is not None else None)
            await self._give_up(target, st, reason)
            return

        # PR reached the handoff point (PR Doctor carries it to auto-merge).
        # Mark the row handed_off instead of deleting it: the merged close-out
        # above must still run when the PR lands. Deleting here left the batch's
        # issues open whenever the merged PR lacked a closing keyword — nothing
        # was left to close them. The slot still frees now: the orchestrator's
        # in-flight count skips handed_off rows.
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
            # The attempt cap is the PRODUCT's, read back through the claim's
            # recorded product (a claim written before the flip carries none and
            # falls back to the schema default).
            if attempts >= self._policy_for(st.get("product"))["max_attempts"]:
                if not await self._session_ever_ran(st):
                    # The session never ran a single turn (no engine_session_id) —
                    # its queue entry was lost and the reaper ended it, or it died
                    # before turn one (#1100). Escalating that to ``needs-human``
                    # parks a perfectly good issue for ``needs_human_retry_days`` on
                    # evidence of nothing: no diff was attempted, no failure was
                    # observed. RELEASE the claim instead — deleting the row is what
                    # returns the issue to the fresh pool for a real attempt.
                    await self._ctx.emit(
                        "info",
                        f"released {target} (session never ran a turn — not escalating)",
                        kind="unclaimed", target=target)
                    # The cap is reached a sweep AFTER the bounce that carried the
                    # dispatch to it, and nothing re-dispatches in between — so
                    # the row this closes is the ``no_pr`` that sweep already
                    # wrote, upgraded in place rather than left as the bounce it
                    # no longer is (R3-7).
                    await self._finalize_outcome(
                        target, outcome="abandoned",
                        cause="session never ran a turn",
                        upgrade_from=("no_pr",))
                    await self._ctx.delete_target_state(target)
                    return
                reason = f"max attempts ({attempts}) reached with no mergeable PR"
                await self._ctx.emit("warn", f"{target}: {reason} — giving up",
                                     kind="gave_up", target=target)
                # Same sweep-late verdict as the release path above: this
                # escalation belongs to the dispatch the previous sweep closed as
                # ``no_pr``, so it upgrades that row instead of finding nothing.
                await self._finalize_outcome(target, outcome="needs_human",
                                             cause=reason,
                                             upgrade_from=("no_pr",))
                await self._give_up(target, st, reason)
            else:
                # Charge the attempt and mark this session consumed so we don't
                # re-count the SAME ended session on every subsequent sweep; a
                # later re-arm / retry path re-spawns and clears ``_ended``.
                # THIS dispatch is over either way — the row closes as ``no_pr``
                # (a bounce), and the next spawn opens a fresh one.
                await self._finalize_outcome(
                    target, outcome="no_pr",
                    cause="session ended without a mergeable PR")
                new_st = dict(st)
                new_st["attempts"] = attempts
                new_st["_ended"] = True
                await self._ctx.set_target_state(target, new_st)

    async def _session_ever_ran(self, st: dict) -> bool:
        """Whether this target's session ever started turn one.

        FAILS SAFE to True — "we could not prove it never ran" must keep the normal
        give-up path, so a missing probe, a vanished row or a probe error can never
        silently disable escalation. Only a probe that positively answers False (a
        live row with a NULL ``engine_session_id``) diverts to the release path."""
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
