"""CicdDoctorLoop — driven entirely through fakes (no network)."""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from server.automations.cicd_doctor import CicdDoctorLoop
from server.automations.context import AutomationContext
from server.db import db_session, init_db
from server.db.models import CiFinding, CiTestResult

JUNIT_FLAKY = b"""<testsuite><testcase classname="t" name="flaky" time="1">
<failure message="boom"/></testcase></testsuite>"""
JUNIT_PASS = b'<testsuite><testcase classname="t" name="flaky" time="1"/></testsuite>'


def _runs(n, sha="s1", ts=1000.0):
    return [{"id": i, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": sha, "head_branch": "feat", "conclusion": "success",
             "created_ts": ts + i, "duration_s": 60.0} for i in range(1, n + 1)]


def _loop(ctx, *, runs, junit_by_run, mode="propose", config=None, issues=None):
    issues = issues if issues is not None else []
    async def read_config():
        return mode, {"enabled_repos": [], "file_issues": True, "min_window_days": 21,
                      "min_runs": 30, "max_flake_pct": 2, "max_p95_minutes": 10,
                      "demote_flake_pct": 5, "demote_window_days": 7, "retire_days": 60,
                      "flaky_test_min_pct": 2, "retention_days": 90, **(config or {})}
    async def fetch_junit(repo, run_id):
        return junit_by_run.get(run_id, [])
    return CicdDoctorLoop(
        ctx,
        read_config=read_config,
        list_repos=lambda config: _async(["o/r"]),
        fetch_runs=lambda repo, since: _async([r for r in runs
                                               if not since or r["created_ts"] > since]),
        fetch_jobs=lambda repo, rid: _async([{"id": rid * 10, "name": "e2e",
                                              "conclusion": "success", "duration_s": 60.0}]),
        fetch_junit=fetch_junit,
        fetch_repo_meta=lambda repo: _async(("master", {"lint-and-test"})),
        fetch_workflows=lambda repo, br: _async({}),
        file_issue=lambda repo, t, b: _async(issues.append(t) or 42),
        close_issue=lambda repo, n: _async(True),
        clock=lambda: 2000.0,
    )


async def _async(v):
    return v


def test_mode_off_ingests_nothing(tmp_path):
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        ctx = AutomationContext("cicd-doctor")
        loop = _loop(ctx, runs=_runs(2), junit_by_run={}, mode="off")
        await loop.tick()
        async with db_session() as db:
            assert (await db.execute(select(CiTestResult))).scalars().all() == []
        # Cursor must not be advanced when mode is off.
        assert await ctx.get_target_state("cursor:o/r") == {}
    asyncio.run(run())


def test_ingest_is_cursor_idempotent(tmp_path):
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        ctx = AutomationContext("cicd-doctor")
        loop = _loop(ctx, runs=_runs(2),
                     junit_by_run={1: [("junit-python", JUNIT_PASS)]})
        await loop.tick()
        await loop.tick()  # second tick: cursor skips both runs
        async with db_session() as db:
            rows = (await db.execute(select(CiTestResult))).scalars().all()
            assert len(rows) == 1 and rows[0].test_id == "t::flaky"
        cursor = await ctx.get_target_state("cursor:o/r")
        assert cursor["last_created_ts"] == 1002.0
    asyncio.run(run())


def test_flaky_test_becomes_finding_and_issue(tmp_path):
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        ctx = AutomationContext("cicd-doctor")
        issues: list[str] = []
        # Evidence volume matters: flaky_test_findings needs >=3 distinct shas.
        # sha s1 both fails (run 1) and passes (run 2); s2 and s3 are clean
        # -> 33.3% flaky across 3 shas, above the 2% threshold.
        runs = [
            {"id": 1, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s1", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1001.0, "duration_s": 60.0},
            {"id": 2, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s1", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1002.0, "duration_s": 60.0},
            {"id": 3, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s2", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1003.0, "duration_s": 60.0},
            {"id": 4, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s3", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1004.0, "duration_s": 60.0},
        ]
        loop = _loop(ctx, runs=runs,
                     junit_by_run={1: [("junit-python", JUNIT_FLAKY)],
                                   2: [("junit-python", JUNIT_PASS)],
                                   3: [("junit-python", JUNIT_PASS)],
                                   4: [("junit-python", JUNIT_PASS)]},
                     issues=issues)
        await loop.tick()
        async with db_session() as db:
            f = (await db.execute(select(CiFinding).where(
                CiFinding.kind == "flaky_test"))).scalars().one()
            assert f.status == "open" and f.issue_number == 42
        assert any("flaky" in t for t in issues)
        assert ctx.cicd_doctor_counts["open"] >= 1
    asyncio.run(run())


def test_detected_event_lands_outside_db_session(tmp_path):
    """Regression for I1: emit (which opens its own db_session) was called while
    the reconcile db_session still held the SQLite write lock, causing the INSERT
    to hit the busy_timeout and be silently swallowed.  After the fix, reconcile
    side effects (emit, file_issue, close_issue) run after the session exits, so
    the event always lands."""
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        ctx = AutomationContext("cicd-doctor")
        issues: list[str] = []
        runs = [
            {"id": 1, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s1", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1001.0, "duration_s": 60.0},
            {"id": 2, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s1", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1002.0, "duration_s": 60.0},
            {"id": 3, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s2", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1003.0, "duration_s": 60.0},
            {"id": 4, "workflow_path": "wf.yml", "event": "pull_request",
             "head_sha": "s3", "head_branch": "feat", "conclusion": "success",
             "created_ts": 1004.0, "duration_s": 60.0},
        ]
        loop = _loop(ctx, runs=runs,
                     junit_by_run={1: [("junit-python", JUNIT_FLAKY)],
                                   2: [("junit-python", JUNIT_PASS)],
                                   3: [("junit-python", JUNIT_PASS)],
                                   4: [("junit-python", JUNIT_PASS)]},
                     issues=issues)
        await loop.tick()
        # The "detected" event must actually land in the DB — on the old code it
        # was swallowed by the busy_timeout because emit opened a nested write
        # session while reconcile's session still held the lock.
        events = await ctx.list_events(kind="detected")
        assert events, "detected event was not emitted (emit ran inside the write lock)"
    asyncio.run(run())


def test_analyze_windows_out_stale_history(tmp_path):
    """#449: _analyze must ignore rows older than the analysis horizon. A run
    outside the window doesn't count toward the "runs but no JUnit" verdict; one
    inside it does. Proven via the windowed run_count → missing_junit finding."""
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        from server.db.models import CiRun
        ctx = AutomationContext("cicd-doctor")
        # Collapse every window to 1 day so horizon == 1 day (86400s).
        cfg = {"retention_days": 1, "retire_days": 1, "min_window_days": 1,
               "demote_window_days": 1}
        loop = _loop(ctx, runs=[], junit_by_run={}, config=cfg)  # clock() == 2000.0
        config = (await loop._read_config())[1]

        async def seed(created_ts):
            async with db_session() as db:
                db.add(CiRun(id=int(created_ts), repo="o/r", workflow_path="wf.yml",
                             event="pull_request", head_sha="s", head_branch="feat",
                             conclusion="success", created_ts=created_ts, duration_s=1.0))

        # A run created well before now - horizon (2000 - 86400 < 0, so anything
        # with a tiny ts is inside; use a large negative-relative gap): seed one
        # far in the past and one "now".
        await seed(1.0)          # far outside the 1-day window (age ~2000s vs 86400s? inside!)
        fresh = await loop._analyze("o/r", config)
        # 2000 - 86400 is negative, so ts=1.0 is INSIDE the window → run counts →
        # missing_junit finding present (there are runs, no test rows).
        assert any(f.kind == "missing_junit" for f in fresh)

        # Now widen the gap: a run at ts=1.0 with clock far in the future is OUTSIDE.
        loop._clock = lambda: 1_000_000.0
        fresh2 = await loop._analyze("o/r", config)
        assert not any(f.kind == "missing_junit" for f in fresh2), \
            "stale run outside the horizon must not count toward analysis"
    asyncio.run(run())


def test_non_reentrant(tmp_path):
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        ctx = AutomationContext("cicd-doctor")
        loop = _loop(ctx, runs=_runs(1), junit_by_run={})
        await asyncio.gather(loop.tick(), loop.tick())  # must not double-ingest
        async with db_session() as db:
            from server.db.models import CiRun
            assert len((await db.execute(select(CiRun))).scalars().all()) == 1
    asyncio.run(run())
