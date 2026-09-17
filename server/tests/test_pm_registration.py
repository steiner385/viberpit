"""Registration refuses existing canonical owners, including concurrent requests."""
import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.db.models import Base, Project, ProjectRepo
from server.tests.test_pm_api import IDENTITY_HEADERS, _client


@pytest.mark.parametrize("existing", [["first"], ["first", "legacy"]])
def test_registration_refuses_canonical_owner_without_partial_writes(monkeypatch, existing):
    async def identities(repos):
        return {repo: repo if repo == "other/free" else "current/repo" for repo in repos}

    monkeypatch.setattr("server.automations.target_identity.resolve_repositories", identities)
    with _client() as client:
        from server.db import db_session

        async def seed():
            async with db_session() as db:
                for slug in existing:
                    project = Project(slug=slug, name=slug)
                    db.add(project)
                    await db.flush()
                    db.add(ProjectRepo(project_id=project.id, repo=f"{slug}/old"))
        client.portal.call(seed)
        response = client.post("/api/pm/projects", headers=IDENTITY_HEADERS, json={
            "name": "second", "repos": [{"repo": "other/free"}, {"repo": "renamed/repo"}],
        })
        assert response.status_code == 409, response.text
        assert all(slug in response.json()["detail"]["message"] for slug in existing)
        projects = client.get("/api/pm/projects", headers=IDENTITY_HEADERS).json()
        assert {p["slug"] for p in projects} == set(existing)
        assert sum(len(p["repos"]) for p in projects) == len(existing)


@pytest.mark.parametrize("bad_identity", [None, "not-a-repo"])
def test_registration_unresolved_identity_creates_nothing(monkeypatch, bad_identity):
    async def identities(repos):
        return {repo: bad_identity for repo in repos}
    monkeypatch.setattr("server.automations.target_identity.resolve_repositories", identities)
    with _client() as client:
        response = client.post("/api/pm/projects", headers=IDENTITY_HEADERS, json={
            "name": "new", "repos": [{"repo": "owner/repo"}],
        })
        assert response.status_code == 409, response.text
        assert "unresolved" in response.json()["detail"]["message"]
        assert client.get("/api/pm/projects", headers=IDENTITY_HEADERS).json() == []


def test_registration_coalesces_aliases_and_preserves_primary(monkeypatch):
    async def identities(repos):
        return {repo: "new/repo" if repo in {"old/repo", "NEW/REPO"} else repo for repo in repos}
    monkeypatch.setattr("server.automations.target_identity.resolve_repositories", identities)
    with _client() as client:
        response = client.post("/api/pm/projects", headers=IDENTITY_HEADERS, json={
            "name": "new", "repos": [{"repo": "other/repo"}, {"repo": "old/repo"},
                                      {"repo": "NEW/REPO", "isPrimary": True}],
        })
        assert response.status_code == 201, response.text
        assert {(r["repo"], r["isPrimary"]) for r in response.json()["repos"]} == {
            ("other/repo", False), ("new/repo", True)}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_concurrent_registration_has_one_winner(backend, tmp_path, request, monkeypatch):
    """Two sessions finish preflight before either acquires the database writer lock."""
    from server.api.pm import ProjectCreateBody, create_project
    from server.pm import repo_project

    schema = "registration_" + uuid4().hex
    admin = None
    if backend == "postgresql":
        url = request.getfixturevalue("pg_url")
        admin = create_async_engine(url)
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        from server.db import _make_engine
        engine = _make_engine(f"sqlite+aiosqlite:///{tmp_path}/registration.db")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr("server.db._session_factory", factory)
        ready = asyncio.Barrier(2)
        request_sessions = []

        async def identities(repos):
            # Gathering must not hold the request's write transaction or lock.
            assert not any(db.in_transaction() for db in request_sessions)
            await ready.wait()
            return {repo: "owner/canonical" for repo in repos}

        monkeypatch.setattr(repo_project, "gather_repo_identities", identities)

        async def attempt(slug, alias):
            async with factory() as db:
                request_sessions.append(db)
                try:
                    result = await create_project(ProjectCreateBody(
                        name=slug, repos=[{"repo": alias}]), identity="test", db=db)
                    await db.commit()
                    return result
                except HTTPException as exc:
                    await db.rollback()
                    return exc
        results = await asyncio.gather(attempt("one", "old/name"), attempt("two", "NEW/NAME"))
        assert sum(isinstance(result, dict) for result in results) == 1, results
        failures = [result for result in results if isinstance(result, HTTPException)]
        assert len(failures) == 1 and failures[0].status_code == 409
        async with factory() as db:
            assert len((await db.scalars(select(Project))).all()) == 1
            assert len((await db.scalars(select(ProjectRepo))).all()) == 1
    finally:
        await engine.dispose()
        if admin:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


@pytest.mark.parametrize("invalid", ["inaccessible", "null_slug"])
def test_registration_refuses_unresolved_existing_registration(monkeypatch, invalid):
    async def identities(repos):
        return {repo: (None if invalid == "inaccessible" and repo == "old/repo"
                       else "canonical/repo") for repo in repos}
    monkeypatch.setattr("server.automations.target_identity.resolve_repositories", identities)
    with _client() as client:
        from server.db import db_session

        async def seed():
            async with db_session() as db:
                owner = Project(slug=None if invalid == "null_slug" else "legacy", name="Legacy")
                db.add(owner)
                await db.flush()
                db.add(ProjectRepo(project_id=owner.id, repo="old/repo"))
        client.portal.call(seed)
        response = client.post("/api/pm/projects", headers=IDENTITY_HEADERS, json={
            "name": "new", "repos": [{"repo": "canonical/repo"}],
        })
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["ownershipState"] == "unresolved"
        assert "old/repo" in response.json()["detail"]["message"]
        projects = client.get("/api/pm/projects", headers=IDENTITY_HEADERS).json()
        assert len(projects) == 1 and projects[0]["name"] == "Legacy"


@pytest.mark.asyncio
async def test_registration_lock_never_rolls_back_callers_transaction(tmp_path):
    from server.db import _make_engine
    from server.pm.repo_project import lock_repo_registration

    engine = _make_engine(f"sqlite+aiosqlite:///{tmp_path}/caller.db")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine)
        async with factory() as db:
            db.add(Project(slug="pending", name="Pending"))
            await db.flush()
            with pytest.raises(RuntimeError, match="fresh transaction"):
                await lock_repo_registration(db)
            assert (await db.scalars(select(Project.slug))).all() == ["pending"]
    finally:
        await engine.dispose()
