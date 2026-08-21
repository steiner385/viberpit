"""The probe records health and capacity, and records FAILURE explicitly.

A probe that silently leaves stale values is worse than one that records the
error: ranking would keep dispatching to a host that died an hour ago.
"""
from __future__ import annotations

import asyncio


def _seed(tmp_path, dbname, *hosts):
    from server.db import db_session, init_db

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / dbname}")
        async with db_session() as db:
            for h in hosts:
                db.add(h)
    return go


def test_successful_probe_records_health_and_capacity(tmp_path):
    from sqlalchemy import select as sa_select

    from server.db import db_session
    from server.db.models import Host
    from server.sessions.host_probe import probe_all

    async def fake_fetch(endpoint):
        return {"sha": "abc1234", "protocol": 1, "loadAvg": 1.5,
                "memAvailableMb": 2048, "activeSessions": 4}

    async def run():
        await _seed(tmp_path, "p1.db", Host(name="rocky", endpoint="https://rocky:9443"))()
        n = await probe_all(fetch=fake_fetch)
        async with db_session() as db:
            row = (await db.execute(
                sa_select(Host).where(Host.name == "rocky"))).scalars().first()
        return n, row

    n, row = asyncio.run(run())
    assert n == 1
    assert row.healthy is True
    assert row.sha == "abc1234"
    assert row.load_avg == 1.5
    assert row.mem_available_mb == 2048
    assert row.active_sessions == 4
    assert row.last_error is None
    assert row.last_probe_at is not None


def test_failed_probe_marks_unhealthy_and_records_the_error(tmp_path):
    from sqlalchemy import select as sa_select

    from server.db import db_session
    from server.db.models import Host
    from server.sessions.host_probe import probe_all

    async def failing_fetch(endpoint):
        raise ConnectionError("connection refused")

    async def run():
        await _seed(tmp_path, "p2.db",
                    Host(name="rocky", endpoint="https://rocky:9443",
                         healthy=True, load_avg=0.1))()
        await probe_all(fetch=failing_fetch)
        async with db_session() as db:
            return (await db.execute(
                sa_select(Host).where(Host.name == "rocky"))).scalars().first()

    row = asyncio.run(run())
    assert row.healthy is False, "a failed probe must flip health, not leave it stale"
    assert "connection refused" in (row.last_error or "")


def test_disabled_hosts_are_not_probed(tmp_path):
    from server.db.models import Host
    from server.sessions.host_probe import probe_all

    calls = []

    async def counting_fetch(endpoint):
        calls.append(endpoint)
        return {"sha": "x", "protocol": 1}

    async def run():
        await _seed(tmp_path, "p3.db",
                    Host(name="off", endpoint="https://off:9443", enabled=False))()
        return await probe_all(fetch=counting_fetch)

    assert asyncio.run(run()) == 0
    assert calls == []


def test_one_failing_host_does_not_stop_the_others(tmp_path):
    """Probing is per-host; a dead host must not hide a live one's metrics."""
    from sqlalchemy import select as sa_select

    from server.db import db_session
    from server.db.models import Host
    from server.sessions.host_probe import probe_all

    async def mixed_fetch(endpoint):
        if "dead" in endpoint:
            raise ConnectionError("refused")
        return {"sha": "ok", "protocol": 1, "loadAvg": 0.2,
                "memAvailableMb": 4096, "activeSessions": 1}

    async def run():
        await _seed(tmp_path, "p4.db",
                    Host(name="dead", endpoint="https://dead:9443"),
                    Host(name="live", endpoint="https://live:9443"))()
        await probe_all(fetch=mixed_fetch)
        async with db_session() as db:
            rows = (await db.execute(sa_select(Host))).scalars().all()
        return {r.name: r for r in rows}

    rows = asyncio.run(run())
    assert rows["dead"].healthy is False
    assert rows["live"].healthy is True
    assert rows["live"].sha == "ok"


def test_probe_loop_survives_a_failing_tick(monkeypatch):
    """A loop that dies on one bad tick silently freezes the whole registry, and
    stale health is what causes dispatch into a dead host."""
    import server.sessions.host_probe as hp

    calls = {"n": 0}

    async def flaky(*, fetch=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db hiccup")
        return 1

    monkeypatch.setattr(hp, "probe_all", flaky)

    async def run():
        stop = asyncio.Event()

        async def stopper():
            while calls["n"] < 3:
                await asyncio.sleep(0.01)
            stop.set()

        await asyncio.gather(hp.probe_loop(interval_s=0.01, stop=stop), stopper())

    asyncio.run(asyncio.wait_for(run(), timeout=5))
    assert calls["n"] >= 3, "loop must continue past the raising tick"
