"""Tests for the archived-session cap: purge_oldest_archived, _enforce_archive_limit,
and the on-archive enforcement hook.

Uses file-backed SQLite via asyncio.run() so init/seed/query share one event-loop
context, mirroring test_archive.py.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from sqlalchemy import select as sa_select

from server.db import db_session, init_db
from server.db.models import AppSettings
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


_BASE = datetime(2026, 1, 1, 12, 0, 0)


async def _seed(rows: list[dict]) -> None:
    async with db_session() as db:
        for r in rows:
            db.add(DbSession(**r))


def _archived(idx: int, *, archived_at: datetime | None, created_at: datetime | None = None) -> dict:
    """An archived row with a controllable archived_at (and optional created_at)."""
    row = dict(
        id=f"arch{idx}", name=f"A{idx}", account_name="acc1",
        claude_session_id=None, status="archived", cwd=f"/old/{idx}",
        archived_at=archived_at,
    )
    if created_at is not None:
        row["created_at"] = created_at
    return row


async def _archived_ids() -> list[str]:
    async with db_session() as db:
        rows = (await db.execute(
            sa_select(DbSession.id).where(DbSession.status == "archived")
        )).scalars().all()
    return sorted(rows)


# ---------------------------------------------------------------------------
# purge_oldest_archived
# ---------------------------------------------------------------------------


def test_purge_oldest_archived_deletes_oldest_first(tmp_path):
    """purge_oldest_archived(2) removes the 2 oldest by archived_at, keeps the newest."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([
                _archived(0, archived_at=_BASE + timedelta(hours=0)),  # oldest
                _archived(1, archived_at=_BASE + timedelta(hours=1)),
                _archived(2, archived_at=_BASE + timedelta(hours=2)),  # newest
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            deleted = await orch.purge_oldest_archived(2)
            return deleted, await _archived_ids()

    deleted, remaining = asyncio.run(run())
    assert set(deleted) == {"arch0", "arch1"}
    assert remaining == ["arch2"]


def test_purge_oldest_archived_zero_is_noop(tmp_path):
    """purge_oldest_archived(0) deletes nothing."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([_archived(0, archived_at=_BASE)])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            deleted = await orch.purge_oldest_archived(0)
            return deleted, await _archived_ids()

    deleted, remaining = asyncio.run(run())
    assert deleted == []
    assert remaining == ["arch0"]


def test_purge_oldest_archived_null_archived_at_falls_back_to_created_at(tmp_path):
    """A legacy row with no archived_at sorts by created_at, so it's treated as oldest."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([
                # No archived_at, but created earliest → should be picked first.
                _archived(0, archived_at=None, created_at=_BASE - timedelta(days=1)),
                _archived(1, archived_at=_BASE + timedelta(hours=1)),
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            deleted = await orch.purge_oldest_archived(1)
            return deleted, await _archived_ids()

    deleted, remaining = asyncio.run(run())
    assert deleted == ["arch0"]
    assert remaining == ["arch1"]


# ---------------------------------------------------------------------------
# _enforce_archive_limit
# ---------------------------------------------------------------------------


async def _set_max(limit: int | None) -> None:
    async with db_session() as db:
        db.add(AppSettings(id="singleton", max_archived_sessions=limit))


def test_enforce_archive_limit_trims_to_cap(tmp_path):
    """With max=2 and 5 archived, enforcement purges the 3 oldest, leaving the 2 newest."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _set_max(2)
            await _seed([
                _archived(i, archived_at=_BASE + timedelta(hours=i)) for i in range(5)
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            n = await orch._enforce_archive_limit()
            return n, await _archived_ids()

    n, remaining = asyncio.run(run())
    assert n == 3
    assert remaining == ["arch3", "arch4"]  # two newest survive


def test_enforce_archive_limit_noop_when_unset(tmp_path):
    """No max configured (no settings row) → enforcement deletes nothing."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([
                _archived(i, archived_at=_BASE + timedelta(hours=i)) for i in range(3)
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            n = await orch._enforce_archive_limit()
            return n, await _archived_ids()

    n, remaining = asyncio.run(run())
    assert n == 0
    assert remaining == ["arch0", "arch1", "arch2"]


def test_enforce_archive_limit_noop_when_under_cap(tmp_path):
    """Archived count already within the cap → nothing purged."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _set_max(10)
            await _seed([
                _archived(i, archived_at=_BASE + timedelta(hours=i)) for i in range(3)
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            n = await orch._enforce_archive_limit()
            return n, await _archived_ids()

    n, remaining = asyncio.run(run())
    assert n == 0
    assert remaining == ["arch0", "arch1", "arch2"]


# ---------------------------------------------------------------------------
# on-archive enforcement hook
# ---------------------------------------------------------------------------


def test_archive_session_enforces_cap(tmp_path):
    """Archiving a fresh session past the cap purges the oldest, holding the count at max."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    cwd = tmp_path / "fresh_cwd"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            cwd.mkdir()
            await init_db(db_url=db_url)
            await _set_max(1)
            # One already-archived (old) session + one live session about to be archived.
            await _seed([
                _archived(0, archived_at=_BASE),  # oldest — should be purged
                dict(id="live1", name="Live", account_name="acc1",
                     claude_session_id=None, status="idle", cwd=str(cwd)),
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            from server.sessions.types import SessionRecord
            orch._records["live1"] = SessionRecord(
                id="live1", name="Live", account="acc1",
                claude_session_id="", cols=80, rows=24, cwd=str(cwd),
            )
            await orch.archive_session("live1")
            # Cap enforcement runs as a fire-and-forget task off the request
            # path — drain the orchestrator's background tasks before asserting.
            await asyncio.gather(*list(getattr(orch, "_bg_tasks", ()) or ()),
                                 return_exceptions=True)
            return await _archived_ids()

    remaining = asyncio.run(run())
    # Cap is 1: the freshly-archived live1 survives, the older arch0 is purged.
    assert remaining == ["live1"]
