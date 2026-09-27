"""Orchestrator wiring: config seam, repo listing, status ctx map."""
from __future__ import annotations

import asyncio
import json
import re
from unittest.mock import patch

from server.db import db_session, init_db
from server.db.models import Automation, Session as DbSession
from server.github import repo_canon

_FIELD = re.compile(r'(r\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\)')


def _gh_resolving(mapping: dict[str, str]):
    """Fake GraphQL gh: answers each aliased repository field via *mapping*
    (slug -> nameWithOwner); a slug absent from it resolves to itself."""
    def run(args, **kw):
        data = {alias: {"nameWithOwner": mapping.get(f"{o}/{n}", f"{o}/{n}")}
                for alias, o, n in _FIELD.findall(kw.get("input") or "")}

        class P:
            returncode, stdout, stderr = 0, json.dumps({"data": data}), ""
        return P()
    return run


def _orch():
    from server.sessions.orchestrator import SessionOrchestrator
    return SessionOrchestrator.__new__(SessionOrchestrator)  # no full init needed


def test_read_config_off_unless_enabled_with_mode(tmp_path):
    async def run():
        # Per-test file DB: a bare init_db() joins the suite-shared DB, where an
        # earlier test's registry.ensure_rows() already created the cicd-doctor
        # row -> UNIQUE constraint failure (order-dependent, CI-only).
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        from server.sessions.orchestrator import SessionOrchestrator
        orch = _orch()
        mode, config = await SessionOrchestrator._cicd_read_config(orch)
        assert mode == "off" and config["min_runs"] == 30
        async with db_session() as db:
            db.add(Automation(id="cicd-doctor", enabled=True, mode="propose",
                              config={"min_runs": 10}))
            await db.flush()
        mode, config = await SessionOrchestrator._cicd_read_config(orch)
        assert mode == "propose" and config["min_runs"] == 10
    asyncio.run(run())


def test_read_config_enabled_without_mode_defaults_to_propose(tmp_path):
    """A 2-mode automation (off/propose) has no mode <select> in the UI, so the
    toggle enables it with mode left NULL. Enabled + no explicit mode must resolve
    to the first non-off mode, else the loop is stuck 'off' forever."""
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        from server.sessions.orchestrator import SessionOrchestrator
        orch = _orch()
        async with db_session() as db:
            db.add(Automation(id="cicd-doctor", enabled=True, mode=None))
            await db.flush()
        mode, _ = await SessionOrchestrator._cicd_read_config(orch)
        assert mode == "propose"
    asyncio.run(run())


def test_read_config_disabled_is_off_even_with_mode(tmp_path):
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        from server.sessions.orchestrator import SessionOrchestrator
        orch = _orch()
        async with db_session() as db:
            db.add(Automation(id="cicd-doctor", enabled=False, mode="propose"))
            await db.flush()
        mode, _ = await SessionOrchestrator._cicd_read_config(orch)
        assert mode == "off"
    asyncio.run(run())


def test_list_repos_config_overrides_managed(tmp_path):
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        from server.sessions.orchestrator import SessionOrchestrator
        orch = _orch()
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd="/tmp",
                             repo="o/managed"))
            await db.flush()
        with patch.object(repo_canon.gitrepos, "_run_gh", _gh_resolving({})):
            assert await SessionOrchestrator._cicd_list_repos(
                orch, {"enabled_repos": []}) == ["o/managed"]
        assert await SessionOrchestrator._cicd_list_repos(
            orch, {"enabled_repos": ["o/pinned"]}) == ["o/pinned"]
    asyncio.run(run())


def test_list_repos_collapses_a_renamed_repo_alias(tmp_path):
    """#1710: session rows that predate a rename carry the old slug, a GitHub
    redirect alias for the new one. Listed raw, the doctor ran a second lane over
    the same runs; the managed list must collapse onto the canonical name."""
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        from server.sessions.orchestrator import SessionOrchestrator
        orch = _orch()
        async with db_session() as db:
            for sid, repo in (("s1", "o/old-name"), ("s2", "o/new-name"), ("s3", "o/other")):
                db.add(DbSession(id=sid, name="n", account_name="a", cwd="/tmp", repo=repo))
            await db.flush()
        gh = _gh_resolving({"o/old-name": "o/new-name"})
        with patch.object(repo_canon.gitrepos, "_run_gh", gh):
            assert await SessionOrchestrator._cicd_list_repos(
                orch, {"enabled_repos": []}) == ["o/new-name", "o/other"]
    asyncio.run(run())


def test_list_repos_caches_canonicalization_within_ttl(tmp_path):
    """DeployMonitorLoop ticks every 120s and CicdDoctorLoop every 900s, both
    through this one method — without a cache, an unchanging slug set still
    re-issues a GraphQL lookup (and re-files repo_canon's WARNING) every 120s
    (#2513). A repeat call for the same slug set within the TTL must not call
    gh again; past the TTL it must."""
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        from server.sessions.orchestrator import SessionOrchestrator
        orch = _orch()
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd="/tmp",
                             repo="o/managed"))
            await db.flush()
        calls: list[str | None] = []
        resolving = _gh_resolving({})

        def gh(args, **kw):
            calls.append(kw.get("input"))
            return resolving(args, **kw)

        now = [1000.0]
        orch._clock = lambda: now[0]
        with patch.object(repo_canon.gitrepos, "_run_gh", gh):
            assert await SessionOrchestrator._cicd_list_repos(
                orch, {"enabled_repos": []}) == ["o/managed"]
            assert await SessionOrchestrator._cicd_list_repos(
                orch, {"enabled_repos": []}) == ["o/managed"]
        assert len(calls) == 1, "second call within the TTL must hit the cache"

        now[0] += 301.0  # past the 300s TTL
        with patch.object(repo_canon.gitrepos, "_run_gh", gh):
            assert await SessionOrchestrator._cicd_list_repos(
                orch, {"enabled_repos": []}) == ["o/managed"]
        assert len(calls) == 2, "a call past the TTL must refresh"
    asyncio.run(run())


def test_list_repos_cache_invalidates_on_a_changed_slug_set(tmp_path):
    """A newly-added managed repo must not be hidden behind a stale cache entry
    keyed on the old slug set."""
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        from server.sessions.orchestrator import SessionOrchestrator
        orch = _orch()
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd="/tmp",
                             repo="o/managed"))
            await db.flush()
        with patch.object(repo_canon.gitrepos, "_run_gh", _gh_resolving({})):
            assert await SessionOrchestrator._cicd_list_repos(
                orch, {"enabled_repos": []}) == ["o/managed"]
            async with db_session() as db:
                db.add(DbSession(id="s2", name="n", account_name="a", cwd="/tmp",
                                 repo="o/other"))
                await db.flush()
            assert await SessionOrchestrator._cicd_list_repos(
                orch, {"enabled_repos": []}) == ["o/managed", "o/other"]
    asyncio.run(run())


def test_automation_statuses_includes_cicd_ctx():
    from server.sessions import orchestrator as orch_mod
    import inspect
    src = inspect.getsource(orch_mod.SessionOrchestrator.automation_statuses)
    assert '"cicd-doctor"' in src  # ctx map entry — badge must not be stuck on idle
