"""_rollup_passed must DRAIN the stale backlog, not fold one batch and stop (#833).

Measured on the live DB, 2026-08-30: ci_test_result held 27.4M rows against a
7-day design window — 12.3M rows stale, 12.0M of them the passed rows this fold
exists to delete, the oldest 36 days old. Nothing was erroring; the fold was
losing an arithmetic race, silently:

    _ROLLUP_BATCH = 10,000 rows x one batch per 900s tick  ->  ceiling ~960k/day
    KinDash ingest alone                                   ->  ~2M rows/day

These tests pin the new contract: keep folding batches until the backlog is
gone or a time budget runs out, commit progress per batch, and say so loudly
when stopping early — a losing race must be visible long before the table is
4x oversized.
"""
from __future__ import annotations

import logging

import pytest
from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import CiTestDaily, CiTestResult

NOW = 1_800_000_000.0
OLD = NOW - 10 * 86400.0  # older than the 7-day raw window


def _engine(monkeypatch=None, batch=None, clock=None):
    from server.automations import cicd_doctor as mod
    if batch is not None and monkeypatch is not None:
        monkeypatch.setattr(mod, "_ROLLUP_BATCH", batch)
    engine = mod.CicdDoctorLoop.__new__(mod.CicdDoctorLoop)
    engine._clock = clock or (lambda: NOW)
    return engine


async def _seed(n: int, *, test_id: str = "t", outcome: str = "passed") -> None:
    async with db_session() as db:
        for run_id in range(1, n + 1):
            db.add(CiTestResult(repo="o/r", run_id=run_id, suite="s",
                                test_id=test_id, outcome=outcome, created_ts=OLD))


@pytest.mark.asyncio
async def test_rollup_drains_a_multi_batch_backlog_in_one_call(monkeypatch):
    """25 stale rows with a batch size of 10 must fold in ONE call — the old
    one-batch-per-tick behavior would leave 15 behind for later ticks and lose
    any race where ingest outpaces one batch per tick."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    await _seed(25)
    engine = _engine(monkeypatch, batch=10)

    await engine._rollup_passed({})

    async with db_session() as db:
        raw = (await db.execute(select(CiTestResult))).scalars().all()
        daily = (await db.execute(select(CiTestDaily))).scalars().all()
    assert raw == [], f"backlog not drained: {len(raw)} stale rows left behind"
    assert len(daily) == 1 and daily[0].passed == 25, \
        "every folded row must be counted exactly once across batches"


@pytest.mark.asyncio
async def test_rollup_stops_at_the_time_budget_and_keeps_partial_progress(monkeypatch, caplog):
    """When the budget expires mid-drain the fold must stop AFTER a completed
    batch (progress persisted, nothing double-counted) and WARN that it is
    behind — silence is how the 12M-row backlog happened."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    await _seed(25)

    # Deterministic clock: every call advances past the budget, so exactly one
    # batch fits regardless of machine speed.
    t = {"now": NOW}
    def clock():
        t["now"] += 1000.0
        return t["now"]
    engine = _engine(monkeypatch, batch=10, clock=clock)

    with caplog.at_level(logging.WARNING):
        await engine._rollup_passed({"rollup_budget_s": 0.5})

    async with db_session() as db:
        raw_n = len((await db.execute(select(CiTestResult))).scalars().all())
        daily = (await db.execute(select(CiTestDaily))).scalars().all()
    assert raw_n == 15, "exactly one full batch should have been folded"
    assert daily and daily[0].passed == 10, "the folded batch must be committed"
    behind = [r for r in caplog.records
              if "rollup" in r.message and "behind" in r.message]
    assert behind, "stopping early must be logged as a WARNING naming the lag"
    assert any("15" in r.message for r in behind), (
        "the WARNING must name the rows still stale (#833: 'log the lag' means "
        "the actual backlog size, not just that one exists)")


@pytest.mark.asyncio
async def test_rollup_counts_match_across_batch_boundaries(monkeypatch):
    """Anti-drift: folding 3 tests x 8 rows through batch-size-5 boundaries must
    produce the same day-counts as one giant batch would."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    async with db_session() as db:
        run = 0
        for tid in ("a", "b", "c"):
            for _ in range(8):
                run += 1
                db.add(CiTestResult(repo="o/r", run_id=run, suite="s",
                                    test_id=tid, outcome="passed", created_ts=OLD))
    engine = _engine(monkeypatch, batch=5)

    await engine._rollup_passed({})

    async with db_session() as db:
        daily = (await db.execute(select(CiTestDaily))).scalars().all()
    got = {(d.test_id): d.passed for d in daily}
    assert got == {"a": 8, "b": 8, "c": 8}


@pytest.mark.asyncio
async def test_failed_and_error_rows_survive_a_full_drain(monkeypatch):
    """The drain loop must preserve the fold's exclusions exactly."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    await _seed(12, test_id="ok")
    async with db_session() as db:
        db.add(CiTestResult(repo="o/r", run_id=100, suite="s", test_id="bad",
                            outcome="failed", message="boom", created_ts=OLD))
        db.add(CiTestResult(repo="o/r", run_id=101, suite="s", test_id="err",
                            outcome="error", message="boom", created_ts=OLD))
    engine = _engine(monkeypatch, batch=5)

    await engine._rollup_passed({})

    async with db_session() as db:
        raw = (await db.execute(select(CiTestResult))).scalars().all()
    assert sorted(r.outcome for r in raw) == ["error", "failed"]


@pytest.mark.asyncio
async def test_sql_day_bucketing_matches_python_reference_at_utc_boundaries():
    """Anti-drift oracle (#833): the fold buckets created_ts into a YYYY-MM-DD
    day IN SQL (dialect-specific strftime/to_char). The existing 174k
    ci_test_daily rows were bucketed by the Python reference below — a bucket
    that drifts (off-by-timezone, truncation vs rounding of fractional seconds)
    would silently split history into duplicate day keys, so the SQL result
    must match the reference exactly, including at UTC-midnight boundaries."""
    from datetime import datetime, timezone

    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    midnight = OLD - (OLD % 86400.0)  # a UTC day boundary older than the window
    stamps = {
        "second-before": midnight - 1.0,
        "frac-before": midnight - 0.25,
        "on-midnight": midnight,
        "frac-after": midnight + 0.25,
    }
    async with db_session() as db:
        for run_id, (tid, ts) in enumerate(stamps.items(), start=1):
            db.add(CiTestResult(repo="o/r", run_id=run_id, suite="s", test_id=tid,
                                outcome="passed", created_ts=ts))
    engine = _engine()

    await engine._rollup_passed({})

    expected = {tid: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
                for tid, ts in stamps.items()}
    async with db_session() as db:
        daily = (await db.execute(select(CiTestDaily))).scalars().all()
    assert {d.test_id: d.day for d in daily} == expected


@pytest.mark.asyncio
async def test_mixed_passed_and_skipped_fold_into_one_bucket_row():
    """passed + skipped rows for the same (repo, test, day) arriving in the SAME
    batch must land in ONE daily row. The upsert's source query has to
    pre-aggregate to one row per conflict target — two source rows hitting the
    same (repo, test_id, day) makes Postgres refuse the statement outright
    ('ON CONFLICT DO UPDATE command cannot affect row a second time')."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    await _seed(6, test_id="t")
    async with db_session() as db:
        for run_id in (200, 201, 202):
            db.add(CiTestResult(repo="o/r", run_id=run_id, suite="s", test_id="t",
                                outcome="skipped", created_ts=OLD))
    engine = _engine()

    await engine._rollup_passed({})

    async with db_session() as db:
        daily = (await db.execute(select(CiTestDaily))).scalars().all()
    assert len(daily) == 1, "same (repo, test, day) must collapse to one row"
    assert (daily[0].passed, daily[0].skipped) == (6, 3)


@pytest.mark.asyncio
async def test_fold_accumulates_into_existing_bucket_with_null_counts():
    """Every CiTestDaily count column is nullable (the reconcile rule), so an
    existing bucket can legitimately hold NULL counts. Accumulating into it must
    COALESCE to 0 — NULL + n is NULL in SQL, which would silently erase the
    increment."""
    from datetime import datetime, timezone

    from sqlalchemy import update

    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    day = datetime.fromtimestamp(OLD, timezone.utc).strftime("%Y-%m-%d")
    async with db_session() as db:
        db.add(CiTestDaily(repo="o/r", test_id="t", day=day))
    async with db_session() as db:
        # Constructor kwargs of None still trigger the model's default=0, so
        # force REAL NULLs the way a reconcile-added column would hold them.
        await db.execute(update(CiTestDaily).values(
            passed=None, failed=None, skipped=None))
    await _seed(4, test_id="t")
    engine = _engine()

    await engine._rollup_passed({})

    async with db_session() as db:
        daily = (await db.execute(select(CiTestDaily))).scalars().all()
    assert len(daily) == 1
    assert daily[0].passed == 4, "NULL existing count must accumulate as 0 + 4"
    assert daily[0].failed is None, (
        "failed is reserved (no writer) — the conflict path must leave an "
        "existing bucket's failed untouched, exactly like the old Python fold")
