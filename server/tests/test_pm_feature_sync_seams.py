"""DB-integration tests for apply_feature_plan's persona resolution (#2164):
Feature.persona_slugs is a raw mirror of the doc's declared personas;
PersonaFeature is the resolved graph, rewritten on every apply, restricted to
slugs that match a live (non-orphaned) Persona row in the SAME project.

Mirrors server/tests/test_pm_persona_sync_seams.py's fixture pattern:
isolated_db sets up a fresh per-test DB as a side effect (it is a sync
fixture, not an async context manager); db_session() is the actual async
context manager used for all DB work.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from server.db import db_session
from server.db.models import Feature, Persona, PersonaFeature, Project
from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
from server.pm.feature_sync_seams import apply_feature_plan


@pytest.mark.asyncio
async def test_apply_writes_persona_slugs_and_resolved_links(isolated_db):
    async with db_session() as db:
        proj = Project(slug="p1", name="P1")
        db.add(proj)
        await db.flush()
        project_id = proj.id
        persona = Persona(project_id=project_id, slug="teen", title="Teen")
        db.add(persona)
        await db.flush()

    plan = FeatureIndexPlan(
        upserts=(FeatureUpsert(slug="chores", title="Chores", state="live", summary="",
                               repo_path="docs/product/features/chores.md", parent_slug=None,
                               links=(), personas=("teen", "unknown-slug")),),
        orphan_slugs=(), revive_slugs=(), warnings=())
    await apply_feature_plan(project_id, plan)

    async with db_session() as db:
        feat = (await db.execute(
            select(Feature).where(Feature.project_id == project_id, Feature.slug == "chores")
        )).scalar_one()
        assert feat.persona_slugs == ["teen", "unknown-slug"]
        links = (await db.execute(
            select(PersonaFeature).where(PersonaFeature.feature_id == feat.id)
        )).scalars().all()
        assert len(links) == 1  # "unknown-slug" doesn't resolve to a live Persona row


@pytest.mark.asyncio
async def test_reapply_with_fewer_personas_drops_stale_link(isolated_db):
    async with db_session() as db:
        proj = Project(slug="p2", name="P2")
        db.add(proj)
        await db.flush()
        project_id = proj.id
        persona = Persona(project_id=project_id, slug="teen", title="Teen")
        db.add(persona)
        await db.flush()

    plan = FeatureIndexPlan(
        upserts=(FeatureUpsert(slug="chores", title="Chores", state="live", summary="",
                               repo_path="p", parent_slug=None, links=(), personas=("teen",)),),
        orphan_slugs=(), revive_slugs=(), warnings=())
    await apply_feature_plan(project_id, plan)

    reapplied = FeatureIndexPlan(
        upserts=(FeatureUpsert(slug="chores", title="Chores", state="live", summary="",
                               repo_path="p", parent_slug=None, links=(), personas=()),),
        orphan_slugs=(), revive_slugs=(), warnings=())
    await apply_feature_plan(project_id, reapplied)

    async with db_session() as db:
        feat = (await db.execute(
            select(Feature).where(Feature.project_id == project_id, Feature.slug == "chores")
        )).scalar_one()
        assert feat.persona_slugs is None
        links = (await db.execute(
            select(PersonaFeature).where(PersonaFeature.feature_id == feat.id)
        )).scalars().all()
        assert links == []


@pytest.mark.asyncio
async def test_orphaned_persona_row_does_not_get_a_link(isolated_db):
    """A Persona row that has itself been orphaned (its doc vanished) must not
    receive a PersonaFeature link even if a feature doc still declares its
    slug — same "declared but unresolved" treatment as an unknown slug."""
    async with db_session() as db:
        proj = Project(slug="p3", name="P3")
        db.add(proj)
        await db.flush()
        project_id = proj.id
        persona = Persona(project_id=project_id, slug="teen", title="Teen", orphaned=True)
        db.add(persona)
        await db.flush()

    plan = FeatureIndexPlan(
        upserts=(FeatureUpsert(slug="chores", title="Chores", state="live", summary="",
                               repo_path="p", parent_slug=None, links=(), personas=("teen",)),),
        orphan_slugs=(), revive_slugs=(), warnings=())
    await apply_feature_plan(project_id, plan)

    async with db_session() as db:
        feat = (await db.execute(
            select(Feature).where(Feature.project_id == project_id, Feature.slug == "chores")
        )).scalar_one()
        assert feat.persona_slugs == ["teen"]
        links = (await db.execute(
            select(PersonaFeature).where(PersonaFeature.feature_id == feat.id)
        )).scalars().all()
        assert links == []


@pytest.mark.asyncio
async def test_persona_link_scoped_to_same_project_only(isolated_db):
    """A slug matching a Persona row in a DIFFERENT project must not resolve —
    unlike FeatureLink, personas are not cross-project (per the brief)."""
    async with db_session() as db:
        proj_a = Project(slug="pa", name="PA")
        proj_b = Project(slug="pb", name="PB")
        db.add_all([proj_a, proj_b])
        await db.flush()
        project_a_id, project_b_id = proj_a.id, proj_b.id
        # Persona "teen" only exists in project B.
        db.add(Persona(project_id=project_b_id, slug="teen", title="Teen"))
        await db.flush()

    plan = FeatureIndexPlan(
        upserts=(FeatureUpsert(slug="chores", title="Chores", state="live", summary="",
                               repo_path="p", parent_slug=None, links=(), personas=("teen",)),),
        orphan_slugs=(), revive_slugs=(), warnings=())
    await apply_feature_plan(project_a_id, plan)

    async with db_session() as db:
        feat = (await db.execute(
            select(Feature).where(Feature.project_id == project_a_id, Feature.slug == "chores")
        )).scalar_one()
        assert feat.persona_slugs == ["teen"]
        links = (await db.execute(
            select(PersonaFeature).where(PersonaFeature.feature_id == feat.id)
        )).scalars().all()
        assert links == []
