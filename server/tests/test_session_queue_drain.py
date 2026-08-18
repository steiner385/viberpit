"""Queue drain: slot-free → head starts; failure path; restart rehydrate."""
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


async def _make_orch(
    *,
    db_url: str,
    tmp_path,
    max_active_sessions: int,
    reuse_db: bool = False,
) -> SessionOrchestrator:
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

    if not reuse_db:
        # Seed AppSettings with the requested cap (only on first creation)
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


def test_close_drains_head_in_priority_order(tmp_path):
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'd.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            a = await orch.create_session("one")
            b = await orch.create_session("two")     # queued pos 1
            c = await orch.create_session("three")   # queued pos 2
            from server.sessions import session_queue as sq
            await sq.move(c.id, "top")               # c overtakes b
            await orch.close_session(a.id)
            await orch._maybe_start_queued()         # deterministic in tests
            assert orch._records[c.id].status != "queued"   # c started
            assert orch._records[b.id].status == "queued"   # b still waits
            assert (await sq.positions()) == {b.id: 1}
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_start_failure_ends_session_and_continues(tmp_path):
    """A failed-to-start session must be marked ended (not needs_attention).

    ended ∈ NON_ACTIVE_SESSION_STATUSES, so it does not consume a cap slot and
    the drain loop can start the next queued entry within the cap on the very
    next iteration — no skip_capacity_check bypass required.
    """
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'd2.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            a = await orch.create_session("one")
            bad = await orch.create_session("bad")
            ok = await orch.create_session("ok")
            # Sabotage the head's start (monkeypatch _spawn_session to raise once
            # for bad.id — e.g. wrap the real method).
            real = orch._spawn_session
            async def flaky(session_id, **kw):
                if session_id == bad.id:
                    raise RuntimeError("insufficient disk headroom at /x")
                return await real(session_id, **kw)
            orch._spawn_session = flaky
            await orch.close_session(a.id)
            # The close fires a background drain task; let it complete then also
            # call the explicit drain to cover the "idempotent: empty queue → 0" path.
            # Yield to the event loop so the background task runs first.
            await asyncio.sleep(0)
            started = await orch._maybe_start_queued()
            # The background drain already processed bad (→ ended) and ok
            # (→ started), so the explicit call finds an empty queue and returns 0.
            # What matters: bad is ended, ok started, queue is empty.
            assert started == 0                                  # background task did the work
            assert orch._records[bad.id].status == "ended"       # failed start → ended, not needs_attention
            assert orch._records[ok.id].status != "queued"       # ok started within the cap
            from server.sessions import session_queue as sq
            assert (await sq.positions()) == {}                  # both rows gone from queue
            # Cap invariant: active count must never exceed max_active_sessions (1).
            from server.db.models import NON_ACTIVE_SESSION_STATUSES
            active = sum(
                1 for r in orch._records.values()
                if r.status not in NON_ACTIVE_SESSION_STATUSES
            )
            assert active <= 1, f"cap breached: {active} active sessions with max=1"
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_rehydrate_preserves_queued_and_drains(tmp_path):
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'd3.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            a = await orch.create_session("one")
            b = await orch.create_session("two")            # queued
            # Close 'a' without triggering drain (to simulate a clean closed state
            # before restart) — we just call close_session normally; the slot is freed
            # but b remains queued so the next rehydrate sees it.
            await orch.close_session(a.id)
        finally:
            await _stop_orch(orch)

        # Simulate daemon restart: fresh orchestrator over the SAME DB.
        orch2 = await _make_orch(db_url=db_url, tmp_path=tmp_path,
                                 max_active_sessions=1, reuse_db=True)
        try:
            await orch2.rehydrate()
            assert orch2._records[b.id].status == "queued"  # survived as queued
            await orch2._maybe_start_queued()
            assert orch2._records[b.id].status != "queued"  # drained into the free slot
        finally:
            await _stop_orch(orch2)

    asyncio.run(run())


def test_queued_status_not_touched_by_midturn_normalization(tmp_path):
    """rehydrate must NOT rewrite 'queued' to 'idle' (only working/in_progress/condensing)."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'd4.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            a = await orch.create_session("one")
            b = await orch.create_session("two")  # queued
        finally:
            await _stop_orch(orch)

        # Second orchestrator over same DB
        orch2 = await _make_orch(db_url=db_url, tmp_path=tmp_path,
                                 max_active_sessions=1, reuse_db=True)
        try:
            await orch2.rehydrate()
            # 'queued' must not be normalized to 'idle'
            assert orch2._records[b.id].status == "queued"
        finally:
            await _stop_orch(orch2)

    asyncio.run(run())


def test_maybe_start_queued_tolerates_missing_lock_attribute(tmp_path):
    """_maybe_start_queued must not raise AttributeError if _queue_drain_lock is absent.

    Regression guard for issue #662: a live SessionOrchestrator created by an older
    code path (mid-session code update without daemon restart) won't have
    _queue_drain_lock set in __init__. Deleting the attribute here simulates that
    scenario; the method must lazy-init the lock and complete without error.
    """
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'd5.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=2)
        try:
            # Simulate the old-instance state by removing the attribute after init.
            del orch._queue_drain_lock
            assert not hasattr(orch, "_queue_drain_lock"), "precondition: attr must be gone"

            # Must not raise — should lazy-init the lock and return 0 (empty queue).
            result = await orch._maybe_start_queued()
            assert result == 0
            # The lock should have been lazily created.
            assert hasattr(orch, "_queue_drain_lock")
        finally:
            await _stop_orch(orch)

    asyncio.run(run())
