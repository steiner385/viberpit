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


class _FakeCtx:
    async def get_target_state(self, key):
        return {}

    async def set_target_state(self, key, value):
        return None
