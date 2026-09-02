"""Orchestrator integration tests for the worktree janitor loop."""
import asyncio
import os
import pytest
from datetime import datetime
from unittest.mock import patch


from server.db import db_session, dispose_db, init_db
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _build_orch(tmp_path, **row):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    await init_db(db_url=db_url)
    return SessionOrchestrator(sdk_factory=fake_sdk_factory(), repo_root=str(tmp_path))


class TestJanitorLoop:
    @pytest.mark.asyncio
    async def test_janitor_sweep_once_collects_records_and_worktrees(self, tmp_path):
        """Both _records (rehydrated idle sessions) and _worktrees (live drivers)
        must contribute their cwds to the live_cwds frozenset."""
        from ccswitch.worktrees import WorktreeInfo

        orch = await _build_orch(tmp_path)
        # Fake a record (rehydrated idle, no driver)
        from server.sessions.types import SessionRecord
        orch._records["idle-1"] = SessionRecord(
            id="idle-1", name="idle", account="acct1",
            claude_session_id="c1", cols=80, rows=24,
            cwd="/repo/idle-wt", status="idle",
        )
        # Fake a live worktree entry
        orch._worktrees["live-1"] = WorktreeInfo(
            path="/repo/live-wt", branch="main", is_child=False, repo="/repo"
        )

        captured = {}

        def mock_sweep(repo_root, live_cwds, **kw):
            captured["live_cwds"] = live_cwds
            return {"scanned": 0, "removed": 0, "kept_live": 0,
                    "kept_young": 0, "kept_dirty": 0, "kept_unpushed": 0,
                    "kept_error": 0, "evicted_dirty": 0}

        with patch("server.sessions.orchestrator.sweep_orphaned_worktrees", mock_sweep,
                   create=True):
            with patch("ccswitch.worktrees_janitor.sweep_orphaned_worktrees", mock_sweep):
                orch._janitor_sweep_once()

        # Dispose the engine while this loop is still alive so the next test's
        # init_db() does not cross-loop dispose a stale aiosqlite engine.
        await dispose_db()

        assert "/repo/idle-wt" in captured["live_cwds"]
        assert "/repo/live-wt" in captured["live_cwds"]

    @pytest.mark.asyncio
    async def test_janitor_sweep_once_uses_abspath(self, tmp_path):
        """Relative cwd paths must be resolved to absolute before comparison."""
        import os

        orch = await _build_orch(tmp_path)
        from server.sessions.types import SessionRecord
        orch._records["rel-1"] = SessionRecord(
            id="rel-1", name="r", account="acct1",
            claude_session_id="c2", cols=80, rows=24,
            cwd="relative/path", status="idle",
        )

        captured = {}

        def mock_sweep(repo_root, live_cwds, **kw):
            captured["live_cwds"] = live_cwds
            return {"scanned": 0, "removed": 0, "kept_live": 0,
                    "kept_young": 0, "kept_dirty": 0, "kept_unpushed": 0,
                    "kept_error": 0, "evicted_dirty": 0}

        with patch("ccswitch.worktrees_janitor.sweep_orphaned_worktrees", mock_sweep):
            orch._janitor_sweep_once()

        await dispose_db()

        assert all(os.path.isabs(p) for p in captured["live_cwds"])

    @pytest.mark.asyncio
    async def test_janitor_loop_skips_during_shutdown(self, tmp_path):
        """When _shutting_down is True the sweep must not be called."""
        orch = await _build_orch(tmp_path)
        orch._shutting_down = True

        called = [False]

        real_sweep = orch._janitor_sweep_once

        def patched_sweep():
            called[0] = True
            return real_sweep()

        orch._janitor_sweep_once = patched_sweep
        # Run one iteration of the loop with a very short interval
        task = asyncio.create_task(orch._worktree_janitor_loop(interval=0.0))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        await dispose_db()

        assert called[0] is False

    @pytest.mark.asyncio
    async def test_janitor_sweep_includes_archived_session_cwds(self, tmp_path):
        """Archived sessions are absent from _records but their cwds must still appear
        in live_cwds so the 24 h janitor does not sweep their stay-in-place worktrees
        (#921)."""
        orch = await _build_orch(tmp_path)

        # Seed an archived row in the DB (simulating a session already archived).
        archived_cwd = str(tmp_path / "archived-wt")
        async with db_session() as db:
            db.add(DbSession(
                id="archived-1", name="Archived", account_name="acc1",
                claude_session_id=None, status="archived",
                cwd=archived_cwd, archived_at=datetime(2026, 1, 1),
            ))

        captured = {}

        def mock_sweep(repo_root, live_cwds, **kw):
            captured["live_cwds"] = live_cwds
            return {"scanned": 0, "removed": 0, "kept_live": 0,
                    "kept_young": 0, "kept_dirty": 0, "kept_unpushed": 0,
                    "kept_error": 0, "evicted_dirty": 0}

        with patch("ccswitch.worktrees_janitor.sweep_orphaned_worktrees", mock_sweep):
            archived_cwds = await orch._fetch_archived_cwds()
            orch._janitor_sweep_once(archived_cwds)

        await dispose_db()

        assert os.path.abspath(archived_cwd) in captured["live_cwds"]
