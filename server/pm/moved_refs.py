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

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from server.db.models import (
    AuditEvent, BacklogGroup, MilestoneLadder, MilestoneNote, MovedIssueRef,
    OwnerRecommendation, ProjectRepo, WorkItem, WorkItemDep)
from server.pm.holds import RECONCILER_ACTOR, release_on_move
from server.pm.product_repos import active_repos_sync
from server.pm.refs import ISSUE_REF_RE, resolve_moved, split_ref

__all__ = [
    "BadPairs", "MIGRATE_AUDIT_TYPE", "RECORD_AUDIT_TYPE", "migrate_pair", "migrate_pending",
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
    per bad pair. The API maps it to 422 ``bad_pairs``."""

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__("; ".join(f"{e['old']} -> {e['new']}: {e['message']}" for e in errors))


# --------------------------------------------------------------------------- read


def moved_map(db: Session) -> dict[str, str]:
    """Every recorded pair, ``{old_ref: new_ref}``. Load it once per pass."""
    return {o: n for o, n in db.execute(
        select(MovedIssueRef.old_ref, MovedIssueRef.new_ref)).all() if o and n}


def pairs_for_project(db: Session, project_id: str) -> list[MovedIssueRef]:
    return list(db.scalars(select(MovedIssueRef).where(MovedIssueRef.project_id == project_id)
                           .order_by(MovedIssueRef.old_ref)))


def pair_to_dict(row: MovedIssueRef) -> dict[str, Any]:
    from server.time_iso import iso_utc
    return {"old": row.old_ref, "new": row.new_ref,
            "status": "migrated" if row.migrated_at is not None else "pending",
            "migratedAt": iso_utc(row.migrated_at), "migration": row.migration}


# --------------------------------------------------------------------------- record


def _repos(db: Session, project_id: str) -> tuple[dict[str, str], dict[str, str]]:
    """(every repo of the product, its active repos), each ``{casefold: spelling}``."""
    all_repos = [r for r in db.scalars(select(ProjectRepo.repo).where(
        ProjectRepo.project_id == project_id)) if r]
    return ({r.casefold(): r for r in all_repos},
            {r.casefold(): r for r in active_repos_sync(db, project_id)})


def validate_pairs(db: Session, project_id: str,
                   pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Canonical ``(old, new)`` pairs, or :class:`BadPairs` naming every bad one.

    Rules: both refs are ``owner/name#N``; the old ref's repo is one of the product's
    repos (retired included), the new ref's an ACTIVE one; no duplicate old ref; no
    chain — a new ref may not be some pair's old ref, nor an old ref some pair's new
    ref, counting the pairs already stored. Repo spelling is canonicalised to the
    registered one, so the map's keys match the mirror's."""
    all_repos, active = _repos(db, project_id)
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
            if old_repo.casefold() not in all_repos:
                msg = f"{old_repo} is not a repo of this product"
            elif new_repo.casefold() not in active:
                msg = f"{new_repo} is not an active repo of this product"
            else:
                old = f"{all_repos[old_repo.casefold()]}#{old_n}"
                new = f"{active[new_repo.casefold()]}#{new_n}"
                if old.casefold() == new.casefold():
                    msg = "old and new are the same issue"
        if msg:
            errors.append({"old": old, "new": new, "message": msg})
        out.append((old, new))
    if errors:
        raise BadPairs(errors)

    stored = moved_map(db)
    batch_olds = [o.casefold() for o, _ in out]
    olds = {o.casefold() for o, _ in out} | {
        o.casefold() for o in stored if o.casefold() not in batch_olds}
    news = {n.casefold() for _, n in out} | {
        n.casefold() for o, n in stored.items() if o.casefold() not in batch_olds}
    for old, new in out:
        msg = None
        if batch_olds.count(old.casefold()) > 1:
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


def record_pairs(db: Session, project_id: str, pairs: Iterable[tuple[str, str]], *,
                 actor: str) -> list[MovedIssueRef]:
    """Upsert validated *pairs* on ``old_ref``. A pair re-pointed at a different new
    ref is migrated afresh (its earlier migration no longer describes it)."""
    rows: list[MovedIssueRef] = []
    for old, new in pairs:
        row = db.scalars(select(MovedIssueRef).where(MovedIssueRef.old_ref == old)).first()
        if row is None:
            row = MovedIssueRef(project_id=project_id, old_ref=old, new_ref=new, created_by=actor)
            db.add(row)
        elif row.new_ref != new:
            row.new_ref, row.created_by = new, actor
            row.migrated_at, row.migration = None, None
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
    release the old one with signal ``moved``. A ``decision`` hold is copied as-is
    — the person's pending choice moves with the issue — and cleared on the old item
    by the system actor, not through ``refuse_decision_release``'s generic path: the
    old repo is retired, so its proposal can no longer be acted on there."""
    kind = old.hold_kind
    if kind is None:
        return None
    copied = new.hold_kind is None
    if copied:
        for f in _HOLD_FIELDS:
            setattr(new, f, getattr(old, f))
    release_on_move(db, old)
    return {"kind": kind, "copied": copied, "decision": kind == "decision"}


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
    either work item is not mirrored yet. Already migrated → its recorded detail."""
    if row.migrated_at is not None:
        return row.migration
    old_ref, new_ref = row.old_ref or "", row.new_ref or ""
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
    """Retry every pending pair (of *project_id*, or all); returns how many migrated."""
    stmt = select(MovedIssueRef).where(MovedIssueRef.migrated_at.is_(None))
    if project_id is not None:
        stmt = stmt.where(MovedIssueRef.project_id == project_id)
    return sum(1 for row in db.scalars(stmt).all()
               if migrate_pair(db, row, now=now, actor=actor) is not None)


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
