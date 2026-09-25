"""The persona-index DB-apply seam (issue #2164): upsert -> orphan -> revive
round-trips against a real DB, mirroring server/tests/test_db_persona_models.py's
fixture pattern (isolated_db sets up a fresh per-test DB; db_session() is the
actual async context manager, not isolated_db itself)."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from server.db import db_session
from server.db.models import Persona, Project
from server.pm.persona_index import PersonaIndexPlan, PersonaRowView, PersonaUpsert
from server.pm.persona_sync_seams import apply_persona_plan, list_persona_rows


@pytest.mark.asyncio
async def test_apply_upserts_then_orphans_then_revives(isolated_db):
    async with db_session() as db:
        proj = Project(slug="p1", name="P1")
        db.add(proj)
        await db.flush()
        project_id = proj.id

    plan = PersonaIndexPlan(
        upserts=(PersonaUpsert(slug="teen", title="Teen", role="teen", age_band="13-17",
                               tier="standard", summary="s", repo_path="docs/product/personas/teen.md"),),
        orphan_slugs=(), revive_slugs=())
    await apply_persona_plan(project_id, plan)
    rows = await list_persona_rows(project_id)
    assert rows == [PersonaRowView(slug="teen", orphaned=False)]

    orphan_plan = PersonaIndexPlan(upserts=(), orphan_slugs=("teen",), revive_slugs=())
    await apply_persona_plan(project_id, orphan_plan)
    rows = await list_persona_rows(project_id)
    assert rows == [PersonaRowView(slug="teen", orphaned=True)]

    revive_plan = PersonaIndexPlan(
        upserts=(PersonaUpsert(slug="teen", title="Teen", role="teen", age_band="13-17",
                               tier="standard", summary="s2", repo_path="docs/product/personas/teen.md"),),
        orphan_slugs=(), revive_slugs=("teen",))
    await apply_persona_plan(project_id, revive_plan)
    rows = await list_persona_rows(project_id)
    assert rows == [PersonaRowView(slug="teen", orphaned=False)]
    async with db_session() as db:
        p = (await db.execute(
            select(Persona).where(Persona.project_id == project_id)
        )).scalar_one()
        assert p.summary == "s2"
