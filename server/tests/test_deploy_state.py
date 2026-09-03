"""deploy_state: the fleet's durable 'deployed version' (spec §1)."""
from __future__ import annotations
import asyncio


def _db(tmp_path, name):
    from server.db import init_db
    return init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / name}")


def test_read_returns_none_before_any_deploy(tmp_path):
    from server.deploy.state import read_deploy_state

    async def run():
        await _db(tmp_path, "a.db")
        return await read_deploy_state()

    assert asyncio.run(run()) is None


def test_record_then_read_round_trips_and_daemon_sha_only_moves_when_daemon_changed(tmp_path):
    from server.deploy.state import read_deploy_state, record_deploy

    async def run():
        await _db(tmp_path, "b.db")
        first = await record_deploy(sha="a" * 40, short="aaaaaaa", daemon_changed=True)
        second = await record_deploy(sha="b" * 40, short="bbbbbbb", daemon_changed=False)
        return first, second, await read_deploy_state()

    first, second, latest = asyncio.run(run())
    assert first.daemon_sha == "a" * 40
    assert second.deployed_sha == "b" * 40 and second.deployed_short == "bbbbbbb"
    assert second.daemon_sha == "a" * 40, "edge-only deploy must not advance daemon_sha"
    assert latest == second
    assert latest.deployed_at is not None


def test_host_has_checkout_sha_column(tmp_path):
    from sqlalchemy import select as sa_select
    from server.db import db_session
    from server.db.models import Host

    async def run():
        await _db(tmp_path, "c.db")
        async with db_session() as db:
            db.add(Host(name="r", endpoint="https://r:9443", checkout_sha="abc1234"))
        async with db_session() as db:
            return (await db.execute(sa_select(Host.checkout_sha).where(Host.name == "r"))).scalar_one()

    assert asyncio.run(run()) == "abc1234"
