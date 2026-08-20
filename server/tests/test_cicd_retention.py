import pytest
from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import CiTestResult


@pytest.mark.asyncio
async def test_repeated_testcase_keeps_last_attempt(monkeypatch):
    """A retried test emits one <testcase> per attempt; only the final one is stored."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    from server.automations.cicd_doctor import CicdDoctorLoop

    xml = """<testsuites><testsuite name="s">
      <testcase classname="c" name="flaky" time="0.1"><failure>boom</failure></testcase>
      <testcase classname="c" name="flaky" time="0.2"/>
    </testsuite></testsuites>"""

    engine = CicdDoctorLoop.__new__(CicdDoctorLoop)
    engine._fetch_runs = lambda repo, since: _runs()
    engine._fetch_jobs = lambda repo, run_id: _none()
    engine._fetch_junit = lambda repo, run_id: _junit(xml)
    engine._ctx = _FakeCtx()

    await engine._ingest("owner/repo")

    async with db_session() as db:
        rows = (await db.execute(select(CiTestResult))).scalars().all()
    assert len(rows) == 1, f"expected one deduped row, got {len(rows)}"
    assert rows[0].outcome == "passed"  # the LAST attempt won


async def _runs():
    return [{"id": 1, "workflow_path": "ci.yml", "event": "push", "head_sha": "abc",
             "head_branch": "main", "conclusion": "success", "created_ts": 1000.0,
             "duration_s": 1.0}]


async def _none():
    return []


async def _junit(xml):
    return [("suite-1", xml)]


@pytest.mark.asyncio
async def test_same_testid_different_suites_not_collapsed(monkeypatch):
    """Same test_id under different suite names must NOT be collapsed."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    from server.automations.cicd_doctor import CicdDoctorLoop

    # Two suites, each with a testcase having the same classname/name
    # (therefore same test_id), but they represent different sharded runs.
    xml1 = """<testsuites><testsuite name="junit-vitest-server-1">
      <testcase classname="c" name="my_test" time="0.5"><failure>error</failure></testcase>
    </testsuite></testsuites>"""

    xml2 = """<testsuites><testsuite name="junit-vitest-server-2">
      <testcase classname="c" name="my_test" time="0.6"/>
    </testsuite></testsuites>"""

    engine = CicdDoctorLoop.__new__(CicdDoctorLoop)
    engine._fetch_runs = lambda repo, since: _runs()
    engine._fetch_jobs = lambda repo, run_id: _none()
    engine._fetch_junit = lambda repo, run_id: _junit_two_suites(xml1, xml2)
    engine._ctx = _FakeCtx()

    await engine._ingest("owner/repo")

    async with db_session() as db:
        rows = (await db.execute(select(CiTestResult))).scalars().all()
    assert len(rows) == 2, f"expected two rows (one per suite), got {len(rows)}"
    suites = {row.suite for row in rows}
    assert "junit-vitest-server-1" in suites, "suite-1 row missing"
    assert "junit-vitest-server-2" in suites, "suite-2 row missing"
    # Verify outcomes differ (first failed, second passed)
    outcomes_by_suite = {row.suite: row.outcome for row in rows}
    assert outcomes_by_suite["junit-vitest-server-1"] == "failed"
    assert outcomes_by_suite["junit-vitest-server-2"] == "passed"


async def _junit_two_suites(xml1, xml2):
    return [("junit-vitest-server-1", xml1), ("junit-vitest-server-2", xml2)]


class _FakeCtx:
    async def get_target_state(self, key):
        return {}

    async def set_target_state(self, key, value):
        return None


@pytest.mark.asyncio
async def test_ci_test_daily_table_exists():
    """The rollup table is created by create_all and accepts a day-count row."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    from server.db.models import CiTestDaily

    async with db_session() as db:
        db.add(CiTestDaily(repo="o/r", test_id="t1", day="2026-08-20",
                           passed=5, failed=1, skipped=0))
    async with db_session() as db:
        row = (await db.execute(select(CiTestDaily))).scalars().one()
    assert (row.passed, row.failed, row.skipped) == (5, 1, 0)


@pytest.mark.asyncio
async def test_rollup_folds_passed_and_keeps_failed():
    """Passed rows past the raw window become day-counts; failed rows stay raw."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    from server.automations.cicd_doctor import CicdDoctorLoop
    from server.db.models import CiTestDaily

    now = 1_800_000_000.0
    old = now - 10 * 86400.0          # older than the 7-day raw window
    async with db_session() as db:
        # Distinct run_id per row: uq_ci_test_result_run_suite_test forbids more
        # than one (run_id, suite, test_id) row, so 3 passes for the same test
        # come from 3 different CI runs, as they would in reality.
        for run_id in (1, 2, 3):
            db.add(CiTestResult(repo="o/r", run_id=run_id, suite="s", test_id="t1",
                                outcome="passed", created_ts=old))
        db.add(CiTestResult(repo="o/r", run_id=1, suite="s", test_id="t2",
                            outcome="failed", message="boom", created_ts=old))

    engine = CicdDoctorLoop.__new__(CicdDoctorLoop)
    engine._clock = lambda: now
    await engine._rollup_passed({})

    async with db_session() as db:
        raw = (await db.execute(select(CiTestResult))).scalars().all()
        daily = (await db.execute(select(CiTestDaily))).scalars().all()

    assert [r.outcome for r in raw] == ["failed"], "failed rows must stay raw"
    assert len(daily) == 1 and daily[0].passed == 3
    assert daily[0].test_id == "t1"
