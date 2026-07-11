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


def test_handed_off_rows_do_not_hold_slots(tmp_path):
    """A handed_off row is kept only so the merged close-out can run — PR
    Doctor owns the PR now, so the row must not occupy a concurrency slot
    (or the runner would stall at max_concurrent handed-off rows)."""
    async def run():
        await _init(tmp_path)
        orch = _orch_with_ctx()
        ctx = orch._backlog_ctx
        await ctx.set_target_state("o/r#1", {"claimed": True, "session_id": "s1"})
        await ctx.set_target_state("o/r#2", {"claimed": True, "session_id": "s2",
                                             "handed_off": True})
        assert await orch._backlog_count_in_flight() == 1

    asyncio.run(run())


def test_repo_load_skips_handed_off_rows(tmp_path):
    async def run():
        await _init(tmp_path)
        orch = _orch_with_ctx()
        ctx = orch._backlog_ctx
        await ctx.set_target_state("o/r#1", {"claimed": True, "repo": "o/r"})
        await ctx.set_target_state("o/r#2", {"claimed": True, "repo": "o/r",
                                             "handed_off": True})
        await ctx.set_target_state("o/k#3", {"gave_up": True, "repo": "o/k"})
        assert await orch._backlog_repo_load() == {"o/r": 1}

    asyncio.run(run())


def test_heal_orphans_seam_queries_then_heals(tmp_path):
    """_backlog_heal_orphans = query_claimed_refs → heal_orphan_claims, both
    through the shared _gh_run seam; empty repo scope short-circuits."""
    async def run():
        import json as _json
        import subprocess

        await _init(tmp_path)
        orch = _orch_with_ctx()
        claimed = _json.dumps({"data": {"claimed": {"nodes": [
            {"number": 5, "title": "t", "url": "u",
             "repository": {"nameWithOwner": "o/r"}}]}}})
        crossref = _json.dumps({"data": {"repository": {"issue": {
            "timelineItems": {"nodes": []}}}}})
        procs = iter([
            subprocess.CompletedProcess([], 0, stdout=claimed, stderr=""),
            subprocess.CompletedProcess([], 0, stdout=crossref, stderr=""),
        ])
        calls: list[list[str]] = []

        def gh_run(args, token=None):
            calls.append(list(args))
            if args[:2] == ["api", "graphql"]:
                return next(procs)
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        orch._gh_run = gh_run
        healed = await orch._backlog_heal_orphans(
            {"enabled_repos": ["o/r"]}, set())
        assert healed == [("o/r#5", "reclaimed")]
        # Empty repo scope → no gh call at all.
        n = len(calls)
        assert await orch._backlog_heal_orphans({"enabled_repos": []}, set()) == []
        assert len(calls) == n

    asyncio.run(run())
