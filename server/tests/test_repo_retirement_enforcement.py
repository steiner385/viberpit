"""Retired product repos get no new sessions, claims, writes or sync, and every
primary-repo reader follows the current primary
(docs/superpowers/specs/2026-11-18-product-repo-lifecycle-design.md
§ "Where retirement is enforced")."""
from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from server.db import db_session
from server.db.clock import db_utc_now
from server.db.models import Project, ProjectRepo
from server.pm import product_repos
from server.pm.product_repos import RepoRetired
from server.tests.test_pm_api import IDENTITY_HEADERS, _client, canonical_mutation_repositories  # noqa: F401

H = IDENTITY_HEADERS


@pytest.fixture(autouse=True)
def _fresh_retired_cache():
    product_repos.invalidate_retired_cache()
    yield
    product_repos.invalidate_retired_cache()


async def _seed() -> str:
    """Product "lifecycle": o/old retired (superseded by o/new), o/new primary, o/side."""
    async with db_session() as db:
        p = Project(slug="lifecycle", name="Lifecycle")
        db.add(p)
        await db.flush()
        db.add_all([
            ProjectRepo(project_id=p.id, repo="o/side", is_primary=False),
            ProjectRepo(project_id=p.id, repo="o/old", is_primary=False,
                        retired_at=db_utc_now(), retired_reason="archived",
                        superseded_by="o/new"),
            ProjectRepo(project_id=p.id, repo="o/new", is_primary=True),
        ])
        await db.commit()
        return p.id


def test_product_views_drop_retired(isolated_db):
    from server.pm.mirror_store import list_product_views

    async def go():
        await _seed()
        async with db_session() as db:
            return await db.run_sync(list_product_views)
    views = asyncio.run(go())
    assert [v.repos for v in views] == [["o/new", "o/side"]]


def test_pm_sync_skips_retired(isolated_db):
    from server.sessions.orchestrator import SessionOrchestrator

    async def go():
        await _seed()
        return await SessionOrchestrator._pm_sync_list_repos(None)  # type: ignore[arg-type]
    assert sorted(v.repo for v in asyncio.run(go())) == ["o/new", "o/side"]


def test_mutation_owner_refuses_retired(isolated_db):
    from server.pm.repo_project import OwnershipConflict, require_mutation_owner
    from server.tests.pm_ownership_fixtures import ownership_snapshot

    async def go():
        pid = await _seed()
        async with db_session() as db:
            snap = await ownership_snapshot(db, "o/old", "o/new")
            owner = await require_mutation_owner(db, "o/new", snapshot=snap)
            with pytest.raises(OwnershipConflict) as exc:
                await require_mutation_owner(db, "O/Old", snapshot=snap)
            return pid, owner, exc.value
    pid, owner, exc = asyncio.run(go())
    assert owner is not None and owner.project_id == pid
    assert exc.state == "retired"
    assert "o/old is retired; use o/new" in str(exc)
    assert exc.result()["ownershipState"] == "retired"


def test_create_session_refuses_retired_repo(isolated_db, tmp_path):
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    async def go():
        await _seed()
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
        with patch("server.sessions.orchestrator.gitrepos.ensure_clone",
                   side_effect=AssertionError("must refuse before cloning")):
            with pytest.raises(RepoRetired) as by_repo:
                await orch.create_session(name="x", repo="o/old")
        # A cwd whose origin is the retired repo is refused too (any identity).
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        subprocess.run(["git", "-C", str(checkout), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin",
                        "https://github.com/o/old.git"], check=True)
        with pytest.raises(RepoRetired) as by_cwd:
            await orch.create_session(name="y", cwd=str(checkout), identity="someone@x")
        return by_repo.value, by_cwd.value
    by_repo, by_cwd = asyncio.run(go())
    assert by_repo.repo == "o/old" and by_repo.superseded_by == "o/new"
    assert str(by_repo) == "o/old is retired; use o/new"
    assert by_cwd.repo == "o/old"


def test_repo_retired_survives_the_daemon_envelope():
    from server.ipc.client import DaemonError, reconstruct_create_session_error

    err = RepoRetired("o/old", "o/new")
    rebuilt = reconstruct_create_session_error(
        DaemonError(type(err).__name__, str(err), err.error_data))
    assert isinstance(rebuilt, RepoRetired)
    assert (rebuilt.repo, rebuilt.superseded_by, str(rebuilt)) == (
        "o/old", "o/new", "o/old is retired; use o/new")


def _create_and_retire(c) -> None:
    resp = c.post("/api/pm/projects", json={"name": "Ghost", "repos": [
        {"repo": "o/ghost-mcp", "isPrimary": True}, {"repo": "o/ghost", "isPrimary": False}]},
        headers=H)
    assert resp.status_code == 201, resp.text
    resp = c.post("/api/pm/projects/ghost/repos/o/ghost-mcp/retire", json={
        "reason": "archived", "supersededBy": "o/ghost", "newPrimary": "o/ghost"}, headers=H)
    assert resp.status_code == 200, resp.text


def _assert_repo_retired(resp) -> None:
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "repo_retired"
    assert detail["message"] == "o/ghost-mcp is retired; use o/ghost"


def test_create_session_api_refuses_retired_repo():
    with _client() as c, patch("server.sessions.orchestrator.gitrepos.ensure_clone",
                               side_effect=AssertionError("must refuse before cloning")):
        _create_and_retire(c)
        _assert_repo_retired(c.post("/api/sessions", json={
            "name": "x", "repo": "o/ghost-mcp"}, headers=H))


def test_create_issue_api_refuses_retired_repo():
    with _client() as c, patch("server.github.ci_issues.create_issue",
                               side_effect=AssertionError("no gh write")):
        _create_and_retire(c)
        _assert_repo_retired(c.post("/api/github/issues", json={
            "repo": "o/ghost-mcp", "title": "t", "body": "b"}, headers=H))


def test_comment_api_refuses_retired_repo():
    with _client() as c, patch("server.github.ci_issues.comment_on_issue",
                               side_effect=AssertionError("no gh write")), \
            patch("server.github.ci_issues.comment_on_pr",
                  side_effect=AssertionError("no gh write")):
        _create_and_retire(c)
        _assert_repo_retired(c.post("/api/github/issues/o/ghost-mcp/7/comments",
                                    json={"body": "hi"}, headers=H))
        _assert_repo_retired(c.post("/api/prs/o/ghost-mcp/7/comments",
                                    json={"body": "hi"}, headers=H))


def test_issue_filing_skips_retired_repo(isolated_db):
    from server.automations import issue_filing

    def run(*_a, **_k):
        raise AssertionError("no gh write against a retired repo")

    async def go():
        await _seed()
        return (
            await issue_filing.apply(issue_filing.Decision("file", "no_issue"), repo="o/old",
                                     title="t", body="b", labels=[], linked=None, run=run),
            await issue_filing.apply(issue_filing.Decision("comment", "open"), repo="o/old",
                                     title="t", body="b", labels=[],
                                     linked=issue_filing.Linked(5), run=run),
        )
    filed, commented = asyncio.run(go())
    assert filed == issue_filing.Applied(issue_number=None, commented=False)
    assert commented == issue_filing.Applied(issue_number=5, commented=False)


def test_cicd_list_repos_drops_retired(isolated_db):
    from server.sessions.orchestrator import SessionOrchestrator

    async def go():
        await _seed()
        orch = SessionOrchestrator.__new__(SessionOrchestrator)  # no full init needed
        return await orch._cicd_list_repos({"enabled_repos": ["o/old", "o/new"]})
    assert asyncio.run(go()) == ["o/new"]


_LADDER = {"version": 1, "title": "L", "spec": "s", "start": "2026-01-01",
           "milestones": [], "numbers": []}


async def _primary_readers(pid: str, slug: str) -> tuple:
    """What each primary-repo reader currently resolves for the product."""
    from server.automations.capacity_wiring import sources_for
    from server.pm.milestone_refresh import _load
    from server.pm.repo_project import primary_repo_for_slug

    async with db_session() as db:
        slug_primary = await primary_repo_for_slug(db, slug)
    loaded = await _load(pid)
    products = await sources_for(MagicMock()).products()  # products() reads only the DB
    return (slug_primary, loaded[2] if loaded else None,
            next(p["primaryRepo"] for p in products if p["slug"] == slug))


def test_primary_readers_ignore_a_retired_row_still_flagged_primary(isolated_db):
    """Legacy/hand-edited data: a retired row that still carries is_primary must
    never be read as the primary — every reader goes through ``ACTIVE``."""
    from server.db.models import MilestoneLadder

    async def go():
        async with db_session() as db:
            p = Project(slug="legacy", name="Legacy")
            db.add(p)
            await db.flush()
            db.add(ProjectRepo(project_id=p.id, repo="o/old", is_primary=True,
                               retired_at=db_utc_now()))
            await db.flush()
            db.add(ProjectRepo(project_id=p.id, repo="o/new", is_primary=True))
            db.add(MilestoneLadder(project_id=p.id, definition=_LADDER))
            await db.commit()
            pid = p.id
        return await _primary_readers(pid, "legacy")
    assert asyncio.run(go()) == ("o/new",) * 3


def test_primary_readers_follow_new_primary():
    from server.db.models import MilestoneLadder

    with _client() as c:
        resp = c.post("/api/pm/projects", json={"name": "Ghost", "repos": [
            {"repo": "o/ghost-mcp", "isPrimary": True}]}, headers=H)
        assert resp.status_code == 201, resp.text
        pid = resp.json()["id"]

        async def seed_ladder():
            async with db_session() as db:
                db.add(MilestoneLadder(project_id=pid, definition=_LADDER))
                await db.commit()
        c.portal.call(seed_ladder)

        async def readers():
            return await _primary_readers(pid, "ghost")

        assert c.portal.call(readers) == ("o/ghost-mcp",) * 3
        assert c.post("/api/pm/projects/ghost/repos", json={"repo": "o/ghost"},
                      headers=H).status_code == 201
        assert c.post("/api/pm/projects/ghost/repos/o/ghost/primary", json={},
                      headers=H).status_code == 200
        assert c.portal.call(readers) == ("o/ghost",) * 3
        resp = c.get("/api/pm/projects/ghost/milestones", headers=H)
        assert resp.status_code == 200, resp.text
        # Retiring the old one changes nothing for the readers: it is no longer primary.
        assert c.post("/api/pm/projects/ghost/repos/o/ghost-mcp/retire",
                      json={"reason": "archived"}, headers=H).status_code == 200
        assert c.portal.call(readers) == ("o/ghost",) * 3
