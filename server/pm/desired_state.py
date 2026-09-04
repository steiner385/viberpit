"""Desired Projects v2 field values derived from ccswitch's decision layer (spec 003 §6 step 2).

Every value carries the timestamp of the decision that produced it so the diff engine can apply
the ownership rule (§3): push only when decided_at is strictly newer than GitHub's gh_updated_at.
Status precedence: Done > session signals > needs-human > Blocked > Ready > Backlog.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .score import rice_score

STATUS_BACKLOG = "Backlog"
STATUS_READY = "Ready"
STATUS_IN_PROGRESS = "In progress"
STATUS_BLOCKED = "Blocked"
STATUS_NEEDS_HUMAN = "Needs human"
STATUS_DONE = "Done"
STATUS_OPTIONS: tuple[str, ...] = (
    STATUS_BACKLOG, STATUS_READY, STATUS_IN_PROGRESS, STATUS_BLOCKED, STATUS_NEEDS_HUMAN, STATUS_DONE)

ACTIVE_SESSION_STATUSES = frozenset({"in_progress", "retrying", "condensing"})
HUMAN_WAIT_SESSION_STATUSES = frozenset(
    {"waiting_question", "waiting_input", "ready_review", "needs_attention"})


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
    needs_human_label: bool
    initiative_stage: str | None
    initiative_stage_at: datetime | None


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
        return Decision(STATUS_NEEDS_HUMAN, f.session_at)
    if f.session_status in ACTIVE_SESSION_STATUSES:
        return Decision(STATUS_IN_PROGRESS, f.session_at)
    if f.needs_human_label or f.triage_state == "needs_human":
        return Decision(STATUS_NEEDS_HUMAN, f.classified_at)
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
