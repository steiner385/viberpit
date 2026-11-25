"""Sync-now wakes the pm-sync loop instead of waiting for its next 60 s tick (#3450).

The loop lives in the daemon; the edge stamps ``sync_requested_at`` and calls
``pm_sync_wake``. These tests pin the halves: the wake ends the idle sleep, a wake
that lands mid-tick buys one more pass, and the endpoint reports what the pass
actually synced rather than only that a sync was requested.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from server.aio import cancel_and_reap
from server.app import create_app
from server.db import db_session
from server.db.models import ProjectRepo
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory
from server.tests.pm_ownership_fixtures import identity_repositories
from server.tests.test_pm_api import IDENTITY_HEADERS


@pytest.fixture(autouse=True)
def canonical_mutation_repositories(monkeypatch):
    # Product creation resolves the repo's identity; stub GitHub as test_pm_api does.
    monkeypatch.setattr("server.automations.target_identity.resolve_repositories",
                        identity_repositories)


async def _until(pred, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_wake_ends_idle_sleep_so_pass_runs_before_the_interval():
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    ticks: list[int] = []

    async def fake_tick() -> None:
        ticks.append(1)

    orch._pm_sync_tick_once = fake_tick
    loop_task = asyncio.create_task(orch._pm_sync_tick_loop(interval=60.0))
    try:
        await _until(lambda: len(ticks) == 1)
        # Idle now: the next scheduled tick is 60 s away, so only a wake runs another.
        assert await orch.pm_sync_wake() is True
        await _until(lambda: len(ticks) == 2)
    finally:
        await cancel_and_reap(loop_task, phase="test pm-sync loop")


@pytest.mark.asyncio
async def test_wake_during_a_tick_buys_one_more_pass():
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    ticks: list[int] = []
    in_first_tick = asyncio.Event()
    release_first_tick = asyncio.Event()

    async def slow_first_tick() -> None:
        ticks.append(1)
        if len(ticks) == 1:
            in_first_tick.set()
            await release_first_tick.wait()

    orch._pm_sync_tick_once = slow_first_tick
    loop_task = asyncio.create_task(orch._pm_sync_tick_loop(interval=60.0))
    try:
        await in_first_tick.wait()
        # The stamp lands while the first tick may already have read the rows.
        await orch.pm_sync_wake()
        release_first_tick.set()
        await _until(lambda: len(ticks) == 2)
    finally:
        await cancel_and_reap(loop_task, phase="test pm-sync loop")


class _Sessions:
    """Stands in for the orchestrator facade. ``on_wake`` plays the daemon's pass."""

    def __init__(self, *, woke: bool, on_wake=None) -> None:
        self._woke = woke
        self._on_wake = on_wake
        self.wakes = 0

    async def pm_sync_wake(self) -> bool:
        self.wakes += 1
        if self._on_wake is not None:
            await self._on_wake()
        return self._woke


async def _pass_reads_repos(last_synced_at: datetime) -> None:
    """What a pm-sync pass does to every repo of the product: clear the request and
    stamp the time the pass started."""
    async with db_session() as db:
        await db.execute(update(ProjectRepo).values(
            sync_requested_at=None, last_synced_at=last_synced_at))


@contextmanager
def _edge(sessions):
    """A worker-role edge (pm-sync loop not started, as in test_pm_api) with one
    product, KinDash, whose sync endpoint answers through *sessions*."""
    tmp = tempfile.mkdtemp()
    env = {"VIBERPIT_DB_URL": f"sqlite+aiosqlite:///{tmp}/test.db",
           "VIBERPIT_DEV": "1", "VIBERPIT_TAILNET_ALLOWLIST": ""}
    with patch.dict(os.environ, env):
        app = create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))
        with patch("server.sessions.orchestrator.daemon_role", lambda: "worker"):
            with TestClient(app, raise_server_exceptions=False) as c:
                resp = c.post("/api/pm/projects", json={
                    "name": "KinDash", "repos": [{"repo": "steiner385/KinDash",
                                                  "isPrimary": True}]},
                    headers=IDENTITY_HEADERS)
                assert resp.status_code == 201, resp.text
                app.state.sessions = sessions
                yield c


def _sync_now(c):
    return c.post("/api/pm/projects/kindash/sync", headers=IDENTITY_HEADERS)


def test_sync_now_reports_daemon_unavailable_without_waiting():
    sessions = _Sessions(woke=False)
    with _edge(sessions) as c, patch("server.api.pm.SYNC_NOW_WAIT_S", 30.0):
        resp = _sync_now(c)
    assert resp.status_code == 202
    body = resp.json()
    assert body["requested"] is True
    assert body["status"] == "daemon_unavailable"
    assert body["repos"] == [{"repo": "steiner385/KinDash", "synced": False,
                              "lastSyncedAt": None}]
    assert sessions.wakes == 1


def test_sync_now_reports_synced_once_the_pass_has_run():
    async def the_pass():
        await _pass_reads_repos(datetime.now(timezone.utc))

    sessions = _Sessions(woke=True, on_wake=the_pass)
    with _edge(sessions) as c:
        resp = _sync_now(c)
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "synced"
    assert body["repos"][0]["synced"] is True
    assert body["repos"][0]["lastSyncedAt"] is not None


def test_sync_now_reports_pending_when_the_pass_never_runs():
    sessions = _Sessions(woke=True)  # woken, but nothing runs the pass
    with _edge(sessions) as c, patch("server.api.pm.SYNC_NOW_WAIT_S", 0.0):
        resp = _sync_now(c)
    body = resp.json()
    assert body["status"] == "pending"
    assert body["repos"][0]["synced"] is False
