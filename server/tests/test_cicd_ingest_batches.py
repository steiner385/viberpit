"""Large artifact ingestion must yield without changing transactional semantics."""
import asyncio
import threading
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.automations.cicd import junit
from server.automations.context import AutomationContext
from server.db import db_session, init_db
from server.db.models import CiJob, CiRun, CiTestResult
from server.tests.test_cicd_doctor_loop import _loop, _runs


@pytest.mark.asyncio
async def test_large_ingest_parses_off_loop_and_inserts_bounded_batches(tmp_path, monkeypatch):
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/ingest.db")
    loop_thread = threading.get_ident()
    parse_threads = []
    parse = junit.parse_junit_xml

    def record_parse(xml):
        parse_threads.append(threading.get_ident())
        return parse(xml)

    monkeypatch.setattr(junit, "parse_junit_xml", record_parse)
    xml = ('<testsuite>' + ''.join(
        f'<testcase name="case{i}" time="0.25"/>' for i in range(2501)
    ) + '</testsuite>').encode()
    ctx = AutomationContext("cicd-doctor")
    engine = _loop(ctx, runs=_runs(1), junit_by_run={1: [
        ("suite", xml), ("suite", b'<malformed'),
        ("suite", b'<testsuite><testcase name="case0"><failure message="last"/>'
         b'</testcase></testsuite>'),
    ]})
    sizes = []
    turns = []
    execute = AsyncSession.execute

    async def record_execute(self, statement, params=None, **kwargs):
        assert threading.get_ident() == loop_thread
        if getattr(statement, "is_insert", False) and statement.table.name == "ci_test_result":
            # Each preceding insert must give a scheduled peer a turn.
            assert len(turns) == len(sizes)
            sizes.append(len(params))
            asyncio.get_running_loop().call_soon(turns.append, len(sizes))
        return await execute(self, statement, params, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", record_execute)
    await engine._ingest("o/r")
    assert parse_threads and all(t != loop_thread for t in parse_threads)
    assert sum(sizes) == 2501 and 1 < len(sizes) and max(sizes) <= 1000
    async with db_session() as db:
        rows = (await db.execute(select(CiTestResult))).scalars().all()
    assert len(rows) == len({r.id for r in rows}) == 2501
    result = next(r for r in rows if r.test_id == "case0")
    assert (result.outcome, result.message) == ("failed", "last")
    assert all((r.repo, r.run_id, r.suite, r.head_sha, r.head_branch, r.created_ts)
               == ("o/r", 1, "suite", "s1", "feat", 1001.0) for r in rows)
    assert all(r.time_s == 0.25 and r.message is None for r in rows if r.test_id != "case0")
    assert await ctx.get_target_state("cursor:o/r") == {"last_created_ts": 1001.0}
    # Replayed runs after a missed cursor update must still be idempotent.
    await ctx.set_target_state("cursor:o/r", {})
    await engine._ingest("o/r")
    assert sum(sizes) == 2501


@pytest.mark.asyncio
async def test_failed_later_insert_rolls_back_run_jobs_cases_and_cursor(tmp_path, monkeypatch):
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/rollback.db")
    ctx = AutomationContext("cicd-doctor")
    ctx.set_target_state = AsyncMock()
    xml = ('<testsuite>' + ''.join(
        f'<testcase name="case{i}"/>' for i in range(2501)
    ) + '</testsuite>').encode()
    engine = _loop(ctx, runs=_runs(1), junit_by_run={1: [("suite", xml)]})
    execute = AsyncSession.execute
    inserts = 0

    async def fail_second_insert(self, statement, params=None, **kwargs):
        nonlocal inserts
        if getattr(statement, "is_insert", False) and statement.table.name == "ci_test_result":
            inserts += 1
            if inserts == 2:
                raise RuntimeError("injected insert failure")
        return await execute(self, statement, params, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", fail_second_insert)
    with pytest.raises(RuntimeError, match="injected insert failure"):
        await engine._ingest("o/r")
    ctx.set_target_state.assert_not_awaited()
    async with db_session() as db:
        for model in (CiRun, CiJob, CiTestResult):
            assert (await db.execute(select(model))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_later_batch", [False, True])
async def test_fetches_finish_before_write_transaction(tmp_path, monkeypatch, fail_later_batch):
    """Fetching a second run must not hold the SQLite writer needed by a peer."""
    from sqlalchemy import text

    await init_db(f"sqlite+aiosqlite:///{tmp_path}/two_runs.db")
    ctx = AutomationContext("cicd-doctor")
    xml = b'<testsuite><testcase name="case"/></testsuite>'
    engine = _loop(ctx, runs=_runs(2), junit_by_run={})

    async def fetch_junit(repo, run_id):
        if run_id == 2:
            async with db_session() as peer:
                await peer.execute(text("PRAGMA busy_timeout=100"))
                peer.add(CiRun(id=99, repo="peer"))
        return [("suite", xml)]

    engine._fetch_junit = fetch_junit
    execute = AsyncSession.execute

    async def maybe_fail_insert(self, statement, params=None, **kwargs):
        if (fail_later_batch and getattr(statement, "is_insert", False)
                and statement.table.name == "ci_test_result" and params[0]["run_id"] == 2):
            raise RuntimeError("injected later run failure")
        return await execute(self, statement, params, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", maybe_fail_insert)
    if fail_later_batch:
        with pytest.raises(RuntimeError, match="injected later run failure"):
            await engine._ingest("o/r")
    else:
        await engine._ingest("o/r")
    async with db_session() as db:
        assert await db.get(CiRun, 99) is not None
        for model in (CiRun, CiJob, CiTestResult):
            rows = (await db.execute(select(model).where(model.repo == "o/r"))).scalars().all()
            assert len(rows) == (0 if fail_later_batch else 2)
    assert await ctx.get_target_state("cursor:o/r") == (
        {} if fail_later_batch else {"last_created_ts": 1002.0})
