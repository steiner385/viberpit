"""Lifecycle invariants: ensure_driver, close/archive queue-row purge, drain stale-entry check."""
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


def test_ensure_driver_refuses_queued(tmp_path):
    """ensure_driver must return None for a queued session without starting a driver."""

    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'lc1.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            a = await orch.create_session("active")
            assert a.status == "idle"

            b = await orch.create_session("queued-one")
            assert b.status == "queued"

            # Call ensure_driver on the queued session — must return None.
            from server.sessions.types import SYSTEM_IDENTITY
            result = await orch.ensure_driver(b.id, SYSTEM_IDENTITY)
            assert result is None, f"expected None for queued session, got {result}"

            # Session must still be queued with its queue row intact.
            assert orch._records[b.id].status == "queued"
            assert b.id not in orch._drivers

            from server.sessions import session_queue as sq
            positions = await sq.positions()
            assert b.id in positions, "queue row must still exist"
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_close_of_queued_session_does_not_resurrect(tmp_path):
    """Closing a queued session must purge its queue row so drain cannot resurrect it."""

    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'lc2.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            a = await orch.create_session("active")
            assert a.status == "idle"

            b = await orch.create_session("will-close-queued")
            assert b.status == "queued"

            # Close the queued session directly.
            await orch.close_session(b.id)
            assert orch._records.get(b.id) is None or \
                   orch._records[b.id].status == "ended", \
                   "closed session must be ended or evicted from records"

            # Queue row must be gone.
            from server.sessions import session_queue as sq
            positions = await sq.positions()
            assert b.id not in positions, "queue row must be purged on close"

            # Now close the active session; drain must NOT resurrect b.
            await orch.close_session(a.id)
            await orch._maybe_start_queued()

            # b must remain ended (not restarted).
            rec_b = orch._records.get(b.id)
            if rec_b is not None:
                assert rec_b.status == "ended", \
                    f"drain must not resurrect closed-queued session; status={rec_b.status}"
            assert b.id not in orch._drivers, "no driver must exist for the closed queued session"
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_drain_skips_stale_entry(tmp_path):
    """_maybe_start_queued must skip a queue entry whose DB row is no longer queued."""

    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'lc3.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            a = await orch.create_session("active")
            assert a.status == "idle"

            b = await orch.create_session("stale-queued")
            assert b.status == "queued"

            # Manufacture the orphan: directly change b's in-memory status to "ended"
            # WITHOUT calling close_session (so the queue row remains intact).
            orch._records[b.id].status = "ended"

            # Also update DB row to "ended" without touching the queue table.
            from server.db import db_session as db_ctx
            from server.db.models import Session as DbSession
            from sqlalchemy import update as sa_update
            async with db_ctx() as db:
                await db.execute(
                    sa_update(DbSession)
                    .where(DbSession.id == b.id)
                    .values(status="ended")
                )

            # Verify the queue row still exists.
            from server.sessions import session_queue as sq
            positions_before = await sq.positions()
            assert b.id in positions_before, "queue row must still exist before drain"

            # Free capacity (close a) so drain would normally start the next entry.
            await orch.close_session(a.id)
            # Run drain explicitly (the background task may have already run; idempotent).
            await orch._maybe_start_queued()

            # b must not have been started (no driver, status stays ended).
            assert b.id not in orch._drivers, "stale-entry session must not get a driver"
            rec_b = orch._records.get(b.id)
            if rec_b is not None:
                assert rec_b.status == "ended", \
                    f"stale-entry status must remain ended, got {rec_b.status}"

            # Queue row must be gone (removed by drain as part of skipping).
            positions_after = await sq.positions()
            assert b.id not in positions_after, "stale queue row must be removed by drain"
        finally:
            await _stop_orch(orch)

    asyncio.run(run())
