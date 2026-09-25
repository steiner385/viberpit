"""Persona layer in the planning spine (issue #2164): `Persona` /
`PersonaFeature` models and `Feature.persona_slugs`, mirroring the existing
Feature/FeatureLink/route_labels pattern in server/db/models.py."""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from server.db import db_session
from server.db.models import Feature, Persona, PersonaFeature, Project


@pytest.mark.asyncio
async def test_persona_unique_per_project_slug(isolated_db):
    async with db_session() as db:
        proj = Project(slug="p1", name="P1")
        db.add(proj)
        await db.flush()
        project_id = proj.id
        db.add(Persona(project_id=project_id, slug="teen", title="Teen"))
    with pytest.raises(IntegrityError):
        async with db_session() as db:
            db.add(Persona(project_id=project_id, slug="teen", title="Teen dup"))


@pytest.mark.asyncio
async def test_persona_feature_link_and_feature_persona_slugs_column(isolated_db):
    async with db_session() as db:
        proj = Project(slug="p2", name="P2")
        db.add(proj)
        await db.flush()
        persona = Persona(project_id=proj.id, slug="teen", title="Teen")
        feat = Feature(project_id=proj.id, slug="chores", title="Chores",
                       persona_slugs=["teen", "parent-admin"])
        db.add_all([persona, feat])
        await db.flush()
        db.add(PersonaFeature(persona_id=persona.id, feature_id=feat.id))
        await db.flush()
        got = (await db.get(Feature, feat.id))
        assert got.persona_slugs == ["teen", "parent-admin"]
