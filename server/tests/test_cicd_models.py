"""CI/CD Doctor history tables exist after init_db and round-trip rows."""
from __future__ import annotations

import asyncio

from server.db import db_session
from server.db.models import CiFinding, CiJob, CiRun, CiTestResult


def test_ci_tables_roundtrip(isolated_db):
    async def run():
        async with db_session() as db:
            db.add(CiRun(id=1, repo="o/r", workflow_path=".github/workflows/ci.yml",
                         event="pull_request", head_sha="abc", head_branch="feat",
                         conclusion="success", created_ts=1000.0, duration_s=300.0))
            db.add(CiJob(id=10, run_id=1, repo="o/r", name="e2e", conclusion="failure",
                         duration_s=120.0, head_sha="abc", head_branch="feat",
                         created_ts=1000.0))
            db.add(CiTestResult(repo="o/r", run_id=1, suite="junit-python",
                                test_id="tests/test_x.py::test_y", outcome="failed",
                                time_s=0.5, head_sha="abc", head_branch="feat",
                                created_ts=1000.0))
            db.add(CiFinding(repo="o/r", kind="flaky_test",
                             fingerprint="flaky_test:o/r:tests/test_x.py::test_y",
                             status="open", title="flaky test", detail={"pct": 8.0}))
            await db.flush()
        async with db_session() as db:
            from sqlalchemy import select
            f = (await db.execute(select(CiFinding))).scalars().one()
            assert f.status == "open" and f.issue_number is None
            assert f.first_seen_at is not None
    asyncio.run(run())
