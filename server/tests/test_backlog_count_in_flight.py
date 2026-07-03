"""Backlog runner in-flight count = active durable target states, not session rows.

A spawned session never reaches a terminal DB status on its own (`ended` is only
set on daemon shutdown), so the old session-row count ("origin=backlog-runner AND
status != 'ended'") never dropped: a finished run kept holding its concurrency
slot forever and the runner stopped spawning new work. The slot is really freed
when the sweep deletes/gives-up the run's TARGET STATE (merge / handoff /
give-up), so in-flight must be counted from active (non-gave_up) target states.
"""
from __future__ import annotations

import asyncio


def _orch_with_ctx():
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._backlog_ctx = AutomationContext("backlog-runner")
    return orch


async def _init(tmp_path):
    from server.db import init_db
    from server.automations.registry import registry
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'cif.db'}")
    await registry.ensure_rows()


def test_counts_active_states_ignores_gave_up(tmp_path):
    async def run():
        await _init(tmp_path)
        orch = _orch_with_ctx()
        ctx = orch._backlog_ctx
        # Two active runs (claimed) + one terminal give-up guard row.
        await ctx.set_target_state("o/r#1", {"claimed": True, "session_id": "s1"})
        await ctx.set_target_state("o/r#2", {"claimed": True, "session_id": "s2"})
        await ctx.set_target_state("o/r#3", {"gave_up": True})

        assert await orch._backlog_count_in_flight() == 2

    asyncio.run(run())


def test_handoff_deleting_state_frees_the_slot(tmp_path):
    async def run():
        await _init(tmp_path)
        orch = _orch_with_ctx()
        ctx = orch._backlog_ctx
        await ctx.set_target_state("o/r#1", {"claimed": True, "session_id": "s1"})
        await ctx.set_target_state("o/r#2", {"claimed": True, "session_id": "s2"})
        assert await orch._backlog_count_in_flight() == 2

        # The sweep hands off #1 (PR armed for auto-merge) → deletes its state.
        # The slot must recycle immediately even though session s1 is still a live,
        # non-'ended' DB row (spawned sessions never auto-terminate).
        await ctx.delete_target_state("o/r#1")
        assert await orch._backlog_count_in_flight() == 1

    asyncio.run(run())


def test_no_states_is_zero(tmp_path):
    async def run():
        await _init(tmp_path)
        orch = _orch_with_ctx()
        assert await orch._backlog_count_in_flight() == 0

    asyncio.run(run())
