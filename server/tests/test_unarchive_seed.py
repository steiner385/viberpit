"""Unarchiving an AUTOMATICALLY archived session seeds the restored session with a
"why you're back" prompt, so that when the user next opens it the agent knows its
archival was likely premature and re-checks state before continuing.

Behavior under test (see SessionOrchestrator._compose_unarchive_seed / unarchive):
  - AUTO archive (autopilot / backlog-runner): unarchive stages the seed in
    _pending_initial_prompt (lazy delivery on next attach) AND persists it to the
    initial_prompt column (durable across a daemon restart).
  - user/manual archive: no seed — nothing staged, initial_prompt stays NULL.
  - the seed text carries the provenance, the archive reason, and the goal.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from sqlalchemy import select as sa_select

from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _seed(rows: list[dict]) -> None:
    async with db_session() as db:
        for r in rows:
            db.add(DbSession(**r))


def _archived_row(**over) -> dict:
    base = dict(id="s1", name="S", account_name="a", claude_session_id="c1",
                status="archived", cwd="/tmp/does-not-matter")
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# _compose_unarchive_seed — pure helper
# ---------------------------------------------------------------------------


def test_compose_seed_auto_archive_carries_provenance_reason_and_goal():
    seed = SessionOrchestrator._compose_unarchive_seed(
        "autopilot",
        {"reason": "autopilot: goal met", "goal": "ship feature X"},
        datetime(2026, 8, 15, 9, 30),
        "ship feature X",
    )
    assert seed is not None
    assert "automatically archived on 2026-08-15 by autopilot" in seed
    assert "autopilot: goal met" in seed          # the captured reason
    assert "ship feature X" in seed               # the goal
    assert "premature" in seed                    # the "was this wrong" framing


def test_compose_seed_backlog_runner_names_the_runner():
    seed = SessionOrchestrator._compose_unarchive_seed(
        "backlog-runner", {}, None, None)
    assert seed is not None
    assert "the backlog runner" in seed
    # No archived_at → no date clause; no reason → the fallback premise.
    assert "on the assumption its work was already complete" in seed


def test_compose_seed_returns_none_for_user_archive():
    assert SessionOrchestrator._compose_unarchive_seed(
        "user", {"goal": "g"}, datetime(2026, 8, 15), "g") is None


def test_compose_seed_returns_none_for_no_provenance():
    assert SessionOrchestrator._compose_unarchive_seed(None, None, None, "g") is None


# ---------------------------------------------------------------------------
# unarchive() — staging + persistence
# ---------------------------------------------------------------------------


def test_unarchive_auto_stages_and_persists_seed(tmp_path):
    """An autopilot-archived session, on unarchive, stages the seed for next-attach
    delivery and writes it to the durable initial_prompt column."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'db.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([_archived_row(
                cwd=str(tmp_path / "cwd"), repo="o/r", archived_by="autopilot",
                active_goal="ship feature X",
                archive_context={"repo": "o/r", "goal": "ship feature X",
                                 "reason": "autopilot: goal met"})])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            await orch.unarchive("s1")
            staged = orch.pop_pending_initial_prompt("s1")
            async with db_session() as db:
                row = (await db.execute(
                    sa_select(DbSession).where(DbSession.id == "s1"))).scalars().first()
            return staged, row.initial_prompt

    staged, persisted = asyncio.run(run())
    assert staged is not None
    assert "ship feature X" in staged
    assert "autopilot" in staged
    # The durable copy matches what was staged in memory, byte for byte.
    assert persisted == staged


def test_unarchive_user_archive_seeds_nothing(tmp_path):
    """A plain user archive gets no seed: nothing staged, initial_prompt stays NULL."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'db.db'}"

    async def run():
        with patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([_archived_row(cwd=str(tmp_path / "cwd"), archived_by="user")])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            await orch.unarchive("s1")
            staged = orch.pop_pending_initial_prompt("s1")
            async with db_session() as db:
                row = (await db.execute(
                    sa_select(DbSession).where(DbSession.id == "s1"))).scalars().first()
            return staged, row.initial_prompt

    staged, persisted = asyncio.run(run())
    assert staged is None
    assert persisted is None
