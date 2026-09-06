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
    "Daily session cap bounds cost")."""
    mode: str
    unclassified_count: int
    pending_count: int
    no_fit_clusters: tuple[NoFitCluster, ...]
    stale_features: tuple[StaleFeature, ...]
    day_spawns: int
    day_cap: int


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
    threshold = int(config.get("drift_threshold", 5))
    qualifying = [c for c in state.no_fit_clusters if len(c.item_numbers) >= threshold]
    if qualifying:
        top = max(qualifying, key=lambda c: len(c.item_numbers))
        return ProposeFeatureMapDiff(
            suggestion=top.suggestion, item_numbers=top.item_numbers, repo=top.repo)

    # 3. Staleness: indicator only, lowest non-idle priority.
    if state.stale_features:
        return NudgeStale(slugs=tuple(f.slug for f in state.stale_features))

    return Idle()
