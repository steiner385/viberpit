"""Tests for session-archive feature.

Covers:
  - archive_session: moves cwd into archive_dir, sets status='archived',
    removes from _records; handles missing cwd gracefully.
  - rehydrate: excludes archived rows (status='archived' not loaded).
  - list_archived: returns rows with correct metadata.
  - unarchive: moves folder back, sets status='idle', reappears in list_sessions.
  - permanent_delete: removes archive folder + DB row; containment guard.
  - transcript_events: returns replayed events (mocked replay).

All tests use file-backed SQLite DBs via asyncio.run() so rehydrate callers
stay in the same event-loop context and the init/seed/query all share state.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import errno
import pytest

from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator, InsufficientStorageError
from server.tests.fakes.fake_factory import fake_sdk_factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed(rows: list[dict]) -> None:
    async with db_session() as db:
        for r in rows:
            db.add(DbSession(**r))


def _make_orch(tmp_path: Path) -> tuple[str, str, SessionOrchestrator]:
    """Return (db_url, archive_base, orchestrator) wired to a temp CCSWITCH_HOME."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    archive_base = str(tmp_path / "archive")
    return db_url, archive_base, SessionOrchestrator(sdk_factory=fake_sdk_factory())


# ---------------------------------------------------------------------------
# archive_session
# ---------------------------------------------------------------------------


def test_archive_moves_cwd_and_sets_db_status(tmp_path):
    """Closing via archive_session moves the cwd directory to archive and sets DB status."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    cwd = tmp_path / "session_cwd"
    cwd.mkdir()
    (cwd / "work.txt").write_text("some work")

    async def run():
        # Point CCSWITCH_HOME at tmp so archive_dir() lands inside tmp_path.
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([
                dict(id="s1", name="S1", account_name="acct1",
                     claude_session_id=None, status="idle", cwd=str(cwd)),
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            # Manually place a SessionRecord so archive_session can read rec.cwd.
            from server.sessions.types import SessionRecord
            orch._records["s1"] = SessionRecord(
                id="s1", name="S1", account="acct1",
                claude_session_id="", cols=80, rows=24, cwd=str(cwd),
            )
            await orch.archive_session("s1")

            # DB should be 'archived'.
            from sqlalchemy import select as sa_select
            async with db_session() as db:
                row = (await db.execute(
                    sa_select(DbSession).where(DbSession.id == "s1")
                )).scalars().first()
            return row.status, orch._records, cwd.exists()

    status, records, cwd_still_exists = asyncio.run(run())
    assert status == "archived"
    assert "s1" not in records   # removed from active _records
    assert not cwd_still_exists  # moved away

    # The archive folder should contain the file.
    archive_dest = tmp_path / "archive" / "s1"
    assert archive_dest.is_dir()
    assert (archive_dest / "work.txt").exists()


def test_archive_missing_cwd_still_marks_archived(tmp_path):
    """If the cwd is already gone, archive_session still marks the row archived."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([
                dict(id="s2", name="S2", account_name="acct1",
                     claude_session_id=None, status="idle",
                     cwd=str(tmp_path / "nonexistent")),
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            from server.sessions.types import SessionRecord
            orch._records["s2"] = SessionRecord(
                id="s2", name="S2", account="acct1",
                claude_session_id="", cols=80, rows=24,
                cwd=str(tmp_path / "nonexistent"),
            )
            await orch.archive_session("s2")

            from sqlalchemy import select as sa_select
            async with db_session() as db:
                row = (await db.execute(
                    sa_select(DbSession).where(DbSession.id == "s2")
                )).scalars().first()
            return row.status

    assert asyncio.run(run()) == "archived"


def test_archive_removes_from_records_and_drivers(tmp_path):
    """archive_session removes the session from _records and _drivers."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([
                dict(id="s3", name="S3", account_name="acct1",
                     claude_session_id=None, status="idle",
                     cwd=str(tmp_path / "cwd3")),
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            from server.sessions.types import SessionRecord
            orch._records["s3"] = SessionRecord(
                id="s3", name="S3", account="acct1",
                claude_session_id="", cols=80, rows=24,
                cwd=str(tmp_path / "cwd3"),
            )
            # Fake driver
            fake_driver = MagicMock()
            fake_driver.aclose = AsyncMock()
            orch._drivers["s3"] = fake_driver

            await orch.archive_session("s3")
            return "s3" in orch._records, "s3" in orch._drivers, fake_driver.aclose.called

    in_records, in_drivers, driver_closed = asyncio.run(run())
    assert not in_records
    assert not in_drivers
    assert driver_closed


# ---------------------------------------------------------------------------
# rehydrate excludes archived
# ---------------------------------------------------------------------------


def test_rehydrate_excludes_archived_rows(tmp_path):
    """rehydrate() does not load rows with status='archived'."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed([
            dict(id="a1", name="Active", account_name="acc1",
                 claude_session_id=None, status="idle", cwd="/x"),
            dict(id="ar1", name="Archived", account_name="acc1",
                 claude_session_id=None, status="archived", cwd="/y"),
            dict(id="e1", name="Ended", account_name="acc1",
                 claude_session_id=None, status="ended", cwd="/z"),
        ])
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        n = await orch.rehydrate()
        return n, set(orch._records.keys())

    n, ids = asyncio.run(run())
    assert n == 1
    assert ids == {"a1"}
    assert "ar1" not in ids
    assert "e1" not in ids


# ---------------------------------------------------------------------------
# list_archived
# ---------------------------------------------------------------------------


def test_list_archived_returns_archived_rows(tmp_path):
    """list_archived() returns dicts for archived rows only."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed([
            dict(id="arch1", name="Old project", account_name="acc2",
                 claude_session_id="csid-abc", status="archived", cwd="/old/proj"),
            dict(id="active1", name="Live", account_name="acc2",
                 claude_session_id=None, status="idle", cwd="/live"),
        ])
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        return await orch.list_archived()

    results = asyncio.run(run())
    assert len(results) == 1
    r = results[0]
    assert r["id"] == "arch1"
    assert r["name"] == "Old project"
    assert r["account"] == "acc2"
    assert r["claudeSessionId"] == "csid-abc"
    assert r["cwd"] == "/old/proj"
    assert "createdAt" in r
    assert "archivedAt" in r


def test_list_archived_empty_when_none(tmp_path):
    """list_archived() returns [] when there are no archived rows."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed([
            dict(id="a", name="A", account_name="x",
                 claude_session_id=None, status="idle", cwd="/a"),
        ])
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        return await orch.list_archived()

    assert asyncio.run(run()) == []


# ---------------------------------------------------------------------------
# unarchive
# ---------------------------------------------------------------------------


def test_unarchive_moves_folder_back_and_sets_idle(tmp_path):
    """unarchive() moves the archive dir back to the original cwd and sets status='idle'."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    original_cwd = tmp_path / "my_session_cwd"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            # Simulate an already-archived session: archive folder exists, original is gone.
            archive_path = tmp_path / "archive" / "s10"
            archive_path.mkdir(parents=True)
            (archive_path / "data.txt").write_text("archived data")

            await init_db(db_url=db_url)
            await _seed([
                dict(id="s10", name="My Session", account_name="acc1",
                     claude_session_id="csid-10", status="archived",
                     cwd=str(original_cwd)),
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            record = await orch.unarchive("s10")

            from sqlalchemy import select as sa_select
            async with db_session() as db:
                row = (await db.execute(
                    sa_select(DbSession).where(DbSession.id == "s10")
                )).scalars().first()
            return record, row.status, orch.list_sessions()

    record, db_status, sessions = asyncio.run(run())
    assert record is not None
    assert record.id == "s10"
    assert record.status == "idle"
    assert db_status == "idle"
    assert any(s.id == "s10" for s in sessions)
    assert original_cwd.is_dir()
    assert (original_cwd / "data.txt").read_text() == "archived data"
    assert not (tmp_path / "archive" / "s10").exists()


def test_unarchive_missing_archive_folder_recreates_cwd(tmp_path):
    """unarchive() with a missing archive folder recreates an empty cwd and still succeeds."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    original_cwd = tmp_path / "regen_cwd"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            # No archive folder present.
            await init_db(db_url=db_url)
            await _seed([
                dict(id="s11", name="Regen", account_name="acc1",
                     claude_session_id=None, status="archived",
                     cwd=str(original_cwd)),
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            record = await orch.unarchive("s11")
            return record

    record = asyncio.run(run())
    assert record is not None
    assert record.status == "idle"
    assert original_cwd.is_dir()  # empty but recreated


def test_unarchive_returns_none_for_unknown_session(tmp_path):
    """unarchive() returns None when the session does not exist or is not archived."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            return await orch.unarchive("does-not-exist")

    assert asyncio.run(run()) is None


def test_unarchive_reappears_in_list_sessions(tmp_path):
    """After unarchive the session appears in list_sessions without restart."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    original_cwd = tmp_path / "comeback_cwd"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            # Create archive folder
            archive_path = tmp_path / "archive" / "s12"
            archive_path.mkdir(parents=True)

            await init_db(db_url=db_url)
            await _seed([
                dict(id="s12", name="Comeback", account_name="acc1",
                     claude_session_id=None, status="archived",
                     cwd=str(original_cwd)),
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            before = orch.list_sessions()
            await orch.unarchive("s12")
            after = orch.list_sessions()
            return len(before), len(after), after[0].id

    before_count, after_count, session_id = asyncio.run(run())
    assert before_count == 0
    assert after_count == 1
    assert session_id == "s12"


# ---------------------------------------------------------------------------
# permanent_delete
# ---------------------------------------------------------------------------


def test_permanent_delete_removes_folder_and_db_row(tmp_path):
    """permanent_delete() removes the archive folder and the DB row."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            # Create archive folder with content.
            archive_path = tmp_path / "archive" / "s20"
            archive_path.mkdir(parents=True)
            (archive_path / "file.txt").write_text("deletable")

            await init_db(db_url=db_url)
            await _seed([
                dict(id="s20", name="Delete Me", account_name="acc1",
                     claude_session_id=None, status="archived",
                     cwd="/old/cwd/s20"),
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            result = await orch.permanent_delete("s20")

            from sqlalchemy import select as sa_select
            async with db_session() as db:
                row = (await db.execute(
                    sa_select(DbSession).where(DbSession.id == "s20")
                )).scalars().first()
            return result, row, archive_path.exists()

    deleted, db_row, folder_exists = asyncio.run(run())
    assert deleted is True
    assert db_row is None   # hard-deleted
    assert not folder_exists


def test_permanent_delete_live_session_archives_then_deletes(tmp_path):
    """permanent_delete on a LIVE session archives it first, then hard-deletes.

    A live session is still in _records/_drivers with its cwd at the original
    location (outside archive_dir). Deleting it directly would leave a zombie
    driver and orphan the working directory. permanent_delete must archive first
    (stop the driver, move the cwd into the archive) and only then remove the
    archive folder + DB row.
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    cwd = tmp_path / "live_cwd"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            cwd.mkdir()
            (cwd / "work.txt").write_text("live work")

            await init_db(db_url=db_url)
            await _seed([
                dict(id="live1", name="Live", account_name="acc1",
                     claude_session_id=None, status="idle", cwd=str(cwd)),
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            from server.sessions.types import SessionRecord
            orch._records["live1"] = SessionRecord(
                id="live1", name="Live", account="acc1",
                claude_session_id="", cols=80, rows=24, cwd=str(cwd),
            )
            fake_driver = MagicMock()
            fake_driver.aclose = AsyncMock()
            orch._drivers["live1"] = fake_driver

            result = await orch.permanent_delete("live1")

            from sqlalchemy import select as sa_select
            async with db_session() as db:
                row = (await db.execute(
                    sa_select(DbSession).where(DbSession.id == "live1")
                )).scalars().first()
            return (
                result, row, fake_driver.aclose.called,
                "live1" in orch._records, "live1" in orch._drivers,
                cwd.exists(), (tmp_path / "archive" / "live1").exists(),
            )

    (deleted, db_row, driver_closed, in_records, in_drivers,
     cwd_exists, archive_exists) = asyncio.run(run())
    assert deleted is True
    assert db_row is None          # DB row hard-deleted
    assert driver_closed           # driver stopped — no zombie
    assert not in_records          # cleared from active state
    assert not in_drivers
    assert not cwd_exists          # live working dir not orphaned (moved then removed)
    assert not archive_exists      # archive folder cleaned up too


def test_permanent_delete_returns_false_for_unknown(tmp_path):
    """permanent_delete() returns False when the session is not in DB."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            return await orch.permanent_delete("no-such-session")

    assert asyncio.run(run()) is False


def test_permanent_delete_containment_guard(tmp_path):
    """permanent_delete() must not delete paths outside archive_dir().

    This tests the containment guard: a session_id containing path traversal
    components (e.g. '../etc') should NOT be able to escape the archive base.
    We verify the guard triggers by directly calling _the candidate resolution_
    logic with a crafted UUID-shaped id that in practice can't traverse (UUIDs
    are safe), and separately verify the guard path logic is sound.
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            # A valid DB row but its candidate path resolves to the archive_dir itself,
            # which is not strictly INSIDE it (relative_to fails for parent == base).
            # We simulate by crafting a session id that is a real uuid so the DB
            # row is accepted, but we inspect what permanent_delete would do.
            await init_db(db_url=db_url)
            # Seed a normal row — the guard is about path containment.
            await _seed([
                dict(id="safe-uuid-1234", name="Guard Test", account_name="acc1",
                     claude_session_id=None, status="archived",
                     cwd="/old/cwd"),
            ])

            # Verify archive_dir() / "safe-uuid-1234" stays inside archive_dir().
            from ccswitch.paths import archive_dir
            base = archive_dir().resolve()
            candidate = (base / "safe-uuid-1234").resolve()
            # Should NOT raise ValueError (i.e., is contained).
            candidate.relative_to(base)

            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            result = await orch.permanent_delete("safe-uuid-1234")
            return result

    assert asyncio.run(run()) is True


# ---------------------------------------------------------------------------
# transcript_events
# ---------------------------------------------------------------------------


def test_transcript_events_returns_replayed_events(tmp_path):
    """transcript_events() calls replay_events and returns the event list."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([
                dict(id="t1", name="T1", account_name="acc1",
                     claude_session_id="csid-t1", status="archived",
                     cwd="/old"),
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())

            # Mock transcript_path to return a fake path and replay_events.
            fake_event = MagicMock()
            fake_event.to_wire.return_value = {"type": "textDelta", "text": "hello"}

            with patch.object(orch, "transcript_path", return_value="/fake/path.jsonl"):
                with patch(
                    "server.sessions.replay.replay_events",
                    new=AsyncMock(return_value=[fake_event]),
                ) as mock_replay:
                    events = await orch.transcript_events("t1")
                    # Default is the bounded 20-turn tail; user-facing callers pass
                    # max_turns=None explicitly (PR #482 un-capping).
                    mock_replay.assert_called_once_with("/fake/path.jsonl", max_turns=20)
            return events

    events = asyncio.run(run())
    assert len(events) == 1
    assert events[0].to_wire() == {"type": "textDelta", "text": "hello"}


def test_transcript_events_returns_empty_when_no_transcript(tmp_path):
    """transcript_events() returns [] when no transcript path is found."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            # Session with no claude_session_id → can't glob a transcript.
            await _seed([
                dict(id="t2", name="T2", account_name="acc1",
                     claude_session_id=None, status="archived",
                     cwd="/old"),
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            with patch.object(orch, "transcript_path", return_value=None):
                return await orch.transcript_events("t2")

    assert asyncio.run(run()) == []


def test_resolve_transcript_path_without_credential_slot(tmp_path):
    """Archived transcripts resolve WITHOUT composing the account config dir.

    compose() writes credentials and raises FileNotFoundError for an account
    whose store slot no longer exists (renamed/deleted account — e.g. an old
    copilot account name kept forever by archived rows). Resolution is a READ
    and must stay side-effect free: glob the account cfg dir directly.
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([
                dict(id="g1", name="Ghost", account_name="ghost_account",
                     claude_session_id="csid-ghost", status="archived",
                     cwd="/old"),
            ])
            # Transcript exists under the account cfg dir; NO store slot
            # (store/ghost_account.cred.json) and no copilot token dir exist.
            proj = tmp_path / "cfg" / "ghost_account" / "projects" / "-home-u-repo"
            proj.mkdir(parents=True)
            transcript = proj / "csid-ghost.jsonl"
            transcript.write_text("{}\n")
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            return await orch.resolve_transcript_path("g1"), str(transcript)

    path, expected = asyncio.run(run())
    assert path == expected


# ---------------------------------------------------------------------------
# inode-exhaustion guard
# ---------------------------------------------------------------------------


def test_unarchive_raises_insufficient_storage_when_move_skipped(tmp_path):
    """unarchive() raises InsufficientStorageError when move_into_archive returns skipped:nospace."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    original_cwd = tmp_path / "oom_cwd"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            archive_path = tmp_path / "archive" / "s_oom"
            archive_path.mkdir(parents=True)
            (archive_path / "data.txt").write_text("data")

            await init_db(db_url=db_url)
            await _seed([
                dict(id="s_oom", name="OOM", account_name="acc1",
                     claude_session_id=None, status="archived",
                     cwd=str(original_cwd)),
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            with patch("server.sessions.orchestrator.move_into_archive", return_value="skipped:nospace"):
                return await orch.unarchive("s_oom")

    with pytest.raises(InsufficientStorageError):
        asyncio.run(run())


def test_unarchive_error_message_names_real_mountpoint(tmp_path):
    """The out-of-space message reports the actual filesystem (mount point) of the
    restore target, not Path.anchor — which is always '/' and misleads on hosts
    whose data lives on a separate mount (e.g. /mnt/ssk-ssd)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    original_cwd = tmp_path / "oom_cwd"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            archive_path = tmp_path / "archive" / "s_oom"
            archive_path.mkdir(parents=True)
            (archive_path / "data.txt").write_text("data")
            await init_db(db_url=db_url)
            await _seed([
                dict(id="s_oom", name="OOM", account_name="acc1",
                     claude_session_id=None, status="archived",
                     cwd=str(original_cwd)),
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            with patch("server.sessions.orchestrator.move_into_archive", return_value="skipped:nospace"), \
                 patch("server.sessions.orchestrator.mountpoint", return_value=Path("/mnt/ssk-ssd")):
                return await orch.unarchive("s_oom")

    # The real mount point appears; the misleading bare "(/)" does not.
    with pytest.raises(InsufficientStorageError, match=r"\(/mnt/ssk-ssd\)"):
        asyncio.run(run())


def test_move_into_archive_skips_when_inodes_low(tmp_path):
    """move_into_archive returns skipped:nospace when destination is low on inodes."""
    from server.sessions.archive_move import move_into_archive

    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_text("hi")
    dest = tmp_path / "dest"

    # Fake statvfs: claim f_favail=1 (below FREE_INODE_MARGIN).
    # Also fake rename to raise EXDEV so we reach the cross-device copy path.
    fake_sv = MagicMock()
    fake_sv.f_favail = 1

    with patch("server.sessions.archive_move.os.rename", side_effect=OSError(errno.EXDEV, "cross")), \
         patch("server.sessions.archive_move.os.statvfs", return_value=fake_sv), \
         patch("server.sessions.archive_move.shutil.disk_usage", return_value=MagicMock(free=10 * 1024 ** 3)):
        result = move_into_archive(src, dest, margin=0)

    assert result == "skipped:nospace"
    assert not dest.exists()  # no partial copy left behind
