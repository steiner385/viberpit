"""product_repos: the single source of truth for a product's live (non-retired) repos."""
import asyncio

import pytest

from server.db import db_session
from server.db.clock import db_utc_now
from server.db.models import Project, ProjectRepo
from server.pm import product_repos
from server.pm.repo_project import primary_repo_for_slug


@pytest.fixture(autouse=True)
def _fresh_retired_cache():
    product_repos.invalidate_retired_cache()
    yield
    product_repos.invalidate_retired_cache()


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


def test_is_retired_unprimed_cache_refills_itself(isolated_db):
    async def go():
        await _seed()
        product_repos.invalidate_retired_cache()
        first = await product_repos.is_retired("O/Old")  # no manual priming
        return first, await product_repos.is_retired("o/new")
    assert asyncio.run(go()) == (True, False)


def test_is_retired_cache_invalidated(isolated_db):
    async def go():
        pid = await _seed()
        product_repos.invalidate_retired_cache()
        assert await product_repos.is_retired("o/old") is True
        async with db_session() as db:  # unretire, then the stale snapshot is dropped
            row = (await db.execute(
                product_repos.select(ProjectRepo).where(ProjectRepo.repo == "o/old"))).scalar_one()
            row.retired_at = None
            row.is_primary = False
            await db.commit()
        assert await product_repos.is_retired("o/old") is True  # still inside TTL
        product_repos.invalidate_retired_cache()
        return pid, await product_repos.is_retired("o/old")
    assert asyncio.run(go())[1] is False


def test_is_retired_fails_open_on_db_error(monkeypatch):
    product_repos.invalidate_retired_cache()

    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr("server.db.db_session", boom)
    assert asyncio.run(product_repos.is_retired("o/old")) is False


def test_primary_repo_for_slug_skips_retired(isolated_db):
    async def go():
        await _seed()
        async with db_session() as db:
            return await primary_repo_for_slug(db, "lifecycle")
    assert asyncio.run(go()) == "o/new"
