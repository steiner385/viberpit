"""Pure onboarding state-machine engine (design 2026-08-31 §5).

No I/O — the way ``server/sdlc/engine.py`` is pure. A total function over a
frozen ``ProjectOnboardingView`` (counts the API/orchestrator build from DB
rows) returning the next wizard action. The wizard UI is a VIEW over this
engine; these transitions are the single source of truth for what each
``Project.onboarding_state`` offers next.

State order (phase-1 constant PROJECT_ONBOARDING_STATES):

    registered ─▶ syncing ─▶ map_proposed ─▶ map_merged ─▶ classifying ─▶ live

The propose-only gate mirrors the SDLC engine: the engine NEVER advances the
state itself. It returns ``AwaitMapPr``/``AwaitTriage`` while a gate is open and
a human tap (map PR merge, triage queue drain → advance) moves the state on.
"""
from __future__ import annotations

from dataclasses import dataclass

from server.db.models import PROJECT_ONBOARDING_STATES


# --------------------------------------------------------------------------- #
# Pure input view (built from DB rows by callers — NOT the ORM models)         #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ProjectOnboardingView:
    """The counts the engine decides over. ``backfill_done_count`` is how many
    of the project's ``repo_count`` repos have finished pm-sync backfill."""
    state: str
    repo_count: int
    backfill_done_count: int
    feature_count: int
    open_unclassified_count: int
    pending_proposal_count: int


# --------------------------------------------------------------------------- #
# Onboarding actions (what the wizard should offer / do next)                   #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Register:
    """No repos attached yet — the wizard's register step is incomplete."""


@dataclass(frozen=True)
class AwaitBackfill:
    """pm-sync is still mirroring history; nothing to do but wait."""


@dataclass(frozen=True)
class ProposeMap:
    """Backfill done — offer/spawn the archaeology map-synthesis session."""


@dataclass(frozen=True)
class AwaitMapPr:
    """The map PR is the curation gate: wait for the human to merge + advance."""


@dataclass(frozen=True)
class Classify:
    """Map merged + features indexed — offer/spawn backlog classification."""


@dataclass(frozen=True)
class AwaitTriage:
    """Proposals are queued (or the queue is empty); the human drains the inbox
    and advances the project to live."""


@dataclass(frozen=True)
class Live:
    """Onboarding complete — the continuous loop takes over (phase 3)."""


@dataclass(frozen=True)
class OnboardingNeedsHuman:
    """Nothing the engine can safely do; ``reason`` is surfaced in the wizard."""
    reason: str = ""


OnboardingAction = (
    Register | AwaitBackfill | ProposeMap | AwaitMapPr
    | Classify | AwaitTriage | Live | OnboardingNeedsHuman)


# --------------------------------------------------------------------------- #
# Decision core                                                                 #
# --------------------------------------------------------------------------- #

def next_onboarding_action(view: ProjectOnboardingView) -> OnboardingAction:
    """Decide the next onboarding action for a project. Pure + total."""
    state = view.state

    if state in ("registered", "syncing"):
        # `syncing` is declared but no loop writes it (pm-sync backfills without
        # touching onboarding_state), so `registered` carries the same ladder.
        if view.repo_count <= 0:
            return Register()
        if view.backfill_done_count < view.repo_count:
            return AwaitBackfill()
        return ProposeMap()

    if state == "map_proposed":
        # The state is set only after a synthesis session was spawned. Whether
        # its PR is open, merged or never opened is not correlated here; the
        # wizard shows the PR search link and a "propose again" beside the wait.
        return AwaitMapPr()

    if state == "map_merged":
        if view.feature_count <= 0:
            return OnboardingNeedsHuman(
                reason="map merged but no features were indexed")
        if view.open_unclassified_count > 0:
            return Classify()
        return AwaitTriage()

    if state == "classifying":
        if view.open_unclassified_count > 0:
            return Classify()
        return AwaitTriage()

    if state == "live":
        return Live()

    return OnboardingNeedsHuman(reason=f"unknown onboarding state {state!r}")


def advance_onboarding_state(state: str) -> str:
    """Successor state for a human-gate advance tap; ``live`` is absorbing.

    Raises ``ValueError`` on an unknown state so a corrupt row surfaces loudly
    (mirrors ``sdlc.engine.advance_stage``)."""
    if state not in PROJECT_ONBOARDING_STATES:
        raise ValueError(f"unknown onboarding state {state!r}")
    idx = PROJECT_ONBOARDING_STATES.index(state)
    if idx + 1 >= len(PROJECT_ONBOARDING_STATES):
        return "live"
    return PROJECT_ONBOARDING_STATES[idx + 1]
