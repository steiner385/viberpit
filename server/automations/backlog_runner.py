"""The backlog-runner automation: pulls high-value issues off the backlog and
spawns implementation sessions, handing finished PRs to PR Doctor."""
from __future__ import annotations

from .context import observation_failed, publish_observation

import asyncio
import logging
import os
from datetime import datetime, timezone
from dataclasses import replace
from typing import Any, Awaitable, Callable, Protocol, cast

from ccswitch.worktrees import DISK_EXHAUSTION_MARKERS
from server.github.pr_status import PullRequestInfo

from .backlog_grouping import load_plan, mark_consumed, plan_to_batch
from .backlog_selection import SELECTION_STRATEGIES, Batch, should_free_slot
from .base import AutomationStatus, ConfigField
from .context import AutomationContext
from .engine_policy import EnginePolicy
from .product_policy import (
    NO_PRODUCT_POLICY_KEY, ImplementationDecision, effective_policy, is_on)
from server.pm.outcomes import bounced_out, gated_ladder_top, next_rung, top_tier
from server.sessions.model_catalog import model_tier

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


def is_pending_dispatch(state: dict) -> bool:
    """A durable batch which a deliberate tap may continue through claim validation."""
    return bool(state.get("proposed") or (state.get("waiting_account")
                                         and state.get("ownership_token")))


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
        ConfigField(key="max_concurrent", label="Max occupied slots", type="int", default=1,
                    help="Work claims allowed at once across every product, "
                         "including proposals awaiting approval."),
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
    ]
    # ``eligible_label`` gated the old GitHub label search; the runner now selects
    # from the board-mirror queue (spec 003 §7a), where board status — not a label
    # — says what is workable, so the key has no equivalent to migrate to. Most of
    # the rest moved to the PRODUCT policy (``server/automations/product_policy.py``):
    # which products to work, each one's board reach, in-flight cap, weight,
    # session model and dependency rule. ``max_attempts`` alone did not move —
    # it was retired outright, replaced by ``ladder_top`` + the model ladder
    # (spec 2026-09-18 §4, plan 4a task 2). Listed here so the rows that still
    # carry them keep the rest of their settings (and so the one-shot seed can
    # read the old values off the raw blob once).
    retired_config_keys: tuple[str, ...] = (
        "eligible_label", "enabled_repos", "repo_weights", "repo_caps",
        "queue_min_status", "max_attempts", "default_model", "respect_dependencies",
        # spec 2026-09-18: a hold releases on a human signal, never on a timer.
        "needs_human_retry_days")

    def status(self, ctx) -> AutomationStatus:
        counts = getattr(ctx, "backlog_counts", None) if ctx is not None else None
        admission_error = getattr(ctx, "backlog_admission_error", None)
        if admission_error is not None:
            return AutomationStatus(
                text="Last dispatch check unavailable [admission_unavailable]. No work was started.",
                level="unknown", counts=counts, waiting_reason="policy",
                observed_at=admission_error)
        admission = getattr(ctx, "backlog_last_admission", None)
        if admission is not None and not admission.permitted:
            reasons = "; ".join(f"{item.text} [{item.code}]" for item in admission.constraints
                                if item.state != "permitted")
            return AutomationStatus(
                text=f"Last dispatch check: {reasons}. Rechecked before the next dispatch.",
                level="unknown" if admission.state == "unknown" else "warn",
                counts=counts, waiting_reason="policy", observed_at=admission.observed_at)

        if counts is None:
            return AutomationStatus(text="Workload counts unavailable", level="unknown")
        # Per-product rows (``_backlog_count_candidates``), absent on a tick that
        # never reached the count seam. An empty map is the real, reportable state:
        # the runner is enabled but no product opted in, which as a bare
        # "0 eligible" reads like an empty backlog rather than a policy nobody set.
        products = getattr(ctx, "backlog_products", _NO_COUNT_YET)
        n = counts.get("in_flight")
        errors = getattr(ctx, "backlog_count_errors", [])
        if errors or products is None:
            # The read FAILED. "We don't know" and "no product is opted in" must
            # not share a sentence: an operator debugging a broken mirror read
            # would otherwise be told their policies are unset. The in-flight
            # count comes from a different seam and is still true, so it stays.
            return AutomationStatus(
                text=((f"{n} work in flight · " if n is not None else "")
                      + " · ".join(errors or [
                          "product counts unavailable (the queue read failed)"])),
                level="warn", counts=counts)
        counted = products is not _NO_COUNT_YET
        rows: dict = products if isinstance(products, dict) else {}
        if counted and not _on_products(rows) and not n and not counts.get("needs_human"):
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
        text = (f"{n} work in flight, "
                + (f"{counts['eligible']} eligible" if "eligible" in counts
                   else "queue counts unavailable")
                + (f", {counts['blocked']} blocked" if counts.get("blocked") else "")
                + (f", {counts['untriaged']} untriaged" if counts.get("untriaged") else "")
                + (f", {counts['needs_human']} needs-human" if counts.get("needs_human") else ""))
        # One segment per ON product after the summary: which products this runner
        # is working, how, and how hard. Joined with " · " on ONE line because the
        # web renders ``AutomationStatus.text`` without honouring newlines.
        # ``products`` (spec §9/§10) carries the SAME rows, structured, for the
        # web's per-product table (Task 5) — the text is the human summary, this
        # is the machine one, built from one loop so they can never disagree.
        if counts.get("unassigned_in_flight"):
            text += f" · {counts['unassigned_in_flight']} unattributed"
        on = sorted((slug, p) for slug, p in rows.items()
                    if is_on(p) or p.get("in_flight"))
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
        return AutomationStatus(text=text, level=level, counts=counts, products=products_out,
                                waiting_reason=getattr(ctx, "backlog_waiting_reason", None))

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
                 default_model: str, ownership_token: str) -> Awaitable[Any]: ...


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
# to pick the target up). Used only for the ``abandoned`` outcome (a person closed
# the PR unmerged) and the merged-but-unclosed human gate. The SIGNAL itself is
# the durable ``gave_up`` row ``_give_up`` writes — this seam only tells the
# outside world about it.
NeedsHuman = Callable[[str, str], Awaitable[None]]
# THE model-ladder path (spec 2026-09-18 §4): ``escalate(target, reason, rung)``.
# ``rung`` is ALWAYS a tier — the next attempt's pin, written as a
# ``stronger_model`` hold (agent-settable, queue-eligible) so the next refill
# re-claims the item. A climb that passes the product's ``ladder_top`` is NOT
# this seam: it routes through the existing ``_give_up`` (owner_action hold +
# comment + the retried ``held`` handoff), same as any other give-up (review R1
# I2/I4). Called BEFORE the row is released (never after — review R1 C1): the
# caller awaits this first and only deletes the claim once it returns without
# raising, so a raise here leaves the claim untouched for the next sweep to
# retry the whole branch.
Escalate = Callable[[str, str, str], Awaitable[None]]
# Which ``owner/repo#N`` refs are currently held (owner_action / decision) — the
# runner's own ``gave_up`` rows follow the hold now, not a timer (spec 2026-09-18
# §3): ``_release_give_ups`` drops a row once none of its refs is in this set.
# An unwired seam (stub) returns an empty set, so no row is ever released.
HeldTargets = Callable[[], Awaitable["frozenset[str]"]]
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


async def _stub_escalate(_target: str, _reason: str, _rung: str) -> None:
    # LADDER fallback: unwired seam, nothing is held — the row deletion still
    # returns the item to the pool with no hold behind it.
    return None


async def _stub_held_targets() -> "frozenset[str]":
    # Unwired seam: nothing reads as held, so no gave_up row is ever released.
    return frozenset()


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
        admission: Callable[..., Awaitable[tuple[ImplementationDecision, dict]]] | None = None,
        seed_policies: SeedPolicies | None = None,
        announce: Announce | None = None,
        needs_human: NeedsHuman | None = None,
        escalate: Escalate | None = None,
        session_ended: SessionEnded | None = None,
        session_ran: SessionRan | None = None,
        session_work_stopped: SessionEnded | None = None,
        handoff: Callable[[str, dict, PullRequestInfo], Awaitable[bool]] | None = None,
        closeout_completed: Callable[[str, dict], Awaitable[bool]] | None = None,
        pr_doctor_attempts_for: PrDoctorAttemptsFor | None = None,
        count_candidates: CountCandidates | None = None,
        close_out: CloseOut | None = None,
        refresh_groups: RefreshGroups | None = None,
        credential_alerts: CredentialAlerts | None = None,
        verify_ready: VerifyReady | None = None,
        queue_tail: QueueTail | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        session_gone: SessionEnded | None = None,
        held_targets: HeldTargets | None = None,
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
        self._admission = admission
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
        self._escalate = escalate or _stub_escalate  # the model-ladder path
        self._held_targets = held_targets or _stub_held_targets  # what backs a gave_up row
        self._session_ended = session_ended         # (session_id) -> bool | None
        # (session_id) -> True when the session row is terminal OR no longer exists.
        # The evidence bar for discharging a pre-ownership (token-less) claim; an
        # unwired seam leaves those rows untouched (``_release_legacy_claim``).
        self._session_gone = session_gone
        self._closeout_completed = closeout_completed
        self._handoff = handoff
        self._session_work_stopped = session_work_stopped
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
        self._group_refresh_last_error: str | None = None  # debounce the failure event per distinct error
        self._counts_published = False                  # cold start: first ``_stash_counts`` done
        self._last_groups_emitted: int | None = None    # debounce the refreshed event
        self._lock = asyncio.Lock()
        self._accounted_dispatches: set[str] = set()
        self._spawns_today: list[float] = []        # spawn timestamps within the rolling day
        self._circuit_open_emitted = False          # debounce the circuit_open event
        self._spawn_backoff: dict[str, tuple[int, float]] = {}  # target → (fails, retry_at)
        self._spawn_circuit_until: float | None = None  # breaker open until this clock time
        self._consec_spawn_failures = 0             # resets on any successful spawn
        self._no_account_emitted = False            # debounce the skipped_no_account event
        self._waiting_reason: str | None = None
        self._last_pick: dict | None = None          # queue provenance of the last successful spawn (#990)
        # Why the last refill made NO claim attempt (``refill_stalled``), or None
        # once one was attempted. Debounces the event to one per reason transition.
        self._stall_reason: str | None = None

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._refill_and_sweep()
            except Exception:  # pragma: no cover — defensive: a tick must never crash the loop
                observation_failed(self._ctx)
                log.warning("backlog_runner.tick_failed", exc_info=True)

    # -- refill ------------------------------------------------------------- #

    def _policy_for(self, product: "str | None") -> dict:
        """The policy this batch / claim is governed by.

        ONE lookup for every per-product decision in the loop (dispatch, model,
        the ladder's ``ladder_top``), so they cannot disagree about which product
        owns a target.

        A product the map does not know falls back to the snapshot's own no-product
        default (``NO_PRODUCT_POLICY_KEY``) — the schema defaults with the SAME
        approval-gate clamp on ``ladder_top`` every product's policy got, so the
        ladder for a product-less claim stops where the gate says and not one tier
        above it. Only a snapshot from a seam that carries no default (a test's, a
        pre-clamp one) reaches the pure ``effective_policy(None)``.

        Either way the fallback is dispatch ``off``, deliberately fail-SAFE on the
        one decision where being wrong inverts an operator's instruction:
        ``propose`` means "do not start a session without me", and a fallback of
        ``auto`` would spawn for every proposing product the moment a policy read
        hiccuped. Nothing legitimate is stranded by it — the QUEUE is the off-gate,
        so a refill batch always carries a real ON slug — and the two OTHER
        decisions this feeds keep working for a pre-flip claim that carries no
        product: the defaults hand back ``default_model`` (``auto``) those claims
        used to run under, and the ladder's top applies the same way.
        """
        if product:
            policy = self._policies.get(product)
            if policy is not None:
                return policy
        return self._policies.get(NO_PRODUCT_POLICY_KEY) or effective_policy(None)

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
        self._waiting_reason = None
        config: dict = {}
        try:
            config = await self._refill_and_sweep_body()
        finally:
            # ALWAYS publish the claim/queue snapshot, even when refill or the sweep
            # raised: the stash used to sit after them, so one raising tick left
            # ``backlog_counts`` unset — "Workload counts unavailable" — for the
            # life of the process. ``tick()`` still records the failure itself.
            await self._stash_counts(config)

    async def _refill_and_sweep_body(self) -> dict:
        """The tick proper; returns the config it ran under so the ``finally``
        above can stash counts against it."""
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
                # PRODUCT entries only: a snapshot also carries the no-product
                # default (``NO_PRODUCT_POLICY_KEY``), and counting that as "we
                # have policies" would run refill — including the ~10-minute
                # grouping refresh — on an effectively empty snapshot.
                policies_known = any(slug != NO_PRODUCT_POLICY_KEY for slug in self._policies)
                log.warning("backlog_runner.policy_read_failed (refill %s)",
                            "on the previous snapshot" if policies_known else "skipped",
                            exc_info=True)
        # Surface stale-credential alerts every tick, before refill — a paused
        # repo must both notify the dashboard and be excluded from selection.
        await self._check_credentials()
        # Release spent give-ups BEFORE refill, not as part of the sweep that
        # follows it: on the very tick a hold releases, the queue already
        # offers the ref, so ``select_batch`` would consume its plan group only
        # for ``_refill``'s ``gave_up`` guard to discard the pick — burning the
        # group until the next grouping refresh. Releasing first lets the same
        # tick spawn it.
        await self._release_give_ups()
        # Recover admitted launches before checking the daily dispatch budget.
        await self._reconcile_dangling()
        if not self._counts_published:
            # Cold start: the grouping optimizer below is a model call over the
            # whole backlog (~10 min on 2026-09-16, more on a failure + retry), and
            # until the first stash the card reads "Workload counts unavailable"
            # while the actionable count beside it says 430. The counts are two
            # cheap DB reads — publish them before the expensive part.
            await self._stash_counts(config)
        if mode != "off" and policies_known:
            # The grouping optimizer runs BEFORE refill so a cold start (or a
            # just-consumed plan) can produce groups and spawn in the same tick.
            await self._maybe_refresh_groups(config)
            await self._refresh_dispatch_budget()
            self._prune_day()
            if len(self._spawns_today) < config["max_spawns_per_day"]:
                self._circuit_open_emitted = False
                await self._refill(config)
            else:
                self._waiting_reason = "budget"
                if not self._circuit_open_emitted:
                    self._circuit_open_emitted = True
                    await self._ctx.emit("warn", "daily spawn cap reached — pausing",
                                         kind="circuit_open")
        # handoff + terminal-watch runs every tick, regardless of mode. The live
        # counts ``BacklogRunner.status(ctx)`` renders are stashed by the caller's
        # ``finally`` once this returns (or raises).
        await self._sweep()
        return config

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
        """Publish one claim snapshot; missing measurements never become zeroes.

        Execution still uses its fresh capacity check under the dispatch lock.
        """
        counts: dict[str, int] = {}
        errors: list[str] = []
        states = None
        try:
            states = await self._ctx.list_target_states()
            counts["in_flight"] = sum(is_in_flight(s) for s in states.values())
            # The RUNNER'S OWN give-ups, not every hold on the board: an item a
            # person or a session parked has no row here. (What the card calls this
            # count is items 3b/6's concern.)
            counts["needs_human"] = sum(bool(s.get("gave_up")) for s in states.values())
        except Exception:
            errors.append("workload counts unavailable (the claim read failed)")
        per_product: dict | None = None
        if self._count_candidates is not None:
            try:
                eligible, blocked, untriaged, per_product = await self._count_candidates()
                if per_product is not None:
                    counts.update(eligible=eligible, blocked=blocked, untriaged=untriaged)
            except Exception:
                pass
            if per_product is None:
                errors.append("product counts unavailable (the queue read failed)")
        if states is not None and per_product is not None:
            by_product = in_flight_by_product(states)
            for slug, row in per_product.items():
                row["in_flight"] = by_product.get(slug, 0)
            counts["unassigned_in_flight"] = counts["in_flight"] - sum(
                row["in_flight"] for row in per_product.values())
        if states is None:
            per_product = None
        if not counts and errors:
            observation_failed(self._ctx)
            setattr(self._ctx, "backlog_counts", counts)
        else:
            publish_observation(self._ctx, "backlog_counts", counts, degraded=bool(errors))
        human_wait = states is not None and any(
            s.get("gave_up") or s.get("proposed") for s in states.values())
        setattr(self._ctx, "backlog_waiting_reason",
                "human" if human_wait else self._waiting_reason)
        setattr(self._ctx, "backlog_count_errors", errors)
        if self._count_candidates is not None:
            setattr(self._ctx, "backlog_products", per_product)
        setattr(self._ctx, "backlog_last_pick", self._last_pick)
        self._counts_published = True

    async def _reconcile_dangling(self) -> None:
        """Recover a reserved Session identity without guessing that work stopped.

        A timer or a missing Session row cannot distinguish a crashed launcher
        from a slow remote one. Retain both current and legacy claims until their
        lifecycle can be reconciled from durable evidence.
        """
        from server.db.models import AutomationTargetState, Session as DbSession
        from .ownership import StaleOwnership, owned

        for target, st in (await self._ctx.list_target_states()).items():
            token = st.get("ownership_token")
            if not token or st.get("session_id") or st.get("proposed"):
                continue
            try:
                async with owned(token) as tx:
                    operation = tx.operation
                    if (operation.owner != self._ctx.automation_id
                            or operation.provenance.get("origin_ref") != target):
                        continue
                    state_row = await tx.db.get(
                        AutomationTargetState, (self._ctx.automation_id, target))
                    if state_row is None or state_row.state != st:
                        continue  # a human/other decision changed the original snapshot
                    sid = operation.session_id or operation.reserved_session_id
                    if sid is None:
                        continue
                    session = await tx.db.get(DbSession, sid)
                    if (session is None or session.origin != operation.owner
                            or session.origin_ref != target or session.parent_session_id is not None):
                        continue  # missing or unrelated rows never establish launch identity
                    if operation.session_id is None:
                        if operation.phase not in {"launching", "uncertain"}:
                            continue
                        tx.bind_session(sid)
                    # Recover association and the runner snapshot atomically. This
                    # makes native evidence observable to the next sweep; neither
                    # persistence nor terminal status is itself a stopped proof.
                    if operation.provenance.get("runner_dispatch"):
                        await self._settle_dispatch(tx, target, sid, session.branch)
                    else:
                        state_row.state = {**st, "session_id": sid,
                                           "branch": session.branch or st.get("branch")}
                await self._refresh_dispatch_budget()
            except StaleOwnership:
                continue

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
        except Exception as exc:
            log.warning("backlog_runner.group_refresh_failed", exc_info=True)
            self._group_refresh_retry_at = now + GROUP_REFRESH_BACKOFF_S
            # The event names the error: "retrying shortly" alone sent an operator
            # to the journal to learn WHY (2026-09-16). Debounced per distinct
            # error rather than per streak — a changed cause is news.
            error = f"{type(exc).__name__}: {exc}"
            if error != self._group_refresh_last_error:
                self._group_refresh_last_error = error
                await self._ctx.emit(
                    "warn", f"group plan refresh failed — retrying shortly ({error})",
                    kind="groups_refresh_failed", detail={"error": error})
            return
        self._group_refresh_retry_at = None
        self._group_refresh_last_error = None
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

    def _stall_changed(self, reason: str) -> bool:
        """Record why this refill is making no claim attempt; True the first time
        *reason* is seen since the last attempt, so ``refill_stalled`` is emitted
        once per transition rather than once per 60-second tick."""
        if self._stall_reason == reason:
            return False
        self._stall_reason = reason
        return True

    async def _slot_holders(self) -> list[dict]:
        """The rows occupying slots right now, for the capacity stall event: which
        target, which session, and whether the row predates the ownership store
        (``legacy`` = no ``ownership_token``, the shape that sat on every slot for
        13 hours on 2026-09-16 with its sessions long deleted)."""
        try:
            states = await self._ctx.list_target_states()
        except Exception:  # pragma: no cover — a diagnostic read must not stall the tick
            log.warning("backlog_runner.slot_holders_failed", exc_info=True)
            return []
        return [{"target": target, "session_id": st.get("session_id"),
                 "legacy": not st.get("ownership_token"),
                 "proposed": bool(st.get("proposed")),
                 "claimed_at": st.get("claimed_at")}
                for target, st in sorted(states.items()) if is_in_flight(st)][:20]

    async def _refill(self, config: dict) -> None:
        # Every early exit below that makes NO claim attempt reports itself as a
        # ``refill_stalled`` warn (once per reason transition): an enabled runner
        # over a full queue that only ever logs ``groups_refreshed`` is the failure
        # this loop must never be silent about again. Exits that already have
        # their own event (daily budget → ``circuit_open``, no account →
        # ``skipped_no_account``, an admission refusal) are left as they are.
        if self._spawn_circuit_until is not None:
            if self._clock() < self._spawn_circuit_until:
                retry_in = int(self._spawn_circuit_until - self._clock())
                if self._stall_changed("spawn_circuit"):
                    await self._ctx.emit(
                        "warn", f"no claim: spawn circuit breaker open (retry in {retry_in}s)",
                        kind="refill_stalled",
                        detail={"reason": "spawn_circuit", "retry_in_s": retry_in})
                return                      # breaker open — no spawn attempts at all
            self._spawn_circuit_until = None    # cooldown over → half-open probe
        in_flight = await self._count_in_flight()
        free = config["max_concurrent"] - in_flight
        if free <= 0:
            self._waiting_reason = "capacity"
            if self._stall_changed("capacity"):
                holders = await self._slot_holders()
                legacy = [h["target"] for h in holders if h["legacy"]]
                await self._ctx.emit(
                    "warn",
                    f"no claim: no slot free, {in_flight}/{config['max_concurrent']} in flight"
                    + (f" — {len(legacy)} held by pre-ownership claim(s): "
                       f"{', '.join(legacy)}" if legacy else ""),
                    kind="refill_stalled",
                    detail={"reason": "capacity", "in_flight": in_flight,
                            "max_concurrent": config["max_concurrent"],
                            "holders": holders})
        for _ in range(max(0, free)):
            if len(self._spawns_today) >= config["max_spawns_per_day"]:
                self._waiting_reason = "budget"
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
                # A planned, non-empty backlog with nothing claimable is a stall;
                # an empty plan is just an idle backlog and stays quiet.
                planned = self._last_groups_emitted
                if planned and self._stall_changed("no_batch"):
                    await self._ctx.emit(
                        "warn",
                        f"no claim: plan has {planned} group(s) but none is claimable "
                        "against the live queue — refreshing the plan",
                        kind="refill_stalled",
                        detail={"reason": "no_batch", "groups_planned": planned})
                return
            fails_retry = self._spawn_backoff.get(batch.primary)
            if fails_retry is not None and self._clock() < fails_retry[1]:
                # The selected target is cooling down after a failed spawn.
                # break, not continue: _select_batch would hand us the same
                # highest-priority batch again and spin this for-loop.
                retry_in = int(fails_retry[1] - self._clock())
                if self._stall_changed("target_cooldown"):
                    await self._ctx.emit(
                        "warn",
                        f"no claim: {batch.primary} is cooling down after a failed "
                        f"spawn/check (retry in {retry_in}s)",
                        kind="refill_stalled", target=batch.primary,
                        detail={"reason": "target_cooldown", "retry_in_s": retry_in,
                                "failures": fails_retry[0]})
                break
            st = await self._ctx.get_target_state(batch.primary)
            if st.get("gave_up"):
                continue
            if self._verify_ready is not None and not await self._live_check(batch):
                # break, not continue: ``select_batch`` already consumed this
                # pick's plan group, so continuing would burn the REST of the
                # plan on what is usually one GitHub hiccup. The slot waits a
                # tick instead; the group returns at the next grouping refresh.
                break
            self._stall_reason = None   # a claim is being attempted: the next stall is news again
            result = await self.dispatch_batch(
                batch, source="automatic", account=account,
                default_model="auto")
            if result["status"] in {"refused", "uncertain"} or self._spawn_circuit_until:
                break

    async def dispatch_batch(self, batch: Batch, *, source: str,
                             account: str | None, default_model: str,
                             propose: bool = False) -> dict:
        """One durable all-target dispatch path for automatic, manual and taps."""
        from .ownership import OwnershipConflict, ReleaseProof, StaleOwnership, owned

        def result(status: str, rec=None) -> dict:
            return {"status": status, "session_id": getattr(rec, "id", None),
                    "session_name": getattr(rec, "name", None)}

        previous = await self._ctx.get_target_state(batch.primary)
        existing_slot = bool(previous.get("claimed") and (
            previous.get("ownership_token") or previous.get("proposed")))
        if self._admission is None:
            setattr(self._ctx, "backlog_admission_error", self._wall_clock().isoformat())
            return {**result("refused"), "reason_codes": ["admission_unavailable"]}
        try:
            decision, policy = await self._admission(
                batch, source=source, existing_slot=existing_slot,
                existing_token=previous.get("ownership_token"))
        except Exception:
            log.warning("backlog_runner.admission_unavailable", exc_info=True)
            setattr(self._ctx, "backlog_admission_error", self._wall_clock().isoformat())
            return {**result("refused"), "reason_codes": ["admission_unavailable"]}
        setattr(self._ctx, "backlog_admission_error", None)
        setattr(self._ctx, "backlog_last_admission", decision)
        if not decision.permitted:
            self._waiting_reason = "policy"
            refused = [item for item in decision.constraints if item.state != "permitted"]
            codes = [item.code for item in refused]
            kind = "implementation_refused"
            status = "refused"
            summary = f"skipped {batch.primary}: " + "; ".join(item.text for item in refused)
            if "dispatch_off" in codes:
                kind, status = "skipped_dispatch_off", "policy_refused"
            elif "product_capacity_full" in codes:
                kind, status = "skipped_product_cap", "capacity"
                self._waiting_reason = "capacity"
                summary += f"; in-flight cap ({policy['max_in_flight']})"
            elif "global_capacity_full" in codes:
                status = "capacity"
            elif "daily_budget_full" in codes:
                status = "budget"
            await self._ctx.emit(
                "info", summary,
                kind=kind, target=batch.primary,
                detail={"reason_codes": codes, "product": decision.product_slug})
            return {**result(status), "reason_codes": codes}
        batch = replace(batch, product=decision.product_slug)
        default_model = policy["default_model"]
        propose = propose or (source == "automatic" and decision.configured_dispatch == "propose")
        # Every bounce deletes the claim row now (spec 2026-09-18 §4), so
        # ``previous`` carries no ``attempts`` for a re-queued item — the
        # dispatch-history count (review R1 I3) is what seeds it instead. A
        # LIVE previous claim (propose / waiting-account retry) still carries
        # its own ``attempts`` and is trusted as-is, no extra query.
        prior_count = previous.get("attempts")
        if prior_count is None:
            prior_count = await self._ctx.prior_attempts(batch.primary)
        state = {"repo": batch.repo, "product": batch.product,
                 "attempts": prior_count, "issues": batch.issues,
                 "titles": batch.titles,
                 "claimed": True, "claimed_at": self._wall_clock().isoformat()}
        if propose:
            state["proposed"] = True
        elif account is None:
            state["waiting_account"] = True
        try:
            state = await self._ctx.claim_target_state(
                batch.primary, state, expected=previous, source=source,
                work_stopped=await self._work_stopped(previous))
        except (OwnershipConflict, StaleOwnership) as exc:
            await self._ctx.emit("info", f"{batch.primary}: waiting for existing work",
                                 kind="ownership_wait", target=batch.primary,
                                 detail={"reason": str(exc)})
            return result("busy")
        await self._announce_claim(batch)
        await self._ctx.emit("info", f"claimed {batch.primary}", kind="claimed",
                             target=batch.primary, detail={"issues": batch.issues})
        if propose:
            await self._ctx.emit("info", f"ready to implement {batch.primary}",
                                 kind="selected", target=batch.primary,
                                 detail={"issues": batch.issues})
            return result("proposed")
        if account is None:
            # An intentional dispatch can retry this same unstarted operation.
            # Keep all targets reserved, including every secondary batch issue.
            return result("no_account")
        token = state["ownership_token"]
        async with owned(token) as tx:
            tx.operation.provenance = {**tx.operation.provenance, "runner_dispatch": {
                "expected": state, "model": default_model, "source": source, "accounted_at": None}}
        try:
            rec = await self._spawn(batch, account, default_model=default_model,
                                    ownership_token=token)
        except Exception as exc:
            # Only the store's still-claimed phase proves no launch occurred.
            # A generic error after reservation is ambiguous, never a retry.
            safe = False
            async with owned(token) as tx:
                safe = tx.operation.phase == "claimed"
            if not safe:
                await self._ctx.emit("warn", f"{batch.primary}: launch outcome unknown",
                                     kind="ownership_wait", target=batch.primary,
                                     detail={"reason": str(exc)})
                return result("uncertain")
            await self._ctx.compare_target_state(
                batch.primary, state, None, release_proof=ReleaseProof.NOT_STARTED,
                reason="launch failed before session reservation")
            fails = self._spawn_backoff.get(batch.primary, (0, 0.0))[0] + 1
            delay = _spawn_backoff_delay(fails)
            self._spawn_backoff[batch.primary] = (fails, self._clock() + delay)
            self._consec_spawn_failures += 1
            disk = any(m in str(exc) for m in DISK_EXHAUSTION_MARKERS)
            if disk or self._consec_spawn_failures >= SPAWN_CIRCUIT_THRESHOLD:
                self._spawn_circuit_until = self._clock() + SPAWN_CIRCUIT_COOLDOWN_S
                await self._ctx.emit("warn", f"spawn circuit open: {exc}",
                                     kind="spawn_circuit_open")
            await self._ctx.emit("info", f"released {batch.primary} (spawn failed)",
                                 kind="unclaimed", target=batch.primary)
            return result("failed")
        if rec is None:
            fails = max(1, self._spawn_backoff.get(batch.primary, (0, 0.0))[0])
            self._spawn_backoff[batch.primary] = (fails, self._clock() + _spawn_backoff_delay(fails))
            await self._ctx.compare_target_state(
                batch.primary, state, None, release_proof=ReleaseProof.NOT_STARTED,
                reason="launch refused before session reservation")
            await self._ctx.emit("info", f"released {batch.primary} (launch refused)",
                                 kind="unclaimed", target=batch.primary)
            return result("refused")
        try:
            async with owned(token) as tx:
                acknowledged = await self._settle_dispatch(
                    tx, batch.primary, getattr(rec, "id", None), getattr(rec, "branch", None),
                    model=getattr(rec, "model", None))
        except StaleOwnership:
            acknowledged = False
        await self._refresh_dispatch_budget()
        if not acknowledged:
            return result("busy", rec)
        self._spawn_backoff.pop(batch.primary, None)
        self._consec_spawn_failures = 0
        if self._queue_tail is not None:
            await self._emit_pick(batch)
        return result("spawned", rec)

    async def _settle_dispatch(self, tx, target: str, sid: str | None,
                               branch: str | None, *, model: str | None = None) -> bool:
        """Observer and callback acknowledge one persisted dispatch intent."""
        from server.db.models import AutomationTargetState, Session as DbSession

        intent = tx.operation.provenance.get("runner_dispatch")
        if not intent:
            return False
        if intent.get("accounted_at"):
            return intent.get("session_id") == sid
        row = await tx.db.get(AutomationTargetState, (self._ctx.automation_id, target))
        if row is None or row.state != intent["expected"]:
            return False
        session = await tx.db.get(DbSession, sid) if sid else None
        model = (session.model if session is not None else None) or model or intent["model"]
        state = dict(intent["expected"])
        # The APPLIED model, not the policy's ``default_model``: the ladder's
        # ``next_rung`` (spec 2026-09-18 §4) climbs from what this attempt
        # actually ran on, read back off the sweep's ``st.get("model")``.
        state.update(session_id=sid, branch=branch, model=model)
        state.pop("proposed", None)
        try:
            async with tx.db.begin_nested():
                await self._ctx.record_outcome(
                    target, _db=tx.db, product_slug=state.get("product"), session_id=sid,
                    model=model, attempt_no=state.get("attempts", 0))
        except Exception:
            log.warning("backlog_runner.outcome_start_failed target=%s", target, exc_info=True)
        row.state = state
        tx.operation.provenance = {**tx.operation.provenance, "runner_dispatch": {
            **intent, "model": model, "session_id": sid,
            "accounted_at": self._wall_clock().isoformat()}}
        return True

    async def _refresh_dispatch_budget(self) -> None:
        """Recover admitted dispatches even if their callback or process died."""
        from .product_admission import read_dispatch_accounting

        now = self._wall_clock()
        for token, stamp in (await read_dispatch_accounting(
                now=now, automation_id=self._ctx.automation_id)).items():
            if token not in self._accounted_dispatches:
                age = (now - stamp).total_seconds()
                self._spawns_today.append(self._clock() - max(0, age))
                self._accounted_dispatches.add(token)

    def _prune_day(self) -> None:
        """Drop spawn timestamps older than one rolling day (injected clock)."""
        cutoff = self._clock() - _DAY_SECONDS
        self._spawns_today = [t for t in self._spawns_today if t > cutoff]

    # -- sweep (handoff + terminal-watch) ----------------------------------- #

    async def _release_give_ups(self) -> None:
        """Drop ``gave_up`` rows that no hold backs any more (spec 2026-09-18 §3).

        The row survives ONLY for slot accounting while the item is held; the hold
        on the item is the handoff, and a human releases it (board edit, body edit,
        Retry). Once none of the row's refs is held the row is deleted, which is what
        returns the issue to the pool. Runs BEFORE ``_refill`` — see the call site.

        This is also where the give-up's two-transaction handoff is made whole: a row
        whose ``held`` flag is falsy has no proof its hold was ever written, so it is
        NEVER dropped here — the seam is retried instead, every tick, until it lands.
        Two exceptions, both below: a row whose item is already parked adopts that
        hold rather than re-announcing it, and a token-less legacy row is left inert.
        """
        states = await self._ctx.list_target_states()
        held = await self._held_targets()
        for target, st in states.items():
            if not st.get("gave_up"):
                continue
            refs = st.get("issues") or [target]
            backed = any(ref in held for ref in refs)
            if not st.get("held"):
                # No recorded handoff. If the item IS parked, the hold is there
                # (this row predates the flag, or the CAS after the write was lost)
                # and the row just adopts it — re-announcing would re-stamp
                # ``hold_set_at``/``hold_fingerprint``, losing a pending body-edit
                # release, and comment the issue again. Otherwise retry the seam and
                # keep the row whatever happens.
                #
                # EXCEPT for a pre-ownership row (#1648, no ``ownership_token``):
                # ``compare_target_state`` refuses to touch one, so ``held`` could
                # never be recorded and the retry would fire forever — every tick
                # after a human released the hold would comment again and write a
                # fresh hold, undoing that release. Leave it exactly as inert as it
                # was before this path existed; ``reconcile_legacy_targets`` owns it.
                if not backed and not st.get("ownership_token"):
                    log.warning("backlog_runner.needs_human_retry_skipped_legacy_row "
                                "target=%s", target)
                    continue
                if not backed:
                    try:
                        await self._needs_human(target, st.get("reason") or "")
                    except Exception:
                        log.warning("backlog_runner.needs_human_retry_failed target=%s",
                                    target, exc_info=True)
                        continue
                await self._write_state(target, st, {**st, "held": True})
                continue
            # A bundle is released as a UNIT: while ANY ref of the row is still held
            # the whole row stays, because the batch was claimed and dispatched as
            # one. A Retry on one sibling therefore takes effect only once the last
            # held sibling is released too.
            if backed:
                continue
            if not await self._write_state(target, st, None):
                continue
            await self._ctx.emit("info", f"{target}: hold released — back in the pool",
                                 kind="hold_released", target=target)

    async def _sweep(self) -> None:
        states = await self._ctx.list_target_states()
        gave_up_targets = await self._pr_doctor_gave_up_targets()
        for target, st in states.items():
            if st.get("gave_up"):
                # Terminal: a row still here survived ``_release_give_ups`` this
                # tick, so its hold is still on. Never fall through to the claim
                # bookkeeping below — it holds no claim and no slot.
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

    @staticmethod
    def _outcome_fallback(st: dict) -> "dict | None":
        """The claim's own facts, for spec §13's recovery insert: when the
        spawn-time ``record_outcome`` failed there is no row to finalise, and the
        context inserts one from these instead of losing the bounce. The claim is
        the only surviving account of that dispatch — ``model`` is the APPLIED
        model ``_settle_dispatch`` wrote onto the row (spec 2026-09-18 §4: the
        ladder climbs from what this attempt actually ran on, not a guess), and
        ``attempts`` is the count at spawn (the sweep charges its increment after
        this call), which is exactly what ``record_outcome`` would have written.

        ``None`` for a claim that never dispatched (no ``session_id``: a propose
        claim, or one the reaper caught before the spawn record). Nothing ran, so
        there is no lost history to recover — an insert here would count a bounce
        against an issue body no session ever attempted."""
        if not (st.get("implementation_session_id") or st.get("session_id")):
            return None
        return {"product_slug": st.get("product"),
                "session_id": st.get("implementation_session_id") or st.get("session_id"),
                "model": st.get("model"), "attempt_no": st.get("attempts", 0)}

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

    async def _work_stopped(self, st: dict) -> bool:
        sid = st.get("session_id")
        if st.get("handed_off") or st.get("closeout_started"):
            from .ownership import StaleOwnership, owned
            try:
                async with owned(st["ownership_token"]) as tx:
                    sid = tx.operation.session_id or tx.operation.reserved_session_id
                    if tx.operation.phase in {"claimed", "completed"} and sid is None:
                        return True  # successor has not started any external effect
            except StaleOwnership:
                return False
        if not sid or self._session_work_stopped is None:
            return False
        try:
            return bool(await self._session_work_stopped(sid))
        except Exception:
            return False

    async def _write_state(self, target: str, st: dict, state: dict | None, *,
                           outcome: dict | None = None, action_completed: bool = False) -> bool:
        """Fence the original read, keeping unknown actor ownership intact."""
        from .ownership import ReleaseProof

        release = state is None
        proof = ReleaseProof.WORK_STOPPED
        if release and st.get("handed_off"):
            from .ownership import StaleOwnership, owned
            try:
                async with owned(st["ownership_token"]) as tx:
                    if tx.operation.phase == "claimed":
                        proof = ReleaseProof.NOT_STARTED
            except StaleOwnership:
                return False
        if release and not action_completed and not await self._work_stopped(st):
            return False
        return await self._ctx.compare_target_state(
            target, st, state, outcome=outcome, action_completed=action_completed,
            release_proof=proof if release else None,
            reason="current actor observed stopped or not started" if release else "")

    async def _give_up(self, target: str, st: dict, reason: str, *,
                       outcome: dict | None = None, action_completed: bool = False) -> None:
        # Retain attempt/session provenance and durable exclusion while the human
        # decides. The operational slot can free without permitting a new actor.
        # No timestamp: the owner_action hold ``_backlog_needs_human`` writes on
        # the issue IS the handoff now (spec 2026-09-18 §3) — this row survives
        # only for slot accounting and is dropped by ``_release_give_ups`` once
        # the hold is gone.
        #
        # The handoff spans two transactions (this row, then the hold), so the row
        # carries whether the SECOND one landed. It is parked ``held: False`` first:
        # a row that claimed a hold it does not have would be dropped by the next
        # sweep and the item re-dispatched with no handoff at all. ``held`` only ever
        # goes False → True, and ``_release_give_ups`` retries until it does.
        parked = {**st, "claimed": False, "gave_up": True, "reason": reason, "held": False}
        if not await self._write_state(target, st, parked, outcome=outcome,
                                       action_completed=action_completed):
            return
        await self._ctx.emit("warn", f"{target}: {reason} — needs human",
                             kind="gave_up", target=target)
        try:
            await self._needs_human(target, reason)
        except Exception:
            log.warning("backlog_runner.needs_human_announce_failed target=%s", target,
                        exc_info=True)
            return
        # Compare-and-set against the row just parked. A lost CAS (another actor
        # moved the row meanwhile) is left alone: the next tick's sweep retries.
        await self._write_state(target, parked, {**parked, "held": True})

    async def _release_legacy_claim(self, target: str, st: dict) -> None:
        """Discharge a pre-ownership claim (no ``ownership_token``, #1648) once its
        session is provably over.

        ``compare_target_state`` refuses to touch token-less rows ("intact until
        reconciled") and ``reconcile_legacy_targets`` only *refuses new claims*
        over them, so nothing ever reconciled one: on 2026-09-16 six such rows
        from 2026-09-07 — every session row long deleted — still counted as in
        flight, filled all ``max_concurrent`` slots and stalled the runner for 13
        hours. The evidence bar is the one the sweep already uses to charge a
        failed attempt, plus absence: ``session_gone`` answers True for a
        terminal OR missing session row. A claim that never recorded a session
        has nothing running to protect. The released issue returns to the pool
        with no bounce charged — the old session's fate is unknown, and a phantom
        attempt would be a guess. Unwired seam = rows are left exactly as before.

        Scoped to rows that actually HOLD A SLOT, via the shared ``is_in_flight``
        predicate rather than a second hand-rolled filter: the harm being undone
        is a slot held forever. ``_sweep`` already skips ``gave_up`` (a human
        cooldown), but a ``handed_off`` row belongs to PR Doctor and reaches here
        — deleting one would yank a target out from under another automation."""
        if self._session_gone is None or not is_in_flight(st):
            return
        sid = st.get("session_id")
        if sid:
            try:
                if not await self._session_gone(sid):
                    return
            except Exception:  # pragma: no cover — a probe failure must not stall the sweep
                log.warning("backlog_runner.session_gone_probe_failed sid=%s", sid,
                            exc_info=True)
                return
        if not await self._ctx.release_legacy_target_state(target, st):
            return
        await self._ctx.emit(
            "warn",
            f"released pre-ownership claim {target}: its session "
            f"{'is gone' if sid else 'never started'} — back in the pool",
            kind="legacy_claim_released", target=target,
            detail={"session_id": sid, "claimed_at": st.get("claimed_at"),
                    "issues": st.get("issues")})

    async def _dial_stops_the_ladder(self, target: str, policy: dict) -> "str | None":
        """The operator's ``give_up_after_bounces`` hand-off reason for the bounce
        being charged right now, or ``None`` to keep climbing.

        The ladder's own length is the cap (spec 2026-09-18 §4, decision 2), so this
        dial is an optional STRICTER stop. It has to be consulted BEFORE a rung hold
        is written: the queue's ``skip_reason`` applies ``bounced_out`` to the same
        count, so a rung hold written past the dial is one nothing will ever consume
        — the item sits ``needs_spec`` with no owner hand-off, no comment and no
        ``gave_up`` row (final review I1).

        ``bounced_out`` is THE comparison (``server/pm/outcomes.py``), shared with
        the queue and the roadmap, applied to the queue's OWN count plus the bounce
        this sweep is about to finalise. A zero dial short-circuits before the query:
        ``bounced_out`` answers False for it anyway, and the default path must not
        pay a read per bounce."""
        cap = int(policy["give_up_after_bounces"])
        if cap <= 0:
            return None
        bounces = await self._ctx.bounces_for(target) + 1
        if not bounced_out(bounces, cap):
            return None
        return f"give_up_after_bounces={cap} reached"

    async def _sweep_one(self, target: str, st: dict,
                         gave_up_targets: set[str]) -> None:
        if st.get("proposed"):
            return
        if not st.get("ownership_token"):
            await self._release_legacy_claim(target, st)
            return
        if st.get("closeout_started"):
            # Observe terminal effects after a lost response/restart. Never replay
            # the mutation; missing or partial GitHub evidence retains ownership.
            if self._closeout_completed is not None and await self._closeout_completed(target, st):
                await self._write_state(target, st, None, action_completed=True, outcome={
                    "outcome": "merged", "pr_number": st.get("closing_pr"),
                    "fallback": self._outcome_fallback(st)})
            return
        info = await self._pr_status_for(target)
        if info is not None and info.number in (st.get("unlinked_prs") or []):
            info = None
        # A terminal UI status or an armed PR does not prove the native actor
        # stopped. Keep both the operation and the original state when unknown.
        if not await self._work_stopped(st):
            return
        fallback = self._outcome_fallback(st)
        if info is not None and info.state == "merged":
            # Transfer to an exclusive terminal actor before touching GitHub.
            # A PR worker's old token cannot start/inject after this commit.
            closing = await self._ctx.reserve_target_action(
                target, st, source="terminal-closeout", reason="merged PR close-out",
                state={**st, "closing_pr": info.number, "closeout_started": True})
            if closing is None:
                return
            st = closing
            try:
                outcome = await self._close_out(target, info) if self._close_out else "closed"
            except Exception:
                await self._ctx.compare_target_state(target, st, {
                    **st, "ownership_wait_reason": "close-out outcome unknown; review required"})
                raise
            if outcome == "retry":
                await self._ctx.compare_target_state(target, st, {
                    **st, "ownership_wait_reason": "close-out incomplete; review required"})
                return  # keep the operation; never replay an uncertain external effect
            if outcome == "escalated":
                reason = "human gate: merged PR needs a human close-out"
                await self._give_up(target, st, reason, action_completed=True, outcome={
                    "outcome": "needs_human", "cause": reason,
                    "pr_number": info.number, "pr_url": info.url, "fallback": fallback})
                return
            if outcome == "unlinked":
                new_st = {k: v for k, v in st.items() if k not in {"branch", "closing_pr", "closeout_started"}}
                new_st["unlinked_prs"] = list(dict.fromkeys([
                    *(st.get("unlinked_prs") or []), info.number]))
                if await self._write_state(target, st, new_st, action_completed=True):
                    await self._ctx.emit("warn", f"{target}: merged PR has no batch linkage",
                                         kind="unlinked_pr", target=target)
                return
            attempts = await self._pr_doctor_attempts(
                f"{target.split('#', 1)[0]}#{info.number}")
            if await self._write_state(target, st, None, action_completed=True, outcome={
                    "outcome": "merged", "pr_number": info.number, "pr_url": info.url,
                    "first_pass_merge": attempts == 0 if attempts is not None else None,
                    "fallback": fallback}):
                await self._ctx.emit("info", f"{target} merged", kind="merged", target=target)
            return
        if target in gave_up_targets:
            # PR Doctor gave up: the same ladder path as a no-PR bounce (spec
            # 2026-09-18 §4) — this is still an implement attempt that failed to
            # land, not a person's decision, so it climbs a rung rather than
            # going straight to the owner.
            cause = "pr-doctor gave up"
            previous = st.get("model")
            ladder_policy = self._policy_for(st.get("product"))
            rung = next_rung(previous, floor=None,
                             default_model=ladder_policy["default_model"],
                             top=top_tier(ladder_policy))
            pd_outcome = {
                "outcome": "pr_gave_up", "cause": cause,
                "pr_number": info.number if info else None,
                "pr_url": info.url if info else None, "fallback": fallback}
            if rung is None:
                # Bounced at the top: the EXISTING give-up path (review R1 I2
                # item 1) — owner_action hold + comment + the retried ``held``
                # handoff, not the (now rung-only) escalate seam.
                gated = gated_ladder_top(ladder_policy)
                await self._give_up(target, st, "PR Doctor gave up at the top" + (
                    f"; {gated} needs approval" if gated else ""), outcome=pd_outcome)
                return
            dial = await self._dial_stops_the_ladder(target, ladder_policy)
            if dial is not None:
                # The operator's stricter dial ends the climb here (final review
                # I1): hand off rather than write a rung the queue will refuse.
                await self._give_up(target, st,
                                    f"PR Doctor gave up at {model_tier(previous) or 'sonnet'}; "
                                    f"{dial}", outcome=pd_outcome)
                return
            # Escalate FIRST, release the row SECOND (review R1 I2 item 2): if
            # the hold write raises, the durable trigger (this same
            # pr-doctor-gave-up event) re-fires next tick and the claim is
            # untouched meanwhile. If the release fails (stale ownership /
            # native work not yet observed stopped), stop — nothing else.
            await self._escalate(target, f"PR Doctor gave up; next attempt on {rung}", rung)
            if not await self._write_state(target, st, None, outcome=pd_outcome):
                return  # release lost the fence — leave it for the next sweep
            return
        if info is not None and info.state == "closed":
            # A PERSON closed the PR unmerged: their decision, not a failed
            # attempt — hands straight to the owner with the PR link (spec §4),
            # never climbs the ladder.
            reason = "PR closed unmerged"
            give_up_reason = f"{reason}: {info.url}" if info.url else reason
            await self._give_up(target, st, give_up_reason, outcome={
                "outcome": "abandoned", "cause": reason,
                "pr_number": info.number, "pr_url": info.url, "fallback": fallback})
            return
        if info is not None:
            active = await self._pr_doctor_active(target.split("#", 1)[0])
            if (self._handoff is not None and should_free_slot(info, active)
                    and not st.get("handed_off")):
                if await self._handoff(target, st, info):
                    await self._ctx.emit("info", f"{target} handed off to PR Doctor",
                                         kind="handoff", target=target)
            return
        if st.get("handed_off") or await self._session_alive(st):
            return
        # No attempt cap (spec 2026-09-18 §4): a no-PR bounce climbs one model
        # tier instead. ``attempts`` survives only on the outcome rows
        # (``attempt_no``) now, never on the row itself — the row is deleted on
        # every bounce and the item returns to the pool under whichever hold
        # was written for it.
        attempts = st.get("attempts", 0) + 1
        if not await self._session_ever_ran(st):
            if await self._write_state(target, st, None, outcome={
                    "outcome": "abandoned", "cause": "session never ran a turn",
                    "upgrade_from": ("no_pr",), "fallback": fallback}):
                await self._ctx.emit("info", f"released {target} (session never ran a turn)",
                                     kind="unclaimed", target=target)
            return
        previous = st.get("model")
        ladder_policy = self._policy_for(st.get("product"))
        top = top_tier(ladder_policy)
        rung = next_rung(previous, floor=None,
                         default_model=ladder_policy["default_model"], top=top)
        no_pr_outcome = {"outcome": "no_pr", "cause": "session ended without a mergeable PR",
                         "fallback": fallback}
        if rung is None:
            # Bounced at the top: the EXISTING give-up path (review R1 I2
            # item 1), not the (now rung-only) escalate seam. A top the
            # model-approval gate lowered says so, so the owner reads why the
            # ladder stopped below the tier they configured rather than
            # concluding the strongest model already failed.
            gated = gated_ladder_top(ladder_policy)
            # ONE fallback for "which tier did it bounce at": an unresolvable
            # previous model can only reach this branch when the top IS the tier
            # ``next_rung`` would have climbed from, so the top names it.
            bounced_at = model_tier(previous) or top
            reason = (f"bounced at {bounced_at}; {gated} needs approval" if gated else
                      f"bounced at {bounced_at}: "
                      f"no mergeable PR after {attempts} attempts")
            await self._give_up(target, st, reason, outcome=no_pr_outcome)
            return
        # ``next_rung`` climbs from sonnet when the previous model resolves to no
        # tier, so that is the fallback for every reason below the top.
        climbed_from = model_tier(previous) or "sonnet"
        dial = await self._dial_stops_the_ladder(target, ladder_policy)
        if dial is not None:
            # The operator set a stricter stop than the ladder's top (final review
            # I1). The queue would skip the next dispatch as ``needs_spec``, so a
            # rung hold here could never be consumed — hand off instead, with the
            # same owner_action hold + comment + ``gave_up`` row a top bounce gets.
            await self._give_up(target, st, f"bounced at {climbed_from}; {dial}",
                                outcome=no_pr_outcome)
            return
        # Escalate FIRST, release the row SECOND (review R1 I2 item 2): a raise
        # here leaves the claim untouched — the trigger (session ended, no PR)
        # is durable, so the next sweep retries the whole branch. A failed
        # release (stale fence) also leaves things exactly as they are.
        reason = f"bounced at {climbed_from}; next attempt on {rung}"
        await self._escalate(target, reason, rung)
        if not await self._write_state(target, st, None, outcome=no_pr_outcome):
            return  # release lost the fence — leave it for the next sweep

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
