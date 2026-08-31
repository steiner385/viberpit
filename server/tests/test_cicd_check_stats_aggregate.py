"""Check stability stats must be aggregated in SQL, not materialized into Python.

`_analyze` used to fetch every raw ci_job row in the horizon window and fold it in
memory with `check_stats`. On 2026-08-30 that was a 90-day window against a
292,216-row ci_job table -- built into Python Row objects on the event loop thread
(SQLAlchemy's async layer runs row construction via greenlet on the CALLING
thread), then folded with an O(shas x jobs) inner loop. The GIL-safe stall tracer
(#793) caught the loop wedged there: 37.7s / 23.1s / 21.5s / 20.5s in one day, each
freezing every edge request and session socket.

#828 moved the fold off the loop thread; this (`check_stats_stmt`, #827) removes it
entirely -- the window collapses to ONE ROW PER CHECK inside the database. These
tests are the anti-drift guard: the SQL aggregate must reproduce, row-for-row, what
the in-memory `check_stats` oracle produces.
"""
from __future__ import annotations

import math

import pytest

from server.automations.cicd.stats import check_stats, check_stats_from_agg
from server.automations.cicd.types import CheckAgg, JobObs

DAY = 86400.0
NOW = 100 * DAY
DEFAULT_BRANCH = "master"
DEMOTE_WINDOW = 7.0

# Every field check_stats_stmt returns, in select order, so a fixture row maps 1:1.
_AGG_FIELDS = (
    "check", "runs_total", "min_ts", "max_ts", "p95_duration_s", "shas_total",
    "flaky_shas", "recent_total", "recent_flaky", "default_branch_failures",
    "first_total", "first_flaky", "second_total", "second_flaky",
)


def _agg_from_row(r) -> CheckAgg:
    return CheckAgg(**{f: getattr(r, f) for f in _AGG_FIELDS})


def test_p95_index_formula_matches_check_stats():
    """check_stats indexes p95 as min(len-1, ceil(0.95*(len-1))); check_stats_stmt
    uses the pure-integer 19*len//20. They must agree for every window length -- a
    single off-by-one would silently mis-gate graduation on p95 runtime."""
    for n in range(1, 200):
        py_index = min(n - 1, math.ceil(0.95 * (n - 1)))
        sql_index = (19 * n) // 20
        assert py_index == sql_index, f"len={n}: {py_index} != {sql_index}"


# --- fixtures: (name, sha, branch, conclusion, duration_s, created_ts) ------------

_FIXTURES = {
    "core_metrics": [
        ("e2e", "s1", "feat", "failure", 60.0, NOW - 30 * DAY),
        ("e2e", "s1", "feat", "success", 60.0, NOW - 30 * DAY),   # s1 flaky
        ("e2e", "s2", "feat", "success", 120.0, NOW - 20 * DAY),  # slowest -> p95
        ("e2e", "s3", "master", "failure", 60.0, NOW - 1 * DAY),  # master fail-only
    ],
    "flake_rising": [
        # later half of the window gains failures -> rising
        ("e2e", "a", "feat", "success", 60.0, NOW - 40 * DAY),
        ("e2e", "b", "feat", "success", 60.0, NOW - 30 * DAY),
        ("e2e", "c", "feat", "success", 60.0, NOW - 20 * DAY),
        ("e2e", "c", "feat", "failure", 60.0, NOW - 20 * DAY),
        ("e2e", "d", "feat", "success", 60.0, NOW - 10 * DAY),
        ("e2e", "d", "feat", "failure", 60.0, NOW - 10 * DAY),
    ],
    "timed_out_is_failure": [
        ("e2e", "s1", "master", "timed_out", 60.0, NOW - 2 * DAY),  # master fail-only
        ("e2e", "s2", "feat", "success", 60.0, NOW - 1 * DAY),
    ],
    "recent_window": [
        ("build", "s1", "feat", "success", 10.0, NOW - 1 * DAY),
        ("build", "s1", "feat", "failure", 10.0, NOW - 1 * DAY),    # recent flaky sha
        ("build", "s2", "feat", "success", 10.0, NOW - 30 * DAY),
        ("build", "s2", "feat", "failure", 10.0, NOW - 30 * DAY),   # old flaky sha
    ],
    "multi_check_and_dropped": [
        ("a", "s1", "feat", "success", 5.0, NOW - 1 * DAY),
        ("a", "s1", "feat", "cancelled", 5.0, NOW - 1 * DAY),   # dropped: no evidence
        ("b", "s1", "feat", "skipped", 5.0, NOW - 1 * DAY),     # b has ONLY skipped
        ("a", "s2", "master", "failure", 5.0, NOW - 2 * DAY),   # a: master fail-only
    ],
    "single_job": [
        ("lint", "s1", "feat", "success", 3.0, NOW - 5 * DAY),
    ],
    "p95_spread": [
        ("perf", f"s{i}", "feat", "success", float(i), NOW - i * DAY)
        for i in range(1, 12)  # 11 distinct durations, no flake
    ],
    "null_conclusion_kept": [
        # a NULL conclusion is not cancelled/skipped: check_stats keeps it (neither
        # pass nor fail), so the SQL filter must too.
        ("deploy", "s1", "feat", None, 8.0, NOW - 3 * DAY),
        ("deploy", "s2", "feat", "success", 8.0, NOW - 2 * DAY),
    ],
}


def _jobs(fixture) -> list[JobObs]:
    return [JobObs(check=n, head_sha=s, head_branch=b, conclusion=c or "",
                   duration_s=d, created_ts=t) for n, s, b, c, d, t in fixture]


async def _sql_stats(db, repo: str, fixture, *, id_base: int = 0):
    # id_base keeps ci_job.id unique when a single test inserts two fixtures into
    # the same in-memory DB (the engine pool persists rows across db_session blocks).
    from server.db.models import CiJob
    from server.automations.cicd_doctor import check_stats_stmt

    for i, (n, s, b, c, d, t) in enumerate(fixture, start=id_base + 1):
        db.add(CiJob(id=i, run_id=i, repo=repo, name=n, conclusion=c,
                     duration_s=d, head_sha=s, head_branch=b, created_ts=t))
    await db.flush()
    rows = (await db.execute(check_stats_stmt(
        repo, 0.0, now_ts=NOW, demote_window_days=DEMOTE_WINDOW,
        default_branch=DEFAULT_BRANCH))).all()
    return check_stats_from_agg(_agg_from_row(r) for r in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(_FIXTURES))
async def test_check_stats_stmt_matches_the_in_memory_form(name):
    """Anti-drift guard: the SQL aggregate == the in-memory check_stats fold."""
    from server.db import db_session, init_db
    await init_db(db_url="sqlite+aiosqlite:///:memory:")

    fixture = _FIXTURES[name]
    in_memory = check_stats(_jobs(fixture), default_branch=DEFAULT_BRANCH,
                            now_ts=NOW, demote_window_days=DEMOTE_WINDOW)
    async with db_session() as db:
        from_sql = await _sql_stats(db, "o/r", fixture)
    assert from_sql == in_memory


@pytest.mark.asyncio
async def test_check_stats_stmt_pins_expected_values():
    """Explicit values, so a bug shared by both paths can't hide behind equivalence
    (the numbers mirror test_cicd_stats.test_check_stats_core_metrics)."""
    from server.db import db_session, init_db
    await init_db(db_url="sqlite+aiosqlite:///:memory:")

    async with db_session() as db:
        from_sql = await _sql_stats(db, "o/r", _FIXTURES["core_metrics"])
    st = from_sql["e2e"]
    assert st.runs_total == 4
    assert 28.9 < st.window_days < 29.1
    assert round(st.flake_pct, 1) == 33.3          # 1 flaky sha of 3
    assert st.default_branch_failures == 1
    assert st.p95_duration_s == 120.0
    assert st.recent_flake_pct == 0.0

    async with db_session() as db2:
        rising = await _sql_stats(db2, "o/r2", _FIXTURES["flake_rising"], id_base=1000)
    assert rising["e2e"].flake_rising is True


@pytest.mark.asyncio
async def test_check_stats_stmt_collapses_to_one_row_per_check():
    """The whole point: many raw job rows in, one row per check out."""
    from server.db import db_session, init_db
    from server.db.models import CiJob
    from server.automations.cicd_doctor import check_stats_stmt
    await init_db(db_url="sqlite+aiosqlite:///:memory:")

    async with db_session() as db:
        rid = 0
        # 3 checks x 5 shas x 4 attempts = 60 raw rows.
        for check in ("build", "test", "e2e"):
            for sha in ("s1", "s2", "s3", "s4", "s5"):
                for attempt in range(4):
                    rid += 1
                    db.add(CiJob(id=rid, run_id=rid, repo="o/r", name=check,
                                 conclusion="success" if attempt else "failure",
                                 duration_s=30.0, head_sha=sha, head_branch="feat",
                                 created_ts=NOW - rid))
        await db.flush()
        rows = (await db.execute(check_stats_stmt(
            "o/r", 0.0, now_ts=NOW, demote_window_days=DEMOTE_WINDOW,
            default_branch=DEFAULT_BRANCH))).all()

    assert len(rows) == 3, f"expected one row per check, got {len(rows)}"
    by_check = {r.check: r for r in rows}
    assert by_check["build"].runs_total == 20   # 5 shas x 4 attempts
    assert by_check["build"].shas_total == 5
    assert by_check["build"].flaky_shas == 5    # every sha has a fail + 3 passes


@pytest.mark.asyncio
async def test_check_stats_stmt_respects_window_and_repo():
    """Rows outside the since_ts window or belonging to another repo are excluded --
    the same clamp _analyze relies on so a tick never folds full history."""
    from server.db import db_session, init_db
    from server.db.models import CiJob
    from server.automations.cicd_doctor import check_stats_stmt
    await init_db(db_url="sqlite+aiosqlite:///:memory:")

    async with db_session() as db:
        db.add(CiJob(id=1, run_id=1, repo="o/r", name="e2e", conclusion="success",
                     duration_s=1.0, head_sha="new", head_branch="feat",
                     created_ts=5000.0))
        db.add(CiJob(id=2, run_id=2, repo="o/r", name="e2e", conclusion="failure",
                     duration_s=1.0, head_sha="old", head_branch="feat",
                     created_ts=10.0))            # before since_ts -> excluded
        db.add(CiJob(id=3, run_id=3, repo="other/repo", name="e2e",
                     conclusion="failure", duration_s=1.0, head_sha="x",
                     head_branch="feat", created_ts=5000.0))  # other repo -> excluded
        await db.flush()
        rows = (await db.execute(check_stats_stmt(
            "o/r", 1000.0, now_ts=6000.0, demote_window_days=DEMOTE_WINDOW,
            default_branch=DEFAULT_BRANCH))).all()

    assert len(rows) == 1
    assert rows[0].shas_total == 1, "only the in-window sha for this repo counts"
