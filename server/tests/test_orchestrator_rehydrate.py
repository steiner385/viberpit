"""Tests for Task 2: orchestrator.rehydrate() rebuilds detached SessionRecords."""
import asyncio
from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _seed(rows):
    async with db_session() as db:
        for r in rows:
            db.add(DbSession(**r))


def test_rehydrate_loads_non_ended_rows_as_detached(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed([
            dict(id="a", name="Alpha", account_name="acct1",
                 claude_session_id="claude-a", status="idle", cwd="/repo/a"),
            dict(id="b", name="Beta", account_name="acct2",
                 claude_session_id=None, status="working", cwd="/repo/b"),
            dict(id="c", name="Gone", account_name="acct1",
                 claude_session_id="claude-c", status="ended", cwd="/repo/c"),
        ])
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        n = await orch.rehydrate()
        return n, {r.id: r for r in orch.list_sessions()}, orch._drivers

    n, recs, drivers = asyncio.run(run())
    assert n == 2                       # a + b, not the ended c
    assert set(recs) == {"a", "b"}
    assert recs["a"].detached is True
    assert recs["a"].claude_session_id == "claude-a"
    assert recs["b"].claude_session_id == ""   # NULL coerced to ""
    assert recs["b"].status == "idle"          # working normalized to idle
    assert drivers == {}                        # lazy: no drivers spawned


def test_rehydrate_is_idempotent(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed([dict(id="a", name="A", account_name="x",
                          claude_session_id="cs", status="idle", cwd="/r")])
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        first = await orch.rehydrate()
        second = await orch.rehydrate()    # no new rows added
        return first, second, len(orch.list_sessions())

    first, second, count = asyncio.run(run())
    assert (first, second, count) == (1, 0, 1)
