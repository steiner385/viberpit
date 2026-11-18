"""The moved-issue map: old ref → new ref when a product's issues moved repos
(docs/superpowers/specs/2026-11-18-product-repo-lifecycle-design.md, "Moved-issue
map: map and migrate").

THE one implementation of recording a pair, migrating ViberPit-owned state from the
old work item to the new one, and reading the map. Everything here takes a SYNC
``Session``: the API routes and the pm_sync pass reach it through ``run_sync``.

A pair whose new (or old) work item is not mirrored yet stays *pending*
(``migrated_at`` NULL); ``migrate_pending`` retries it after each mirror advance.
Migration is idempotent: overlay fields copy only into empty slots, a rewritten dep
no longer names the old ref, and a re-run finds nothing left to move.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.db.models import (
    AuditEvent, BacklogGroup, DecisionProposal, MilestoneLadder, MilestoneNote, MovedIssueRef,
    OwnerRecommendation, ProjectRepo, WorkItem, WorkItemDep)
from server.pm.decisions import STATUS_PENDING
from server.pm.holds import ASSISTANT_PREFIX, RECONCILER_ACTOR, actor_class, release_on_move
from server.pm.mirror_store import move_decision_hold
from server.pm.product_repos import active_repos_sync
from server.pm.refs import ISSUE_REF_RE, resolve_moved, split_ref

log = logging.getLogger(__name__)

__all__ = [
    "AlreadyMigrated", "BadPairs", "MIGRATE_AUDIT_TYPE", "RECORD_AUDIT_TYPE", "may_record", "migrate_pair",
    "migrate_pending",
    "moved_map", "pair_to_dict", "pairs_for_project", "record_pairs", "resolve_moved",
    "suggest_pairs", "validate_pairs",
]

RECORD_AUDIT_TYPE = "pm.moved_ref.record"
MIGRATE_AUDIT_TYPE = "pm.moved_ref.migrate"

#: Overlay columns copied old → new where the new item's column is empty. The hold
#: columns move as ONE unit (``_HOLD_FIELDS``), never field by field.
_OVERLAY_FIELDS = ("feature_id", "reach", "impact", "effort", "confidence", "triage_state",
                   "classified_by", "classified_at")
_HOLD_FIELDS = ("hold_kind", "hold_reason", "hold_by", "hold_set_at", "hold_fingerprint",
                "hold_detail")
#: ``triage_state``'s column default: a freshly mirrored item reads it, so it is empty.
_EMPTY_TRIAGE = "unclassified"
#: ``MilestoneNote`` statuses that end a note (``milestone_asks``): not rewritten.
_CLOSED_NOTE_STATUSES = frozenset({"superseded", "done", "dropped"})
_PENDING_RECOMMENDATION = "pending"
_SUPERSEDED_RECOMMENDATION = "superseded"


class BadPairs(ValueError):
    """One or more pairs failed validation; ``errors`` is one ``{old, new, message}``
    per bad pair. The API maps it to 422 with ``code`` as the error."""

    code = "bad_pairs"

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__("; ".join(f"{e['old']} -> {e['new']}: {e['message']}" for e in errors))


class AlreadyMigrated(BadPairs):
    """A pair re-pointed at a different new ref after its state already moved: the
    first migration cannot be undone automatically, so it is refused (422
    ``already_migrated``) and left for a person to correct by hand."""

    code = "already_migrated"


# --------------------------------------------------------------------------- read


def moved_map(db: Session, project_id: str | None = None) -> dict[str, str]:
    """Recorded pairs, ``{old_ref: new_ref}`` — *project_id*'s only when a reader
    knows its product, every product's when it does not. Load it once per pass."""
    stmt = select(MovedIssueRef.old_ref, MovedIssueRef.new_ref)
    if project_id is not None:
        stmt = stmt.where(MovedIssueRef.project_id == project_id)
    return {o: n for o, n in db.execute(stmt).all() if o and n}


def pairs_for_project(db: Session, project_id: str) -> list[MovedIssueRef]:
    return list(db.scalars(select(MovedIssueRef).where(MovedIssueRef.project_id == project_id)
                           .order_by(MovedIssueRef.old_ref)))


def pair_to_dict(row: MovedIssueRef) -> dict[str, Any]:
    from server.time_iso import iso_utc
    return {"old": row.old_ref, "new": row.new_ref,
            "status": "migrated" if row.migrated_at is not None else "pending",
            "migratedAt": iso_utc(row.migrated_at), "migration": row.migration}


# --------------------------------------------------------------------------- record


def may_record(actor: str) -> bool:
    """Whether *actor* (``holds.actor_for_request``) may record pairs. Migration
    releases the old issue's hold, so this follows the hold routes: a person and the
    system may; of the agent class only the assistant acting on its operator's
    confirmed card (``assistant:<identity>``) may — a session or an unattributed
    loopback caller may not."""
    return actor_class(actor) != "agent" or actor.startswith(ASSISTANT_PREFIX)


def _repos(db: Session, project_id: str) -> tuple[dict[str, str], dict[str, str]]:
    """(the product's retired repos, its active repos), each ``{casefold: spelling}``."""
    retired = [r for r in db.scalars(select(ProjectRepo.repo).where(
        ProjectRepo.project_id == project_id, ProjectRepo.retired_at.is_not(None))) if r]
    return ({r.casefold(): r for r in retired},
            {r.casefold(): r for r in active_repos_sync(db, project_id)})


def validate_pairs(db: Session, project_id: str,
                   pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Canonical ``(old, new)`` pairs, or :class:`BadPairs` naming every bad one.

    Rules: both refs are ``owner/name#N``; the old ref's repo is a RETIRED repo of
    the product (migration releases the old item's hold, so an active repo's issue
    must never be reachable here), the new ref's an ACTIVE one; no duplicate old ref;
    an old ref recorded under another product is refused; no chain — a new ref may
    not be some pair's old ref, nor an old ref some pair's new ref, counting the
    pairs already stored. Repo spelling is canonicalised to the registered one, so
    the map's keys match the mirror's. A migrated pair re-pointed at a different new
    ref raises :class:`AlreadyMigrated`."""
    retired, active = _repos(db, project_id)
    items = [((o or "").strip(), (n or "").strip()) for o, n in pairs]
    errors: list[dict[str, str]] = []
    out: list[tuple[str, str]] = []
    for old, new in items:
        msg = None
        if not ISSUE_REF_RE.match(old) or not ISSUE_REF_RE.match(new):
            msg = "refs must look like owner/name#N"
        else:
            old_parts, new_parts = split_ref(old), split_ref(new)
            assert old_parts is not None and new_parts is not None  # the regex matched
            (old_repo, old_n), (new_repo, new_n) = old_parts, new_parts
            if old_repo.casefold() in active:
                msg = f"old repo {old_repo} must be retired first"
            elif old_repo.casefold() not in retired:
                msg = f"{old_repo} is not a repo of this product"
            elif new_repo.casefold() not in active:
                msg = f"{new_repo} is not an active repo of this product"
            else:
                old = f"{retired[old_repo.casefold()]}#{old_n}"
                new = f"{active[new_repo.casefold()]}#{new_n}"
                if old.casefold() == new.casefold():
                    msg = "old and new are the same issue"
        if msg:
            errors.append({"old": old, "new": new, "message": msg})
        out.append((old, new))
    if errors:
        raise BadPairs(errors)

    rows = {(r.old_ref or "").casefold(): r for r in db.scalars(select(MovedIssueRef))}
    migrated = [{"old": o, "new": n, "message": f"already migrated to {row.new_ref}; "
                 "correct it by hand"} for o, n in out
                if (row := rows.get(o.casefold())) is not None and row.migrated_at is not None
                and row.project_id == project_id and row.new_ref != n]
    if migrated:
        raise AlreadyMigrated(migrated)

    stored = moved_map(db)
    batch_olds = [o.casefold() for o, _ in out]
    olds = {o.casefold() for o, _ in out} | {
        o.casefold() for o in stored if o.casefold() not in batch_olds}
    news = {n.casefold() for _, n in out} | {
        n.casefold() for o, n in stored.items() if o.casefold() not in batch_olds}
    for old, new in out:
        msg = None
        row = rows.get(old.casefold())
        if row is not None and row.project_id != project_id:
            msg = "old ref is already recorded under another product"
        elif batch_olds.count(old.casefold()) > 1:
            msg = "old ref appears more than once"
        elif new.casefold() in olds:
            msg = f"{new} is itself mapped to another issue (chained moves are not allowed)"
        elif old.casefold() in news:
            msg = f"{old} is already the new ref of another pair (chained moves are not allowed)"
        if msg:
            errors.append({"old": old, "new": new, "message": msg})
    if errors:
        raise BadPairs(errors)
    return out


def _stored(db: Session, project_id: str, old: str) -> MovedIssueRef | None:
    return db.scalars(select(MovedIssueRef).where(
        MovedIssueRef.project_id == project_id, MovedIssueRef.old_ref == old)).first()


def record_pairs(db: Session, project_id: str, pairs: Iterable[tuple[str, str]], *,
                 actor: str) -> list[MovedIssueRef]:
    """Upsert validated *pairs* on ``(project_id, old_ref)``. A still-pending pair
    re-pointed at a different new ref just takes the new one (``validate_pairs``
    refuses it once migrated). A concurrent first insert of the same old ref loses
    the unique key to the other writer and re-reads its row instead of failing."""
    rows: list[MovedIssueRef] = []
    for old, new in pairs:
        row = _stored(db, project_id, old)
        if row is None:
            try:
                with db.begin_nested():
                    row = MovedIssueRef(project_id=project_id, old_ref=old, new_ref=new,
                                        created_by=actor)
                    db.add(row)
            except IntegrityError:
                row = _stored(db, project_id, old)
                if row is None:  # the winner belongs to another product
                    raise BadPairs([{"old": old, "new": new, "message":
                                     "old ref is already recorded under another product"}])
        if row.new_ref != new and row.migrated_at is None:
            row.new_ref, row.created_by, row.migration = new, actor, None
        rows.append(row)
    db.flush()
    return rows


# --------------------------------------------------------------------------- migrate


def _issue(db: Session, ref: str) -> WorkItem | None:
    parsed = split_ref(ref)
    if parsed is None:
        return None
    repo, number = parsed
    return db.scalars(select(WorkItem).where(
        func.lower(WorkItem.repo) == repo.lower(), WorkItem.number == number,
        WorkItem.kind == "issue")).first()


def _is_empty(field: str, value: Any) -> bool:
    return value is None or (field == "triage_state" and value == _EMPTY_TRIAGE)


def _copy_overlay(old: WorkItem, new: WorkItem) -> list[str]:
    copied = []
    for f in _OVERLAY_FIELDS:
        src = getattr(old, f)
        if not _is_empty(f, src) and _is_empty(f, getattr(new, f)):
            setattr(new, f, src)
            copied.append(f)
    return copied


def _move_hold(db: Session, old: WorkItem, new: WorkItem) -> dict[str, Any] | None:
    """Copy the old item's hold onto the new one when the new one has none, then
    release the old one with signal ``moved``. A ``decision`` hold goes through
    :func:`_move_decision` instead: it is one fact with its pending proposal."""
    kind = old.hold_kind
    if kind is None:
        return None
    if kind == "decision":
        return _move_decision(db, old, new)
    copied = new.hold_kind is None
    if copied:
        for f in _HOLD_FIELDS:
            setattr(new, f, getattr(old, f))
    release_on_move(db, old)
    return {"kind": kind, "copied": copied, "decision": False}


def _move_decision(db: Session, old: WorkItem, new: WorkItem) -> dict[str, Any]:
    """Move a ``decision`` hold WITH its pending proposal, so ``decisions.accept`` on
    the proposal (which loads ``proposal.work_item_id``) releases the hold on the new
    item. Same pairing as a repo rename (``mirror_store.move_decision_hold``). When
    the new item is already held, already has a pending proposal, or the old hold
    has no pending proposal to travel with, both stay on the old item untouched
    (``decision_kept_on_old``) — never a hold without its proposal or vice versa."""
    pending = list(db.scalars(select(DecisionProposal).where(
        DecisionProposal.work_item_id == old.id, DecisionProposal.status == STATUS_PENDING)))
    new_pending = db.scalars(select(DecisionProposal.id).where(
        DecisionProposal.work_item_id == new.id,
        DecisionProposal.status == STATUS_PENDING)).first()
    if not pending or new_pending is not None or new.hold_kind is not None:
        return {"kind": "decision", "copied": False, "decision": True,
                "decision_kept_on_old": True}
    for dp in pending:
        dp.work_item_id = new.id
    move_decision_hold(db, old.id, new.id)  # old holds a decision, new is unheld
    release_on_move(db, old)
    return {"kind": "decision", "copied": True, "decision": True,
            "proposals": [dp.id for dp in pending]}


def _rewrite_deps(db: Session, old_ref: str, new_ref: str, new: WorkItem) -> dict[str, int]:
    """Re-point every dep edge naming *old_ref* at *new_ref*. An edge that would
    duplicate an existing one (or point at itself) is deactivated instead."""
    refs = (old_ref, new_ref)
    rows = list(db.scalars(select(WorkItemDep).where(
        WorkItemDep.src_ref.in_(refs) | WorkItemDep.dst_ref.in_(refs))))
    touching = [d for d in rows if old_ref in (d.src_ref, d.dst_ref)]
    taken = {(d.src_ref, d.dst_ref, d.source) for d in rows if d not in touching}
    rewritten = deactivated = 0
    for d in touching:
        src = new_ref if d.src_ref == old_ref else d.src_ref
        dst = new_ref if d.dst_ref == old_ref else d.dst_ref
        if (src, dst, d.source) in taken or src == dst:
            if d.active is not False:
                d.active = False
                deactivated += 1
            continue
        if d.src_ref == old_ref:
            d.src_ref, d.src_work_item_id = new_ref, new.id
        if d.dst_ref == old_ref:
            d.dst_ref, d.dst_work_item_id = new_ref, new.id
        taken.add((src, dst, d.source))
        rewritten += 1
    return {"rewritten": rewritten, "deactivated": deactivated}


def _rewrite_notes(db: Session, project_id: str | None, old_ref: str, new_ref: str) -> int:
    notes = db.scalars(select(MilestoneNote).join(
        MilestoneLadder, MilestoneLadder.id == MilestoneNote.ladder_id).where(
        MilestoneLadder.project_id == project_id, MilestoneNote.ref == old_ref)).all()
    n = 0
    for note in notes:
        if note.status not in _CLOSED_NOTE_STATUSES:
            note.ref = new_ref
            n += 1
    return n


def _rewrite_recommendations(db: Session, project_id: str | None, old_ref: str, new_ref: str,
                             new: WorkItem) -> int:
    """Open (``pending``) recommendations on the old ref follow it; one that would
    duplicate a pending recommendation already on the new ref is superseded (the
    single-writer invariant: one pending row per rule/target/action)."""
    pending = list(db.scalars(select(OwnerRecommendation).where(
        OwnerRecommendation.project_id == project_id,
        OwnerRecommendation.status == _PENDING_RECOMMENDATION,
        OwnerRecommendation.target_ref.in_((old_ref, new_ref)))))
    on_new = {(r.rule, r.action) for r in pending if r.target_ref == new_ref}
    n = 0
    for r in pending:
        if r.target_ref != old_ref:
            continue
        if (r.rule, r.action) in on_new:
            r.status = _SUPERSEDED_RECOMMENDATION
        else:
            r.target_ref, r.work_item_id = new_ref, new.id
            on_new.add((r.rule, r.action))
        n += 1
    return n


def _drop_groups(db: Session, old_ref: str) -> int:
    """Delete the current plan generation's groups naming *old_ref*, so the grouper
    re-plans them from the live queue."""
    current = db.scalar(select(func.max(BacklogGroup.generation)))
    if current is None:
        return 0
    n = 0
    for g in db.scalars(select(BacklogGroup).where(BacklogGroup.generation == current)):
        if old_ref in (g.refs or ()):
            db.delete(g)
            n += 1
    return n


def migrate_pair(db: Session, row: MovedIssueRef, *, now: datetime,
                 actor: str = RECONCILER_ACTOR) -> dict | None:
    """Move ViberPit-owned state from ``row.old_ref``'s work item to
    ``row.new_ref``'s. ``None`` (and ``migration = {"status": "pending"}``) when
    either work item is not mirrored yet, and ``None`` with no writes at all while the
    old ref's repo is not retired (un-retired after recording — the same gate as
    ``validate_pairs``). Already migrated → its recorded detail.

    The row is re-read ``FOR UPDATE`` (a no-op on SQLite), so the API's migrate-now
    and a pm_sync retry racing on one pair serialise and the loser sees it done."""
    db.execute(select(MovedIssueRef).where(MovedIssueRef.id == row.id)
               .with_for_update().execution_options(populate_existing=True))
    if row.migrated_at is not None:
        return row.migration
    old_ref, new_ref = row.old_ref or "", row.new_ref or ""
    parsed = split_ref(old_ref)
    retired, _active = _repos(db, row.project_id or "")
    if parsed is None or parsed[0].casefold() not in retired:
        # The old repo was un-retired since the pair was recorded: migrating now would
        # release a hold on an active repo's issue. Stay pending, write nothing.
        return None
    old, new = _issue(db, old_ref), _issue(db, new_ref)
    if old is None or new is None:
        row.migration = {"status": "pending",
                         "missing": [r for r, w in ((old_ref, old), (new_ref, new)) if w is None]}
        return None
    detail: dict[str, Any] = {
        "status": "migrated",
        "overlay": _copy_overlay(old, new),
        "hold": _move_hold(db, old, new),
        "deps": _rewrite_deps(db, old_ref, new_ref, new),
        "notes": _rewrite_notes(db, row.project_id, old_ref, new_ref),
        "recommendations": _rewrite_recommendations(db, row.project_id, old_ref, new_ref, new),
        "groupsDropped": _drop_groups(db, old_ref),
    }
    row.migrated_at, row.migration = now, detail
    from server.audit import redact_detail
    db.add(AuditEvent(type=MIGRATE_AUDIT_TYPE, identity=actor, via="moved_ref",
                      detail=redact_detail({"projectId": row.project_id, "old": old_ref,
                                            "new": new_ref, "migration": detail})))
    db.flush()
    return detail


def migrate_pending(db: Session, *, project_id: str | None = None, now: datetime,
                    actor: str = RECONCILER_ACTOR) -> int:
    """Retry every pending pair (of *project_id*, or all); returns how many migrated.
    Each pair runs in its own savepoint: one that fails (logged at INFO, retried
    next pass) is rolled back alone and never stalls the others."""
    stmt = select(MovedIssueRef.id).where(MovedIssueRef.migrated_at.is_(None))
    if project_id is not None:
        stmt = stmt.where(MovedIssueRef.project_id == project_id)
    n = 0
    for row_id in db.scalars(stmt).all():
        try:
            with db.begin_nested():
                row = db.get(MovedIssueRef, row_id)
                if row is not None and migrate_pair(db, row, now=now, actor=actor) is not None:
                    n += 1
        except Exception as exc:  # noqa: BLE001 - one poison pair must not stall the rest
            log.info("moved_refs.migrate_failed id=%s: %s", row_id, exc)
    return n


# --------------------------------------------------------------------------- suggest


@dataclass(frozen=True)
class _Titled:
    ref: str
    title: str


def _fold(title: str | None) -> str:
    return " ".join((title or "").split()).casefold()


def _unique_by_title(items: Iterable[_Titled]) -> dict[str, _Titled]:
    """``{folded title: item}`` keeping only titles that occur exactly once."""
    seen: dict[str, _Titled] = {}
    dupes: set[str] = set()
    for it in items:
        key = _fold(it.title)
        if not key:
            continue
        if key in seen:
            dupes.add(key)
        seen[key] = it
    return {k: v for k, v in seen.items() if k not in dupes}


def suggest_pairs(db: Session, from_repo: str, to_repo: str) -> list[dict[str, str]]:
    """Proposed pairs by exact title match (case and whitespace folded) between
    *from_repo*'s OPEN issues and *to_repo*'s issues. A title shared by two issues
    on either side is ambiguous and skipped, as is any ref already in the map.
    Suggestions only: nothing here writes."""
    def items(repo: str, *, open_only: bool) -> list[_Titled]:
        stmt = select(WorkItem.repo, WorkItem.number, WorkItem.title).where(
            func.lower(WorkItem.repo) == repo.lower(), WorkItem.kind == "issue")
        if open_only:
            stmt = stmt.where(WorkItem.state == "open")
        return [_Titled(f"{r}#{n}", t or "") for r, n, t in db.execute(
            stmt.order_by(WorkItem.number)).all()]

    stored = moved_map(db)
    mapped = set(stored) | set(stored.values())
    olds = _unique_by_title(items(from_repo, open_only=True))
    news = _unique_by_title(items(to_repo, open_only=False))
    out = []
    for key, old in olds.items():
        new = news.get(key)
        if new is None or old.ref in mapped or new.ref in mapped:
            continue
        out.append({"old": old.ref, "new": new.ref, "title": old.title})
    return sorted(out, key=lambda p: split_ref(p["old"]) or (p["old"], 0))
