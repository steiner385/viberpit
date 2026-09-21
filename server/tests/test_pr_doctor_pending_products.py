"""``_pr_doctor_pending_products`` — product slugs PR Doctor is currently
gating Backlog Runner for (spec 2026-09-21 pr-doctor-priority).

Reuses the ``_build_orch``/file-DB pattern from ``test_pr_doctor_sweep.py``.
"""
from __future__ import annotations

import asyncio

from server.tests.test_pr_doctor_sweep import _build_orch, _pr


async def _register_repo(repo: str, slug: str) -> None:
    from server.db import db_session
    from server.db.models import Project, ProjectRepo

    async with db_session() as db:
        project = Project(slug=slug, name=slug)
        db.add(project)
        await db.flush()
        db.add(ProjectRepo(project_id=project.id, repo=repo))


async def _init_db(tmp_path) -> None:
    from server.db import init_db
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'pending.db'}")


def test_a_classified_stuck_pr_gates_its_product(tmp_path):
    async def run():
        await _init_db(tmp_path)
        await _register_repo("o/r", "product-a")

        stuck = _pr(checks="failing", merge_state_status="BLOCKED")
        orch = _build_orch(cache={("o/r", "feat/x"): stuck}, pairs=[("o/r", "feat/x")])

        pending = await orch._pr_doctor_pending_products()

        assert pending == frozenset({"product-a"})

    asyncio.run(run())


def test_a_gave_up_target_does_not_gate(tmp_path):
    async def run():
        await _init_db(tmp_path)
        await _register_repo("o/r", "product-a")

        stuck = _pr(number=9, checks="failing", merge_state_status="BLOCKED")
        orch = _build_orch(cache={("o/r", "feat/x"): stuck}, pairs=[("o/r", "feat/x")])
        await orch._pr_doctor_ctx.set_target_state("o/r#9", {"gave_up": True})

        pending = await orch._pr_doctor_pending_products()

        assert pending == frozenset()

    asyncio.run(run())


def test_a_healthy_pr_does_not_gate(tmp_path):
    async def run():
        await _init_db(tmp_path)
        await _register_repo("o/r", "product-a")

        healthy = _pr(checks="passing", merge_state_status="CLEAN", mergeable="MERGEABLE")
        orch = _build_orch(cache={("o/r", "feat/x"): healthy}, pairs=[("o/r", "feat/x")])

        pending = await orch._pr_doctor_pending_products()

        assert pending == frozenset()

    asyncio.run(run())
