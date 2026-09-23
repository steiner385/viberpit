"""#2229: a manual/API create_session against a product already at the Backlog
Runner's own concurrent-session pin queues instead of spawning past it — and,
unlike a phantom drop, the queued row is real (visible via get_session/
list_sessions) and drains once the product frees up."""
from __future__ import annotations

import asyncio

from server.db.models import Automation, AutomationTargetState, Project, ProjectRepo
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.test_session_cap_enqueue import _make_orch, _stop_orch


async def _seed_product_at_cap(repo: str, *, slug: str = "acme") -> None:
    """One registered product/repo with the runner already holding its
    default pin (max_in_flight=1, no policy row configured)."""
    from server.db import db_session
    async with db_session() as db:
        db.add(Automation(id="backlog-runner"))
        db.add(Project(id="proj-1", slug=slug))
        db.add(ProjectRepo(project_id="proj-1", repo=repo))
        db.add(AutomationTargetState(
            automation_id="backlog-runner", target=f"{repo}#1",
            state={"product": slug, "claimed": True}))


async def _make_capacity_orch(*, db_url: str, tmp_path) -> SessionOrchestrator:
    # A high global cap: only the PRODUCT cap should be able to queue in these
    # tests — proves the two mechanisms are independent.
    orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=10)
    # The real Backlog Runner loop starts ticking immediately (orch.start()
    # schedules its first tick with no initial delay) and its sweep reaps any
    # AutomationTargetState with no ownership_token/session_id as a "legacy
    # claim nothing is protecting" (_release_legacy_claim) — exactly the
    # hand-seeded shape these tests use to simulate an in-flight runner claim.
    # Left running, whether that reap wins the race against this test's own
    # assertions is scheduling-dependent (flaky); these tests are about the
    # NEW create_session/drain capacity check, not the runner loop itself, so
    # stop it from ticking again.
    from server.aio import cancel_and_reap
    await cancel_and_reap(getattr(orch, "_backlog_task", None), phase="test-setup")
    return orch


def test_create_session_queues_when_product_at_capacity(tmp_path):
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"
        orch = await _make_capacity_orch(db_url=db_url, tmp_path=tmp_path)
        try:
            await _seed_product_at_cap("acme/widgets")
            rec = await orch.create_session(
                "manual", repo="acme/widgets", initial_prompt="hi",
                cwd=str(tmp_path / "reporoot"))
            assert rec.status == "queued"
            # Real row, not a phantom: visible via get_session and the queue.
            fetched = orch.get_session(rec.id)
            assert fetched is not None and fetched.status == "queued"
            from server.sessions import session_queue as sq
            assert rec.id in await sq.positions()
            assert rec.id not in orch._drivers
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_create_session_spawns_for_unregistered_repo(tmp_path):
    """A repo with no registered product is never gated — most repos aren't
    onboarded products, and this cap must not touch them."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'b.db'}"
        orch = await _make_capacity_orch(db_url=db_url, tmp_path=tmp_path)
        try:
            rec = await orch.create_session(
                "manual", repo="someone/unregistered", initial_prompt="hi",
                cwd=str(tmp_path / "reporoot"))
            assert rec.status != "queued"
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_create_session_spawns_when_product_under_capacity(tmp_path):
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'c.db'}"
        orch = await _make_capacity_orch(db_url=db_url, tmp_path=tmp_path)
        try:
            from server.db import db_session
            async with db_session() as db:
                db.add(Automation(id="backlog-runner"))
                db.add(Project(id="proj-1", slug="acme"))
                db.add(ProjectRepo(project_id="proj-1", repo="acme/widgets"))
            # No claims recorded — 0 in-flight against a pin of 1.
            rec = await orch.create_session(
                "manual", repo="acme/widgets", initial_prompt="hi",
                cwd=str(tmp_path / "reporoot"))
            assert rec.status != "queued"
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_queue_drain_leaves_product_capacity_entry_queued_until_it_clears(tmp_path):
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'd.db'}"
        orch = await _make_capacity_orch(db_url=db_url, tmp_path=tmp_path)
        try:
            await _seed_product_at_cap("acme/widgets")
            rec = await orch.create_session(
                "manual", repo="acme/widgets", initial_prompt="hi",
                cwd=str(tmp_path / "reporoot"))
            assert rec.status == "queued"

            # Global cap has headroom, but the product is still full: draining
            # must leave the entry queued rather than spawning past the pin.
            started = await orch._maybe_start_queued()
            assert started == 0
            still = orch.get_session(rec.id)
            assert still.status == "queued"

            # The runner's claim clears — now the drain may proceed.
            from server.db import db_session
            from sqlalchemy import delete
            async with db_session() as db:
                await db.execute(delete(AutomationTargetState).where(
                    AutomationTargetState.target == "acme/widgets#1"))
            started = await orch._maybe_start_queued()
            assert started == 1
            live = orch.get_session(rec.id)
            assert live.status != "queued"
        finally:
            await _stop_orch(orch)

    asyncio.run(run())
