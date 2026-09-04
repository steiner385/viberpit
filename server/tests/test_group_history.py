"""Group-run / group-failure store unit tests (SPEC §1 recording rules)."""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from server.db import db_session
from server.db.models import GroupFailure, GroupRun
from server.pipeline.group_history import (
    count_group_ejects,
    count_group_runs,
    group_run_samples,
    record_group_outcome,
)

import pytest


@pytest.fixture(autouse=True)
def _fresh_db(isolated_db):
    """Every test gets its own DB — table-wide selects need no wiping (#728)."""



def _green_checks() -> list[dict]:
    return [
        {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS",
         "started_at": "2026-07-12T12:00:00Z", "completed_at": "2026-07-12T12:05:00Z"},
        {"name": "lint", "status": "COMPLETED", "conclusion": "skipped",
         "started_at": "2026-07-12T12:01:00Z", "completed_at": "2026-07-12T12:03:00Z"},
        {"name": "unit", "status": "COMPLETED", "conclusion": "NEUTRAL",
         "started_at": "2026-07-12T12:02:00Z", "completed_at": "2026-07-12T12:10:00Z"},
    ]


def test_green_group_records_one_run_with_duration():
    async def run():
        await record_group_outcome("o/r", "g1", _green_checks(), "2026-07-12T12:10:00Z")
        async with db_session() as db:
            runs = (await db.execute(select(GroupRun))).scalars().all()
            assert len(runs) == 1
            # max(completed) 12:10:00 - min(started) 12:00:00 = 600s
            assert runs[0].duration_secs == 600
            # completed_at = max(completedAt) ISO
            assert runs[0].completed_at == "2026-07-12T12:10:00Z"
            fails = (await db.execute(select(GroupFailure))).scalars().all()
            assert fails == []

    asyncio.run(run())


def test_green_group_idempotent_on_completed_at():
    async def run():
        await record_group_outcome("o/r", "g1", _green_checks(), "2026-07-12T12:10:00Z")
        # Re-record same group (same max completed_at) -> still one row.
        await record_group_outcome("o/r", "g1", _green_checks(), "2026-07-12T12:11:00Z")
        async with db_session() as db:
            n = (await db.execute(select(func.count()).select_from(GroupRun))).scalar_one()
            assert n == 1

    asyncio.run(run())


def test_failing_group_records_deduped_failures():
    async def run():
        checks = [
            {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS",
             "started_at": "2026-07-12T12:00:00Z", "completed_at": "2026-07-12T12:05:00Z"},
            {"name": "e2e", "status": "COMPLETED", "conclusion": "failure",
             "started_at": "2026-07-12T12:00:00Z", "completed_at": "2026-07-12T12:08:00Z"},
            {"name": "flaky", "status": "COMPLETED", "conclusion": "TIMED_OUT",
             "started_at": "2026-07-12T12:00:00Z", "completed_at": "2026-07-12T12:09:00Z"},
        ]
        await record_group_outcome("o/r", "gX", checks, "2026-07-12T12:09:00Z")
        # Re-record -> deduped on (repo, group_sha, check_name).
        await record_group_outcome("o/r", "gX", checks, "2026-07-12T12:09:00Z")
        async with db_session() as db:
            fails = (await db.execute(
                select(GroupFailure).order_by(GroupFailure.check_name)
            )).scalars().all()
            assert [f.check_name for f in fails] == ["e2e", "flaky"]
            assert {f.conclusion for f in fails} == {"FAILURE", "TIMED_OUT"}
            # A failing group produces NO GroupRun.
            runs = (await db.execute(select(GroupRun))).scalars().all()
            assert runs == []

    asyncio.run(run())


def test_missing_timestamps_skipped_quietly():
    async def run():
        checks = [
            {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS",
             "started_at": None, "completed_at": None},
        ]
        await record_group_outcome("o/r", "g1", checks, "2026-07-12T12:00:00Z")
        async with db_session() as db:
            runs = (await db.execute(select(GroupRun))).scalars().all()
            assert runs == []

    asyncio.run(run())


def test_non_terminal_group_not_recorded():
    async def run():
        checks = [
            {"name": "build", "status": "IN_PROGRESS", "conclusion": None,
             "started_at": "2026-07-12T12:00:00Z", "completed_at": None},
        ]
        await record_group_outcome("o/r", "g1", checks, "2026-07-12T12:00:00Z")
        async with db_session() as db:
            assert (await db.execute(select(GroupRun))).scalars().all() == []
            assert (await db.execute(select(GroupFailure))).scalars().all() == []

    asyncio.run(run())


def test_group_run_samples_newest_first():
    async def run():
        async with db_session() as db:
            db.add(GroupRun(repo="o/r", duration_secs=100, completed_at="2026-07-10T00:00:00Z"))
            db.add(GroupRun(repo="o/r", duration_secs=200, completed_at="2026-07-11T00:00:00Z"))
            db.add(GroupRun(repo="o/r", duration_secs=300, completed_at="2026-07-12T00:00:00Z"))
            db.add(GroupRun(repo="other/repo", duration_secs=999, completed_at="2026-07-12T00:00:00Z"))
        samples = await group_run_samples("o/r")
        assert samples == [300, 200, 100]
        assert await group_run_samples("o/r", limit=2) == [300, 200]

    asyncio.run(run())


def test_counts_windowed_and_distinct():
    async def run():
        async with db_session() as db:
            db.add(GroupRun(repo="o/r", duration_secs=100, completed_at="2026-07-01T00:00:00Z"))
            db.add(GroupRun(repo="o/r", duration_secs=200, completed_at="2026-07-10T00:00:00Z"))
            db.add(GroupRun(repo="o/r", duration_secs=300, completed_at="2026-07-12T00:00:00Z"))
            # Two failing checks in the same group_sha -> DISTINCT sha counts once.
            db.add(GroupFailure(repo="o/r", check_name="e2e", group_sha="gA",
                                observed_at="2026-07-11T00:00:00Z", conclusion="FAILURE"))
            db.add(GroupFailure(repo="o/r", check_name="unit", group_sha="gA",
                                observed_at="2026-07-11T00:00:00Z", conclusion="FAILURE"))
            db.add(GroupFailure(repo="o/r", check_name="e2e", group_sha="gB",
                                observed_at="2026-07-11T00:00:00Z", conclusion="TIMED_OUT"))
            # Out-of-window failure ignored.
            db.add(GroupFailure(repo="o/r", check_name="e2e", group_sha="gOld",
                                observed_at="2026-07-01T00:00:00Z", conclusion="FAILURE"))
        since = "2026-07-05T00:00:00Z"
        assert await count_group_runs("o/r", since) == 2  # 07-10, 07-12
        assert await count_group_ejects("o/r", since) == 2  # DISTINCT gA, gB

    asyncio.run(run())


def test_uppercase_conclusion_comparison():
    async def run():
        # Lowercase conclusions must normalize for terminal + bucket classification.
        checks = [
            {"name": "build", "status": "COMPLETED", "conclusion": "success",
             "started_at": "2026-07-12T12:00:00Z", "completed_at": "2026-07-12T12:04:00Z"},
        ]
        await record_group_outcome("o/r", "g1", checks, "2026-07-12T12:04:00Z")
        async with db_session() as db:
            runs = (await db.execute(select(GroupRun))).scalars().all()
            assert len(runs) == 1
            assert runs[0].duration_secs == 240

    asyncio.run(run())


def test_eject_window_matches_live_observed_at_format():
    """The live recorder passes datetime.isoformat() (+00:00); observed_at must
    be stored Z-form so a Z-form since_iso windows it correctly (lexicographic
    ISO comparison — '+' sorts below digits)."""
    async def run():
        checks = [
            {"name": "e2e", "status": "COMPLETED", "conclusion": "FAILURE",
             "started_at": "2026-07-12T10:00:00Z", "completed_at": "2026-07-12T10:10:00Z"},
        ]
        await record_group_outcome("o/r", "sha-z", checks,
                                   now_iso="2026-07-12T12:00:00+00:00")
        assert await count_group_ejects("o/r", "2026-07-12T00:00:00Z") == 1
        assert await count_group_ejects("o/r", "2026-07-13T00:00:00Z") == 0

    asyncio.run(run())


def test_sim_ctx_reads_real_store_rows():
    """Direct coverage of the SSE frame's _sim_ctx glue (review follow-up):
    real store rows in, per-repo sim inputs out; sample-less repos skipped."""
    async def run():
        checks = _green_checks()
        await record_group_outcome("o/r", "g1", checks, "2026-07-12T12:10:00Z")
        from server.api.pipeline import _sim_ctx
        ctx = await _sim_ctx(["o/r", "other/empty"])
        assert set(ctx) == {"o/r"}
        assert ctx["o/r"]["duration_samples"] == [600]
        assert ctx["o/r"]["eject_prob"] == 0.0  # 1 run, 0 ejects, <5 samples

    asyncio.run(run())
