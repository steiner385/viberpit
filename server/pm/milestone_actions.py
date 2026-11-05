"""Release-milestone write rules (spec 2026-11-05 M4, M8–M10, §7): THE one place a
milestone is ticked, annotated or advanced. The HTTP routes in
``server/api/pm_milestones.py`` are thin wrappers, and Phase 3's session tools reach
these rules through those routes, never around them.

Every function takes the caller's ``AsyncSession`` and an :class:`Actor`, writes its
audit row in the same transaction, and never commits (the route does). A refused
write raises :class:`MilestoneActionError`; nothing here touches the definition (the
product repo's YAML is the only definition, M2).

Who may do what (M9, amended): every write needs a *person* identity, which includes a
session or the assistant delegating for its human. An *owner* action additionally
needs a direct request (``via == "api"``; fail closed): override advance,
re-ticking another person's tick, and resolving or editing another person's note.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.audit import record_audit
from server.db.models import (
    Milestone,
    MilestoneLadder,
    MilestoneMeasurement,
    MilestoneNote,
    _utcnow,
)
from server.pm.holds import actor_class
from server.pm.milestone_asks import ASK_STATUSES, DECISION_STATUSES, NOTE_KINDS
from server.pm.milestone_def import Condition, from_json
from server.pm.milestone_status import Reading, evaluate, gate_met
from server.pm.milestone_view import read_time_gate
from server.time_iso import iso_utc
from server.url_safety import MAX_URL_LEN, is_absolute_http_url

_STATUSES: dict[str, tuple[str, ...]] = {"decision": DECISION_STATUSES, "ask": ASK_STATUSES}
_DEFAULT_STATUS: dict[str, str] = {"decision": "locked", "ask": "open"}
_RESOLVED_ASK = frozenset({"done", "dropped"})

NUMBERS_SCOPE = "numbers"
STATUS_NOTE_MAX = 200
NOTE_TEXT_MAX = 500
OVERRIDE_REASON_MIN = 10
# The one ``via`` an owner action may arrive on: a direct web/API request (the route
# default). Fail closed, so a new ``via`` (mcp-external, internal, cli, …) is never owner.
OWNER_VIA = "api"


class MilestoneActionError(Exception):
    """A refused milestone write; routes answer ``status`` with
    ``{"error": code, **detail}``."""

    def __init__(self, status: int, code: str, detail: dict[str, Any] | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.detail = detail or {}


@dataclass(frozen=True)
class Actor:
    identity: str
    via: str


@dataclass(frozen=True)
class AdvanceResult:
    from_key: str | None
    to_key: str | None
    override: bool


def is_person(actor: Actor) -> bool:
    return actor_class(actor.identity) == "person"


def is_owner(actor: Actor) -> bool:
    """A person making a direct request. Allowlisted, not denylisted: a delegated
    session/assistant call carries the person's identity (M9/M10), and any other
    ``via`` is refused too."""
    return is_person(actor) and actor.via == OWNER_VIA


def _require_person(actor: Actor) -> None:
    if not is_person(actor):
        raise MilestoneActionError(403, "person_required")


def _clean(text: str | None) -> str | None:
    s = (text or "").strip()
    return s or None


def _link(url: str | None) -> str | None:
    """An optional link, trimmed: blank is none; anything but an absolute http(s)
    URL is refused (it is rendered as an ``href``)."""
    link = _clean(url)
    if link is not None and not is_absolute_http_url(link, max_len=MAX_URL_LEN):
        raise MilestoneActionError(400, "bad_url", {"max": MAX_URL_LEN})
    return link


async def _audit(db: AsyncSession, verb: str, actor: Actor, ladder: MilestoneLadder,
                 detail: dict[str, Any]) -> None:
    await record_audit(db, f"pm.milestone.{verb}", actor.identity,
                       {"ladder": ladder.id, **detail}, via=actor.via)


async def _milestone(db: AsyncSession, ladder: MilestoneLadder, key: str) -> Milestone:
    """A live rung (a removed one only while it is still current)."""
    m = (await db.execute(select(Milestone).where(
        Milestone.ladder_id == ladder.id, Milestone.key == key))).scalar_one_or_none()
    if m is None or (m.removed_at is not None and m.state != "current"):
        raise MilestoneActionError(404, "unknown_milestone", {"milestoneKey": key})
    return m


def _defined_condition(ladder: MilestoneLadder, milestone_key: str,
                       condition_key: str) -> Condition:
    definition = from_json(ladder.definition) if ladder.definition else None
    conds: Sequence[Condition] = ()
    if definition is not None:
        if milestone_key == NUMBERS_SCOPE:
            conds = definition.numbers
        else:
            conds = next((m.gate for m in definition.milestones if m.key == milestone_key), ())
    cond = next((c for c in conds if c.key == condition_key), None)
    if cond is None:
        raise MilestoneActionError(404, "unknown_condition",
                                   {"milestoneKey": milestone_key, "conditionKey": condition_key})
    if cond.measure.kind != "manual":
        raise MilestoneActionError(400, "not_manual", {"kind": cond.measure.kind})
    return cond


# ---------------------------------------------------------------- ticks


async def tick_condition(
    db: AsyncSession,
    ladder: MilestoneLadder,
    milestone_key: str,
    condition_key: str,
    *,
    actor: Actor,
    met: bool | None = None,
    value: float | None = None,
    evidence: str,
    url: str | None,
) -> MilestoneMeasurement:
    """Set a manual condition (a gate's ``{"met": bool}`` or a numbers entry's
    ``{"n": value}``) with the evidence for it, upserting its measurement row."""
    _require_person(actor)
    cond = _defined_condition(ladder, milestone_key, condition_key)
    if (met is None) == (value is None):
        raise MilestoneActionError(400, "bad_tick", {"message": "give exactly one of met or value"})
    if met is not None and not isinstance(met, bool):
        raise MilestoneActionError(400, "bad_tick", {"message": "met must be true or false"})
    if value is not None:
        if milestone_key != NUMBERS_SCOPE:
            raise MilestoneActionError(400, "bad_tick", {"message": "a value is only for numbers"})
        if (isinstance(value, bool) or not isinstance(value, int | float)
                or not math.isfinite(value)):
            raise MilestoneActionError(400, "bad_tick", {"message": "value must be a finite number"})
    proof = _clean(evidence)
    if proof is None:
        raise MilestoneActionError(400, "evidence_required")
    link = _link(url)

    row = (await db.execute(select(MilestoneMeasurement).where(
        MilestoneMeasurement.ladder_id == ladder.id,
        MilestoneMeasurement.scope == milestone_key,
        MilestoneMeasurement.key == condition_key))).scalar_one_or_none()
    previous = None
    if row is not None:
        previous = {"value": row.value, "by": row.manual_by}
        if row.manual_by not in (None, actor.identity) and not is_owner(actor):
            raise MilestoneActionError(403, "owner_required", {"tickedBy": row.manual_by})
    else:
        row = MilestoneMeasurement(ladder_id=ladder.id, scope=milestone_key, key=condition_key)
        db.add(row)

    now = _utcnow()
    reading_value: dict[str, Any] = {"met": met} if met is not None else {"n": value}
    row.value = reading_value
    row.status = evaluate(cond.measure, Reading(reading_value, now, None, link))
    row.measured_at = now
    row.fetched_at = now
    row.error = None
    row.manual_by = actor.identity
    row.manual_evidence = proof
    row.manual_url = link
    await db.flush()
    await _audit(db, "tick", actor, ladder, {
        "milestoneKey": milestone_key, "conditionKey": condition_key,
        "value": reading_value, "status": row.status, "evidence": proof, "url": link,
        "previous": previous})
    return row


# ---------------------------------------------------------------- status line


async def set_status_note(db: AsyncSession, ladder: MilestoneLadder, milestone_key: str, *,
                          actor: Actor, text: str | None) -> Milestone:
    """Set (or, with empty text, clear) a milestone's one-line status."""
    _require_person(actor)
    note = _clean(text)
    if note is not None and len(note) > STATUS_NOTE_MAX:
        raise MilestoneActionError(400, "too_long", {"max": STATUS_NOTE_MAX})
    m = await _milestone(db, ladder, milestone_key)
    previous = m.status_note
    m.status_note = note
    await db.flush()
    await _audit(db, "status_note", actor, ladder,
                 {"milestoneKey": milestone_key, "text": note, "previous": previous})
    return m


# ---------------------------------------------------------------- notes


def _note_text(text: str | None) -> str:
    s = _clean(text)
    if s is None:
        raise MilestoneActionError(400, "text_required")
    if len(s) > NOTE_TEXT_MAX:
        raise MilestoneActionError(400, "too_long", {"max": NOTE_TEXT_MAX})
    return s


def _check_status(kind: str, status: str) -> None:
    if status not in _STATUSES[kind]:
        raise MilestoneActionError(400, "bad_status",
                                   {"allowed": list(_STATUSES[kind]), "kind": kind})


async def add_note(db: AsyncSession, ladder: MilestoneLadder, *, actor: Actor, kind: str,
                   text: str, ref: str | None = None, url: str | None = None,
                   milestone_key: str | None = None, status: str | None = None) -> MilestoneNote:
    """Record an owner decision or ask, ladder-wide or on one live milestone."""
    _require_person(actor)
    if kind not in NOTE_KINDS:
        raise MilestoneActionError(400, "bad_kind", {"allowed": list(NOTE_KINDS)})
    body = _note_text(text)
    link = _link(url)
    state = status if status is not None else _DEFAULT_STATUS[kind]
    _check_status(kind, state)
    if milestone_key is not None:
        m = await _milestone(db, ladder, milestone_key)
        if m.removed_at is not None:
            raise MilestoneActionError(404, "unknown_milestone", {"milestoneKey": milestone_key})
    now = _utcnow()
    note = MilestoneNote(ladder_id=ladder.id, milestone_key=milestone_key, kind=kind,
                         ref=_clean(ref), text=body, url=link, status=state,
                         created_by=actor.identity, created_at=now, updated_at=now)
    if kind == "ask" and state in _RESOLVED_ASK:
        note.resolved_by, note.resolved_at = actor.identity, now
    db.add(note)
    await db.flush()
    await _audit(db, "note_add", actor, ladder, {
        "id": note.id, "kind": kind, "status": state, "milestoneKey": milestone_key,
        "ref": note.ref, "text": body})
    return note


async def update_note(db: AsyncSession, ladder: MilestoneLadder, note_id: str, *, actor: Actor,
                      status: str | None = None, text: str | None = None) -> MilestoneNote:
    """Change a note's status and/or text. Moving an ask to done/dropped stamps who
    resolved it; reopening clears that."""
    _require_person(actor)
    note = (await db.execute(select(MilestoneNote).where(
        MilestoneNote.id == note_id, MilestoneNote.ladder_id == ladder.id))).scalar_one_or_none()
    if note is None:
        raise MilestoneActionError(404, "unknown_note", {"id": note_id})
    if status is None and text is None:
        raise MilestoneActionError(400, "nothing_to_update")
    if status is not None:
        _check_status(note.kind, status)
    body = _note_text(text) if text is not None else None
    if note.created_by not in (None, actor.identity) and not is_owner(actor):
        raise MilestoneActionError(403, "owner_required", {"createdBy": note.created_by})

    before = {"status": note.status, "text": note.text}
    now = _utcnow()
    if body is not None:
        note.text = body
    if status is not None and status != note.status:
        note.status = status
        if note.kind == "ask":
            if status in _RESOLVED_ASK:
                note.resolved_by, note.resolved_at = actor.identity, now
            else:
                note.resolved_by, note.resolved_at = None, None
    note.updated_at = now
    await db.flush()
    await _audit(db, "note_update", actor, ladder, {
        "id": note.id, "kind": note.kind, "before": before,
        "after": {"status": note.status, "text": note.text}})
    return note


# ---------------------------------------------------------------- advance


async def advance(db: AsyncSession, ladder: MilestoneLadder, *, actor: Actor,
                  expected_current: str | None, override_reason: str | None,
                  now: datetime) -> AdvanceResult:
    """Move the ladder one rung: exit the current milestone (when its read-time gate
    is met, or with an owner override that snapshots what was unmet) and enter the
    next live one. With no current milestone, start the ladder at its first live rung.
    The ladder's rows are locked, and ``expected_current`` turns a concurrent second
    advance into a 409 ``stale_current`` instead of a double step."""
    _require_person(actor)
    rows = (await db.execute(select(Milestone).where(
        Milestone.ladder_id == ladder.id).with_for_update())).scalars().all()
    ordered = sorted(rows, key=lambda m: (m.ordinal is None, m.ordinal or 0))
    current = next((m for m in ordered if m.state == "current"), None)
    current_key = current.key if current is not None else None
    if current is None and any(m.state == "done" for m in ordered):
        raise MilestoneActionError(409, "ladder_complete")
    if current_key != expected_current:
        raise MilestoneActionError(409, "stale_current", {"current": current_key})
    reason = _clean(override_reason)

    # Sync renumbers only live rungs; a removed rung keeps a stale ordinal, so "next"
    # is never "ordinal > current.ordinal" but the lowest live upcoming rung.
    upcoming = next((m for m in ordered if m.removed_at is None and m.state == "upcoming"), None)
    if current is None:
        first = upcoming
        if first is None:
            raise MilestoneActionError(409, "no_milestones")
        first.state, first.entered_at = "current", now
        await db.flush()
        await _audit(db, "advance", actor, ladder,
                     {"from": None, "to": first.key, "override": False, "unmet": []})
        return AdvanceResult(None, first.key, False)

    measurements = (await db.execute(select(MilestoneMeasurement).where(
        MilestoneMeasurement.ladder_id == ladder.id))).scalars().all()
    gate = read_time_gate(ladder, current, measurements, now)
    unmet = [{"key": g["key"], "status": g["status"], "value": g["value"]}
             for g in gate if g["status"] != "met"]
    overridden = not gate_met(g["status"] for g in gate)
    if not overridden:
        if reason is not None:
            raise MilestoneActionError(400, "override_not_needed")
    else:
        if reason is None:
            raise MilestoneActionError(409, "gate_unmet", {"unmet": unmet})
        if len(reason) < OVERRIDE_REASON_MIN:
            raise MilestoneActionError(400, "reason_too_short", {"min": OVERRIDE_REASON_MIN})
        if not is_owner(actor):
            raise MilestoneActionError(403, "override_requires_owner")

    current.state, current.exited_at = "done", now
    if overridden:
        current.exit_override = {"by": actor.identity, "reason": reason,
                                 "at": iso_utc(now), "unmet": unmet}
    nxt = upcoming
    if nxt is not None:
        nxt.state, nxt.entered_at = "current", now
    await db.flush()
    await _audit(db, "advance", actor, ladder, {
        "from": current.key, "to": nxt.key if nxt is not None else None,
        "override": overridden, "unmet": unmet if overridden else [],
        **({"reason": reason} if overridden else {})})
    return AdvanceResult(current.key, nxt.key if nxt is not None else None, overridden)
