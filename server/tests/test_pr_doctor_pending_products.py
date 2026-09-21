"""``_pr_doctor_pending_products`` — product slugs PR Doctor is currently
gating Backlog Runner for (spec 2026-09-21 pr-doctor-priority).

Reuses the ``_build_orch``/file-DB pattern from ``test_pr_doctor_sweep.py``.
"""
from __future__ import annotations

import asyncio

from dataclasses import replace

from server.tests.test_pr_doctor_sweep import (
    _build_orch, _init_pr_doctor_db, _pr, _set_config, _set_mode)


async def _register_repo(repo: str, slug: str) -> None:
    from server.db import db_session
    from server.db.models import Project, ProjectRepo

    async with db_session() as db:
        project = Project(slug=slug, name=slug)
        db.add(project)
        await db.flush()
        db.add(ProjectRepo(project_id=project.id, repo=repo))


async def _init_db(tmp_path) -> None:
    await _init_pr_doctor_db(tmp_path)


def _stuck(number=7):
    return _pr(number=number, checks="failing", merge_state_status="BLOCKED")


def test_a_stuck_pr_pr_doctor_will_fix_gates_its_product(monkeypatch, tmp_path):
    async def run():
        await _init_db(tmp_path)
        await _set_mode(monkeypatch, "auto")
        await _set_config({"auto_fix_failing_ci": True})
        await _register_repo("o/r", "product-a")

        stuck = _pr(checks="failing", merge_state_status="BLOCKED")
        orch = _build_orch(cache={("o/r", "feat/x"): stuck}, pairs=[("o/r", "feat/x")])

        pending = await orch._pr_doctor_pending_products()

        assert pending == frozenset({"product-a"})

    asyncio.run(run())


def test_a_gave_up_target_does_not_gate(monkeypatch, tmp_path):
    async def run():
        await _init_db(tmp_path)
        await _set_mode(monkeypatch, "auto")
        await _set_config({"auto_fix_failing_ci": True})
        await _register_repo("o/r", "product-a")

        stuck = _pr(number=9, checks="failing", merge_state_status="BLOCKED")
        orch = _build_orch(cache={("o/r", "feat/x"): stuck}, pairs=[("o/r", "feat/x")])
        await orch._pr_doctor_ctx.set_target_state("o/r#9", {"gave_up": True})

        pending = await orch._pr_doctor_pending_products()

        assert pending == frozenset()

    asyncio.run(run())


def test_a_healthy_pr_does_not_gate(monkeypatch, tmp_path):
    async def run():
        await _init_db(tmp_path)
        await _set_mode(monkeypatch, "auto")
        await _set_config({"auto_fix_failing_ci": True})
        await _register_repo("o/r", "product-a")

        healthy = _pr(checks="passing", merge_state_status="CLEAN", mergeable="MERGEABLE")
        orch = _build_orch(cache={("o/r", "feat/x"): healthy}, pairs=[("o/r", "feat/x")])

        pending = await orch._pr_doctor_pending_products()

        assert pending == frozenset()

    asyncio.run(run())


def _gate_case(name, mode, config, pr, state=None, expect=frozenset()):
    def test(monkeypatch, tmp_path):
        async def run():
            await _init_db(tmp_path)
            await _set_mode(monkeypatch, mode)
            await _set_config(config)
            await _register_repo("o/r", "product-a")
            orch = _build_orch(cache={("o/r", "feat/x"): pr}, pairs=[("o/r", "feat/x")])
            if state:
                await orch._pr_doctor_ctx.set_target_state(f"o/r#{pr.number}", state)
            assert await orch._pr_doctor_pending_products() == expect

        asyncio.run(run())
    test.__name__ = name
    return test


_ON = {"auto_fix_failing_ci": True}
test_mode_off_does_not_gate = _gate_case(
    "test_mode_off_does_not_gate", "off", _ON, _stuck())
test_mode_propose_does_not_gate = _gate_case(
    "test_mode_propose_does_not_gate", "propose", _ON, _stuck())
test_auto_flag_off_does_not_gate = _gate_case(
    "test_auto_flag_off_does_not_gate", "auto", {"auto_fix_failing_ci": False}, _stuck())
test_blocked_review_gate_does_not_gate = _gate_case(
    "test_blocked_review_gate_does_not_gate", "auto", _ON,
    _pr(checks="passing", merge_state_status="BLOCKED"))
test_attempts_at_ceiling_does_not_gate = _gate_case(
    "test_attempts_at_ceiling_does_not_gate", "auto", {**_ON, "max_attempts": 3},
    _stuck(), state={"total_attempts": 3})
test_auto_with_flag_on_gates = _gate_case(
    "test_auto_with_flag_on_gates", "auto", _ON, _stuck(), expect=frozenset({"product-a"}))


def test_stale_per_branch_cache_does_not_gate_when_population_says_merged(monkeypatch, tmp_path):
    async def run():
        await _init_db(tmp_path)
        await _set_mode(monkeypatch, "auto")
        await _set_config(_ON)
        await _register_repo("o/r", "product-a")
        stale_open = replace(_stuck(number=578), head_ref="feat/x")
        merged_pop = replace(stale_open, state="merged", checks="none", merge_state_status="")
        orch = _build_orch(cache={("o/r", "feat/x"): stale_open}, pairs=[("o/r", "feat/x")],
                           records={}, population={"o/r": (merged_pop,)})

        assert await orch._pr_doctor_pending_products() == frozenset()

    asyncio.run(run())
