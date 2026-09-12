"""Journaled removal of one blocked-by edge (roadmap spec §8/§10, #1472).

Every GitHub write goes through a fake ``mutate`` seam — nothing here may reach
GitHub. What is pinned: a journal row BEFORE the call, marked applied or failed
after it, the local dep rows deactivated only on success, and the sync request.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import GhWriteJournal, Project, ProjectRepo, WorkItem, WorkItemDep

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db_env():
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite+aiosqlite:///{tmp}/t.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": url, "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            yield url


async def _seed(db_url: str, *, blocker_node: str | None = "N3") -> str:
    """Returns the blocked item's id. #5 is blocked by #3, natively AND in prose."""
    await init_db(db_url)
    async with db_session() as db:
        p = Project(slug="ccswitch", name="ccswitch", onboarding_state="live")
        db.add(p)
        await db.flush()
        db.add(ProjectRepo(project_id=p.id, repo="o/r", is_primary=True))
        src = WorkItem(repo="o/r", number=5, kind="issue", title="Blocked", state="open",
                       gh_node_id="N5")
        dst = WorkItem(repo="o/r", number=3, kind="issue", title="Blocker", state="open",
                       gh_node_id=blocker_node)
        db.add_all([src, dst])
        await db.flush()
        for source in ("native", "prose"):
            db.add(WorkItemDep(src_ref="o/r#5", dst_ref="o/r#3", source=source, active=True,
                               src_work_item_id=src.id, dst_work_item_id=dst.id))
        return src.id


def _mutate(payload: dict, calls: list[tuple[str, str]]):
    async def mutate(owner: str, doc: str) -> dict:
        calls.append((owner, doc))
        return payload
    return mutate


async def _run(db_url: str, src_id: str, mutate):
    from server.pm.dep_remove import remove
    async with db_session() as db:
        src = (await db.execute(select(WorkItem).where(WorkItem.id == src_id))).scalar_one()
        result = await remove(db, src=src, blocker_ref="o/r#3", mutate=mutate, now=NOW)
        await db.commit()
        return result


@pytest.mark.asyncio
async def test_success_journals_applied_deactivates_every_row_and_requests_sync(db_env):
    src_id = await _seed(db_env)
    calls: list[tuple[str, str]] = []
    result = await _run(db_env, src_id, _mutate({"data": {"m0": {"clientMutationId": None}}}, calls))
    assert result == {"ok": True, "src": "o/r#5", "blocker": "o/r#3"}
    assert calls == [("o", 'mutation { m0: removeBlockedBy(input: {issueId: "N5", '
                            'blockingIssueId: "N3"}) { clientMutationId } }')]
    async with db_session() as db:
        rows = (await db.execute(select(GhWriteJournal))).scalars().all()
        assert [(r.field, r.applied, r.error) for r in rows] == [("remove_blocked_by", True, None)]
        deps = (await db.execute(select(WorkItemDep))).scalars().all()
        assert {d.source: d.active for d in deps} == {"native": False, "prose": False}
        repo = (await db.execute(select(ProjectRepo))).scalar_one()
        assert repo.sync_requested_at is not None


@pytest.mark.asyncio
async def test_github_refusal_marks_the_row_failed_and_keeps_the_deps(db_env):
    src_id = await _seed(db_env)
    payload = {"data": {"m0": None}, "errors": [{"path": ["m0"], "message": "no such edge"}]}
    result = await _run(db_env, src_id, _mutate(payload, []))
    assert result == {"ok": False, "step": "mutate", "error": "no such edge"}
    async with db_session() as db:
        row = (await db.execute(select(GhWriteJournal))).scalar_one()
        assert (row.applied, row.error) == (False, "no such edge")
        deps = (await db.execute(select(WorkItemDep))).scalars().all()
        assert all(d.active for d in deps)


@pytest.mark.asyncio
async def test_missing_node_id_is_refused_before_any_call(db_env):
    src_id = await _seed(db_env, blocker_node=None)
    calls: list[tuple[str, str]] = []
    result = await _run(db_env, src_id, _mutate({}, calls))
    assert result["ok"] is False and result["step"] == "node_id"
    assert calls == []
    async with db_session() as db:
        assert (await db.execute(select(GhWriteJournal))).scalars().all() == []


@pytest.mark.asyncio
async def test_unmirrored_blocker_is_refused(db_env):
    src_id = await _seed(db_env)
    from server.pm.dep_remove import remove
    async with db_session() as db:
        src = (await db.execute(select(WorkItem).where(WorkItem.id == src_id))).scalar_one()
        result = await remove(db, src=src, blocker_ref="o/r#999", mutate=_mutate({}, []), now=NOW)
    assert result["ok"] is False and result["step"] == "resolve"
