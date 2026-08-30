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
    assert any("rollup" in r.message and "behind" in r.message for r in caplog.records), \
        "stopping early must be logged as a WARNING naming the lag"


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
