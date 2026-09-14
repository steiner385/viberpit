"""Read-only coverage uses canonical evidence and the shared ownership resolver."""
from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.api import pm
from server.auth.middleware import current_identity
from server.db.models import Base, Project, ProjectRepo


@pytest_asyncio.fixture
async def coverage_env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/coverage.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    active = []

    @asynccontextmanager
    async def sessions():
        async with factory() as db:
            active.append(db)
            try:
                yield db
            finally:
                active.remove(db)

    monkeypatch.setattr(pm, "db_session", sessions)
    app = FastAPI()
    app.include_router(pm.router)
    app.dependency_overrides[current_identity] = lambda: "test@example.com"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, factory, active, app
    await engine.dispose()


async def seed(factory, specs):
    async with factory() as db:
        for pid, slug, repos in specs:
            db.add(Project(id=pid, slug=slug, name=pid))
            for i, repo in enumerate(repos):
                db.add(ProjectRepo(id=f"{pid}-{i}", project_id=pid, repo=repo))
        await db.commit()


@pytest.mark.asyncio
async def test_coverage_aliases_across_products_are_ambiguous_and_read_only(coverage_env, monkeypatch):
    client, factory, active, _ = coverage_env
    await seed(factory, [("a", "alpha", ["owner/old", "OWNER/CURRENT"]),
                         ("b", "beta", ["owner/current"])])

    async def resolve(repos):
        assert not active, "GitHub must run outside the database session"
        assert set(repos) == {"owner/old", "OWNER/CURRENT", "owner/current"}
        return {r: "owner/current" for r in repos}

    monkeypatch.setattr(pm, "resolve_repositories", resolve)
    shared = AsyncMock(wraps=pm.resolve_repo_ownership)
    monkeypatch.setattr(pm, "resolve_repo_ownership", shared)
    response = await client.get("/api/pm/projects/alpha/policy/coverage")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["scope"] == "repository-coverage"
    assert datetime.fromisoformat(data["observedAt"]).tzinfo is not None
    assert "not permission" in data["explanation"]
    assert len(data["repositories"]) == 2
    for repo in data["repositories"]:
        assert repo["state"] == "ambiguous"
        assert repo["canonicalRepo"] == "owner/current"
        assert repo["owner"] is None
        assert {p["slug"] for p in repo["candidates"]} == {"alpha", "beta"}
    assert shared.await_count == 1
    assert shared.await_args.kwargs["canonical_repos"]["owner/old"] == "owner/current"
    async with factory() as db:
        assert (await db.get(ProjectRepo, "a-0")).repo == "owner/old"
        assert (await db.get(Project, "a")).automation_policy is None


@pytest.mark.asyncio
@pytest.mark.parametrize("other_slug,other_identity", [(None, "owner/current"), ("beta", None)])
async def test_coverage_unresolved_registration_never_selects_owner(
    coverage_env, monkeypatch, other_slug, other_identity,
):
    client, factory, _, _ = coverage_env
    await seed(factory, [("a", "alpha", ["owner/current"]),
                         ("b", other_slug, ["owner/old"])])
    monkeypatch.setattr(pm, "resolve_repositories", AsyncMock(return_value={
        "owner/current": "owner/current", "owner/old": other_identity,
    }))
    data = (await client.get("/api/pm/projects/alpha/policy/coverage")).json()
    assert data["repositories"][0]["state"] == "unresolved"
    assert data["repositories"][0]["owner"] is None


@pytest.mark.asyncio
async def test_coverage_empty_unknown_project_and_auth(coverage_env, monkeypatch):
    client, factory, _, app = coverage_env
    await seed(factory, [("a", "alpha", [])])
    fetch = AsyncMock(return_value={})
    monkeypatch.setattr(pm, "resolve_repositories", fetch)
    assert (await client.get("/api/pm/projects/alpha/policy/coverage")).json()["repositories"] == []
    fetch.assert_not_awaited()
    assert (await client.get("/api/pm/projects/missing/policy/coverage")).status_code == 404
    fetch.assert_not_awaited()
    app.dependency_overrides.clear()
    assert (await client.get("/api/pm/projects/alpha/policy/coverage")).status_code == 401
    fetch.assert_not_awaited()
