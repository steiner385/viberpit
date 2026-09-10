"""The runner's candidate pool comes from the global priority queue (spec 003 §7a).

``SessionOrchestrator._backlog_query_queue`` is the ONE composition all three
runner consumers (selection, grouping refresh, the eligible/blocked counts) draw
from: ``rank(load_queue_rows(...))`` over the Projects v2 board mirror, with the
runner's own claims excluded and the ranking knobs read from stored config. No
GitHub call is involved — a real (SQLite) mirror is seeded and read.
"""
from __future__ import annotations

import asyncio

from server.db import db_session, init_db
from server.db.models import (GhProject, GhProjectItem, Project, ProjectRepo,
                              WorkItem, WorkItemDep)


def _orch_with_ctx():
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._backlog_ctx = AutomationContext("backlog-runner")
    orch._backlog_last_queue = []
    return orch


async def _init(tmp_path, name="q.db"):
    from server.automations.registry import registry
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / name}")
    await registry.ensure_rows()


async def _seed_product(slug: str, repo: str) -> None:
    async with db_session() as db:
        project = Project(slug=slug, name=slug)
        db.add(project)
        await db.flush()
        db.add(ProjectRepo(project_id=project.id, repo=repo, is_primary=True))
        db.add(GhProject(project_id=project.id, gh_node_id=f"PVT_{slug}",
                         gh_project_number=1, title=slug))
        await db.flush()


async def _seed_item(repo: str, number: int, *, status: str = "Ready",
                     score: float | None = None) -> str:
    from sqlalchemy import select
    async with db_session() as db:
        item = WorkItem(repo=repo, number=number, kind="issue", state="open",
                        title=f"issue {number}", body="body",
                        url=f"https://github.com/{repo}/issues/{number}",
                        author="steiner385", labels=[], assignees=[])
        db.add(item)
        await db.flush()
        project_id = (await db.execute(select(ProjectRepo.project_id).where(
            ProjectRepo.repo == repo))).scalars().first()
        gh_id = (await db.execute(select(GhProject.id).where(
            GhProject.project_id == project_id))).scalars().first()
        db.add(GhProjectItem(gh_project_id=gh_id, work_item_id=item.id,
                             gh_item_node_id=f"PVTI_{repo}_{number}",
                             status=status, score=score))
        await db.flush()
        return item.id


def _config(**over) -> dict:
    cfg = {"enabled_repos": [], "queue_min_status": "Ready",
           "respect_dependencies": True, "needs_human_retry_days": 7}
    cfg.update(over)
    return cfg


# --------------------------------------------------------------------------- #
# Claims: an issue this runner already holds is not a candidate again          #
# --------------------------------------------------------------------------- #

def test_query_queue_excludes_refs_claimed_by_the_runner(tmp_path):
    async def run():
        await _init(tmp_path)
        await _seed_product("ccswitch", "o/r")
        for n in (1, 2, 3, 4):
            await _seed_item("o/r", n, score=float(10 - n))
        orch = _orch_with_ctx()
        # #1 claimed with an explicit bundle (#1 + #2 rode one batch); #3 claimed
        # by a row that never recorded `issues` — the target itself is the claim.
        await orch._backlog_ctx.set_target_state(
            "o/r#1", {"claimed": True, "issues": ["o/r#1", "o/r#2"]})
        await orch._backlog_ctx.set_target_state("o/r#3", {"claimed": True})
        # A row that is NOT claimed must not hide its issue from the queue.
        await orch._backlog_ctx.set_target_state("o/r#4", {"claimed": False})

        cands = await orch._backlog_query_queue(_config())

        assert [c["ref"] for c in cands] == ["o/r#4"]

    asyncio.run(run())


def test_query_queue_records_the_last_ranked_result(tmp_path):
    """The ranked pool is kept for the status card's provenance (Phase C, Task 7)."""
    async def run():
        await _init(tmp_path, "last.db")
        await _seed_product("ccswitch", "o/r")
        await _seed_item("o/r", 1, score=3.0)
        orch = _orch_with_ctx()

        cands = await orch._backlog_query_queue(_config())

        assert orch._backlog_last_queue == cands

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Counts: (eligible, blocked) both come from the queue                         #
# --------------------------------------------------------------------------- #

def test_count_candidates_returns_queue_eligible_and_blocked(tmp_path):
    async def run():
        await _init(tmp_path, "count.db")
        await _seed_product("ccswitch", "o/r")
        await _seed_item("o/r", 1, score=5.0)          # eligible
        blocked = await _seed_item("o/r", 2, score=9.0)  # blocked by open #1
        async with db_session() as db:
            db.add(WorkItemDep(src_ref="o/r#2", dst_ref="o/r#1", source="native",
                               active=True, src_work_item_id=blocked))
        orch = _orch_with_ctx()

        async def _read_config():
            return "auto", _config()
        orch._backlog_read_config = _read_config

        assert await orch._backlog_count_candidates() == (1, 1)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# enabled_repos still scopes the pool (empty = every onboarded product)        #
# --------------------------------------------------------------------------- #

def test_query_queue_scopes_candidates_to_enabled_repos(tmp_path):
    async def run():
        await _init(tmp_path, "repos.db")
        await _seed_product("ccswitch", "o/r")
        await _seed_product("kindash", "o/k")
        await _seed_item("o/r", 1, score=1.0)
        await _seed_item("o/k", 2, score=9.0)
        orch = _orch_with_ctx()

        # Empty list = all onboarded products (score order, so o/k#2 first).
        assert [c["ref"] for c in await orch._backlog_query_queue(_config())] == \
            ["o/k#2", "o/r#1"]
        # A non-empty list scopes the pool to those repos.
        scoped = await orch._backlog_query_queue(_config(enabled_repos=["o/r"]))
        assert [c["ref"] for c in scoped] == ["o/r#1"]

    asyncio.run(run())


def test_count_candidates_scopes_to_enabled_repos(tmp_path):
    """The card's gauge counts the same pool selection draws from."""
    async def run():
        await _init(tmp_path, "countrepos.db")
        await _seed_product("ccswitch", "o/r")
        await _seed_product("kindash", "o/k")
        await _seed_item("o/r", 1, score=1.0)
        await _seed_item("o/k", 2, score=9.0)
        orch = _orch_with_ctx()

        async def _read_config():
            return "auto", _config(enabled_repos=["o/r"])
        orch._backlog_read_config = _read_config

        assert await orch._backlog_count_candidates() == (1, 0)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# queue_min_status: the operator's Ready-vs-Backlog choice reaches rank()      #
# --------------------------------------------------------------------------- #

def test_query_queue_min_status_comes_from_config(tmp_path):
    async def run():
        await _init(tmp_path, "minstatus.db")
        await _seed_product("ccswitch", "o/r")
        await _seed_item("o/r", 1, status="Ready", score=1.0)
        await _seed_item("o/r", 2, status="Backlog", score=9.0)
        orch = _orch_with_ctx()

        ready_only = await orch._backlog_query_queue(_config())
        assert [c["ref"] for c in ready_only] == ["o/r#1"]

        both = await orch._backlog_query_queue(_config(queue_min_status="Backlog"))
        assert [c["ref"] for c in both] == ["o/r#2", "o/r#1"]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# The grouping refresh draws from the same pool (no label search)              #
# --------------------------------------------------------------------------- #

def test_refresh_groups_groups_the_queue_pool(tmp_path, monkeypatch):
    async def run():
        await _init(tmp_path, "groups.db")
        await _seed_product("ccswitch", "o/r")
        await _seed_item("o/r", 1, score=5.0)
        orch = _orch_with_ctx()

        async def _healthy():
            return None
        orch._backlog_healthy_account = _healthy
        seen: dict = {}

        async def _propose(cands, config, _config_dir=None):
            seen["candidates"] = cands
            return [{"refs": [c["ref"] for c in cands], "repo": "o/r"}]

        async def _store(groups, cands):
            seen["stored"] = cands
            return len(groups)

        monkeypatch.setattr("server.automations.backlog_grouping.propose_groups", _propose)
        monkeypatch.setattr("server.automations.backlog_grouping.store_plan", _store)

        assert await orch._backlog_refresh_groups(_config()) == 1
        assert [c["ref"] for c in seen["candidates"]] == ["o/r#1"]

    asyncio.run(run())
