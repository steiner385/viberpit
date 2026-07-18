"""Smoke tests for the fake_backlog harness — zero-network.

Verifies:
- FakeGh records every call and returns scripted / default procs.
- FakeSpawn persists a Session row queryable by origin and de-dups.
"""
from __future__ import annotations

import asyncio
import subprocess

# --------------------------------------------------------------------------- #
# FakeGh                                                                       #
# --------------------------------------------------------------------------- #

def test_fakegh_records_call_and_returns_default():
    from server.tests.fakes.fake_backlog import FakeGh

    fake = FakeGh()
    result = fake(["api", "graphql", "-f", "query={}"])
    assert fake.calls == [["api", "graphql", "-f", "query={}"]]
    assert result.returncode == 0
    assert result.stdout == ""


def test_fakegh_scripted_prefix_match():
    from server.tests.fakes.fake_backlog import FakeGh

    scripted = subprocess.CompletedProcess(
        args=[], returncode=0, stdout='{"data": {}}', stderr=""
    )
    fake = FakeGh({("api", "graphql"): scripted})

    result = fake(["api", "graphql", "-f", "query=x"])
    assert result is scripted
    assert fake.calls[0] == ["api", "graphql", "-f", "query=x"]


def test_fakegh_longest_prefix_wins():
    from server.tests.fakes.fake_backlog import FakeGh

    short = subprocess.CompletedProcess(args=[], returncode=1, stdout="short", stderr="")
    long_ = subprocess.CompletedProcess(args=[], returncode=0, stdout="long", stderr="")
    fake = FakeGh({("api",): short, ("api", "graphql"): long_})

    result = fake(["api", "graphql", "-f", "query=x"])
    assert result is long_


def test_fakegh_unmatched_returns_default():
    from server.tests.fakes.fake_backlog import FakeGh

    scripted = subprocess.CompletedProcess(args=[], returncode=0, stdout="hit", stderr="")
    fake = FakeGh({("repo", "list"): scripted})

    result = fake(["pr", "list"])
    assert result.returncode == 0
    assert result.stdout == ""  # default, not the scripted one


def test_fakegh_records_multiple_calls():
    from server.tests.fakes.fake_backlog import FakeGh

    fake = FakeGh()
    fake(["a", "b"])
    fake(["c", "d"])
    assert fake.calls == [["a", "b"], ["c", "d"]]


# --------------------------------------------------------------------------- #
# FakeSpawn                                                                     #
# --------------------------------------------------------------------------- #

def test_fakespawn_persists_row_queryable_by_origin(tmp_path):
    from sqlalchemy import select

    from server.db import db_session, init_db
    from server.db.models import Session
    from server.tests.fakes.fake_backlog import FakeSpawn

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"

    async def run():
        await init_db(db_url=db_url)
        spawn = FakeSpawn()
        await spawn(origin_ref="o/r#7")

        async with db_session() as db:
            rows = (await db.execute(
                select(Session).where(Session.origin == "backlog-runner")
            )).scalars().all()

        assert len(rows) == 1
        assert rows[0].origin_ref == "o/r#7"
        assert rows[0].origin == "backlog-runner"
        assert spawn.spawns == ["o/r#7"]

    asyncio.run(run())


def test_fakespawn_deduplicates_by_origin_ref(tmp_path):
    from sqlalchemy import func, select

    from server.db import db_session, init_db
    from server.db.models import Session
    from server.tests.fakes.fake_backlog import FakeSpawn

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'b.db'}"

    async def run():
        await init_db(db_url=db_url)
        spawn = FakeSpawn()
        await spawn(origin_ref="o/r#3")
        await spawn(origin_ref="o/r#3")  # duplicate — should not insert again

        async with db_session() as db:
            count = (await db.execute(
                select(func.count()).select_from(Session).where(
                    Session.origin == "backlog-runner", Session.origin_ref == "o/r#3"
                )
            )).scalar_one()

        assert count == 1
        assert spawn.spawns == ["o/r#3"]  # only one real insert

    asyncio.run(run())


def test_fakespawn_dedupe_key_overrides_origin_ref(tmp_path):
    from sqlalchemy import select

    from server.db import db_session, init_db
    from server.db.models import Session
    from server.tests.fakes.fake_backlog import FakeSpawn

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'c.db'}"

    async def run():
        await init_db(db_url=db_url)
        spawn = FakeSpawn()
        await spawn(origin_ref="o/r#1", dedupe_key="batch-xyz")
        await spawn(origin_ref="o/r#2", dedupe_key="batch-xyz")  # same dedupe key

        async with db_session() as db:
            rows = (await db.execute(
                select(Session).where(Session.origin == "backlog-runner")
            )).scalars().all()

        assert len(rows) == 1
        assert rows[0].origin_ref == "batch-xyz"
        assert len(spawn.spawns) == 1

    asyncio.run(run())
