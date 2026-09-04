"""Version-skew gating in probe_host (spec §4): a runner not on the deployed commit is
healthy=False so the ranker never dispatches to it."""
from __future__ import annotations
import asyncio
from sqlalchemy import select as sa_select


def _seed(tmp_path, dbname, *hosts):
    from server.db import db_session, init_db

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / dbname}")
        async with db_session() as db:
            for h in hosts:
                db.add(h)
    return go


def _row(name):
    from server.db import db_session
    from server.db.models import Host

    async def go():
        async with db_session() as db:
            return (await db.execute(sa_select(Host).where(Host.name == name))).scalars().first()
    return go


def test_stale_checkout_marks_unhealthy_with_skew_error(tmp_path):
    from server.db.models import Host
    from server.deploy.state import record_deploy
    from server.ipc.protocol import PROTOCOL_VERSION
    from server.sessions.host_probe import probe_all

    async def fake_fetch(endpoint):
        return {"sha": "old1111", "checkoutSha": "old1111", "protocol": PROTOCOL_VERSION}

    async def run():
        await _seed(tmp_path, "skew.db", Host(name="rocky", endpoint="https://rocky:9443"))()
        await record_deploy(sha="new2222" + "0" * 33, short="new2222", daemon_changed=True)
        await probe_all(fetch=fake_fetch, is_ancestor=lambda a, d: False)
        return await _row("rocky")()

    row = asyncio.run(run())
    assert row.healthy is False
    assert "version skew" in (row.last_error or "")
    assert row.checkout_sha == "old1111" and row.sha == "old1111"


def test_in_sync_runner_stays_healthy(tmp_path):
    from server.db.models import Host
    from server.deploy.state import record_deploy
    from server.ipc.protocol import PROTOCOL_VERSION
    from server.sessions.host_probe import probe_all

    async def fake_fetch(endpoint):
        return {"sha": "new2222", "checkoutSha": "new2222", "protocol": PROTOCOL_VERSION}

    async def run():
        await _seed(tmp_path, "ok.db", Host(name="rocky", endpoint="https://rocky:9443"))()
        await record_deploy(sha="new2222" + "0" * 33, short="new2222", daemon_changed=True)
        await probe_all(fetch=fake_fetch, is_ancestor=lambda a, d: True)
        return await _row("rocky")()

    row = asyncio.run(run())
    assert row.healthy is True and row.last_error is None


def test_no_deploy_state_does_not_gate(tmp_path):
    from server.db.models import Host
    from server.ipc.protocol import PROTOCOL_VERSION
    from server.sessions.host_probe import probe_all

    async def fake_fetch(endpoint):
        return {"sha": "anything", "checkoutSha": "anything", "protocol": PROTOCOL_VERSION}

    async def run():
        await _seed(tmp_path, "nostate.db", Host(name="rocky", endpoint="https://rocky:9443"))()
        await probe_all(fetch=fake_fetch, is_ancestor=lambda a, d: False)
        return await _row("rocky")()

    assert asyncio.run(run()).healthy is True


def test_protocol_mismatch_still_wins_over_skew(tmp_path):
    from server.db.models import Host
    from server.deploy.state import record_deploy
    from server.ipc.protocol import PROTOCOL_VERSION
    from server.sessions.host_probe import probe_all

    async def fake_fetch(endpoint):
        return {"sha": "old1111", "checkoutSha": "old1111", "protocol": PROTOCOL_VERSION + 1}

    async def run():
        await _seed(tmp_path, "proto.db", Host(name="rocky", endpoint="https://rocky:9443"))()
        await record_deploy(sha="new2222" + "0" * 33, short="new2222", daemon_changed=True)
        await probe_all(fetch=fake_fetch, is_ancestor=lambda a, d: False)
        return await _row("rocky")()

    row = asyncio.run(run())
    assert row.healthy is False and "protocol mismatch" in row.last_error
