"""One batched repository ownership authority; ambiguous evidence never wins."""
from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

import pytest

from server.db import db_session, init_db
from server.db.models import Project, ProjectRepo
from server.pm.repo_project import RepoOwner, owning_projects_by_repo, resolve_repo_ownership

REPO = "steiner385/viberpit"
OTHER_REPO = "steiner385/kindash"


@pytest.fixture
def db_env():
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite+aiosqlite:///{tmp}/t.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": url, "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            yield url


async def _seed(db_url: str, pairs: list[tuple[str, str | None]]) -> dict[str, str]:
    """``(project_slug, repo)`` pairs, inserted IN ORDER. Returns slug → project id."""
    await init_db(db_url)
    ids: dict[str, str] = {}
    async with db_session() as db:
        for slug, repo in pairs:
            pid = ids.get(slug)
            if pid is None:
                p = Project(slug=slug, name=slug, onboarding_state="map_merged")
                db.add(p)
                await db.flush()
                ids[slug] = pid = p.id
            db.add(ProjectRepo(project_id=pid, repo=repo, is_primary=True))
    return ids


class _CountingExecute:
    """Wraps ``AsyncSession.execute`` to count round trips."""

    def __init__(self, db):
        self._orig = db.execute
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        return await self._orig(*args, **kwargs)


@pytest.mark.asyncio
async def test_single_owner_returns_id_and_slug(db_env):
    ids = await _seed(db_env, [("viberpit", REPO)])
    async with db_session() as db:
        owners = await owning_projects_by_repo(db, {REPO})
    assert owners == {REPO: RepoOwner(project_id=ids["viberpit"], slug="viberpit")}


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [
    [("alpha", REPO), ("zeta", REPO)],
    [("zeta", REPO), ("alpha", REPO)],
])
async def test_two_owners_refused_whatever_the_insertion_order(
        db_env, order):
    """Ownership ambiguity is independent of row or slug ordering."""
    await _seed(db_env, order)
    async with db_session() as db:
        owners = await owning_projects_by_repo(db, {REPO})
    assert owners == {}


@pytest.mark.asyncio
async def test_unowned_repo_is_absent_not_none_valued(db_env):
    """Absent, so ``.get()`` is ``None`` and every caller skips rather than
    guessing a project."""
    await _seed(db_env, [("viberpit", REPO)])
    async with db_session() as db:
        owners = await owning_projects_by_repo(db, {REPO, "someone/unknown"})
    assert set(owners) == {REPO}
    assert owners.get("someone/unknown") is None


@pytest.mark.asyncio
async def test_empty_and_null_repos_are_dropped_without_a_query(db_env):
    """``ProjectRepo.repo`` is nullable, and callers build the input set from
    ``WorkItem.repo`` — which is nullable too. Falsy repos never reach SQL."""
    ids = await _seed(db_env, [("viberpit", REPO), ("viberpit", None)])
    async with db_session() as db:
        counter = _CountingExecute(db)
        with patch.object(db, "execute", counter):
            assert await owning_projects_by_repo(db, set()) == {}
            assert await owning_projects_by_repo(db, {""}) == {}
            assert counter.calls == 0  # nothing to ask about → no round trip
            # The project's NULL-repo row is not an owner of anything.
            assert await owning_projects_by_repo(db, {"", REPO}) == {
                REPO: RepoOwner(project_id=ids["viberpit"], slug="viberpit")}
            assert counter.calls == 1


@pytest.mark.asyncio
async def test_batch_of_repos_is_one_query_not_n(db_env):
    ids = await _seed(db_env, [
        ("viberpit", REPO), ("kindash", OTHER_REPO), ("infra", "steiner385/infra")])
    async with db_session() as db:
        counter = _CountingExecute(db)
        with patch.object(db, "execute", counter):
            owners = await owning_projects_by_repo(
                db, {REPO, OTHER_REPO, "steiner385/infra", "someone/unknown"})
    assert counter.calls == 1
    assert owners == {
        REPO: RepoOwner(project_id=ids["viberpit"], slug="viberpit"),
        OTHER_REPO: RepoOwner(project_id=ids["kindash"], slug="kindash"),
        "steiner385/infra": RepoOwner(project_id=ids["infra"], slug="infra"),
    }


@pytest.mark.asyncio
async def test_verified_aliases_coalesce_within_product(db_env):
    alias = "steiner385/old-name"
    ids = await _seed(db_env, [("product", alias), ("product", REPO.upper())])
    async with db_session() as db:
        counter = _CountingExecute(db)
        with patch.object(db, "execute", counter):
            result = await resolve_repo_ownership(
                db, [REPO, alias.upper()], canonical_repos={alias: REPO, REPO: REPO})
    assert counter.calls == 1
    for resolution in result.values():
        assert resolution.state == "unique"
        assert resolution.canonical_repo == REPO
        assert resolution.owner == RepoOwner(ids["product"], "product")
        assert resolution.candidates == (resolution.owner,)


@pytest.mark.asyncio
async def test_verified_aliases_across_products_ambiguous(db_env):
    alias = "steiner385/old-name"
    ids = await _seed(db_env, [("alpha", alias), ("zeta", REPO)])
    async with db_session() as db:
        result = (await resolve_repo_ownership(
            db, [REPO], canonical_repos={alias: REPO, REPO: REPO}))[REPO]
    assert result.state == "ambiguous"
    assert result.owner is None
    assert {o.project_id for o in result.candidates} == set(ids.values())


@pytest.mark.asyncio
async def test_missing_requires_verified_request_and_coverage(db_env):
    await _seed(db_env, [("product", REPO)])
    async with db_session() as db:
        result = await resolve_repo_ownership(
            db, [OTHER_REPO], canonical_repos={REPO: REPO, OTHER_REPO: OTHER_REPO})
        unknown = await resolve_repo_ownership(db, [OTHER_REPO])
    assert result[OTHER_REPO].state == "missing"
    assert unknown[OTHER_REPO].state == "unresolved"
    assert unknown[OTHER_REPO].canonical_repo is None


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [None, "malformed", "/", "owner/..", "owner/repo/extra"])
async def test_unavailable_registration_blocks_unique_without_guessing_alias(db_env, identity):
    alias = "someone/old-name"
    await _seed(db_env, [("product", REPO), ("other", alias)])
    async with db_session() as db:
        result = (await resolve_repo_ownership(
            db, [REPO], canonical_repos={REPO: REPO, alias: identity}))[REPO]
    assert result.state == "unresolved"
    assert result.owner is None
    assert result.unresolved_repos == (alias,)
    assert len(result.candidates) == 1


@pytest.mark.asyncio
async def test_null_slug_is_unresolved_not_missing_or_unique(db_env):
    await _seed(db_env, [("product", REPO)])
    async with db_session() as db:
        p = Project(slug=None, name="Unnameable")
        db.add(p)
        await db.flush()
        db.add(ProjectRepo(project_id=p.id, repo=REPO))
    async with db_session() as db:
        result = (await resolve_repo_ownership(
            db, [REPO], canonical_repos={REPO: REPO}))[REPO]
        assert await owning_projects_by_repo(db, [REPO]) == {}
    assert result.state == "unresolved"
    assert result.owner is None
    assert result.unresolved_repos == (REPO,)


@pytest.mark.asyncio
async def test_unrelated_verified_registration_does_not_block(db_env):
    ids = await _seed(db_env, [("product", REPO), ("other", OTHER_REPO)])
    async with db_session() as db:
        result = (await resolve_repo_ownership(
            db, [REPO.upper()], canonical_repos={REPO: REPO, OTHER_REPO: OTHER_REPO}))[REPO.upper()]
    assert result.state == "unique"
    assert result.owner == RepoOwner(ids["product"], "product")


@pytest.mark.asyncio
async def test_partial_request_snapshot_is_explicit(db_env):
    await _seed(db_env, [("product", REPO)])
    async with db_session() as db:
        result = await resolve_repo_ownership(
            db, [REPO, OTHER_REPO], canonical_repos={REPO: REPO})
    assert result[REPO].state == "unique"
    assert result[OTHER_REPO].state == "unresolved"
    assert result[OTHER_REPO].unresolved_repos == (OTHER_REPO,)


@pytest.mark.asyncio
async def test_conflicting_casefold_snapshot_cannot_choose_evidence(db_env):
    await _seed(db_env, [("product", REPO)])
    async with db_session() as db:
        result = (await resolve_repo_ownership(
            db, [REPO], canonical_repos={REPO: REPO, REPO.upper(): OTHER_REPO}))[REPO]
    assert result.state == "unresolved"
    assert result.canonical_repo is None
    assert result.owner is None


@pytest.mark.asyncio
async def test_empty_database_requires_verified_identity_for_missing(db_env):
    await _seed(db_env, [])
    async with db_session() as db:
        assert await resolve_repo_ownership(db, [""]) == {}
        verified = (await resolve_repo_ownership(
            db, [REPO], canonical_repos={REPO: REPO}))[REPO]
        unverified = (await resolve_repo_ownership(db, [REPO], canonical_repos={}))[REPO]
    assert verified.state == "missing"
    assert unverified.state == "unresolved"
    assert unverified.unresolved_repos == (REPO,)


@pytest.mark.asyncio
async def test_registration_case_variants_preserve_local_evidence_only(db_env):
    ids = await _seed(db_env, [("product", REPO), ("product", REPO.upper())])
    async with db_session() as db:
        result = (await resolve_repo_ownership(db, [REPO]))[REPO]
    assert result.state == "unique"
    assert result.owner == RepoOwner(ids["product"], "product")
    assert result.canonical_repo is None
