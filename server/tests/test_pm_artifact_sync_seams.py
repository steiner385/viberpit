"""Artifact-frontmatter DB seam tests (issue #2161): listing committed
artifacts and applying a parsed frontmatter batch back onto
Initiative/InitiativeArtifact/InitiativeFeature — "file wins" reconciliation."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from server.db import db_session
from server.db.models import Feature, Initiative, InitiativeArtifact, InitiativeFeature, Project
from server.pm.artifact_sync_seams import (
    ArtifactFrontmatterResult, apply_artifact_frontmatter, list_committed_artifacts,
)


async def _make_project(repo="o/r"):
    async with db_session() as db:
        p = Project(slug="example", name="Example")
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id


async def _make_initiative(project_id, repo="o/r", slug="my-init", **over):
    async with db_session() as db:
        ini = Initiative(project_id=project_id, repo=repo, slug=slug, **over)
        db.add(ini)
        await db.flush()
        await db.commit()
        return ini.id


async def _make_artifact(initiative_id, *, kind="spec", repo_path="docs/sdlc/my-init/spec.md",
                         commit_sha="abc123"):
    async with db_session() as db:
        art = InitiativeArtifact(initiative_id=initiative_id, kind=kind,
                                 repo_path=repo_path, commit_sha=commit_sha)
        db.add(art)
        await db.flush()
        await db.commit()
        return art.id


async def _make_feature(project_id, slug):
    async with db_session() as db:
        f = Feature(project_id=project_id, slug=slug, title=slug, state="live")
        db.add(f)
        await db.flush()
        await db.commit()
        return f.id


@pytest.mark.asyncio
async def test_list_committed_artifacts_excludes_uncommitted(isolated_db):
    project_id = await _make_project()
    ini_id = await _make_initiative(project_id)
    await _make_artifact(ini_id)
    async with db_session() as db:
        db.add(InitiativeArtifact(initiative_id=ini_id, kind="plan", repo_path=None, commit_sha=None))
        await db.commit()

    rows = await list_committed_artifacts(project_id, "o/r")
    assert len(rows) == 1
    assert rows[0].kind == "spec"
    assert rows[0].initiative_slug == "my-init"


@pytest.mark.asyncio
async def test_list_committed_artifacts_scoped_to_repo(isolated_db):
    project_id = await _make_project()
    ini_id = await _make_initiative(project_id, repo="o/other")
    await _make_artifact(ini_id)

    rows = await list_committed_artifacts(project_id, "o/r")
    assert rows == []


@pytest.mark.asyncio
async def test_apply_stamps_the_artifact_row(isolated_db):
    project_id = await _make_project()
    ini_id = await _make_initiative(project_id)
    art_id = await _make_artifact(ini_id)

    await apply_artifact_frontmatter(project_id, [
        ArtifactFrontmatterResult(artifact_id=art_id, initiative_id=ini_id,
                                  frontmatter_present=True, epic=None, capabilities=())])

    async with db_session() as db:
        row = (await db.execute(select(InitiativeArtifact).where(
            InitiativeArtifact.id == art_id))).scalar_one()
    assert row.frontmatter_present is True
    assert row.frontmatter_epic is None
    assert row.frontmatter_capabilities is None


@pytest.mark.asyncio
async def test_apply_overwrites_initiative_epic_when_stated(isolated_db):
    project_id = await _make_project()
    ini_id = await _make_initiative(project_id, epic_repo="o/r", epic_number=1)
    art_id = await _make_artifact(ini_id)

    await apply_artifact_frontmatter(project_id, [
        ArtifactFrontmatterResult(artifact_id=art_id, initiative_id=ini_id,
                                  frontmatter_present=True, epic="o/r#42", capabilities=())])

    async with db_session() as db:
        ini = (await db.execute(select(Initiative).where(Initiative.id == ini_id))).scalar_one()
    assert ini.epic_repo == "o/r"
    assert ini.epic_number == 42


@pytest.mark.asyncio
async def test_apply_leaves_epic_alone_when_unstated(isolated_db):
    project_id = await _make_project()
    ini_id = await _make_initiative(project_id, epic_repo="o/r", epic_number=1)
    art_id = await _make_artifact(ini_id)

    await apply_artifact_frontmatter(project_id, [
        ArtifactFrontmatterResult(artifact_id=art_id, initiative_id=ini_id,
                                  frontmatter_present=True, epic=None, capabilities=())])

    async with db_session() as db:
        ini = (await db.execute(select(Initiative).where(Initiative.id == ini_id))).scalar_one()
    assert ini.epic_repo == "o/r"
    assert ini.epic_number == 1


@pytest.mark.asyncio
async def test_apply_rewrites_capability_links_file_wins(isolated_db):
    project_id = await _make_project()
    ini_id = await _make_initiative(project_id)
    art_id = await _make_artifact(ini_id)
    old_feature_id = await _make_feature(project_id, "old-cap")
    new_feature_id = await _make_feature(project_id, "new-cap")
    async with db_session() as db:
        db.add(InitiativeFeature(initiative_id=ini_id, feature_id=old_feature_id, is_primary=True))
        await db.commit()

    await apply_artifact_frontmatter(project_id, [
        ArtifactFrontmatterResult(artifact_id=art_id, initiative_id=ini_id,
                                  frontmatter_present=True, epic=None,
                                  capabilities=("new-cap",))])

    async with db_session() as db:
        links = (await db.execute(select(InitiativeFeature).where(
            InitiativeFeature.initiative_id == ini_id))).scalars().all()
    assert {link.feature_id for link in links} == {new_feature_id}


@pytest.mark.asyncio
async def test_apply_skips_unresolvable_capability_slug_without_raising(isolated_db):
    project_id = await _make_project()
    ini_id = await _make_initiative(project_id)
    art_id = await _make_artifact(ini_id)

    await apply_artifact_frontmatter(project_id, [
        ArtifactFrontmatterResult(artifact_id=art_id, initiative_id=ini_id,
                                  frontmatter_present=True, epic=None,
                                  capabilities=("no-such-capability",))])

    async with db_session() as db:
        links = (await db.execute(select(InitiativeFeature).where(
            InitiativeFeature.initiative_id == ini_id))).scalars().all()
    assert links == []
