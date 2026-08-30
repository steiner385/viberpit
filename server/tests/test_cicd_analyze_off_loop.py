"""_analyze's pure-Python folds must run OFF the event loop (issue #827).

The GIL-safe stall tracer (#793) caught the daemon's recurring wedges red-handed
on 2026-08-30 — main thread mid-block:

    _cicd_tick_loop -> _cicd_tick_once -> tick -> _run
      -> _analyze (cicd_doctor.py:362) -> check_stats (cicd/stats.py:93)

#791 moved the 14.5M-row CiTestResult aggregation into SQL but left the CiJob
side materialized: check_stats folds a 90-day job window (ci_job: 292k rows) in
pure Python, synchronously, on the loop thread. Wedges of 37.7s / 23.1s / 21.5s /
20.5s in one day — during each, every edge request 503s, health fires
remote-daemon-unreachable, and every session socket closes 1013.

The fold is pure CPU over detached tuples, so the fix is to run it in a worker
thread. These tests pin the thread, not the timing: timing tests flake, thread
identity doesn't.
"""
from __future__ import annotations

import asyncio
import threading

from server.automations.cicd import stats as stats_mod
from server.db import db_session, init_db
from server.tests.test_cicd_doctor_loop import _loop
from server.automations.registry import AutomationContext


def test_check_stats_runs_off_the_event_loop(tmp_path, monkeypatch):
    """The job-stats fold must execute on a worker thread, not the loop thread."""
    seen: dict[str, int] = {}
    real = stats_mod.check_stats

    def spy(*a, **kw):
        seen["thread"] = threading.get_ident()
        return real(*a, **kw)

    monkeypatch.setattr(stats_mod, "check_stats", spy)

    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        from server.db.models import CiJob
        ctx = AutomationContext("cicd-doctor")
        loop = _loop(ctx, runs=[], junit_by_run={})
        config = (await loop._read_config())[1]
        async with db_session() as db:
            db.add(CiJob(id=1, repo="o/r", run_id=1, name="e2e", head_sha="s1",
                         head_branch="master", conclusion="success",
                         duration_s=60.0, created_ts=1999.0))
        await loop._analyze("o/r", config)
        seen["loop_thread"] = threading.get_ident()

    asyncio.run(run())
    assert "thread" in seen, "check_stats was never called"
    assert seen["thread"] != seen["loop_thread"], (
        "check_stats ran ON the event loop thread — a 90-day job window folds "
        "for 20-40s there and freezes the daemon (issue #827)")


def test_analyze_results_are_identical_off_loop(tmp_path):
    """Offloading must not change what _analyze returns: same findings as before.

    A run with no JUnit inside the window yields missing_junit — proves the
    end-to-end path (query -> fold -> findings) still works through the thread.
    """
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        from server.db.models import CiRun
        ctx = AutomationContext("cicd-doctor")
        loop = _loop(ctx, runs=[], junit_by_run={})
        config = (await loop._read_config())[1]
        async with db_session() as db:
            db.add(CiRun(id=1, repo="o/r", workflow_path="wf.yml", event="push",
                         head_sha="s", head_branch="master", conclusion="success",
                         created_ts=1999.0, duration_s=1.0))
        fresh = await loop._analyze("o/r", config)
        assert any(f.kind == "missing_junit" for f in fresh)

    asyncio.run(run())
