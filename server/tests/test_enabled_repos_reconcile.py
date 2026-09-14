"""The startup sweep that retires a rename alias already sitting in the DB.

Canonicalizing on the write path (``PATCH /api/automations/{id}``) stops a NEW
alias from being stored, but it does nothing for the rows written before the
rename — and those are the ones actually causing duplicate work. pr-doctor keys
its jobs on ``"owner/repo#N"``, so a renamed repo's old slug sitting alongside its own canonical name in
``enabled_repos`` spawned two concurrent sessions on the same pull request.

Harness mirrors ``test_automation_config_store``: a real SQLite DB with the
automation rows registry-seeded, so the sweep is exercised against real rows.
"""
from __future__ import annotations

import asyncio
import json
import re

from server.automations.config_store import reconcile_enabled_repos
from server.github import repo_canon

_FIELD = re.compile(r'(r\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\)')

ALIAS = "acme/old-name"
CANON = "acme/new-name"
OTHER = "cairnea/KinDash"


def _gh(mapping):
    def run(args, **kw):
        data = {}
        for alias, owner, name in _FIELD.findall(kw.get("input") or ""):
            canonical = mapping.get(f"{owner}/{name}")
            data[alias] = {"nameWithOwner": canonical} if canonical else None

        class P:
            returncode = 0
            stdout = json.dumps({"data": data})
            stderr = ""

        return P()

    return run


_RESOLVES = _gh({ALIAS: CANON, CANON: CANON, OTHER: OTHER})


async def _init_db(tmp_path) -> None:
    from server.automations.registry import registry
    from server.db import init_db
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'canon.db'}")
    await registry.ensure_rows()


async def _set_repos(automation_id: str, repos: list[str]) -> None:
    from sqlalchemy import select as sa_select
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        row = (await db.execute(sa_select(Automation).where(
            Automation.id == automation_id))).scalar_one()
        row.config = {**(row.config or {}), "enabled_repos": repos}


async def _get_repos(automation_id: str) -> list[str] | None:
    from sqlalchemy import select as sa_select
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        row = (await db.execute(sa_select(Automation).where(
            Automation.id == automation_id))).scalar_one()
        return (row.config or {}).get("enabled_repos")


def test_sweep_retires_the_alias_and_drops_the_duplicate(tmp_path, monkeypatch):
    async def check():
        await _init_db(tmp_path)
        await _set_repos("pr-doctor", [ALIAS, OTHER, CANON])
        changed = await reconcile_enabled_repos(gh_run=_RESOLVES)
        assert await _get_repos("pr-doctor") == [CANON, OTHER]
        assert changed == 1
    asyncio.run(check())


def test_sweep_covers_every_automation_not_just_pr_doctor(tmp_path, monkeypatch):
    """The alias was in FOUR lists. A sweep that fixed one would leave the rest
    spawning against a slug the others no longer use."""
    async def check():
        await _init_db(tmp_path)
        await _set_repos("pr-doctor", [ALIAS])
        await _set_repos("autopilot", [ALIAS, CANON])
        changed = await reconcile_enabled_repos(gh_run=_RESOLVES)
        assert await _get_repos("pr-doctor") == [CANON]
        assert await _get_repos("autopilot") == [CANON]
        assert changed == 2
    asyncio.run(check())


def test_sweep_is_idempotent(tmp_path, monkeypatch):
    """It runs on EVERY startup — a second pass must report no change, or the
    log line becomes noise nobody reads."""
    async def check():
        await _init_db(tmp_path)
        await _set_repos("pr-doctor", [ALIAS, CANON])
        assert await reconcile_enabled_repos(gh_run=_RESOLVES) == 1
        assert await reconcile_enabled_repos(gh_run=_RESOLVES) == 0
        assert await _get_repos("pr-doctor") == [CANON]
    asyncio.run(check())


def test_an_unreachable_github_leaves_every_row_untouched(tmp_path, monkeypatch):
    """Fail OPEN. This runs at startup, when a transient network state is most
    likely — emptying or reordering enabled_repos then would be a silent outage."""
    def boom(args, **kw):
        raise OSError("network down")

    async def check():
        await _init_db(tmp_path)
        await _set_repos("pr-doctor", [ALIAS, OTHER])
        assert await reconcile_enabled_repos(gh_run=boom) == 0
        assert await _get_repos("pr-doctor") == [ALIAS, OTHER]
    asyncio.run(check())


def test_app_startup_runs_the_sweep(isolated_db, monkeypatch):
    """Wiring, not plumbing: a DB that already holds an alias comes out of a real
    app startup canonical. Seeded BEFORE the app is built, because ``init_db`` and
    ``ensure_rows`` are idempotent and leave an existing row's config alone.

    ``isolated_db`` owns the DB env var, so this test mints no settings literals
    of its own and the app picks the same file up from the environment.
    """
    from unittest.mock import patch as _patch

    async def seed():
        from server.automations.registry import registry
        from server.db import init_db
        await init_db(db_url=isolated_db)
        await registry.ensure_rows()
        await _set_repos("pr-doctor", [ALIAS, CANON])
    asyncio.run(seed())

    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with _patch.object(repo_canon.gitrepos, "_run_gh", _RESOLVES):
        app = create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))
        with TestClient(app, raise_server_exceptions=False):
            pass

    async def read():
        from server.db import init_db
        await init_db(db_url=isolated_db)
        return await _get_repos("pr-doctor")
    assert asyncio.run(read()) == [CANON]
