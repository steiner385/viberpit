"""Flakiness must be aggregated in SQL, not materialized into Python.

`_analyze` used to fetch every raw ci_test_result row in the window and fold it
in memory. On 2026-08-29 that was 14,530,499 rows for one repo (cairnea/KinDash,
7-day window) out of a 26.4M-row table -- built into Python tuples on the event
loop thread, because SQLAlchemy's async layer runs ORM row construction via
greenlet on the CALLING thread. Measured effect on rocky: 624 event-loop blocks
in 12h, worst 99.02s, during which the daemon answered nothing and every session
socket hung.

SELECT DISTINCT is not enough: 33,798 distinct tests x 320 distinct shas means
the distinct triples are ~10.8M, a ~25% saving on 14.5M. The window has to
collapse to one row per test_id in the database.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from server.automations.cicd.stats import (FAIL_OUTCOMES, FLAKE_OUTCOMES,
                                           PASS_OUTCOMES, flake_pcts_from_sha_counts,
                                           test_flake_pcts)
from server.automations.cicd.types import TestObs


def test_outcome_sets_are_exported_for_sql_callers():
    """A SQL filter must reuse these, not re-list the strings (constitution VI)."""
    assert "passed" in PASS_OUTCOMES and "failed" in FAIL_OUTCOMES
    assert FLAKE_OUTCOMES == PASS_OUTCOMES | FAIL_OUTCOMES
    assert "skipped" not in FLAKE_OUTCOMES


@pytest.mark.parametrize("obs", [
    # one sha, pass and fail -> 100% flaky over 1 sha
    [("t1", "s1", "passed"), ("t1", "s1", "failed")],
    # two shas, only one mixed -> 50%
    [("t1", "s1", "passed"), ("t1", "s1", "failed"), ("t1", "s2", "passed")],
    # never flaky
    [("t1", "s1", "passed"), ("t1", "s2", "passed")],
    # always failing is not flaky either
    [("t1", "s1", "failed"), ("t1", "s2", "error")],
    # skipped rows are ignored entirely
    [("t1", "s1", "skipped"), ("t1", "s1", "passed"), ("t1", "s1", "failed")],
    # several tests at once
    [("t1", "s1", "passed"), ("t1", "s1", "failed"),
     ("t2", "s1", "passed"), ("t2", "s2", "failed")],
])
def test_aggregate_form_matches_the_in_memory_form(obs):
    """Anti-drift guard: the SQL shape and the in-memory shape must never disagree.

    The SQL returns (test_id, distinct shas, shas showing BOTH a pass and a fail).
    Feeding those counts through the shared formula must reproduce exactly what
    folding the raw rows produces.
    """
    in_memory = test_flake_pcts([TestObs(test_id=t, head_sha=s, outcome=o)
                                 for t, s, o in obs])
    # Same aggregation the database performs, done by hand over the fixture.
    per_sha: dict[tuple[str, str], set[str]] = {}
    for t, s, o in obs:
        if o in FLAKE_OUTCOMES:
            per_sha.setdefault((t, s), set()).add(o)
    counts: dict[str, list[int]] = {}
    for (t, _s), outcomes in per_sha.items():
        row = counts.setdefault(t, [0, 0])
        row[0] += 1
        if (outcomes & PASS_OUTCOMES) and (outcomes & FAIL_OUTCOMES):
            row[1] += 1
    aggregated = flake_pcts_from_sha_counts(
        [(t, shas, flaky) for t, (shas, flaky) in counts.items()])
    assert aggregated == in_memory


def test_flake_pcts_from_sha_counts_never_divides_by_zero():
    assert flake_pcts_from_sha_counts([("t1", 0, 0)]) == {"t1": (0.0, 0)}


# --- the query itself, against a real database -----------------------------------

@pytest.mark.asyncio
async def test_analyze_collapses_the_window_to_one_row_per_test():
    """The whole point: N raw rows in, one row per test_id out."""
    from server.db import db_session, init_db
    from server.db.models import CiTestResult
    from server.automations.cicd_doctor import flake_counts_stmt
    await init_db(db_url="sqlite+aiosqlite:///:memory:")

    async with db_session() as db:
        run = 0
        # 3 tests x 4 shas x 5 duplicate attempts = 60 raw rows.
        for t in ("t1", "t2", "t3"):
            for sha in ("s1", "s2", "s3", "s4"):
                for attempt in range(5):
                    run += 1
                    # t1 is flaky on s1 only; t2 always passes; t3 always fails.
                    if t == "t1" and sha == "s1":
                        outcome = "failed" if attempt % 2 else "passed"
                    elif t == "t2":
                        outcome = "passed"
                    else:
                        outcome = "failed"
                    db.add(CiTestResult(repo="o/r", run_id=run, suite="s", test_id=t,
                                        head_sha=sha, outcome=outcome, created_ts=1000.0))
        await db.commit()

        raw = (await db.execute(select(CiTestResult).where(CiTestResult.repo == "o/r"))).scalars().all()
        assert len(raw) == 60, "fixture should have 60 raw rows"

        rows = (await db.execute(flake_counts_stmt("o/r", 0.0))).all()

    assert len(rows) == 3, f"expected one row per test_id, got {len(rows)}"
    got = flake_pcts_from_sha_counts([(r[0], r[1], r[2]) for r in rows])
    expected = test_flake_pcts([TestObs(test_id=r.test_id, head_sha=r.head_sha,
                                        outcome=r.outcome) for r in raw])
    assert got == expected
    assert got["t1"] == (25.0, 4)   # flaky on 1 of 4 shas
    assert got["t2"] == (0.0, 4)
    assert got["t3"] == (0.0, 4)


@pytest.mark.asyncio
async def test_flake_counts_respects_the_time_window_and_repo():
    from server.db import db_session, init_db
    from server.db.models import CiTestResult
    from server.automations.cicd_doctor import flake_counts_stmt
    await init_db(db_url="sqlite+aiosqlite:///:memory:")

    async with db_session() as db:
        db.add(CiTestResult(repo="o/r", run_id=1, suite="s", test_id="t", head_sha="new",
                            outcome="passed", created_ts=5000.0))
        db.add(CiTestResult(repo="o/r", run_id=2, suite="s", test_id="t", head_sha="old",
                            outcome="failed", created_ts=10.0))
        db.add(CiTestResult(repo="other/repo", run_id=3, suite="s", test_id="t",
                            head_sha="x", outcome="failed", created_ts=5000.0))
        await db.commit()
        rows = (await db.execute(flake_counts_stmt("o/r", 1000.0))).all()

    assert len(rows) == 1
    assert rows[0][1] == 1, "only the in-window sha for this repo counts"


@pytest.mark.asyncio
async def test_skipped_only_tests_are_absent_from_the_aggregate():
    """test_flake_pcts drops skipped outcomes, so the SQL filter must too."""
    from server.db import db_session, init_db
    from server.db.models import CiTestResult
    from server.automations.cicd_doctor import flake_counts_stmt
    await init_db(db_url="sqlite+aiosqlite:///:memory:")

    async with db_session() as db:
        db.add(CiTestResult(repo="o/r", run_id=1, suite="s", test_id="skipped_only",
                            head_sha="s1", outcome="skipped", created_ts=1.0))
        await db.commit()
        rows = (await db.execute(flake_counts_stmt("o/r", 0.0))).all()

    assert rows == [], "a test with only skipped runs carries no flakiness evidence"
