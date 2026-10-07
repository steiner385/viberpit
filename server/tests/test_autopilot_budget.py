"""The action cap is shared across hosts and survives process replacement."""
import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

import server.db as database
from server.automations.context import AutomationContext
from server.db.models import Automation, AutomationTargetState, Base


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def budget_db(request, tmp_path, monkeypatch):
    schema = None
    if request.param == "postgresql":
        url = request.getfixturevalue("pg_url")
        schema = f"autopilot_budget_{uuid.uuid4().hex}"
        engine = create_async_engine(
            url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/budget.db")
    try:
        async with engine.begin() as conn:
            if schema is not None:
                await conn.execute(CreateSchema(schema))
            await conn.run_sync(lambda c: Base.metadata.create_all(
                c, tables=[Automation.__table__, AutomationTargetState.__table__]))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(database, "_engine", engine)
        monkeypatch.setattr(database, "_session_factory", factory)
        async with database.db_session() as db:
            db.add(Automation(id="autopilot"))
        yield factory
    finally:
        try:
            if schema is not None:
                async with engine.begin() as conn:
                    await conn.execute(DropSchema(schema, cascade=True, if_exists=True))
        finally:
            await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_actions", [0, 2])
async def test_hosts_compete_for_last_slot(budget_db, prior_actions):
    hosts = [AutomationContext("autopilot"), AutomationContext("autopilot")]
    limit = prior_actions + 1
    now = 1_800_000_000.0
    for _ in range(prior_actions):
        assert await hosts[0].reserve_autopilot_action(limit, now)
    ready = asyncio.Event()

    async def reserve(ctx):
        await ready.wait()
        return await ctx.reserve_autopilot_action(limit, now)

    tasks = [asyncio.create_task(reserve(ctx)) for ctx in hosts]
    ready.set()
    assert sorted(await asyncio.gather(*tasks)) == [False, True]
    assert not await AutomationContext("autopilot").reserve_autopilot_action(limit, now)


@pytest.mark.asyncio
async def test_rolling_window_survives_restart_and_prunes_expired_actions(budget_db):
    now = 1_800_000_000.0
    first = AutomationContext("autopilot")
    assert await first.reserve_autopilot_action(2, now)
    assert await first.reserve_autopilot_action(2, now + 1)
    restarted = AutomationContext("autopilot")
    assert not await restarted.reserve_autopilot_action(2, now + 86_399)
    assert await restarted.reserve_autopilot_action(2, now + 86_400)
    assert not await restarted.reserve_autopilot_action(2, now + 86_400)
    assert await restarted.reserve_autopilot_action(2, now + 86_401)
    async with budget_db() as db:
        state = (await db.scalars(select(AutomationTargetState))).one().state
    assert state["action_times"] == [now + 86_400, now + 86_401]


@pytest.mark.asyncio
async def test_zero_cap_and_session_state_are_preserved(budget_db):
    ctx = AutomationContext("autopilot")
    now = 1_800_000_000.0
    target = str(uuid.uuid4())
    session_state = {"paused": True, "action_times": [now]}
    await ctx.set_target_state(target, session_state)
    assert not await ctx.reserve_autopilot_action(0, now)
    assert not await ctx.reserve_autopilot_action(-1, now)
    assert await ctx.reserve_autopilot_action(1, now)
    assert not await ctx.reserve_autopilot_action(0, now + 1)
    assert not await ctx.reserve_autopilot_action(1, now + 1)
    assert await ctx.get_target_state(target) == session_state


@pytest.mark.asyncio
async def test_reservation_remains_charged_after_external_action_failure(budget_db):
    ctx = AutomationContext("autopilot")
    now = 1_800_000_000.0
    with pytest.raises(RuntimeError, match="ambiguous external failure"):
        assert await ctx.reserve_autopilot_action(1, now)
        raise RuntimeError("ambiguous external failure")
    assert not await AutomationContext("autopilot").reserve_autopilot_action(1, now)


@pytest.mark.asyncio
async def test_database_unavailable_does_not_authorize_action(monkeypatch):
    monkeypatch.setattr(database, "_session_factory", None)
    with pytest.raises(RuntimeError, match="Database not initialised"):
        await AutomationContext("autopilot").reserve_autopilot_action(1, 1_800_000_000.0)
