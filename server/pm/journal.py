"""Write-ahead journal for reconciler mutations (spec 003 §6 step 4). Callers own the transaction."""
from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.db.models import GhWriteJournal

from .reconcile_diff import Mutation

DRY_RUN_MARK = "dry_run"
PARKED_PREFIX = "parked: "
INTERRUPTED = "interrupted"
STATES = ("pending", "applied", "failed", "parked", "dry_run")
#: The Projects v2 fields the reconciler owns; the only ones whose applied values
#: are replayed back into the diff engine's ownership check.
OWNED_FIELDS = ("Status", "Score", "Stage")


def own_write_key(node_id: str | None, field: str | None, value: object) -> tuple[str, str, str]:
    """The echo-suppression key for one (item, field, value) triple.

    THE single source of truth for that key shape: ``recent_own_writes`` (journal
    side) and ``mirror_store.upsert_project_items`` (GitHub-read side) must agree
    exactly or an echo is misread as a human edit. Numbers are normalized to float
    before serializing because GitHub returns ``480`` for a Score we wrote as
    ``480.0`` — same value, different JSON text."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = float(value)
    return (node_id or "", field or "", json.dumps(value, sort_keys=True, default=str))


def record_pending(db: Session, mutations: Sequence[Mutation], *, batch_id: str,
                   now: datetime, dry_run: bool) -> list[str]:
    ids: list[str] = []
    for m in mutations:
        row = GhWriteJournal(
            target_node_id=m.target_node_id, field=m.field or m.kind,
            old_value={"value": m.old_value}, new_value={"value": m.new_value},
            mutation_id=m.key(), initiated_by=m.initiated_by, batch_id=batch_id,
            applied=False, error=DRY_RUN_MARK if dry_run else None, created_at=now, updated_at=now)
        db.add(row)
        db.flush()
        ids.append(row.id)
    return ids


def mark_applied(db: Session, row_ids: Sequence[str], now: datetime) -> None:
    for rid in row_ids:
        row = db.get(GhWriteJournal, rid)
        if row is not None:
            row.applied, row.error, row.updated_at = True, None, now


def mark_failed(db: Session, row_id: str, error: str, now: datetime) -> None:
    row = db.get(GhWriteJournal, row_id)
    if row is not None:
        row.applied, row.error, row.updated_at = False, error[:500], now


def park(db: Session, row_id: str, reason: str, now: datetime) -> None:
    mark_failed(db, row_id, PARKED_PREFIX + reason, now)


def state_of(row: GhWriteJournal) -> str:
    if row.applied:
        return "applied"
    if row.error is None:
        return "pending"
    if row.error == DRY_RUN_MARK:
        return "dry_run"
    if row.error.startswith(PARKED_PREFIX):
        return "parked"
    return "failed"


def failure_count(db: Session, mutation_id: str) -> int:
    rows = db.scalars(select(GhWriteJournal).where(GhWriteJournal.mutation_id == mutation_id)).all()
    return sum(1 for r in rows if state_of(r) == "failed" and r.error != INTERRUPTED)


def is_parked(db: Session, mutation_id: str) -> bool:
    rows = db.scalars(select(GhWriteJournal).where(GhWriteJournal.mutation_id == mutation_id)).all()
    return any(state_of(r) == "parked" for r in rows)


def applied_keys(db: Session) -> set[str]:
    return {r.mutation_id for r in db.scalars(
        select(GhWriteJournal).where(GhWriteJournal.applied.is_(True))).all() if r.mutation_id}


def recent_own_writes(db: Session, since: datetime) -> set[tuple[str, str, str]]:
    rows = db.scalars(select(GhWriteJournal).where(
        GhWriteJournal.applied.is_(True), GhWriteJournal.updated_at >= since)).all()
    return {own_write_key(r.target_node_id, r.field, (r.new_value or {}).get("value"))
            for r in rows}


def own_applied_values(db: Session) -> dict[tuple[str, str], object]:
    """The value the reconciler last APPLIED per ``(item_node_id, field)``.

    Feeds ``reconcile_diff.diff_fields(own_values=...)``: a mirrored value that is
    still exactly our own last write is not a human decision, so it must not make
    the field sticky (the field's ``gh_updated_at`` is OUR write time, which would
    otherwise out-rank every older decision forever)."""
    out: dict[tuple[str, str], object] = {}
    latest: dict[tuple[str, str], datetime] = {}
    rows = db.scalars(select(GhWriteJournal).where(
        GhWriteJournal.applied.is_(True), GhWriteJournal.field.in_(OWNED_FIELDS))).all()
    for r in rows:
        key = (r.target_node_id or "", r.field or "")
        seen = latest.get(key)
        if seen is not None and seen >= r.updated_at:
            continue
        latest[key] = r.updated_at
        out[key] = (r.new_value or {}).get("value")
    return out


def recent_dry_run_keys(db: Session, since: datetime) -> set[str]:
    """Mutation keys that already have a dry-run row not older than *since*.

    A dry run plans the same set every tick; without this the journal grows by the
    whole plan every two minutes for as long as dry-run mode is left on."""
    rows = db.scalars(select(GhWriteJournal).where(
        GhWriteJournal.error == DRY_RUN_MARK, GhWriteJournal.created_at >= since)).all()
    return {r.mutation_id for r in rows if r.mutation_id}


def prune_dry_run(db: Session, older_than: datetime) -> int:
    """Delete dry-run rows older than *older_than*; returns the count deleted.

    Only dry-run rows: they record what WOULD have happened, so they have no audit
    value once superseded, unlike applied/failed/parked rows which are the record."""
    rows = db.scalars(select(GhWriteJournal).where(
        GhWriteJournal.error == DRY_RUN_MARK,
        GhWriteJournal.created_at < older_than)).all()
    for row in rows:
        db.delete(row)
    return len(rows)


def recover_interrupted(db: Session, older_than: datetime) -> int:
    n = 0
    for row in db.scalars(select(GhWriteJournal).where(
            GhWriteJournal.applied.is_(False), GhWriteJournal.error.is_(None),
            GhWriteJournal.created_at < older_than)).all():
        row.error, row.updated_at = INTERRUPTED, older_than
        n += 1
    return n


def list_rows(db: Session, *, state: str | None, limit: int) -> list[GhWriteJournal]:
    rows = db.scalars(select(GhWriteJournal).order_by(GhWriteJournal.created_at.desc())).all()
    if state is not None:
        if state not in STATES:
            raise ValueError(f"unknown journal state {state!r}")
        rows = [r for r in rows if state_of(r) == state]
    return list(rows[:limit])
