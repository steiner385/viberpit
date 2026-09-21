"""`_pm_sync_save_unindexed_docs` persists pm-sync's skipped-doc list onto the
project row so `roadmap_health` can surface it (issue #2166)."""
from __future__ import annotations

import pytest


def _orch():
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    return SessionOrchestrator(sdk_factory=fake_sdk_factory())


@pytest.mark.asyncio
async def test_save_unindexed_docs_persists_structured_pairs(isolated_db):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Project

    orch = _orch()
    async with db_session() as db:
        p = Project(slug="example", name="Example")
        db.add(p)
        await db.flush()
        await db.commit()
        project_id = p.id

    await orch._pm_sync_save_unindexed_docs(
        project_id, [("docs/product/features/bad.md", "unterminated frontmatter fence")])

    async with db_session() as db:
        row = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one()
    assert row.unindexed_docs == [
        {"path": "docs/product/features/bad.md", "error": "unterminated frontmatter fence"}]


@pytest.mark.asyncio
async def test_save_unindexed_docs_with_no_pairs_clears_the_column(isolated_db):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Project

    orch = _orch()
    async with db_session() as db:
        p = Project(slug="example", name="Example",
                   unindexed_docs=[{"path": "a.md", "error": "x"}])
        db.add(p)
        await db.flush()
        await db.commit()
        project_id = p.id

    await orch._pm_sync_save_unindexed_docs(project_id, [])

    async with db_session() as db:
        row = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one()
    assert row.unindexed_docs is None
