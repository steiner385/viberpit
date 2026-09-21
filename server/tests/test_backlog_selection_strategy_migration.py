import asyncio

from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import Automation


def test_stored_weighted_selection_strategy_migrates_to_priority(tmp_path):
    """Removal (spec 2026-09-20 D6, #2103): ``weighted`` dropped out of
    ``SELECTION_STRATEGIES``. A row written before the removal still carries the
    literal string in its JSON ``config`` — init_db's reconcile pass must rewrite
    it to ``priority`` (the value it already behaved as) before anything reads
    the config through the now-narrower enum, or that read raises and the whole
    row falls back to defaults."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'w.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(Automation(id="backlog-runner", enabled=True,
                              config={"selection_strategy": "weighted", "max_concurrent": 2}))
        # Re-run init_db against the same file to trigger the reconcile migration,
        # as a real process restart would.
        await init_db(db_url=db_url)
        async with db_session() as db:
            row = (await db.execute(select(Automation).where(
                Automation.id == "backlog-runner"))).scalar_one()
            assert row.config == {"selection_strategy": "priority", "max_concurrent": 2}

    asyncio.run(run())


def test_migration_is_idempotent_and_leaves_other_strategies_alone(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'b.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(Automation(id="backlog-runner", enabled=True,
                              config={"selection_strategy": "balanced"}))
        await init_db(db_url=db_url)
        await init_db(db_url=db_url)
        async with db_session() as db:
            row = (await db.execute(select(Automation).where(
                Automation.id == "backlog-runner"))).scalar_one()
            assert row.config == {"selection_strategy": "balanced"}

    asyncio.run(run())
