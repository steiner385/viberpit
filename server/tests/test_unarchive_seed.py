"""Unarchiving a session seeds NO prompt: the user's own next message resumes it
exactly as if it had never been archived.

A former "why you're back" seed (for autopilot / backlog-runner / completion
archives) was staged for delivery on the next attach, so it ran AHEAD of the user's
own re-prompt and hijacked the turn. These tests pin its absence for every
provenance, and that the dispatch prompt in initial_prompt is left untouched.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

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
    base = dict(id="s1", name="S", account_name="a", engine_session_id="c1",
                status="archived", cwd="/tmp/does-not-matter")
    base.update(over)
    # Archived rows resolve their directory through archive_root, so seed one and
    # materialize it: unarchive then performs a REAL restore.
    base.setdefault("archive_root", str(Path(str(base["cwd"])).parent / "archive"))
    Path(str(base["archive_root"]), str(base["id"])).mkdir(parents=True, exist_ok=True)
    return base


@pytest.mark.parametrize("archived_by", ["autopilot", "backlog-runner", "completion", "user"])
def test_unarchive_stages_no_seed_and_keeps_dispatch_prompt(tmp_path, archived_by):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'db.db'}"

    async def run():
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
            await init_db(db_url=db_url)
            await _seed([_archived_row(
                cwd=str(tmp_path / "cwd"), repo="o/r", archived_by=archived_by,
                initial_prompt="original dispatch prompt",
                active_goal="ship feature X",
                archive_context={"repo": "o/r", "goal": "ship feature X",
                                 "reason": "goal met"})])
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            await orch.unarchive("s1")
            staged = orch.pop_pending_initial_prompt("s1")
            async with db_session() as db:
                row = (await db.execute(
                    sa_select(DbSession).where(DbSession.id == "s1"))).scalars().first()
            return staged, row.initial_prompt, row.status

    staged, persisted, status = asyncio.run(run())
    assert status == "idle"
    assert staged is None
    assert persisted == "original dispatch prompt"
