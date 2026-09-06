"""``server/pm/repo_project.py`` — THE repo→project resolver.

Both the PM API (``server/api/pm.py``) and the daemon-side pm-triage seams
(``server/sessions/orchestrator.py``) route through this one function; it used
to be implemented twice, once per layer, with a hand-maintained promise in a
docstring that the two tie-breaks agreed. These tests pin the tie-break itself
(lowest ``Project.slug``, stable across insertion order) and the batch shape
(ONE query for N repos — the loop resolves a whole classify batch at once).
"""
from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

import pytest

from server.db import db_session, init_db
from server.db.models import Project, ProjectRepo
from server.pm.repo_project import RepoOwner, owning_projects_by_repo

REPO = "steiner385/ccswitch"
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
    ids = await _seed(db_env, [("ccswitch", REPO)])
    async with db_session() as db:
        owners = await owning_projects_by_repo(db, {REPO})
    assert owners == {REPO: RepoOwner(project_id=ids["ccswitch"], slug="ccswitch")}


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [
    [("alpha", REPO), ("zeta", REPO)],
    [("zeta", REPO), ("alpha", REPO)],
])
async def test_two_owners_lowest_slug_wins_whatever_the_insertion_order(
        db_env, order):
    """A repo CAN legally sit under two projects (``ProjectRepo`` is unique on
    ``(project_id, repo)`` and registration does no cross-project check). The
    winner must not depend on row order, or the inbox's ``projectSlug`` and the
    loop's classify scoping disagree between a request and a tick."""
    ids = await _seed(db_env, order)
    async with db_session() as db:
        owners = await owning_projects_by_repo(db, {REPO})
    assert owners[REPO] == RepoOwner(project_id=ids["alpha"], slug="alpha")


@pytest.mark.asyncio
async def test_unowned_repo_is_absent_not_none_valued(db_env):
    """Absent, so ``.get()`` is ``None`` and every caller skips rather than
    guessing a project."""
    await _seed(db_env, [("ccswitch", REPO)])
    async with db_session() as db:
        owners = await owning_projects_by_repo(db, {REPO, "someone/unknown"})
    assert set(owners) == {REPO}
    assert owners.get("someone/unknown") is None


@pytest.mark.asyncio
async def test_empty_and_null_repos_are_dropped_without_a_query(db_env):
    """``ProjectRepo.repo`` is nullable, and callers build the input set from
    ``WorkItem.repo`` — which is nullable too. Falsy repos never reach SQL."""
    ids = await _seed(db_env, [("ccswitch", REPO), ("ccswitch", None)])
    async with db_session() as db:
        counter = _CountingExecute(db)
        with patch.object(db, "execute", counter):
            assert await owning_projects_by_repo(db, set()) == {}
            assert await owning_projects_by_repo(db, {""}) == {}
            assert counter.calls == 0  # nothing to ask about → no round trip
            # The project's NULL-repo row is not an owner of anything.
            assert await owning_projects_by_repo(db, {"", REPO}) == {
                REPO: RepoOwner(project_id=ids["ccswitch"], slug="ccswitch")}
            assert counter.calls == 1


@pytest.mark.asyncio
async def test_batch_of_repos_is_one_query_not_n(db_env):
    ids = await _seed(db_env, [
        ("ccswitch", REPO), ("kindash", OTHER_REPO), ("infra", "steiner385/infra")])
    async with db_session() as db:
        counter = _CountingExecute(db)
        with patch.object(db, "execute", counter):
            owners = await owning_projects_by_repo(
                db, {REPO, OTHER_REPO, "steiner385/infra", "someone/unknown"})
    assert counter.calls == 1
    assert owners == {
        REPO: RepoOwner(project_id=ids["ccswitch"], slug="ccswitch"),
        OTHER_REPO: RepoOwner(project_id=ids["kindash"], slug="kindash"),
        "steiner385/infra": RepoOwner(project_id=ids["infra"], slug="infra"),
    }
