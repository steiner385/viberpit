"""product_repos: the single source of truth for a product's live (non-retired) repos."""
import asyncio

from server.db import db_session
from server.db.clock import db_utc_now
from server.db.models import Project, ProjectRepo
from server.pm import product_repos
from server.pm.repo_project import primary_repo_for_slug


async def _seed():
    async with db_session() as db:
        p = Project(slug="lifecycle", name="Lifecycle")
        db.add(p)
        await db.flush()
        db.add_all([
            ProjectRepo(project_id=p.id, repo="o/old", is_primary=True, retired_at=db_utc_now()),
            ProjectRepo(project_id=p.id, repo="o/side", is_primary=False),
            ProjectRepo(project_id=p.id, repo="o/new", is_primary=True),
        ])
        await db.commit()
        return p.id


def test_active_repos_excludes_retired_primary_first(isolated_db):
    async def go():
        pid = await _seed()
        async with db_session() as db:
            return await product_repos.active_repos(db, pid)
    assert asyncio.run(go()) == ["o/new", "o/side"]


def test_primary_repo_ignores_retired_row(isolated_db):
    async def go():
        pid = await _seed()
        async with db_session() as db:
            first = await product_repos.primary_repo(db, pid)
            row = (await db.execute(
                product_repos.select(ProjectRepo).where(ProjectRepo.repo == "o/new"))).scalar_one()
            row.retired_at = db_utc_now()
            await db.commit()
            return first, await product_repos.primary_repo(db, pid)
    assert asyncio.run(go()) == ("o/new", None)


def test_is_retired_cache_invalidated(isolated_db):
    product_repos.invalidate_retired_cache()
    assert product_repos.is_retired("o/old") is False  # unprimed: fail-open

    async def go():
        await _seed()
        async with db_session() as db:
            return await product_repos.retired_repo_set(db)
    assert asyncio.run(go()) == frozenset({"o/old"})
    assert product_repos.is_retired("O/Old") is True
    assert product_repos.is_retired("o/new") is False
    product_repos.invalidate_retired_cache()
    assert product_repos.is_retired("o/old") is False


def test_primary_repo_for_slug_skips_retired(isolated_db):
    async def go():
        await _seed()
        async with db_session() as db:
            return await primary_repo_for_slug(db, "lifecycle")
    assert asyncio.run(go()) == "o/new"
