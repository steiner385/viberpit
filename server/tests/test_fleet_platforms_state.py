"""``fleet_platform_state``: the live platforms and which are occupied (host-platforms §2)."""
from __future__ import annotations

import pytest

from server.db import db_session, init_db
from server.db.models import Host, Session
from server.pm.fleet_platforms import fleet_platform_state

LINUX = frozenset({"linux"})


@pytest.fixture(autouse=True)
def _host_routing_on(monkeypatch):
    """A platform is only reachable through automation host routing, which the
    suite-wide conftest turns off; these tests are about the registry read."""
    monkeypatch.setenv("VIBERPIT_AUTOMATION_HOST_ROUTING_ENABLED", "true")


async def _setup(tmp_path, hosts, sessions=()):
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
    async with db_session() as db:
        for name, platforms, enabled, healthy in hosts:
            db.add(Host(name=name, endpoint=f"https://{name}:9443", enabled=enabled,
                        healthy=healthy, weight=100, platforms=platforms))
        for i, (host, platform, status) in enumerate(sessions):
            db.add(Session(id=f"s{i}", name=f"s{i}", account_name="a", cwd="/tmp",
                           host=host, platform=platform, status=status))


async def _state():
    async with db_session() as db:
        return await fleet_platform_state(db)


@pytest.mark.asyncio
async def test_no_windows_host_is_linux_only(tmp_path):
    await _setup(tmp_path, [("a", None, True, True)])
    assert await _state() == (LINUX, frozenset())


@pytest.mark.asyncio
async def test_windows_host_is_advertised_and_free(tmp_path):
    await _setup(tmp_path, [("a", None, True, True), ("w", ["windows"], True, True)])
    assert await _state() == (LINUX | {"windows"}, frozenset())


@pytest.mark.asyncio
async def test_fleet_platform_state_ignores_unhealthy_and_disabled_hosts(tmp_path):
    await _setup(tmp_path, [("w1", ["windows"], True, False), ("w2", ["windows"], False, True),
                            ("w3", ["windows"], True, None)])
    assert await _state() == (LINUX, frozenset())


@pytest.mark.asyncio
async def test_platform_busy_when_only_advertiser_has_working_session(tmp_path):
    await _setup(tmp_path, [("w", ["windows"], True, True)],
                 [("w", "windows", "in_progress")])
    assert await _state() == (LINUX | {"windows"}, frozenset({"windows"}))


@pytest.mark.asyncio
async def test_handed_back_session_does_not_hold_platform(tmp_path):
    await _setup(tmp_path, [("w", ["windows"], True, True)],
                 [("w", "windows", "handed_back"), ("w", "windows", "waiting_question")])
    assert (await _state())[1] == frozenset()


@pytest.mark.asyncio
async def test_busy_needs_every_advertiser_occupied(tmp_path):
    await _setup(tmp_path, [("w1", ["windows"], True, True), ("w2", ["windows"], True, True)],
                 [("w1", "windows", "in_progress")])
    assert (await _state())[1] == frozenset()
    async with db_session() as db:
        db.add(Session(id="x", name="x", account_name="a", cwd="/tmp", host="w2",
                       platform="windows", status="retrying"))
    assert (await _state())[1] == frozenset({"windows"})


@pytest.mark.asyncio
async def test_session_without_platform_does_not_occupy(tmp_path):
    await _setup(tmp_path, [("w", ["windows"], True, True)], [("w", None, "in_progress")])
    assert (await _state())[1] == frozenset()


@pytest.mark.asyncio
async def test_routing_off_reaches_no_gated_platform(tmp_path, monkeypatch):
    """With automation host routing off the spawn refuses every gated dispatch, so
    the queue must not offer one: a healthy windows host still advertises Linux
    only, and an ``env: windows`` issue skips as ``environment`` (not claimed,
    refused and released every tick)."""
    from datetime import datetime, timedelta, timezone

    from server.pm.queue import QueueRow, skip_reason
    monkeypatch.setenv("VIBERPIT_AUTOMATION_HOST_ROUTING_ENABLED", "false")
    await _setup(tmp_path, [("w", ["linux", "windows"], True, True)])
    platforms, busy = await _state()
    assert (platforms, busy) == (LINUX, frozenset())
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    row = QueueRow(ref="o/r#1", repo="o/r", number=1, product="p", title="t", body="",
                   url="u", status="Ready", score=1.0, gh_created_at=now - timedelta(days=1),
                   gh_updated_at=now, assignees=(), author="tony",
                   labels=("env: windows",), open_children=0, open_blockers=(),
                   live_session=False, claimed=False, author_id=7)
    assert skip_reason(row, min_status="Ready", respect_dependencies=True, now=now,
                       trusted_authors=frozenset({"user:7"}),
                       platforms=platforms, busy_platforms=busy) == "environment"
