"""Pure derivation of a session's dashboard status from live driver signals.

Kept dependency-free and side-effect-free so it is exhaustively table-testable.
Precedence (highest first): a pending plan-approval, then a pending question
(``waiting_question``), then a pending permission (``waiting_input``), then
context condensing (``condensing``), then an active turn that is backing off on a
529/5xx overload (``retrying``), then any other active turn, then a completed turn
(failed → needs_attention, hand_back → handed_back, complete → complete, else done),
else never-ran. ``limited``/``ended`` are orchestrator overlays and intentionally
NOT modelled here.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StatusSignals:
    turn_active: bool
    pending_permission: bool
    pending_question: bool
    pending_plan: bool
    turns_completed: int
    last_turn_outcome: str = "success"
    condensing: bool = False
    retrying: bool = False


# Statuses at which the assistant has handed output back to the user — either a
# finished turn (done/complete/needs_attention) or a turn paused awaiting the user
# (waiting_input/waiting_question/ready_review). These — and only these — advance a
# session's "active … ago" clock (see SessionOrchestrator._persist_status). `in_progress`
# (working, nothing handed back yet), `idle` (never ran), and the orchestrator overlays
# `limited`/`ended`/`archived` are deliberately excluded. `waiting_external` is also
# excluded: parking on a background task / wakeup is NOT a fresh return, so the clock keeps
# the prior turn's time and the card shows how long it has been parked (since the last
# turn) instead of resetting on every /loop re-arm.
ACTIVITY_STATUSES: frozenset[str] = frozenset({
    "done", "complete", "needs_attention",
    "waiting_input", "waiting_question", "ready_review", "handed_back",
})

# Statuses derived while the SDK still owns the turn (`sig.turn_active`, or condensing
# — which outranks it). Everything else means the turn has been handed back, so an SDK
# set_model issued mid-turn (a "switch after this turn") is now in force for the next
# turn — see SessionOrchestrator._settle_model_pending.
MID_TURN_STATUSES: frozenset[str] = frozenset({"in_progress", "condensing", "retrying"})

# Persisted statuses a freshly rebuilt driver may adopt as its attach snapshot
# (AgentDriver.seed_status): resting hand-backs with nothing live behind them. Mid-turn
# statuses can't be true on a fresh driver, and structured-pending ones
# (waiting_input / ready_review) advertise permission/plan cards that no longer
# exist to render. Shared by every engine's driver so the seedable vocabulary
# cannot drift between them.
SEEDABLE_STATUSES: frozenset[str] = frozenset({
    "done", "complete", "needs_attention", "waiting_question", "waiting_external",
    "handed_back",
})

# The statuses derive_status() produces from a STRUCTURED pending card — a plan
# approval, an AskUserQuestion, a permission prompt. Membership (not a re-listed
# precedence chain) is how the completion hold in SessionOrchestrator reads a
# driver's live signals: it derives the status, and if it names one of these, the
# session is handed back at THAT status. Collapsing all three into
# waiting_question is what put "Answer needed" on sessions whose only open card
# was a permission prompt.
STRUCTURED_HOLD_STATUSES: frozenset[str] = frozenset({
    "ready_review", "waiting_question", "waiting_input",
})


def derive_status(sig: StatusSignals) -> str:
    if sig.pending_plan:
        return "ready_review"
    if sig.pending_question:
        return "waiting_question"
    if sig.pending_permission:
        return "waiting_input"
    if sig.condensing:
        return "condensing"
    if sig.turn_active and sig.retrying:
        return "retrying"
    if sig.turn_active:
        return "in_progress"
    if sig.turns_completed >= 1:
        if sig.last_turn_outcome == "failed":
            return "needs_attention"
        # The agent's own hand_back signal (tool or marker): it needs the user. Its OWN
        # status, NOT waiting_question (#2321). The two are different promises — "there
        # is a card here to tap" vs "I wrote you a question, come read it" — and
        # collapsing them put "Answer needed" on sessions with nothing to answer twice
        # over (2026-09-08, 2026-09-11). Consumers must branch on the status, never on
        # "waiting_question with no card".
        if sig.last_turn_outcome == "hand_back":
            return "handed_back"
        if sig.last_turn_outcome == "complete":
            return "complete"
        return "done"
    return "idle"
