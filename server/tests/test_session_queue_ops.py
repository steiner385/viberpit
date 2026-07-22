"""session_queue_move/cancel/positions on the orchestrator."""
from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import patch

import pytest

from server.db import init_db, dispose_db
from server.db.models import AppSettings
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from ccswitch.oauth import Usage
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return Usage(10.0, reset, 10.0, reset, False)


def _seed_account(name: str) -> None:
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-tok", "refreshToken": f"{name}-ref",
        "expiresAt": int(time.time() * 1000) + 9_000_000, "subscriptionType": "max",
    })
    if not paths.shared_mcp_path().exists():
        write_json_secure(paths.shared_mcp_path(), {})


async def _make_orch(*, db_url: str, tmp_path, max_active_sessions: int) -> SessionOrchestrator:
    """Build a minimal fake-SDK orchestrator with max_active_sessions set in AppSettings."""
    store_home = tmp_path / "store"
    store_home.mkdir(exist_ok=True)
    shared = tmp_path / "dot-claude"
    shared.mkdir(exist_ok=True)
    (shared / "settings.json").write_text("{}")
    (shared / "projects").mkdir(exist_ok=True)
    cfg_dir = tmp_path / "cfg" / "alpha"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    repo_root = tmp_path / "reporoot"
    repo_root.mkdir(exist_ok=True)

    env_patch = patch.dict(os.environ, {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared),
        "CCSWITCH_DB_URL": db_url,
    })
    env_patch.start()

    _seed_account("alpha")

    await init_db(db_url=db_url)

    from server.db import db_session
    async with db_session() as db:
        db.add(AppSettings(id="singleton", max_active_sessions=max_active_sessions))

    compose_patch = patch("server.sessions.orchestrator.compose", return_value=cfg_dir)
    trust_patch = patch("server.sessions.orchestrator.configdir.trust_project")
    select_patch = patch(
        "server.sessions.orchestrator.select_account",
        return_value=__import__("ccswitch.selector", fromlist=["Picked"]).Picked("alpha"),
    )

    compose_patch.start()
    trust_patch.start()
    select_patch.start()

    orch = SessionOrchestrator(
        sdk_factory=fake_sdk_factory(),
        poll_interval=9999.0,
        usage_fn=_fake_usage,
        repo_root=str(repo_root),
    )
    await orch.start()

    orch._test_patches = [env_patch, compose_patch, trust_patch, select_patch]  # type: ignore[attr-defined]
    return orch


async def _stop_orch(orch: SessionOrchestrator) -> None:
    await orch.stop()
    await dispose_db()
    for p in getattr(orch, "_test_patches", []):
        try:
            p.stop()
        except RuntimeError:
            pass


@pytest.fixture
def orch_factory(tmp_path):
    """Fixture that returns a coroutine factory for building isolated orchestrators."""
    async def factory(*, db_url: str, max_active_sessions: int) -> SessionOrchestrator:
        return await _make_orch(db_url=db_url, tmp_path=tmp_path,
                                max_active_sessions=max_active_sessions)
    return factory


def test_move_and_positions(tmp_path, orch_factory):
    async def run():
        orch = await orch_factory(db_url=f"sqlite+aiosqlite:///{tmp_path / 'o.db'}",
                                  max_active_sessions=1)
        try:
            await orch.create_session("one")
            b = await orch.create_session("b")
            c = await orch.create_session("c")
            assert await orch.session_queue_move(c.id, "top")
            assert await orch.session_queue_positions() == {c.id: 1, b.id: 2}
            assert not await orch.session_queue_move("ghost", "up")
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_cancel_marks_ended(tmp_path, orch_factory):
    async def run():
        orch = await orch_factory(db_url=f"sqlite+aiosqlite:///{tmp_path / 'o2.db'}",
                                  max_active_sessions=1)
        try:
            await orch.create_session("one")
            b = await orch.create_session("b")
            assert await orch.session_queue_cancel(b.id)
            assert orch._records[b.id].status == "ended"
            assert await orch.session_queue_positions() == {}
            assert not await orch.session_queue_cancel(b.id)   # already gone
        finally:
            await _stop_orch(orch)

    asyncio.run(run())
