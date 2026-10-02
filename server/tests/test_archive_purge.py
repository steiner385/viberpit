"""Tests for the archived-session cap: purge_oldest_archived, _enforce_archive_limit,
and the on-archive enforcement hook.

Uses file-backed SQLite via asyncio.run() so init/seed/query share one event-loop
context, mirroring test_archive.py.
"""
from __future__ import annotations

import asyncio
import errno
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
from server.sessions.orchestrator import (
    _ARCHIVE_PURGE_MAX_STRIKES,
    _rmtree_best_effort,
    SessionOrchestrator,
)
from server.tests.fakes.fake_factory import fake_sdk_factory


_BASE = datetime(2026, 1, 1, 12, 0, 0)


async def _seed(rows: list[dict]) -> None:
    async with db_session() as db:
        for r in rows:
            db.add(DbSession(**r))


def _archived(idx: int, *, archived_at: datetime | None, created_at: datetime | None = None,
              origin: str | None = None) -> dict:
    """An archived row with a controllable archived_at (and optional created_at/origin)."""
    row = dict(
        id=f"arch{idx}", name=f"A{idx}", account_name="acc1",
        engine_session_id=None, status="archived", cwd=f"/old/{idx}",
        archived_at=archived_at,
    )
    if created_at is not None:
        row["created_at"] = created_at
    if origin is not None:
        row["origin"] = origin
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
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
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
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
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
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
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


def test_purge_detaches_dispatch_outcomes_instead_of_blocking_delete(tmp_path):
    """A dispatched (automation-origin) session has a DispatchOutcome row FK-referencing
    it. permanent_delete must null that reference rather than leaving it dangling — on
    Postgres, deleting the session row with the FK still pointing at it throws
    ForeignKeyViolationError, which wedges the cap purge forever since #921 purges
    automation-origin sessions (the ones dispatch creates outcomes for) first."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([_archived(0, archived_at=_BASE, origin="automation")])
            from server.db.models import DispatchOutcome
            async with db_session() as db:
                # Finalised: a still-``running`` row would pin the session (#2632).
                db.add(DispatchOutcome(session_id="arch0", outcome="no_pr"))
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            deleted = await orch.purge_oldest_archived(1)
            async with db_session() as db:
                outcome = (await db.execute(sa_select(DispatchOutcome))).scalars().first()
            return deleted, outcome.session_id if outcome else "MISSING"

    deleted, outcome_session_id = asyncio.run(run())
    assert deleted == ["arch0"]
    assert outcome_session_id is None  # history row survives, reference detached


def _absent_dir_records(caplog) -> list:
    return [r for r in caplog.records if "archive dir" in r.getMessage()
            and "absent" in r.getMessage().lower()]


def test_purge_absent_archive_dir_with_unset_root_is_not_a_warning(tmp_path, caplog):
    """A new-style archive (archive_root NULL) whose worktree is already gone has nothing
    under the archive home to lose: the purge still drops the row, but the "dir absent"
    line is expected noise, not a stranded-directory alarm — a WARNING here auto-files an
    issue on every such purge (#1573)."""
    import logging
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'viberpit.db'}"

    async def run():
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([_archived(0, archived_at=_BASE)])  # cwd=/old/0 never exists
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
                return await orch.purge_oldest_archived(1)

    assert asyncio.run(run()) == ["arch0"]  # row still deleted
    records = _absent_dir_records(caplog)
    assert records, "the absent-dir condition should still be recorded"
    assert all(r.levelno == logging.INFO for r in records)


def test_purge_absent_archive_dir_with_recorded_root_still_warns(tmp_path, caplog):
    """A legacy row records the root its files were moved into, so an absent dir there is a
    genuine stranded-directory signal (the 2026-08-21 incident) and must stay a WARNING."""
    import logging
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'viberpit.db'}"
    root = tmp_path / "archive-root"
    root.mkdir()

    async def run():
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([{**_archived(0, archived_at=_BASE), "archive_root": str(root)}])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
                return await orch.purge_oldest_archived(1)

    assert asyncio.run(run()) == ["arch0"]
    records = _absent_dir_records(caplog)
    assert [r.levelno for r in records] == [logging.WARNING]


def _unmanaged_cwd_purge_records(tmp_path, caplog, *, kind: str | None) -> tuple[list, bool]:
    """Purge one archived row whose cwd exists outside the managed worktree tree; return
    the containment-guard log records and whether the directory survived."""
    import logging
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'viberpit.db'}"
    scratch = tmp_path / "cfg" / "assistant" / "47ecfd467398f8b1"
    scratch.mkdir(parents=True)

    async def run():
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([{**_archived(0, archived_at=_BASE), "cwd": str(scratch),
                          "kind": kind}])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
                return await orch.purge_oldest_archived(1)

    assert asyncio.run(run()) == ["arch0"]  # row dropped either way
    records = [r for r in caplog.records if "leaving it in place" in r.getMessage()]
    return records, scratch.exists()


def test_purge_assistant_scratch_dir_is_not_a_warning(tmp_path, caplog):
    """The assistant's per-identity scratch dir never sits in a managed worktree; leaving it
    in place is the expected behavior, so it must not log at WARNING (which auto-files an
    issue per purge, #2029)."""
    import logging
    records, survived = _unmanaged_cwd_purge_records(tmp_path, caplog, kind="assistant")
    assert survived
    assert [r.levelno for r in records] == [logging.INFO]


def test_purge_unmanaged_non_assistant_cwd_still_warns(tmp_path, caplog):
    """A non-assistant row pointing outside the managed worktree tree is a genuinely misconfigured
    row and stays a WARNING."""
    import logging
    records, survived = _unmanaged_cwd_purge_records(tmp_path, caplog, kind=None)
    assert survived
    assert [r.levelno for r in records] == [logging.WARNING]


def test_purge_prioritizes_automation_over_human(tmp_path):
    """Automation-origin sessions are purged first; human sessions survive as long as
    there are automation archives to consume (#921)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([
                # Human session archived first (oldest timestamp) — must survive.
                _archived(0, archived_at=_BASE + timedelta(hours=0), origin=None),
                # Two automation sessions archived later — must be purged first.
                _archived(1, archived_at=_BASE + timedelta(hours=1), origin="backlog-runner"),
                _archived(2, archived_at=_BASE + timedelta(hours=2), origin="pr-doctor"),
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            deleted = await orch.purge_oldest_archived(2)
            return deleted, await _archived_ids()

    deleted, remaining = asyncio.run(run())
    # The two automation sessions must have been purged (order within may vary by age).
    assert set(deleted) == {"arch1", "arch2"}
    # The human session (oldest timestamp) survives.
    assert remaining == ["arch0"]


def test_purge_skips_archive_whose_dispatch_is_still_unsettled(tmp_path):
    """#2632: a runner session is auto-archived the moment it ends, BEFORE the
    runner's sweep settles its claim by reading the session row (the investigation
    exit is the session's ``mark_no_pr`` note). With the cap full of human archives
    the automation-first order (#921) used to select that fresh archive and delete
    it, so the sweep found the row gone and released the claim as orphaned —
    every exit unread, the ``evidence`` hold never released. A session whose
    ``dispatch_outcomes`` row is still ``running`` must not be purged; the oldest
    human archive goes instead, and once the outcome is finalised the automation
    archive is first in line again."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'viberpit.db'}"

    async def run():
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([
                _archived(0, archived_at=_BASE + timedelta(hours=0), origin=None),
                _archived(1, archived_at=_BASE + timedelta(hours=1), origin=None),
                _archived(2, archived_at=_BASE + timedelta(hours=2), origin="backlog-runner"),
            ])
            from server.db.models import DispatchOutcome
            async with db_session() as db:
                db.add(DispatchOutcome(id="out2", session_id="arch2", outcome="running",
                                       dispatch_kind="investigate"))
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            first = await orch.purge_oldest_archived(1)
            async with db_session() as db:
                row = await db.get(DispatchOutcome, "out2")
                row.outcome = "no_pr"
            second = await orch.purge_oldest_archived(1)
            return first, second, await _archived_ids()

    first, second, remaining = asyncio.run(run())
    assert first == ["arch0"]      # the unsettled runner archive is pinned
    assert second == ["arch2"]     # settled → automation-first again (#921)
    assert remaining == ["arch1"]


def test_purge_human_purged_when_no_automation_left(tmp_path):
    """When there are no automation sessions, human sessions are purged oldest-first
    as the normal fallback (#921)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([
                _archived(0, archived_at=_BASE + timedelta(hours=0), origin=None),  # oldest
                _archived(1, archived_at=_BASE + timedelta(hours=1), origin=None),  # newest
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            deleted = await orch.purge_oldest_archived(1)
            return deleted, await _archived_ids()

    deleted, remaining = asyncio.run(run())
    assert deleted == ["arch0"]  # oldest human is still removed first
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
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
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
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
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
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
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
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
            cwd.mkdir()
            await init_db(db_url=db_url)
            await _set_max(1)
            # One already-archived (old) session + one live session about to be archived.
            await _seed([
                _archived(0, archived_at=_BASE),  # oldest — should be purged
                dict(id="live1", name="Live", account_name="acc1",
                     engine_session_id=None, status="idle", cwd=str(cwd)),
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            from server.sessions.engine import AgentEngine
            from server.sessions.types import SessionRecord
            orch._records["live1"] = SessionRecord(
                id="live1", name="Live", account="acc1",
                engine=AgentEngine.CLAUDE_CODE,
                engine_session_id="", cols=80, rows=24, cwd=str(cwd),
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


# ---------------------------------------------------------------------------
# #825 defect 1: rmtree that truly never raises on a stale sshfs handle
# ---------------------------------------------------------------------------


def test_rmtree_best_effort_swallows_estale(tmp_path):
    """shutil.rmtree can raise ESTALE even with ignore_errors=True (CPython 3.12's
    _rmtree_safe_fd fstats the dir fd outside the guarded region). The helper must
    catch it and return an error string instead of propagating (#825)."""
    target = tmp_path / "victim"
    target.mkdir()

    def _raise_estale(*a, **k):
        raise OSError(errno.ESTALE, "Stale file handle")

    with patch("server.sessions.orchestrator.shutil.rmtree", _raise_estale):
        err = _rmtree_best_effort(str(target))

    assert err is not None
    assert "116" in err or "ESTALE" in err


def test_rmtree_best_effort_returns_none_on_success(tmp_path):
    """On a clean delete the helper reports success (None) and the dir is gone."""
    target = tmp_path / "victim"
    (target / "sub").mkdir(parents=True)
    (target / "sub" / "f.txt").write_text("x")

    err = _rmtree_best_effort(str(target))

    assert err is None
    assert not target.exists()


def test_rmtree_best_effort_names_foreign_owned_survivor(tmp_path):
    """When ignore_errors=True swallows a per-file failure and leaves the path
    behind, the error string should name the surviving entry and its owner
    instead of just "still present" — #2062: a root-owned __pycache__ left the
    daemon (running as `tony`) unable to explain the retry without someone
    grepping the host by hand."""
    target = tmp_path / "victim"
    stuck = target / "stuck"
    stuck.mkdir(parents=True)

    class _ForeignStat:
        st_uid = 0

    with patch("server.sessions.orchestrator.shutil.rmtree", lambda *a, **k: None), \
         patch("server.sessions.orchestrator.os.getuid", return_value=1000), \
         patch("server.sessions.orchestrator.os.stat", return_value=_ForeignStat()):
        err = _rmtree_best_effort(str(target))

    assert err is not None
    assert "path still present after rmtree" in err
    assert str(stuck) in err
    assert "uid 0" in err
    assert "uid 1000" in err


def test_rmtree_best_effort_still_present_without_foreign_owner(tmp_path):
    """If the path lingers but every surviving entry is owned by us, don't
    fabricate an ownership explanation — fall back to the plain message."""
    target = tmp_path / "victim"
    (target / "stuck").mkdir(parents=True)

    with patch("server.sessions.orchestrator.shutil.rmtree", lambda *a, **k: None):
        err = _rmtree_best_effort(str(target))

    assert err == f"path still present after rmtree: {target}"


# ---------------------------------------------------------------------------
# #825 defect 2: poison-pill handling — one wedged session must not disable the
# purge for every other archived session.
# ---------------------------------------------------------------------------


def test_purge_skips_poison_after_strikes_and_reclaims_others(tmp_path):
    """A session whose permanent_delete keeps failing is skipped after
    _ARCHIVE_PURGE_MAX_STRIKES strikes, so a healthy older-but-not-oldest session is
    still reclaimed instead of the cap being wedged forever (#825)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            # "poison" is the oldest, so it is selected first every sweep.
            await _seed([
                _archived(0, archived_at=_BASE + timedelta(hours=0)),  # poison (oldest)
                _archived(1, archived_at=_BASE + timedelta(hours=1)),  # healthy
            ])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())

            async def fake_permanent_delete(sid: str) -> bool:
                if sid == "arch0":
                    raise OSError(errno.ESTALE, "Stale file handle")
                # Healthy sessions delete cleanly (drop the row like the real path).
                async with db_session() as db:
                    from sqlalchemy import delete as sa_delete
                    await db.execute(sa_delete(DbSession).where(DbSession.id == sid))
                return True

            orch.permanent_delete = fake_permanent_delete  # type: ignore[assignment]

            # Purge one oldest each sweep. The first _ARCHIVE_PURGE_MAX_STRIKES sweeps
            # keep hitting the poison and reclaim nothing.
            for _ in range(_ARCHIVE_PURGE_MAX_STRIKES):
                assert await orch.purge_oldest_archived(1) == []

            # The next sweep must skip the now-poisoned oldest and reclaim the healthy one.
            reclaimed = await orch.purge_oldest_archived(1)
            return reclaimed, await _archived_ids(), sorted(orch._archive_purge_poisoned)

    reclaimed, remaining, poisoned = asyncio.run(run())
    assert reclaimed == ["arch1"]         # healthy session finally reclaimed
    assert remaining == ["arch0"]         # poison left in place, not deleted
    assert poisoned == ["arch0"]          # and recorded as skipped/visible


def test_purge_keeps_row_when_archive_rmtree_fails_then_poisons(tmp_path):
    """A legacy archived session whose archive dir can't be removed (simulated stale
    sshfs handle) must NOT have its DB row deleted — deleting it would strand the dir
    on disk. The row is kept and retried, and poisoned after the strike threshold so it
    stops monopolising selection (#825). Exercises the real permanent_delete path."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            arch_root = tmp_path / "arch"
            (arch_root / "arch0").mkdir(parents=True)  # the (undeletable) archive dir
            await _seed([dict(
                id="arch0", name="A0", account_name="acc1", engine_session_id=None,
                status="archived", cwd="/old/0", archive_root=str(arch_root),
                archived_at=_BASE,
            )])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())

            # Simulate a stale handle: rmtree "runs" but reports it couldn't finish.
            with patch("server.sessions.orchestrator._rmtree_best_effort",
                       return_value="116: Stale file handle"):
                results = [await orch.purge_oldest_archived(1)
                           for _ in range(_ARCHIVE_PURGE_MAX_STRIKES)]
            return results, await _archived_ids(), sorted(orch._archive_purge_poisoned)

    results, remaining, poisoned = asyncio.run(run())
    assert all(r == [] for r in results)   # nothing reclaimed while wedged
    assert remaining == ["arch0"]          # row KEPT, dir not stranded by a phantom delete
    assert poisoned == ["arch0"]           # poisoned after the strikes


def test_purge_success_clears_prior_strikes(tmp_path):
    """A transient failure that later succeeds must not leave the session marked:
    strikes reset on a clean delete so a flaky mount doesn't permanently poison a row."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def run():
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([_archived(0, archived_at=_BASE)])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())

            calls = {"n": 0}

            async def flaky_permanent_delete(sid: str) -> bool:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError(errno.ESTALE, "Stale file handle")
                async with db_session() as db:
                    from sqlalchemy import delete as sa_delete
                    await db.execute(sa_delete(DbSession).where(DbSession.id == sid))
                return True

            orch.permanent_delete = flaky_permanent_delete  # type: ignore[assignment]

            await orch.purge_oldest_archived(1)   # strike 1
            reclaimed = await orch.purge_oldest_archived(1)  # succeeds
            return reclaimed, orch._archive_purge_strikes

    reclaimed, strikes = asyncio.run(run())
    assert reclaimed == ["arch0"]
    assert strikes.get("arch0", 0) == 0   # cleared on success
