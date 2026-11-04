"""Host platforms: persisted from /info, filtered in ranking, honoured by pick()."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select as sa_select

from server.api.fleet import host_to_wire
from server.db import db_session, init_db
from server.db.models import Host
from server.sessions.fleet import HostCandidate, platform_rejection, rank_hosts
from server.sessions.fleet_router import FleetRouter, NoHostAvailable


def _cand(name: str, platforms=None, **kw) -> HostCandidate:
    return HostCandidate(
        name=name, enabled=True, healthy=True, weight=kw.pop("weight", 100),
        active_sessions=0, max_sessions=None, load_avg=None, mem_available_mb=None,
        platforms=frozenset({"linux"}) if platforms is None else frozenset(platforms))


async def _seed(tmp_path, dbname, rows) -> FleetRouter:
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / dbname}")
    async with db_session() as db:
        for name, platforms, weight in rows:
            db.add(Host(name=name, endpoint=f"https://{name}:9443", enabled=True,
                        healthy=True, weight=weight, active_sessions=0,
                        platforms=platforms))
    router = FleetRouter()
    for name, _, _ in rows:
        router.add(name, object())
    return router


def test_probe_persists_platforms_including_fenced_outcome(tmp_path):
    from server.sessions.host_probe import probe_all

    async def run(info, dbname):
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / dbname}")
        async with db_session() as db:
            db.add(Host(name="rocky", endpoint="https://rocky:9443"))

        async def fetch(endpoint):
            return info
        await probe_all(fetch=fetch)
        async with db_session() as db:
            return (await db.execute(
                sa_select(Host).where(Host.name == "rocky"))).scalars().one()

    row = asyncio.run(run({"sha": "a", "protocol": 1,
                           "platforms": ["linux", "windows"]}, "p1.db"))
    assert row.platforms == ["linux", "windows"]
    assert row.healthy is True
    # Protocol-fenced: still recorded.
    row = asyncio.run(run({"sha": "a", "protocol": 999,
                           "platforms": ["linux", "windows"]}, "p2.db"))
    assert row.healthy is False
    assert row.platforms == ["linux", "windows"]
    # Legacy daemon: NULL, not a persisted default.
    row = asyncio.run(run({"sha": "a", "protocol": 1}, "p3.db"))
    assert row.platforms is None


def test_rank_hosts_platform_filters_before_scoring():
    hosts = [_cand("dobby", {"linux"}, weight=1000),
             _cand("rocky", {"linux", "windows"}, weight=1)]
    assert rank_hosts(hosts)[0] == "dobby"
    assert rank_hosts(hosts, platform="windows") == ["rocky"]


def test_rank_hosts_platform_none_unchanged():
    hosts = [_cand("dobby"), _cand("rocky", {"linux", "windows"})]
    assert rank_hosts(hosts, platform=None) == rank_hosts(hosts) == ["dobby", "rocky"]
    assert platform_rejection(hosts[0], None) is None
    assert platform_rejection(hosts[0], "windows") is not None
    assert platform_rejection(hosts[1], "windows") is None


@pytest.mark.asyncio
async def test_pick_platform_ignores_prefer_of_ineligible_host(tmp_path):
    router = await _seed(tmp_path, "k1.db", [
        ("dobby", ["linux"], 100), ("rocky", ["linux", "windows"], 100)])
    assert await router.pick(prefer="dobby", platform="windows") == ["rocky"]


@pytest.mark.asyncio
async def test_pick_no_platform_host_raises_no_host_available(tmp_path):
    router = await _seed(tmp_path, "k2.db", [("dobby", ["linux"], 100)])
    with pytest.raises(NoHostAvailable, match="windows"):
        await router.pick(platform="windows")


@pytest.mark.asyncio
async def test_legacy_host_row_null_platforms_is_linux(tmp_path):
    router = await _seed(tmp_path, "k3.db", [("dobby", None, 100)])
    assert await router.pick() == ["dobby"]
    assert await router.pick(platform="linux") == ["dobby"]
    with pytest.raises(NoHostAvailable):
        await router.pick(platform="windows")


def test_fleet_api_lists_platforms():
    assert host_to_wire(Host(name="r", endpoint="unix:///a",
                             platforms=["linux", "windows"]))["platforms"] == [
        "linux", "windows"]
    assert host_to_wire(Host(name="r", endpoint="unix:///a"))["platforms"] == ["linux"]
