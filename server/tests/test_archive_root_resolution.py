"""Session.archive_root — archived sessions resolve through the root they were
written to, not through the *current* archive home.

``archive_home`` is a PLACEMENT setting (which disk archives live on), while being
archived is a LIFECYCLE fact about a session. Fusing the two meant that pointing
CCSWITCH_ARCHIVE_HOME at a different disk retroactively changed where every past
archive was believed to live: unarchive looked in the new root, missed, and silently
recreated an empty cwd — reporting success while handing the user an empty worktree.

Covers:
  - archive_session (now a status flip) stamps archive_root NULL — the marker
    for "files still at cwd"; only LEGACY rows carry a root.
  - unarchive resolves legacy rows through their stored root even when the
    current home differs.
  - a missing archive raises ArchiveMissingError instead of fabricating an empty cwd.
  - ancient rows (archive_root NULL, cwd moved away) still fall back to the
    current archive home.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest
from sqlalchemy import select as sa_select


def _orch():
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    return SessionOrchestrator(sdk_factory=fake_sdk_factory())


async def _add_archived(*, cwd, archive_root):
    from server.db import db_session
    from server.db.models import Session as DbSession
    async with db_session() as db:
        db.add(DbSession(id="s1", name="S1", account_name="a", engine_session_id=None,
                         status="archived", cwd=str(cwd),
                         archive_root=(str(archive_root) if archive_root else None)))


def test_archive_session_stamps_null_root_and_leaves_cwd(tmp_path):
    """archive_session is a status flip: archive_root is stamped NULL (files stay
    at cwd) — including clearing a stale root left by a legacy archive→unarchive
    round trip — and nothing lands under the archive home."""
    from server.db import db_session, init_db
    from server.db.models import Session as DbSession
    from server.sessions.engine import AgentEngine
    from server.sessions.types import SessionRecord

    home = tmp_path / "archives"
    cwd = tmp_path / "sess_cwd"
    cwd.mkdir()

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path),
                                     "CCSWITCH_ARCHIVE_HOME": str(home)}):
            await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'ar.db'}")
            async with db_session() as db:
                # A stale root from a legacy archive→unarchive round trip must be
                # cleared, or unarchive would look for files that are at cwd.
                db.add(DbSession(id="s1", name="S1", account_name="a",
                                 engine_session_id=None, status="idle", cwd=str(cwd),
                                 archive_root=str(tmp_path / "stale_old_root")))
            orch = _orch()
            orch._records["s1"] = SessionRecord(
                id="s1", name="S1", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="",
                cols=80, rows=24, cwd=str(cwd))
            await orch.archive_session("s1")
            async with db_session() as db:
                return (await db.execute(
                    sa_select(DbSession).where(DbSession.id == "s1"))).scalars().first().archive_root

    assert asyncio.run(run()) is None, "a status-flip archive must stamp archive_root NULL"
    assert cwd.is_dir(), "the worktree must stay in place"
    assert not (tmp_path / "archives" / "s1").exists()


def test_unarchive_resolves_through_stored_root_not_current_home(tmp_path):
    """The regression: the archive home MOVED after this session was archived.

    The bytes are under the OLD root; the current setting points at a new, empty one.
    unarchive must follow the stored root and restore the real tree.
    """
    from server.db import init_db

    old_root = tmp_path / "old_disk"          # where the session really lives
    new_root = tmp_path / "new_disk"          # where the setting now points
    new_root.mkdir(parents=True)
    (old_root / "s1").mkdir(parents=True)
    (old_root / "s1" / "work.txt").write_text("real work")
    cwd = tmp_path / "wt" / "s1"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path),
                                     "CCSWITCH_ARCHIVE_HOME": str(new_root)}):
            await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'res.db'}")
            await _add_archived(cwd=cwd, archive_root=old_root)
            await _orch().unarchive("s1")

    asyncio.run(run())
    assert (cwd / "work.txt").read_text() == "real work", (
        "unarchive must restore from the stored archive_root, not the current home")


def test_unarchive_raises_when_archive_missing_and_leaves_no_empty_cwd(tmp_path):
    """A missing archive must fail loudly — never fabricate an empty worktree."""
    from server.db import init_db
    from server.sessions.orchestrator import ArchiveMissingError

    root = tmp_path / "archives"
    root.mkdir()
    cwd = tmp_path / "wt" / "s1"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path),
                                     "CCSWITCH_ARCHIVE_HOME": str(root)}):
            await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'miss.db'}")
            await _add_archived(cwd=cwd, archive_root=root)
            await _orch().unarchive("s1")

    with pytest.raises(ArchiveMissingError) as exc:
        asyncio.run(run())
    assert "s1" in str(exc.value)
    assert not cwd.exists(), "must not create an empty cwd when the archive is missing"


def test_unarchive_legacy_null_root_falls_back_to_current_home(tmp_path):
    """Rows archived before archive_root existed resolve via the current home."""
    from server.db import init_db

    root = tmp_path / "archives"
    (root / "s1").mkdir(parents=True)
    (root / "s1" / "legacy.txt").write_text("legacy")
    cwd = tmp_path / "wt" / "s1"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path),
                                     "CCSWITCH_ARCHIVE_HOME": str(root)}):
            await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
            await _add_archived(cwd=cwd, archive_root=None)
            await _orch().unarchive("s1")

    asyncio.run(run())
    assert (cwd / "legacy.txt").read_text() == "legacy"
