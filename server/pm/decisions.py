"""Decision proposals (spec 2026-09-18 §6) — the ONE writer, accepter and rejecter.

A coding session that hits a genuine product decision does not guess and does not
hand the issue back: it drafts two to six options, names the one it recommends,
says why, and parks the item on a ``decision`` hold until a person picks. This
module is the single implementation of that lifecycle (constitution VI): the HTTP
routes, the ``propose_decision`` session tool and the assistant's
``resolve_decision`` all call these functions — none of them re-derives a hold, a
status transition or an audit shape of its own.

Every function takes the caller's ``AsyncSession`` and leaves the COMMIT to the
caller, so a route can compose a proposal with whatever else its request writes
(and so a failed GitHub comment cannot half-commit a decision). Async, like the
other API/tool taps in ``server/pm/holds.py`` (``release_via_api``,
``apply_session_hold``), because ``record_audit`` is.

**``now`` must be an AWARE UTC datetime** (``datetime.now(timezone.utc)``, as every
caller in ``server/api/pm.py`` passes). It lands in ``created_at`` / ``resolved_at``
/ ``WorkItem.hold_set_at``, all declared ``DateTime(timezone=True)``; a naive value
from ``server/db/clock.py`` would be stored through the session time zone on
Postgres — local wall clock in a UTC column — and would make the ``resolved_at``
ordering in :func:`accepted_text_for_refs` compare mixed awareness.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from server.db.models import DECISION_PROPOSAL_STATUSES, DecisionProposal, WorkItem
from server.pm import holds

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

log = logging.getLogger(__name__)

#: The proposal lifecycle, unpacked from the ONE status vocabulary in
#: ``server/db/models.py`` so this module never spells the three strings itself.
STATUS_PENDING, STATUS_ACCEPTED, STATUS_REJECTED = DECISION_PROPOSAL_STATUSES

#: Spec §6: fewer than two is not a decision, more than six is a survey. Pinned
#: here because the tool schema, the route and the tests all quote the bounds.
MIN_OPTIONS = 2
MAX_OPTIONS = 6

#: How much of the rationale the hold reason quotes. The hold line is read in a
#: board cell and a dialog header, so it is a lead-in, not the whole argument —
#: the full text lives on the proposal.
HOLD_REASON_RATIONALE_CHARS = 120

#: The hold reason's lead-in, so the reason a person reads on the card and the
#: prefix a test (or a later parser) matches are one string.
HOLD_REASON_PREFIX = "decision pending: "


class DecisionError(ValueError):
    """The proposal is malformed. Every route that calls this module maps it to 422."""


class DecisionConflict(DecisionError):
    """The proposal is well-formed but the ITEM's state refuses it: a decision is
    already pending, a person has parked the item, or the proposal being resolved
    is no longer pending. A subclass so a caller that does not care keeps catching
    ``DecisionError``, and the routes map it to 409 rather than 422 — nothing about
    the request needs fixing, the world moved."""


def hold_reason(rationale: str) -> str:
    """THE ``decision`` hold reason for *rationale* — one formatter, because the
    proposal writer and anything that recognises such a hold must agree."""
    return f"{HOLD_REASON_PREFIX}{rationale.strip()[:HOLD_REASON_RATIONALE_CHARS]}"


def normalize_options(options: "Iterable[Any]") -> list[dict[str, Any]]:
    """Validate and normalise the proposed options (spec §6).

    Answers a list of ``{"key", "label", "detail"}`` dicts with whitespace
    stripped and nothing else carried over — the row is quoted verbatim into a
    prompt later, so an unvalidated extra field would travel with it. Raises
    ``DecisionError`` on anything the person could not choose between: the wrong
    number of options, a missing/duplicate key, a missing label.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in options or []:
        if not isinstance(raw, dict):
            raise DecisionError("each option must be an object with key/label/detail")
        key = str(raw.get("key") or "").strip()
        label = str(raw.get("label") or "").strip()
        detail_raw = raw.get("detail")
        detail = str(detail_raw).strip() if detail_raw is not None else None
        if not key:
            raise DecisionError("each option needs a non-empty key")
        if not label:
            raise DecisionError(f"option {key!r} needs a non-empty label")
        if key in seen:
            raise DecisionError(f"duplicate option key {key!r}")
        seen.add(key)
        out.append({"key": key, "label": label, "detail": detail or None})
    if not (MIN_OPTIONS <= len(out) <= MAX_OPTIONS):
        raise DecisionError(
            f"a decision needs between {MIN_OPTIONS} and {MAX_OPTIONS} options, got {len(out)}")
    return out


def option_keys(proposal: DecisionProposal) -> list[str]:
    """The keys of *proposal*'s options, tolerating a row written before (or
    outside) ``normalize_options`` — a malformed stored row answers ``[]`` rather
    than raising in a reader."""
    out = []
    for opt in proposal.options or []:
        if isinstance(opt, dict) and opt.get("key"):
            out.append(str(opt["key"]))
    return out


def chosen_option(proposal: DecisionProposal) -> dict[str, Any] | None:
    """The option *proposal*'s ``chosen_key`` names, or ``None`` when it is
    unresolved (or names an option that is no longer there)."""
    if not proposal.chosen_key:
        return None
    for opt in proposal.options or []:
        if isinstance(opt, dict) and str(opt.get("key") or "") == proposal.chosen_key:
            return opt
    return None


def decision_text(proposal: DecisionProposal) -> str | None:
    """THE rendering of an accepted decision as one line of prompt text — the one
    formatter (constitution VI), so what the inbox showed, what the issue comment
    said and what the next implement run reads cannot drift apart. ``None`` when
    the proposal has no resolvable chosen option."""
    opt = chosen_option(proposal)
    if opt is None:
        return None
    label = str(opt.get("label") or opt.get("key") or "")
    detail = opt.get("detail")
    return f"Decision ({label}): {detail or label} — {proposal.rationale or ''}"


async def propose(db, wi: WorkItem, *, options: "Iterable[Any]", recommended_key: str,
                  rationale: str, actor: str, now: "datetime",
                  via: str = "api") -> DecisionProposal:
    """Record a decision proposal for *wi* and park the item on it (spec §6).

    The proposal row and the ``decision`` hold are written in the SAME unit of
    work — a proposal nobody is waiting on, or a hold with nothing to resolve it,
    are both dead ends — and the caller commits them together.

    Refuses (``DecisionError``) when the item already has a pending proposal, and
    when a PERSON has parked the item: their park wins, and a session that finds
    one should report back rather than quietly overwriting it. A machine's park
    (the runner's hand-off, an agent's evidence hold) is superseded — the
    decision is the more specific answer to "why is this stopped".
    """
    from sqlalchemy import select

    from server.audit import record_audit
    from server.pm.desired_state import HELD_KINDS
    opts = normalize_options(options)
    keys = [o["key"] for o in opts]
    recommended = str(recommended_key or "").strip()
    if recommended not in keys:
        raise DecisionError(f"recommended_key {recommended!r} is not one of {keys}")
    text = str(rationale or "").strip()
    if not text:
        raise DecisionError("rationale is required")
    existing = (await db.execute(select(DecisionProposal).where(
        DecisionProposal.work_item_id == wi.id,
        DecisionProposal.status == STATUS_PENDING))).scalars().first()
    if existing is not None:
        raise DecisionConflict("a decision is already pending")
    if wi.hold_kind in HELD_KINDS and wi.hold_by and holds.actor_class(wi.hold_by) == "person":
        raise DecisionConflict(
            f"{wi.hold_by} has parked this item ({wi.hold_kind}); a decision cannot replace it")
    proposal = DecisionProposal(
        work_item_id=wi.id, options=opts, recommended_key=recommended, rationale=text,
        status=STATUS_PENDING, proposed_by=actor,
        session_id=actor.split(":", 1)[1] if actor.startswith("session:") else None,
        created_at=now)
    db.add(proposal)
    reason = hold_reason(text)
    holds.set_decision_hold(wi, reason=reason, actor=actor, now=now)
    await db.flush()
    await record_audit(db=db, type="pm.decision.propose", identity=actor,
                       detail={"workItemId": wi.id, "proposalId": proposal.id,
                               "actorClass": holds.actor_class(actor)}, via=via)
    await record_audit(db=db, type="pm.hold.set", identity=actor,
                       detail={"workItemId": wi.id, "kind": "decision", "reason": reason,
                               "actorClass": holds.actor_class(actor)}, via=via)
    return proposal


async def accept(db, proposal: DecisionProposal, *, chosen_key: str, actor: str,
                 now: "datetime", via: str = "api") -> DecisionProposal:
    """Take the decision: record the chosen option and release the hold (spec §6).

    This is the ONE release of a ``decision`` hold anywhere in the system —
    ``release_via_api`` refuses the kind precisely so that a Retry or an Implement
    now cannot end a decision without one being taken.
    """
    from sqlalchemy import select

    from server.audit import record_audit
    _require_pending(proposal)
    key = str(chosen_key or "").strip()
    keys = option_keys(proposal)
    if key not in keys:
        raise DecisionError(f"chosen_key {key!r} is not one of {keys}")
    proposal.status = STATUS_ACCEPTED
    proposal.chosen_key = key
    proposal.resolved_by = actor
    proposal.resolved_at = now
    wi = (await db.execute(select(WorkItem).where(
        WorkItem.id == proposal.work_item_id))).scalar_one_or_none()
    released = holds.clear_decision_hold(wi, actor=actor) if wi is not None else False
    await db.flush()
    await record_audit(db=db, type="pm.decision.accept", identity=actor,
                       detail={"workItemId": proposal.work_item_id,
                               "proposalId": proposal.id, "chosenKey": key,
                               "actorClass": holds.actor_class(actor)}, via=via)
    if released and wi is not None:
        await record_audit(db=db, type="pm.hold.release", identity=actor,
                           detail={"workItemId": wi.id, "kind": "decision",
                                   "actorClass": holds.actor_class(actor),
                                   "signal": "decision"},  # from holds.RELEASE_SIGNALS
                           via=via)
    return proposal


async def reject(db, proposal: DecisionProposal, *, actor: str, now: "datetime",
                 via: str = "api") -> DecisionProposal:
    """Refuse the options and hand the item to the person (spec §6): the proposal
    is rejected and the ``decision`` hold becomes the person's ``owner_action``
    park with ``DECISION_REJECT_REASON``. Nothing re-proposes automatically — the
    machine already tried, and decision 3 of the spec keeps the next move human.

    The hold write is skipped when the item is parked by a hold that is NOT this
    proposal's own ``decision`` hold — a person who said "I'm on this" between the
    proposal and the rejection. Their reason and ``hold_set_at`` are theirs to
    keep, and the item is already where the rejection would put it: parked, needing
    a human. The proposal is still rejected and still audited; only the overwrite
    is dropped.
    """
    from sqlalchemy import select

    from server.audit import record_audit
    from server.pm.desired_state import HELD_KINDS
    _require_pending(proposal)
    proposal.status = STATUS_REJECTED
    proposal.resolved_by = actor
    proposal.resolved_at = now
    wi = (await db.execute(select(WorkItem).where(
        WorkItem.id == proposal.work_item_id))).scalar_one_or_none()
    kept = wi is not None and wi.hold_kind in HELD_KINDS and wi.hold_kind != "decision"
    if wi is not None and not kept:
        holds.set_reject_decision_hold(wi, actor=actor, now=now)
    elif kept and wi is not None:
        log.info("decision.reject_hold_kept proposal=%s item=%s kind=%s by=%s",
                 proposal.id, wi.id, wi.hold_kind, wi.hold_by)
    await db.flush()
    await record_audit(db=db, type="pm.decision.reject", identity=actor,
                       detail={"workItemId": proposal.work_item_id,
                               "proposalId": proposal.id,
                               "actorClass": holds.actor_class(actor)}, via=via)
    if wi is not None and not kept:
        await record_audit(db=db, type="pm.hold.set", identity=actor,
                           detail={"workItemId": wi.id, "kind": "owner_action",
                                   "reason": holds.DECISION_REJECT_REASON,
                                   "actorClass": holds.actor_class(actor)}, via=via)
    return proposal


def _require_pending(proposal: DecisionProposal) -> None:
    """A resolved proposal is resolved: re-accepting would re-clear a hold the
    person may have set again since, and re-rejecting would re-park an item that
    is already moving."""
    if proposal.status != STATUS_PENDING:
        raise DecisionConflict(f"this decision is already {proposal.status}")


async def pending_list(db) -> list[tuple[DecisionProposal, WorkItem | None]]:
    """Every pending proposal, newest first, paired with its work item — what the
    inbox lists beside the classification proposals.

    Returns the work item rather than a wire dict: the repo/number/title come off
    it directly, and the owning product is resolved by the route through the one
    existing ``repo → project`` helper (``owning_projects_by_repo``) instead of a
    second join here that would be a second answer to "who owns this repo". An
    outer join, so a proposal whose item was un-mirrored still reaches the inbox
    to be rejected rather than disappearing with the hold it left behind.
    """
    from sqlalchemy import select
    rows = (await db.execute(
        select(DecisionProposal, WorkItem)
        .outerjoin(WorkItem, WorkItem.id == DecisionProposal.work_item_id)
        .where(DecisionProposal.status == STATUS_PENDING)
        .order_by(DecisionProposal.created_at.desc()))).all()
    return [(p, w) for p, w in rows]


async def accepted_text_for_refs(db, refs: "Sequence[str]") -> dict[str, str]:
    """``owner/repo#N`` → the accepted decision for that issue, newest first, as
    the one line the implement prompt quotes (``decision_text``).

    Refs with no accepted proposal are absent from the result — the caller adds
    nothing to the prompt rather than a "no decision" line. Blind to a malformed
    ref, like every other reader of ``split_ref``.
    """
    from sqlalchemy import select, tuple_

    from server.pm.refs import split_ref
    parts = [p for p in (split_ref(r) for r in refs) if p is not None]
    if not parts:
        return {}
    items = (await db.execute(select(WorkItem).where(
        WorkItem.kind == "issue",
        tuple_(WorkItem.repo, WorkItem.number).in_(parts)))).scalars().all()
    ref_by_id = {w.id: f"{w.repo}#{w.number}" for w in items}
    if not ref_by_id:
        return {}
    # Newest last so a later row overwrites an earlier one for the same item:
    # an issue can be decided more than once over its life, and the prompt
    # carries the decision in force, not the first one ever taken.
    rows = (await db.execute(
        select(DecisionProposal)
        .where(DecisionProposal.work_item_id.in_(list(ref_by_id)),
               DecisionProposal.status == STATUS_ACCEPTED)
        .order_by(DecisionProposal.resolved_at.asc().nulls_first(),
                  DecisionProposal.created_at.asc()))).scalars().all()
    out: dict[str, str] = {}
    for proposal in rows:
        text = decision_text(proposal)
        if text:
            out[ref_by_id[proposal.work_item_id]] = text
    return out
