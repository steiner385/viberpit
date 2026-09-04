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


def test_own_applied_values_keeps_the_latest_write_per_field():
    """The diff engine asks "what did WE last put here?" — so only the newest applied
    row per (node, field) counts, and only for the fields the reconciler owns."""
    db = _db()
    older = Mutation("set_field", "PVTI_1", "Status", None, "In progress", "status:x")
    newer = Mutation("set_field", "PVTI_1", "Status", "In progress", "Blocked", "status:x")
    score = Mutation("set_field", "PVTI_1", "Score", None, 480.0, "score:x")
    pending = Mutation("set_field", "PVTI_2", "Status", None, "Ready", "status:y")
    ids = journal.record_pending(db, [older, newer, score, pending], batch_id="b", now=T,
                                 dry_run=False)
    journal.mark_applied(db, [ids[0]], T - timedelta(hours=2))
    journal.mark_applied(db, [ids[1]], T)
    journal.mark_applied(db, [ids[2]], T)
    # `pending` is never applied -> it is not one of our writes
    assert journal.own_applied_values(db) == {
        ("PVTI_1", "Status"): "Blocked", ("PVTI_1", "Score"): 480.0}
    # a membership mutation (field == kind) is not an owned field
    journal.mark_applied(db, journal.record_pending(db, [M2], batch_id="b", now=T,
                                                    dry_run=False), T)
    assert ("PVT_1", "add_item") not in journal.own_applied_values(db)


def test_own_write_key_normalizes_numbers():
    """GitHub echoes 480 for a Score we wrote as 480.0 — the journal side and the
    mirror side must still produce the SAME key or the echo reads as a human edit."""
    assert journal.own_write_key("PVTI_1", "Score", 480) == \
        journal.own_write_key("PVTI_1", "Score", 480.0)
    assert journal.own_write_key("PVTI_1", "Status", "Ready") == ("PVTI_1", "Status", '"Ready"')
    # bools are not numbers here (True must not collapse to 1.0)
    assert journal.own_write_key("n", "f", True) != journal.own_write_key("n", "f", 1)
    db = _db()
    journal.mark_applied(db, journal.record_pending(
        db, [Mutation("set_field", "PVTI_1", "Score", None, 480.0, "score:x")],
        batch_id="b", now=T, dry_run=False), T)
    assert journal.recent_own_writes(db, T - timedelta(hours=1)) == {
        journal.own_write_key("PVTI_1", "Score", 480)}


def test_dry_run_keys_and_prune_bound_the_journal():
    db = _db()
    journal.record_pending(db, [M1], batch_id="d1", now=T - timedelta(days=2), dry_run=True)
    journal.record_pending(db, [M2], batch_id="d2", now=T, dry_run=True)
    applied = journal.record_pending(db, [M1], batch_id="b", now=T - timedelta(days=2),
                                     dry_run=False)
    journal.mark_applied(db, applied, T - timedelta(days=2))
    since = T - timedelta(hours=24)
    assert journal.recent_dry_run_keys(db, since) == {M2.key()}
    assert journal.prune_dry_run(db, older_than=since) == 1      # only the stale dry-run row
    db.flush()
    states = sorted(journal.state_of(r) for r in db.query(GhWriteJournal).all())
    assert states == ["applied", "dry_run"]                      # the applied row survives


def test_park_and_interrupted_recovery():
    db = _db()
    ids = journal.record_pending(db, [M1, M2], batch_id="b2", now=T, dry_run=False)
    journal.park(db, ids[0], "failed twice", T)
    assert journal.is_parked(db, M1.key()) is True
    assert journal.recover_interrupted(db, older_than=T + timedelta(minutes=10)) == 1
    assert db.get(GhWriteJournal, ids[1]).error == journal.INTERRUPTED
    assert journal.failure_count(db, M2.key()) == 0   # interrupted is not a counted failure
