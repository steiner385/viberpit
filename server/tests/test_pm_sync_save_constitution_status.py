"""`mirror_store.set_project_constitution_status` persists pm-sync's per-pass
governance status onto the project row (issue #2165)."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_set_constitution_status_persists(isolated_db):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Project
    from server.pm.mirror_store import set_project_constitution_status

    async with db_session() as db:
        p = Project(slug="example", name="Example")
        db.add(p)
        await db.flush()
        await db.commit()
        project_id = p.id

    async with db_session() as db:
        await db.run_sync(
            lambda s: set_project_constitution_status(s, project_id, "template"))
        await db.commit()

    async with db_session() as db:
        row = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one()
    assert row.constitution_status == "template"


@pytest.mark.asyncio
async def test_set_constitution_status_none_clears_a_previous_flag(isolated_db):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Project
    from server.pm.mirror_store import set_project_constitution_status

    async with db_session() as db:
        p = Project(slug="example", name="Example", constitution_status="missing")
        db.add(p)
        await db.flush()
        await db.commit()
        project_id = p.id

    async with db_session() as db:
        await db.run_sync(
            lambda s: set_project_constitution_status(s, project_id, None))
        await db.commit()

    async with db_session() as db:
        row = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one()
    assert row.constitution_status is None
