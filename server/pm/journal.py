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
    return {(r.target_node_id or "", r.field or "",
             json.dumps((r.new_value or {}).get("value"), sort_keys=True, default=str)) for r in rows}


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
