"""Orchestrator wiring: config seam, repo listing, status ctx map."""
from __future__ import annotations

import asyncio

from server.db import db_session, init_db
from server.db.models import Automation, Session as DbSession


def _orch():
    from server.sessions.orchestrator import SessionOrchestrator
    return SessionOrchestrator.__new__(SessionOrchestrator)  # no full init needed


def test_read_config_off_unless_enabled_with_mode(tmp_path):
    async def run():
        await init_db()
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


def test_list_repos_config_overrides_managed(tmp_path):
    async def run():
        await init_db()
        from server.sessions.orchestrator import SessionOrchestrator
        orch = _orch()
        async with db_session() as db:
            db.add(DbSession(id="s1", name="n", account_name="a", cwd="/tmp",
                             repo="o/managed"))
            await db.flush()
        assert await SessionOrchestrator._cicd_list_repos(orch, {"enabled_repos": []}) \
            == ["o/managed"]
        assert await SessionOrchestrator._cicd_list_repos(
            orch, {"enabled_repos": ["o/pinned"]}) == ["o/pinned"]
    asyncio.run(run())


def test_automation_statuses_includes_cicd_ctx():
    from server.sessions import orchestrator as orch_mod
    import inspect
    src = inspect.getsource(orch_mod.SessionOrchestrator.automation_statuses)
    assert '"cicd-doctor"' in src  # ctx map entry — badge must not be stuck on idle
