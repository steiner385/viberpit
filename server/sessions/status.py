"""Pure derivation of a session's dashboard status from live driver signals.

Kept dependency-free and side-effect-free so it is exhaustively table-testable.
Precedence (highest first): a pending plan-approval, then a pending question
(``waiting_question``), then a pending permission (``waiting_input``), then
context condensing (``condensing``), then an active turn, then a completed turn,
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


# Statuses at which the assistant has handed output back to the user — either a
# finished turn (done/complete/needs_attention) or a turn paused awaiting the user
# (waiting_input/waiting_question/ready_review). These — and only these — advance a
# session's "active … ago" clock (see SessionOrchestrator._persist_status). `in_progress`
# (working, nothing handed back yet), `idle` (never ran), and the orchestrator overlays
# `limited`/`ended`/`archived` are deliberately excluded.
ACTIVITY_STATUSES: frozenset[str] = frozenset({
    "done", "complete", "needs_attention",
    "waiting_input", "waiting_question", "ready_review",
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
    if sig.turn_active:
        return "in_progress"
    if sig.turns_completed >= 1:
        if sig.last_turn_outcome == "failed":
            return "needs_attention"
        if sig.last_turn_outcome == "complete":
            return "complete"
        return "done"
    return "idle"
