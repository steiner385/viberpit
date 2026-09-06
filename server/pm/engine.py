"""Pure pm-triage decision engine (design §6) — no I/O, never touches the DB or
orchestrator.

Mirrors ``sdlc.engine`` purity: ``next_triage_action`` is a total function over a
plain-dataclass ``TriageState`` view (built by the ``PmTriageLoop`` from DB
counts) that returns one frozen ``TriageAction`` the loop executes. The loop
injects the real I/O (count the backlog, spawn the classify session, open the
map-diff PR, set the stale indicator); this module only decides *what should
happen next*.

Priority ladder, highest first (a tick does exactly one thing):
  1. ``mode == "off"``          → NeedsHuman (automation disabled)
  2. unclassified backlog + budget → ClassifyBatch      (design §6.1)
  3. accepted-no_fit cluster ≥ threshold → ProposeFeatureMapDiff (design §6.3)
  4. stale features            → NudgeStale (indicator only; design §6.4)
  5. otherwise                 → Idle

Auto-linking mechanical PRs by closing keyword (design §6.2) is a *sync-time*
rule with no session, so it is NOT an engine action — it lives in the loop's
pre-classify pass. Rejection is signal (design §6): a rejected proposal moves
its item to ``needs_human`` and the loop never re-classifies it, so a
``needs_human`` item simply never appears in ``unclassified_count`` — no engine
branch is needed for it.

**The drift branch is latched** by ``TriageState.open_map_diff_suggestions``,
the pm counterpart of the ``sdlc.engine`` propose-only gate (which returns
``AwaitApproval`` while a stage's artifact PR is open). ``no_fit`` clusters are
*permanent*: accepting a proposal never clears its ``no_fit_suggestion``, so the
cluster stays at/over threshold forever. Without the latch every tick would
re-propose the same capability, and the spawn-time dedupe cannot save us — it
matches only *non-terminal* sessions, so each time the map-diff session finishes
the next tick opens another archaeology-diff PR, unbounded (the drift branch is
also not covered by the day cap, which gates ``ClassifyBatch`` only). A
suggestion in ``open_map_diff_suggestions`` is therefore excluded from the
qualifying clusters.

Note: ``sdlc.engine`` defines its own frozen ``NeedsHuman``. The two are
*distinct types* — an ``isinstance`` check against the wrong import silently
never matches, so import ``NeedsHuman`` from the engine whose action you handle.
"""
from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Pure input views (constructed by the loop from DB counts — NOT ORM rows)     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NoFitCluster:
    """A group of accepted ``no_fit`` proposals sharing a suggested feature name
    (design §6.3). ``item_numbers`` are the orphaned work-item numbers whose
    capability the map does not yet cover."""
    suggestion: str
    item_numbers: tuple[int, ...]
    repo: str


@dataclass(frozen=True)
class StaleFeature:
    """A ``discovery``/``delivering`` feature with no linked activity in the
    config window (design §6.4)."""
    slug: str
    state: str
    days_idle: int


@dataclass(frozen=True)
class TriageState:
    """Everything the engine needs to decide the next action for one tick.

    ``unclassified_count`` counts open work items that are ``unclassified`` AND
    have no pending proposal (the loop excludes ``needs_human`` and
    already-proposed items). ``pending_count`` is informational (surfaced in
    status). ``day_spawns``/``day_cap`` bound classify-session cost (design §6.1
    "Daily session cap bounds cost").

    ``open_map_diff_suggestions`` latches the drift branch: a suggestion belongs
    here when a map diff for it is already **in flight or already resolved** —
    i.e. either a non-terminal map-diff session exists for that suggestion, or a
    ``Feature`` with that slug now exists (the diff merged, the capability is on
    the map, and the orphaned items get re-proposed through the accept path
    rather than through a second PR). ``no_fit`` clusters never shrink, so
    without this the engine would re-propose the same capability forever."""
    mode: str
    unclassified_count: int
    pending_count: int
    no_fit_clusters: tuple[NoFitCluster, ...]
    stale_features: tuple[StaleFeature, ...]
    day_spawns: int
    day_cap: int
    # Defaulted (and trailing) on purpose: callers built before the latch
    # existed stay valid.
    open_map_diff_suggestions: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Actions (what the loop should do next) — closed union                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ClassifyBatch:
    """Spawn one classify session over ``count`` unclassified items (design §6.1,
    the same path as onboarding Step 2)."""
    count: int


@dataclass(frozen=True)
class ProposeFeatureMapDiff:
    """A ``no_fit`` cluster crossed the drift threshold — open an
    archaeology-diff PR scoped to the unnamed capability (design §6.3)."""
    suggestion: str
    item_numbers: tuple[int, ...]
    repo: str


@dataclass(frozen=True)
class NudgeStale:
    """Set the low-key staleness indicator on these feature slugs (design §6.4).
    No session, no alarm."""
    slugs: tuple[str, ...]


@dataclass(frozen=True)
class Idle:
    """Nothing to do this tick."""


@dataclass(frozen=True)
class NeedsHuman:
    """Automation is off / a human must intervene. ``reason`` hits the feed."""
    reason: str = ""


TriageAction = ClassifyBatch | ProposeFeatureMapDiff | NudgeStale | Idle | NeedsHuman


# --------------------------------------------------------------------------- #
# Decision core                                                                #
# --------------------------------------------------------------------------- #


def next_triage_action(state: TriageState, config: dict) -> TriageAction:
    """Decide the next pm-triage action. Pure + total.

    ``config`` keys: ``drift_threshold`` (default 5), ``batch_size`` (default
    50). ``batch_size`` bounds one classify session; ``drift_threshold`` is the
    minimum cluster size before proposing a map diff.
    """
    if state.mode == "off":
        return NeedsHuman(reason="pm-triage automation is off")

    # 1. Classify the new — highest priority while budget remains.
    if state.unclassified_count > 0 and state.day_spawns < state.day_cap:
        batch_size = int(config.get("batch_size", 50))
        return ClassifyBatch(count=min(state.unclassified_count, batch_size))

    # 2. Drift: the largest cluster at/over threshold becomes a map-diff PR.
    #    Latched suggestions (diff in flight or already merged onto the map) are
    #    excluded so one capability is proposed exactly once — see module docs.
    threshold = int(config.get("drift_threshold", 5))
    latched = set(state.open_map_diff_suggestions)
    qualifying = [
        c for c in state.no_fit_clusters
        if len(c.item_numbers) >= threshold and c.suggestion not in latched
    ]
    if qualifying:
        # Secondary key on ``suggestion``: cluster order comes from an unordered
        # SELECT, so equal-size clusters must not alternate between ticks.
        top = max(qualifying, key=lambda c: (len(c.item_numbers), c.suggestion))
        return ProposeFeatureMapDiff(
            suggestion=top.suggestion, item_numbers=top.item_numbers, repo=top.repo)

    # 3. Staleness: indicator only, lowest non-idle priority.
    if state.stale_features:
        return NudgeStale(slugs=tuple(f.slug for f in state.stale_features))

    return Idle()
