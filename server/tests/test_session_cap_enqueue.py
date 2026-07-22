"""Cap gate: sessions beyond max_active_sessions enqueue instead of spawning."""
from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import patch

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
    from ccswitch.paths import shared_base as _shared_base
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

    # Seed AppSettings with the requested cap
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

    # Store patches on orch for cleanup
    orch._test_patches = [env_patch, compose_patch, trust_patch, select_patch]
    return orch


async def _stop_orch(orch: SessionOrchestrator) -> None:
    await orch.stop()
    from server.db import dispose_db
    await dispose_db()
    for p in getattr(orch, "_test_patches", []):
        try:
            p.stop()
        except RuntimeError:
            pass


def test_sixth_session_queues_and_automations_queue_too(tmp_path):
    """With max=2: 2 spawn live, the 3rd (user) and 4th (automation origin)
    both become status='queued' rows with session_queue entries, and no
    driver/worktree/account work runs for them."""

    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'c.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path,
                                max_active_sessions=2)
        try:
            a = await orch.create_session("one")
            b = await orch.create_session("two")
            assert {a.status, b.status} == {"idle"}
            c = await orch.create_session("three", initial_prompt="hi")
            d = await orch.create_session("four", origin="backlog-runner",
                                          origin_ref="r#1")
            assert c.status == "queued" and d.status == "queued"
            from server.sessions import session_queue as sq
            pos = await sq.positions()
            assert pos == {c.id: 1, d.id: 2}
            entry = await sq.next_entry()
            assert entry.params["initial_prompt"] == "hi"
            # No driver was started for queued sessions:
            assert c.id not in orch._drivers and d.id not in orch._drivers
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_cap_counts_only_active_statuses(tmp_path):
    """ended/archived/queued records do not consume slots."""

    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'c2.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path,
                                max_active_sessions=1)
        try:
            a = await orch.create_session("one")
            await orch.close_session(a.id)          # frees the slot
            b = await orch.create_session("two")
            assert b.status == "idle"               # spawned, not queued
        finally:
            await _stop_orch(orch)

    asyncio.run(run())
