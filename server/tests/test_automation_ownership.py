"""Real multi-connection ownership transactions, on both supported databases."""
import asyncio
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.schema import CreateSchema, DropSchema
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import server.db as database
from server.automations.ownership import (
    CanonicalTarget, OwnershipConflict, ReleaseProof, StaleOwnership,
    acquire, owned, begin_session_execution, attest_session_stopped,
)
from server.db.models import (
    Automation, AutomationOperation, AutomationOwnedTarget, AutomationTargetState, Base, Session,
)


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def ownership_db(request, tmp_path, monkeypatch):
    schema = None
    if request.param == "postgresql":
        # Reuse the canonical guard before constructing any connection. A unique
        # schema keeps concurrent workers and the full-schema pg_engine untouched.
        url = request.getfixturevalue("pg_url")
        schema = f"ownership_{uuid.uuid4().hex}"
        engine = create_async_engine(
            url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/ownership.db")
    tables = [Session.__table__, Automation.__table__, AutomationTargetState.__table__,
              AutomationOperation.__table__, AutomationOwnedTarget.__table__]
    try:
        async with engine.begin() as conn:
            if schema is not None:
                await conn.execute(CreateSchema(schema))
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(database, "_engine", engine)
        monkeypatch.setattr(database, "_session_factory", factory)
        yield factory
    finally:
        try:
            if schema is not None:
                async with engine.begin() as conn:
                    await conn.execute(DropSchema(schema, cascade=True, if_exists=True))
        finally:
            await engine.dispose()


def target(node="I_alpha", ref="owner/repo#1"):
    return CanonicalTarget(node_id=node, display_ref=ref)


async def claim(targets, owner="host-a:actor", source="runner"):
    async with acquire(targets, owner=owner, source=source, reason="dispatch",
                       provenance={"trigger": "manual"}) as tx:
        return tx.token


@pytest.mark.asyncio
@pytest.mark.parametrize("left,right", [
    ([target()], [target(ref="renamed/REPO#1")]),
    ([target("I_a"), target("I_b")], [target("I_c"), target("I_b")]),
])
async def test_concurrent_hosts_overlapping_sets_one_winner(ownership_db, left, right):
    ready = asyncio.Event()

    async def contender(items, host):
        await ready.wait()
        try:
            return await claim(items, owner=host)
        except OwnershipConflict:
            return None

    tasks = [asyncio.create_task(contender(left, "host-a:runner")),
             asyncio.create_task(contender(right, "host-b:manual"))]
    ready.set()
    results = await asyncio.gather(*tasks)
    assert sum(token is not None for token in results) == 1
    async with ownership_db() as db:
        ops = (await db.scalars(select(AutomationOperation))).all()
        assert len(ops) == 1
        rows = (await db.scalars(select(AutomationOwnedTarget))).all()
        assert len(rows) == len(left if results[0] else right)
        assert {row.operation_token for row in rows} == {next(t for t in results if t)}


@pytest.mark.asyncio
async def test_nonoverlapping_claims_both_succeed(ownership_db):
    tokens = await asyncio.gather(claim([target("I_a")]), claim([target("I_b")]))
    assert tokens[0] != tokens[1]


@pytest.mark.asyncio
async def test_conflict_and_caller_failure_roll_back_entire_acquisition(ownership_db):
    await claim([target("I_z")])
    with pytest.raises(OwnershipConflict):
        await claim([target("I_a"), target("I_z")])
    with pytest.raises(RuntimeError, match="retry state failed"):
        async with acquire([target("I_b")], owner="a", source="runner", reason="test"):
            raise RuntimeError("retry state failed")
    async with ownership_db() as db:
        assert [r.key for r in (await db.scalars(select(AutomationOwnedTarget))).all()] == [
            target("I_z").key]


@pytest.mark.asyncio
async def test_restart_uncertainty_has_no_elapsed_time_takeover(ownership_db):
    token = await claim([target()])
    async with owned(token) as tx:
        reserved = tx.reserve_session()
    async with owned(token) as tx:
        assert tx.operation.reserved_session_id == reserved
        tx.mark_uncertain("RPC disconnected after request")
        tx.operation.created_at = datetime(1990, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(OwnershipConflict):
        await claim([target()], owner="new-host")
    async with owned(token) as tx:
        assert tx.operation.phase == "uncertain"
        assert tx.operation.provenance == {"trigger": "manual"}
        assert tx.operation.owner == "host-a:actor"
        with pytest.raises(ValueError):
            tx.reserve_session()
        with pytest.raises(ValueError):
            tx.release(ReleaseProof.NOT_STARTED, "timer elapsed")
        with pytest.raises(ValueError):
            tx.bind_session("unrelated-session")
        tx.bind_session(reserved)
    async with owned(token) as tx:
        assert tx.operation.phase == "active"
        assert tx.operation.session_id == reserved


@pytest.mark.asyncio
async def test_stale_token_cannot_bind_release_transfer_or_write_state(ownership_db):
    old = await claim([target()])
    async with owned(old) as tx:
        replacement = await tx.transfer(owner="host-b", source="doctor",
                                        proof=ReleaseProof.NOT_STARTED, reason="handoff")
    for mutation in ("bind", "release", "transfer", "state"):
        with pytest.raises(StaleOwnership):
            async with owned(old) as tx:
                pytest.fail(f"stale actor reached {mutation} transaction")
    async with owned(replacement) as tx:
        assert tx.operation.predecessor_token == old
        tx.release(ReleaseProof.NOT_STARTED, "cancelled before effect")
    newest = await claim([target()])
    assert newest not in (old, replacement)
    async with ownership_db() as db:
        assert len((await db.scalars(select(AutomationOwnedTarget))).all()) == 1
        assert len((await db.scalars(select(AutomationOperation))).all()) == 3


@pytest.mark.asyncio
async def test_release_after_possible_effect_requires_observed_stop(ownership_db):
    token = await claim([target()])
    async with owned(token) as tx:
        sid = tx.reserve_session()
    async with owned(token) as tx:
        with pytest.raises(ValueError):
            tx.release(ReleaseProof.NOT_STARTED, "request timeout")
        with pytest.raises(ValueError):
            tx.release(ReleaseProof.WORK_STOPPED, "missing driver is not proof")
    await begin_session_execution(sid)
    assert await attest_session_stopped(token, 1)
    async with owned(token) as tx:
        tx.release(ReleaseProof.WORK_STOPPED, "native close attested generation 1")
    assert await claim([target()]) != token


@pytest.mark.asyncio
async def test_transition_and_caller_state_are_one_transaction(ownership_db):
    token = await claim([target()])
    async with ownership_db() as db:
        db.add(Automation(id="runner"))
        await db.commit()
    with pytest.raises(RuntimeError):
        async with owned(token) as tx:
            tx.reserve_session()
            tx.db.add(AutomationTargetState(automation_id="runner", target="owner/repo#1",
                                            state={"operation_token": token}))
            await tx.db.flush()
            raise RuntimeError("failed associated write")
    async with owned(token) as tx:
        assert tx.operation.phase == "claimed"
        assert tx.operation.reserved_session_id is None
        assert (await tx.db.scalars(select(AutomationTargetState))).all() == []


@pytest.mark.asyncio
async def test_database_failure_blocks_and_raises(ownership_db):
    async with ownership_db() as db:
        await db.execute(text("DROP TABLE automation_owned_targets"))
        await db.commit()
    with pytest.raises(DBAPIError):
        await claim([target()])


@pytest.mark.parametrize("node", ["", " ", "owner/repo#1", "I_a:b", "I_a\n"])
def test_canonical_identity_rejects_display_refs_and_malformed_nodes(node):
    with pytest.raises(ValueError):
        target(node)


@pytest.mark.asyncio
async def test_fenced_sweep_waits_for_transfer_then_rejects_old_token(ownership_db):
    """The sweep reads the old attempt while manual dispatch holds target locks."""
    token = await claim([target()])
    async with ownership_db() as db:
        db.add(Automation(id="runner"))
        await db.flush()
        db.add(AutomationTargetState(automation_id="runner", target="owner/repo#1",
                                    state={"operation_token": token}))
        await db.commit()
    started = asyncio.Event()

    async def stale_sweep():
        started.set()
        with pytest.raises(StaleOwnership):
            async with owned(token) as stale:
                row = await stale.db.get(AutomationTargetState, ("runner", "owner/repo#1"))
                await stale.db.delete(row)

    async with owned(token) as tx:
        replacement = await tx.transfer(owner="manual", source="runner",
                                        proof=ReleaseProof.NOT_STARTED, reason="safe transfer")
        state = await tx.db.get(AutomationTargetState, ("runner", "owner/repo#1"))
        state.state = {"operation_token": replacement}
        sweep = asyncio.create_task(stale_sweep())
        await started.wait()
    await asyncio.wait_for(sweep, 5)
    async with owned(replacement) as tx:
        assert tx.operation.owner == "manual"
        state = await tx.db.get(AutomationTargetState, ("runner", "owner/repo#1"))
        assert state.state == {"operation_token": replacement}


@pytest.mark.asyncio
async def test_two_launch_cas_transactions_reserve_exactly_one_session(ownership_db):
    token = await claim([target()])

    async def launch():
        async with owned(token) as tx:
            try:
                return tx.reserve_session()
            except ValueError:
                return None

    results = await asyncio.gather(launch(), launch())
    assert sum(r is not None for r in results) == 1
    async with owned(token) as tx:
        assert tx.operation.reserved_session_id == next(r for r in results if r)


@pytest.mark.asyncio
async def test_existing_session_and_mechanical_actions_remain_owned(ownership_db):
    token = await claim([target()])
    async with owned(token) as tx:
        tx.start_action(session_id="existing-session")
    with pytest.raises(OwnershipConflict):
        await claim([target()], source="consolidation")
    async with owned(token) as tx:
        assert tx.operation.session_id == "existing-session"
        with pytest.raises(ValueError):
            await tx.transfer(owner="next", source="doctor", proof=ReleaseProof.NOT_STARTED,
                              reason="cannot abandon existing work")
        tx.mark_uncertain("injection acknowledgement lost")
    with pytest.raises(OwnershipConflict):
        await claim([target()])


@pytest.mark.asyncio
async def test_transaction_cannot_be_reused_after_exit_or_release(ownership_db):
    token = await claim([target()])
    async with owned(token) as tx:
        tx.release(ReleaseProof.NOT_STARTED, "cancelled")
        with pytest.raises(StaleOwnership):
            tx.reserve_session()
    with pytest.raises(StaleOwnership):
        tx.start_action()


@pytest.mark.asyncio
async def test_unrelated_postgres_work_does_not_wait_for_owned_transaction(ownership_db):
    if database._engine.dialect.name != "postgresql":
        pytest.skip("SQLite intentionally serializes short write transactions")
    token = await claim([target("I_a")])
    async with owned(token):
        other = await asyncio.wait_for(claim([target("I_b")]), 3)
    assert other != token


@pytest.mark.asyncio
async def test_migration_matches_models_and_round_trips(ownership_db):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    path = Path(__file__).parents[1] / "db/migrations/versions/0045_automation_ownership.py"
    spec = importlib.util.spec_from_file_location("ownership_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "0044_dispatch_outcomes"
    tables = [AutomationOperation.__table__, AutomationOwnedTarget.__table__]

    def exercise(conn):
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
            migration.upgrade()
        inspector = inspect(conn)
        for table in tables:
            actual = {c["name"]: c for c in inspector.get_columns(table.name)}
            assert set(actual) == set(table.columns.keys())
            for column in table.columns:
                assert actual[column.name]["nullable"] == column.nullable
                assert actual[column.name]["type"]._type_affinity == column.type._type_affinity
        assert {index["name"] for index in inspector.get_indexes("automation_owned_targets")} == {
            "ix_automation_owned_targets_operation_token"}
        foreign_keys = inspector.get_foreign_keys("automation_owned_targets")
        assert foreign_keys[0]["referred_table"] == "automation_operations"
        assert foreign_keys[0]["referred_columns"] == ["token"]

    async with database._engine.begin() as conn:
        await conn.run_sync(exercise)
    token = await claim([target()])
    async with owned(token) as tx:
        tx.reserve_session()


@pytest.mark.asyncio
async def test_cancelled_acquisition_rolls_back_targets_and_retry_state(ownership_db):
    async with ownership_db() as db:
        db.add(Automation(id="runner"))
        await db.commit()
    with pytest.raises(asyncio.CancelledError):
        async with acquire([target()], owner="runner", source="runner", reason="dispatch") as tx:
            tx.db.add(AutomationTargetState(automation_id="runner", target="owner/repo#1",
                                            state={"operation_token": tx.token}))
            await tx.db.flush()
            raise asyncio.CancelledError
    async with ownership_db() as db:
        assert (await db.scalars(select(AutomationOwnedTarget))).all() == []
        assert (await db.scalars(select(AutomationOperation))).all() == []
        assert (await db.scalars(select(AutomationTargetState))).all() == []
    assert await claim([target()])


@pytest.mark.asyncio
async def test_cancelled_launch_transition_keeps_committed_claim(ownership_db):
    token = await claim([target()])
    with pytest.raises(asyncio.CancelledError):
        async with owned(token) as tx:
            tx.reserve_session()
            await tx.db.flush()
            raise asyncio.CancelledError
    async with owned(token) as tx:
        assert tx.operation.phase == "claimed"
        assert tx.operation.reserved_session_id is None
    with pytest.raises(OwnershipConflict):
        await claim([target()])
