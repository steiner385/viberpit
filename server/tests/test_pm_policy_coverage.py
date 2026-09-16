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
from server.automations import product_admission
from server.automations import target_identity
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

    monkeypatch.setattr(product_admission, "db_session", sessions)
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

    monkeypatch.setattr(target_identity, "resolve_repositories", resolve)
    shared = AsyncMock(wraps=product_admission.resolve_repo_ownership)
    monkeypatch.setattr(product_admission, "resolve_repo_ownership", shared)
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
    monkeypatch.setattr(target_identity, "resolve_repositories", AsyncMock(return_value={
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
    monkeypatch.setattr(target_identity, "resolve_repositories", fetch)
    assert (await client.get("/api/pm/projects/alpha/policy/coverage")).json()["repositories"] == []
    fetch.assert_not_awaited()
    assert (await client.get("/api/pm/projects/missing/policy/coverage")).status_code == 404
    fetch.assert_not_awaited()
    app.dependency_overrides.clear()
    assert (await client.get("/api/pm/projects/alpha/policy/coverage")).status_code == 401
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_effective_preview_batches_identity_and_matches_dispatch(coverage_env, monkeypatch):
    from server.automations import config_store
    from server.automations.context import AutomationContext
    from server.automations.product_policy import decide_implementation

    client, factory, active, app = coverage_env
    await seed(factory, [("a", "alpha", ["owner/one", "owner/two"])])

    async def resolve(repos):
        assert not active
        # Policy changed while GitHub was in flight: the preview must read the new value.
        async with factory() as db:
            row = await db.get(Project, "a")
            row.automation_policy = {"dispatch": "propose"}
            await db.commit()
        return {repo: repo for repo in repos}

    fetch = AsyncMock(side_effect=resolve)
    monkeypatch.setattr(target_identity, "resolve_repositories", fetch)
    monkeypatch.setattr(config_store, "read_automation_config", AsyncMock(return_value=(
        "on", {"max_concurrent": 5, "max_spawns_per_day": 5})))
    monkeypatch.setattr(AutomationContext, "list_target_states", AsyncMock(return_value={}))
    monkeypatch.setattr(product_admission, "read_dispatch_accounting", AsyncMock(return_value={}))
    response = await client.get("/api/pm/projects/alpha/policy/effective")
    assert response.status_code == 200, response.text
    assert fetch.await_count == 1
    result = response.json()
    assert "not a future authorization token" in result["explanation"]
    assert len(result["repositories"]) == 2
    for repo in result["repositories"]:
        decisions = repo["decisions"]
        assert decisions["automaticImplementation"]["state"] == "refused"
        assert decisions["automaticProposal"]["state"] == "permitted"
        assert decisions["manualImplementation"]["state"] == "permitted"
        assert decisions["automaticImplementation"]["remainingChecks"]
    # Service used by execution composes the identical pure decision.
    snapshot = await product_admission.gather_implementation_snapshot(requested_product="alpha")
    decision, _ = snapshot.decide("owner/one", requested_product="alpha")
    preview = result["repositories"][0]["decisions"]["automaticImplementation"]
    assert [c.code for c in decision.constraints] == [c["code"] for c in preview["constraints"]]
    assert decide_implementation is product_admission.decide_implementation
    app.dependency_overrides.clear()
    assert (await client.get("/api/pm/projects/alpha/policy/effective")).status_code == 401


@pytest.mark.asyncio
async def test_registration_added_during_identity_lookup_is_unresolved(coverage_env, monkeypatch):
    client, factory, _, _ = coverage_env
    await seed(factory, [("a", "alpha", ["owner/one"])])

    async def resolve(repos):
        async with factory() as db:
            db.add(ProjectRepo(id="new", project_id="a", repo="owner/new"))
            await db.commit()
        return {repo: repo for repo in repos}

    monkeypatch.setattr(target_identity, "resolve_repositories", resolve)
    result = (await client.get("/api/pm/projects/alpha/policy/coverage")).json()
    assert len(result["repositories"]) == 2
    assert all(repo["state"] == "unresolved" for repo in result["repositories"])


@pytest.mark.asyncio
async def test_dispatch_accounting_uses_recent_durable_stamps_and_rejects_corruption(coverage_env):
    from datetime import timedelta, timezone
    from server.db.models import AutomationOperation

    _, factory, _, _ = coverage_env
    now = datetime.now(timezone.utc)
    async with factory() as db:
        for token, at, owner in (
            ("recent", now - timedelta(hours=1), "backlog-runner"),
            ("old", now - timedelta(days=2), "backlog-runner"),
            ("other", now, "pr-doctor"),
        ):
            db.add(AutomationOperation(token=token, owner=owner, source="automatic", reason="test",
                                       provenance={"runner_dispatch": {"accounted_at": at.isoformat()}}))
        await db.commit()
    assert set(await product_admission.read_dispatch_accounting(now=now)) == {"recent"}
    async with factory() as db:
        row = await db.get(AutomationOperation, "recent")
        row.provenance = {"runner_dispatch": {"accounted_at": "invalid"}}
        await db.commit()
    with pytest.raises(ValueError):
        await product_admission.read_dispatch_accounting(now=now)
