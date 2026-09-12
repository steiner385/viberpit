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
from server.db.models import (DispatchOutcome, GhProject, GhProjectItem, Project, ProjectRepo,
                              WorkItem, WorkItemDep)
from server.pm.outcomes import body_fingerprint


def _orch_with_ctx():
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._backlog_ctx = AutomationContext("backlog-runner")
    orch._backlog_last_queue = []
    orch._backlog_last_policies = {}
    orch._backlog_last_repos = {}
    return orch


async def _init(tmp_path, name="q.db"):
    from server.automations.registry import registry
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / name}")
    await registry.ensure_rows()


async def _seed_product(slug: str, repo: str, *,
                        policy: dict | None = None) -> None:
    """A product the runner works: every candidate read is now scoped to products
    whose policy says ``dispatch != off``, so the fixture opts in by default."""
    async with db_session() as db:
        project = Project(slug=slug, name=slug,
                          automation_policy=policy or {"dispatch": "auto"})
        db.add(project)
        await db.flush()
        db.add(ProjectRepo(project_id=project.id, repo=repo, is_primary=True))
        db.add(GhProject(project_id=project.id, gh_node_id=f"PVT_{slug}",
                         gh_project_number=1, title=slug))
        await db.flush()


async def _set_policy(slug: str, policy: dict | None) -> None:
    from sqlalchemy import select
    async with db_session() as db:
        project = (await db.execute(select(Project).where(
            Project.slug == slug))).scalar_one()
        project.automation_policy = policy


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
    cfg = {"needs_human_retry_days": 7}
    cfg.update(over)
    return cfg


# --------------------------------------------------------------------------- #
# Claims: an issue this runner already holds is not a candidate again          #
# --------------------------------------------------------------------------- #

def test_query_queue_excludes_refs_claimed_by_the_runner(tmp_path):
    async def run():
        await _init(tmp_path)
        await _seed_product("viberpit", "o/r")
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
        await _seed_product("viberpit", "o/r")
        await _seed_item("o/r", 1, score=3.0)
        orch = _orch_with_ctx()

        cands = await orch._backlog_query_queue(_config())

        assert orch._backlog_last_queue == cands

    asyncio.run(run())


def test_gave_up_row_is_a_cooldown_not_a_claim(tmp_path):
    """Phase D: a give-up leaves a durable row instead of the ``needs-human``
    label. The row is NOT a claim (that skip reason would be a lie about a live
    session) — it is the needs-human cooldown, and it expires with
    ``needs_human_retry_days`` so the issue comes back."""
    async def run():
        await _init(tmp_path, "gaveup.db")
        await _seed_product("viberpit", "o/r",
                            policy={"dispatch": "auto", "queue_min_status": "Backlog"})
        await _seed_item("o/r", 5, status="Backlog", score=5.0)
        orch = _orch_with_ctx()
        await orch._backlog_ctx.set_target_state("o/r#5", {
            "repo": "o/r", "issues": ["o/r#5"], "gave_up": True,
            "needs_human_at": "2026-09-09T00:00:00+00:00", "reason": "max attempts"})

        skipped: dict = {}
        cands = await orch._backlog_query_queue(_config(), skipped_out=skipped)
        assert cands == [] and skipped == {"needs_human_cooldown": 1}

        # 0 = permanent handoff: still cooling down, never retried.
        skipped = {}
        cands = await orch._backlog_query_queue(
            _config(needs_human_retry_days=0), skipped_out=skipped)
        assert cands == [] and skipped == {"needs_human_cooldown": 1}

        # Once the window has passed the issue is a candidate again.
        await orch._backlog_ctx.set_target_state("o/r#5", {
            "repo": "o/r", "issues": ["o/r#5"], "gave_up": True,
            "needs_human_at": "2020-01-01T00:00:00+00:00", "reason": "max attempts"})
        skipped = {}
        cands = await orch._backlog_query_queue(_config(), skipped_out=skipped)
        assert [c["ref"] for c in cands] == ["o/r#5"] and skipped == {}

    asyncio.run(run())


def test_gave_up_row_is_never_a_claim_and_an_undated_one_is_no_cooldown(tmp_path):
    """Two rules at once: a ``gave_up`` row is not a claim (its session is long
    gone), and an unreadable/missing ``needs_human_at`` is no cooldown — a row we
    cannot date must not park an issue forever."""
    async def run():
        await _init(tmp_path, "gaveupnostamp.db")
        await _seed_product("viberpit", "o/r",
                            policy={"dispatch": "auto", "queue_min_status": "Backlog"})
        await _seed_item("o/r", 6, status="Backlog", score=5.0)
        orch = _orch_with_ctx()
        await orch._backlog_ctx.set_target_state(
            "o/r#6", {"repo": "o/r", "issues": ["o/r#6"], "gave_up": True,
                      "claimed": True, "needs_human_at": "not-a-date"})

        cands = await orch._backlog_query_queue(_config())

        assert [c["ref"] for c in cands] == ["o/r#6"]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Counts: (eligible, blocked, untriaged) all come from the queue               #
# --------------------------------------------------------------------------- #

def test_count_candidates_returns_queue_eligible_and_blocked(tmp_path):
    async def run():
        await _init(tmp_path, "count.db")
        await _seed_product("viberpit", "o/r")
        await _seed_item("o/r", 1, score=5.0)          # eligible
        blocked = await _seed_item("o/r", 2, score=9.0)  # blocked by open #1
        async with db_session() as db:
            db.add(WorkItemDep(src_ref="o/r#2", dst_ref="o/r#1", source="native",
                               active=True, src_work_item_id=blocked))
        orch = _orch_with_ctx()

        async def _read_config():
            return "on", _config()
        orch._backlog_read_config = _read_config

        eligible, blocked, untriaged, _per_product = \
            await orch._backlog_count_candidates()
        assert (eligible, blocked, untriaged) == (1, 1, 0)

    asyncio.run(run())


def test_count_candidates_reports_below_min_status_items_as_untriaged(tmp_path):
    """In the default Ready mode, a board full of Backlog items would otherwise
    read as a bare "0 eligible" — an empty backlog rather than one waiting on
    triage. The status skip is its own gauge on the card (#990)."""
    async def run():
        await _init(tmp_path, "untriaged.db")
        await _seed_product("viberpit", "o/r")
        await _seed_item("o/r", 1, score=5.0)                     # Ready → eligible
        await _seed_item("o/r", 2, status="Backlog", score=9.0)   # untriaged
        await _seed_item("o/r", 3, status="Backlog", score=8.0)   # untriaged
        orch = _orch_with_ctx()

        async def _read_config():
            return "on", _config()
        orch._backlog_read_config = _read_config

        eligible, blocked, untriaged, _per_product = \
            await orch._backlog_count_candidates()
        assert (eligible, blocked, untriaged) == (1, 0, 2)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Policy scoping: only products whose dispatch is on are in the pool           #
# --------------------------------------------------------------------------- #

def test_count_candidates_gauges_ignore_products_with_dispatch_off(tmp_path):
    """The card's gauges count the same pool selection draws from. Another
    product's stuck backlog must not read as "40 blocked" on a card whose runner
    was never turned on for it — the number is the "why isn't THIS backlog
    draining" note."""
    async def run():
        await _init(tmp_path, "blockedscope.db")
        await _seed_product("viberpit", "o/r")                     # dispatch on
        await _seed_product("kindash", "o/k", policy={"dispatch": "off"})
        await _seed_item("o/r", 1, score=1.0)                      # on, eligible
        await _seed_item("o/k", 2, status="Blocked", score=9.0)    # off, blocked
        orch = _orch_with_ctx()

        async def _read_config():
            return "on", _config()
        orch._backlog_read_config = _read_config

        eligible, blocked, untriaged, per_product = \
            await orch._backlog_count_candidates()
        assert (eligible, blocked, untriaged) == (1, 0, 0)
        assert set(per_product) == {"viberpit"}

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# A failed queue read costs a spawn, never the whole tick                      #
# --------------------------------------------------------------------------- #

def test_select_batch_survives_a_failing_queue_read(tmp_path, monkeypatch):
    """The selection half of a tick shares its coroutine with the sweep half
    (reconcile / orphan heal / give-up), so a raising pool read would skip all of
    it. A queue read that blows up is "no batch this tick", exactly as the old
    label search returning [] was."""
    async def run():
        await _init(tmp_path, "selectfail.db")
        orch = _orch_with_ctx()

        async def _boom(config, *, skipped_out=None, products_out=None):
            raise RuntimeError("mirror unavailable")
        orch._backlog_query_queue = _boom

        async def _repo_load():
            return {}
        orch._backlog_repo_load = _repo_load
        monkeypatch.setattr("ccswitch.gitrepos.stale_credential_repos", lambda: [])

        assert await orch._backlog_select_batch(_config()) is None

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# The Ready-vs-Backlog choice is the PRODUCT's, and it reaches rank()          #
# --------------------------------------------------------------------------- #

def test_query_queue_min_status_comes_from_the_product_policy(tmp_path):
    async def run():
        await _init(tmp_path, "minstatus.db")
        await _seed_product("viberpit", "o/r")
        await _seed_item("o/r", 1, status="Ready", score=1.0)
        await _seed_item("o/r", 2, status="Backlog", score=9.0)
        orch = _orch_with_ctx()

        ready_only = await orch._backlog_query_queue(_config())
        assert [c["ref"] for c in ready_only] == ["o/r#1"]

        await _set_policy("viberpit", {"dispatch": "auto",
                                       "queue_min_status": "Backlog"})
        both = await orch._backlog_query_queue(_config())
        assert [c["ref"] for c in both] == ["o/r#2", "o/r#1"]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# D8: a body that keeps bouncing is parked, not re-dispatched                  #
# --------------------------------------------------------------------------- #

def test_query_queue_excludes_a_bounced_row_and_reports_it(tmp_path):
    async def run():
        await _init(tmp_path, "bounced.db")
        await _seed_product("viberpit", "o/r",
                            policy={"dispatch": "auto", "give_up_after_bounces": 2})
        await _seed_item("o/r", 1, score=5.0)
        fp = body_fingerprint("issue 1", "body")
        async with db_session() as db:
            db.add_all([
                DispatchOutcome(target_ref="o/r#1", body_fingerprint=fp, outcome="no_pr"),
                DispatchOutcome(target_ref="o/r#1", body_fingerprint=fp, outcome="no_pr"),
            ])
        orch = _orch_with_ctx()

        per_product: dict = {}
        cands = await orch._backlog_query_queue(_config(), products_out=per_product)

        assert cands == []
        assert per_product["viberpit"]["bounced"] == 1

    asyncio.run(run())


def test_query_queue_keeps_a_bounced_row_when_give_up_after_bounces_is_zero(tmp_path):
    async def run():
        await _init(tmp_path, "neverbounce.db")
        await _seed_product("viberpit", "o/r",
                            policy={"dispatch": "auto", "give_up_after_bounces": 0})
        await _seed_item("o/r", 1, score=5.0)
        fp = body_fingerprint("issue 1", "body")
        async with db_session() as db:
            db.add_all([
                DispatchOutcome(target_ref="o/r#1", body_fingerprint=fp, outcome="no_pr"),
                DispatchOutcome(target_ref="o/r#1", body_fingerprint=fp, outcome="no_pr"),
            ])
        orch = _orch_with_ctx()

        per_product: dict = {}
        cands = await orch._backlog_query_queue(_config(), products_out=per_product)

        assert [c["ref"] for c in cands] == ["o/r#1"]
        assert per_product["viberpit"]["bounced"] == 0

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# The grouping refresh draws from the same pool (no label search)              #
# --------------------------------------------------------------------------- #

def test_refresh_groups_groups_the_queue_pool(tmp_path, monkeypatch):
    async def run():
        await _init(tmp_path, "groups.db")
        await _seed_product("viberpit", "o/r")
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
