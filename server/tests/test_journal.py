from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from server.db.models import Base, GhWriteJournal
from server.pm import journal
from server.pm.reconcile_diff import Mutation

T = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
M1 = Mutation("set_field", "PVTI_1", "Status", "Backlog", "Ready", "status:o/r#1")
M2 = Mutation("add_item", "PVT_1", None, None, "I_9", "membership:o/r#9")


def _db():
    e = create_engine("sqlite://"); Base.metadata.create_all(e); return Session(e)


def test_record_pending_then_applied_and_failed():
    db = _db()
    ids = journal.record_pending(db, [M1, M2], batch_id="b1", now=T, dry_run=False)
    rows = db.query(GhWriteJournal).order_by(GhWriteJournal.created_at).all()
    assert [r.mutation_id for r in rows] == [M1.key(), M2.key()]
    assert all(r.applied is False and r.error is None and r.batch_id == "b1" for r in rows)
    assert rows[0].old_value == {"value": "Backlog"} and rows[0].new_value == {"value": "Ready"}
    journal.mark_applied(db, [ids[0]], T); journal.mark_failed(db, ids[1], "boom", T)
    assert journal.applied_keys(db) == {M1.key()}
    assert journal.failure_count(db, M2.key()) == 1
    assert journal.recent_own_writes(db, T - timedelta(hours=1)) == {("PVTI_1", "Status", '"Ready"')}
    assert [r.mutation_id for r in journal.list_rows(db, state="failed", limit=10)] == [M2.key()]


def test_dry_run_rows_are_not_pending_or_failures():
    db = _db()
    journal.record_pending(db, [M1], batch_id="d1", now=T, dry_run=True)
    assert journal.failure_count(db, M1.key()) == 0
    assert journal.recover_interrupted(db, older_than=T + timedelta(hours=1)) == 0
    assert [r.error for r in journal.list_rows(db, state="dry_run", limit=5)] == [journal.DRY_RUN_MARK]


def test_park_and_interrupted_recovery():
    db = _db()
    ids = journal.record_pending(db, [M1, M2], batch_id="b2", now=T, dry_run=False)
    journal.park(db, ids[0], "failed twice", T)
    assert journal.is_parked(db, M1.key()) is True
    assert journal.recover_interrupted(db, older_than=T + timedelta(minutes=10)) == 1
    assert db.get(GhWriteJournal, ids[1]).error == journal.INTERRUPTED
    assert journal.failure_count(db, M2.key()) == 0   # interrupted is not a counted failure
