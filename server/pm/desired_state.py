"""Desired Projects v2 field values derived from ccswitch's decision layer (spec 003 §6 step 2).

Every value carries the timestamp of the decision that produced it so the diff engine can apply
the ownership rule (§3): push only when decided_at is strictly newer than GitHub's gh_updated_at.
Status precedence: Done > session signals > Held (hold) > Blocked > Ready > Backlog.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .score import rice_score

STATUS_BACKLOG = "Backlog"
STATUS_READY = "Ready"
STATUS_IN_PROGRESS = "In progress"
STATUS_BLOCKED = "Blocked"
STATUS_HELD = "Held"
STATUS_DONE = "Done"
STATUS_OPTIONS: tuple[str, ...] = (
    STATUS_BACKLOG, STATUS_READY, STATUS_IN_PROGRESS, STATUS_BLOCKED, STATUS_HELD, STATUS_DONE)
#: Columns that only state a fact ``derive_status`` reads: Held ⇔ a parking hold or a
#: hand-back. A card left in one after its fact is gone is not a decision the
#: ownership rule protects (``reconcile_diff.diff_fields``) — a deliberate park is a
#: recorded hold, never a bare column (#3106).
FACT_STATUSES: frozenset[str] = frozenset({STATUS_HELD})


ACTIVE_SESSION_STATUSES = frozenset({"in_progress", "retrying", "condensing"})
# A person's turn: a question, a permission prompt, a plan awaiting review.
HUMAN_WAIT_SESSION_STATUSES = frozenset(
    {"waiting_question", "waiting_input", "ready_review", "handed_back"})
# A failed turn (spec 2026-09-18 §7): a bounce the runner's sweep handles, never a
# parked card — it left the human-wait set so the ladder, not a person, sees it.
FAILED_TURN_SESSION_STATUSES = frozenset({"needs_attention"})
# THE liveness union every "is there a live session on this ref" reader uses.
LIVE_SESSION_STATUSES = (ACTIVE_SESSION_STATUSES | HUMAN_WAIT_SESSION_STATUSES
                         | FAILED_TURN_SESSION_STATUSES)

#: Hold kinds that park the card (spec 2026-09-18 §7). evidence / stronger_model exit
#: on their own and never reach the board.
HELD_KINDS: frozenset[str] = frozenset({"owner_action", "decision"})


@dataclass(frozen=True)
class ItemFacts:
    ref: str
    issue_state: str | None
    issue_closed_at: datetime | None
    triage_state: str | None
    classified_at: datetime | None
    reach: int | None
    impact: int | None
    effort: int | None
    confidence: int | None
    session_status: str | None
    session_at: datetime | None
    merged_pr_at: datetime | None
    open_blocker_refs: tuple[str, ...]
    blockers_at: datetime | None
    initiative_stage: str | None
    initiative_stage_at: datetime | None
    # Spec 2026-09-18 §3: the hold on the item. Only owner_action / decision park it.
    hold_kind: str | None = None
    # When that hold was set. Spec §7: the hold's OWN timestamp is the decision — the
    # writers of a hold (reject_triage, apply_session_hold, _restore_overlay's legacy
    # branch) never touch ``classified_at``, and on a freshly-proposed item that is
    # NULL, which ``should_push(None, …)`` reads as "no decision" and never stamps.
    hold_set_at: datetime | None = None


@dataclass(frozen=True)
class Decision:
    value: str | float | None
    decided_at: datetime | None


@dataclass(frozen=True)
class DesiredItem:
    ref: str
    status: Decision
    score: Decision
    stage: Decision


def derive_status(f: ItemFacts) -> Decision:
    if f.merged_pr_at is not None:
        return Decision(STATUS_DONE, f.merged_pr_at)
    if f.issue_state == "closed":
        return Decision(STATUS_DONE, f.issue_closed_at)
    if f.session_status in HUMAN_WAIT_SESSION_STATUSES:
        return Decision(STATUS_HELD, f.session_at)
    # A failed turn is in-flight, not Ready: the session is still live, the runner's
    # sweep owns the bounce, and ``pm.tree.issue_tier`` reads this exact union for
    # its ``in-flight`` tier. Letting it fall through would push a dying session's
    # card back to Ready while the queue still skips the ref as ``live_session``.
    if f.session_status in (ACTIVE_SESSION_STATUSES | FAILED_TURN_SESSION_STATUSES):
        return Decision(STATUS_IN_PROGRESS, f.session_at)
    if f.hold_kind in HELD_KINDS:
        return Decision(STATUS_HELD, f.hold_set_at or f.classified_at)
    if f.open_blocker_refs:
        return Decision(STATUS_BLOCKED, f.blockers_at)
    if f.triage_state == "classified":
        return Decision(STATUS_READY, f.classified_at)
    return Decision(STATUS_BACKLOG, None)


def derive(f: ItemFacts) -> DesiredItem:
    score = rice_score(f.reach, f.impact, f.effort, f.confidence) \
        if f.triage_state == "classified" else None
    return DesiredItem(
        ref=f.ref,
        status=derive_status(f),
        score=Decision(score, f.classified_at if score is not None else None),
        stage=Decision(f.initiative_stage, f.initiative_stage_at) if f.initiative_stage
        else Decision(None, None))
