"""Manual unarchive pins a sticky auto-archive opt-out, and reversing an AUTOMATED
archive records a learning heuristic the decider later reads.

Behavior under test (see SessionOrchestrator.unarchive / archive_session /
_autopilot_reversal_hint / _autopilot_get_context):
  - unarchive() sets auto_archive_opt_out_reason='manual-unarchive' (DB + record).
  - reversing an autopilot/backlog-runner archive emits an `archive_reversed` event
    carrying the captured decision features; reversing a plain user archive does not.
  - _autopilot_reversal_hint summarizes those events (repo-scoped) into a caution string.
  - _autopilot_get_context stamps no_auto_archive + reversal_hint onto the DecisionContext.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from sqlalchemy import select as sa_select

from server.db import db_session, init_db
from server.db.models import AutomationEvent
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _seed(rows: list[dict]) -> None:
    async with db_session() as db:
        for r in rows:
            db.add(DbSession(**r))


def _archived_row(**over) -> dict:
    base = dict(id="s1", name="S", account_name="a", engine_session_id=None,
                status="archived", cwd="/tmp/does-not-matter")
    base.update(over)
    # Archived rows resolve their directory through archive_root, so seed one and
    # materialize it: unarchive then performs a REAL restore. Before archive_root
    # existed these tests passed only because a missing archive silently produced an
    # empty cwd — the very bug this column removes.
    base.setdefault("archive_root", str(Path(str(base["cwd"])).parent / "archive"))
    Path(str(base["archive_root"]), str(base["id"])).mkdir(parents=True, exist_ok=True)
    return base


def test_unarchive_sets_sticky_opt_out(tmp_path):
    """Unarchiving pins auto_archive_opt_out_reason on both the DB row and the record."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'db.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([_archived_row(cwd=str(tmp_path / "cwd"))])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            record = await orch.unarchive("s1")
            async with db_session() as db:
                row = (await db.execute(
                    sa_select(DbSession).where(DbSession.id == "s1"))).scalars().first()
            return record, row.auto_archive_opt_out_reason

    record, db_reason = asyncio.run(run())
    assert record.auto_archive_opt_out_reason == "manual-unarchive"
    assert db_reason == "manual-unarchive"


def test_reversing_auto_archive_records_heuristic(tmp_path):
    """Unarchiving a session that AUTOPILOT archived emits an `archive_reversed` event
    carrying the captured decision features."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'db.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([_archived_row(
                cwd=str(tmp_path / "cwd"), repo="o/r", archived_by="autopilot",
                archive_context={"repo": "o/r", "goal": "ship feature X",
                                 "goal_source": "explicit", "reason": "autopilot: goal met"})])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            await orch.unarchive("s1")
            async with db_session() as db:
                events = (await db.execute(sa_select(AutomationEvent).where(
                    AutomationEvent.kind == "archive_reversed"))).scalars().all()
                row = (await db.execute(
                    sa_select(DbSession).where(DbSession.id == "s1"))).scalars().first()
            return events, row.archived_by, row.archive_context

    events, archived_by, archive_context = asyncio.run(run())
    assert len(events) == 1
    ev = events[0]
    assert ev.automation_id == "autopilot"
    assert ev.target == "o/r"
    assert ev.detail["goal"] == "ship feature X"
    assert ev.detail["archived_by"] == "autopilot"
    # Provenance is consumed (cleared) once the reversal is recorded.
    assert archived_by is None
    assert archive_context is None


def test_reversing_user_archive_records_no_heuristic(tmp_path):
    """A plain user archive that a user unarchives is NOT a learning signal — no event."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'db.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([_archived_row(cwd=str(tmp_path / "cwd"), archived_by="user")])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            await orch.unarchive("s1")
            async with db_session() as db:
                events = (await db.execute(sa_select(AutomationEvent).where(
                    AutomationEvent.kind == "archive_reversed"))).scalars().all()
            return events

    assert asyncio.run(run()) == []


def test_reversal_hint_summarizes_repo_scoped_events(tmp_path):
    """_autopilot_reversal_hint returns a caution mentioning the repo + count, or None."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'db.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            # Two reversals in o/r, one in other/repo.
            for goal in ("goal A", "goal B"):
                await orch._autopilot_ctx.emit(
                    "info", "reversed", kind="archive_reversed", target="o/r",
                    detail={"repo": "o/r", "goal": goal})
            await orch._autopilot_ctx.emit(
                "info", "reversed", kind="archive_reversed", target="other/repo",
                detail={"repo": "other/repo", "goal": "goal C"})
            return (await orch._autopilot_reversal_hint("o/r"),
                    await orch._autopilot_reversal_hint("clean/repo"))

    hint_dirty, hint_clean = asyncio.run(run())
    assert hint_dirty is not None
    assert "o/r" in hint_dirty
    assert "2" in hint_dirty            # both o/r reversals counted, other/repo excluded
    assert hint_clean is None           # no reversals in this repo → no caution


def test_get_context_stamps_opt_out_and_hint(tmp_path):
    """The single context-assembly seam injects no_auto_archive + reversal_hint so the
    pure loop/decider never touch session state directly."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'db.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            from server.sessions.engine import AgentEngine
            from server.sessions.types import SessionRecord
            rec = SessionRecord(id="s1", name="S", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="",
                                cols=80, rows=24, cwd="/tmp", repo="o/r", status="idle")
            rec.auto_archive_opt_out_reason = "manual-unarchive"
            orch._records["s1"] = rec
            await orch._autopilot_ctx.emit(
                "info", "reversed", kind="archive_reversed", target="o/r",
                detail={"repo": "o/r", "goal": "goal A"})
            return await orch._autopilot_get_context("s1")

    ctx = asyncio.run(run())
    assert ctx.no_auto_archive is True
    assert ctx.reversal_hint is not None and "o/r" in ctx.reversal_hint
