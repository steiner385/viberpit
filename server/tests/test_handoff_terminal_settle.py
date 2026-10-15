"""#3119: a hand-off claim must settle once its PR is terminal.

Rows found on dobby 2026-10-15, every issue closed:

* ``cairnea/KinDash#15735``/``#15958``/``#16235`` — the merged close-out ran
  (#3109), but ``reserve_target_action`` -> ``pr_accounting.detach`` ->
  ``account()`` refused a PR Doctor mechanical attempt whose outcome was
  ``unknown``. Evidence for that outcome can never arrive for a merged PR.
* ``cairnea/KinDash#17284`` — PR #17286 closed unmerged, both sessions deleted.
  No branch resolved and ``find_referencing_pr`` ignores CLOSED PRs, so the
  sweep saw no PR and the ``handed_off`` row returned early on every tick.
"""
from __future__ import annotations

import asyncio

from server.automations.context import AutomationContext
from server.automations.ownership import owned
from server.db import db_session, init_db
from server.db.models import Automation, AutomationTargetState
from server.tests.test_backlog_runner_loop import (  # noqa: F401 — autouse fixture
    _seed_product, _seed_row, build_loop, isolated_ownership_identity,
)
from server.tests.test_stale_claim_expiry import (
    NOW, _pr, _real_work_stopped, _seed_token_handoff,
)

RESULT = {"attempts": 0, "mechanical_attempts": 1, "last_failure": "stuck_enqueue"}


async def _seed_unknown_mechanical_attempt(target: str) -> str:
    """PR Doctor's row for the handed-off PR plus an unaccounted mechanical
    attempt whose GitHub response never came back (``mechanical_outcome: unknown``)."""
    token = (await AutomationContext("backlog-runner").get_target_state(target))["ownership_token"]
    async with db_session() as db:
        if await db.get(Automation, "pr-doctor") is None:
            db.add(Automation(id="pr-doctor"))
            await db.flush()
        db.add(AutomationTargetState(automation_id="pr-doctor", target="o/r#101",
                                     state={"ownership_token": token}))
        await db.commit()
    async with owned(token) as tx:
        tx.operation.provenance = {
            **tx.operation.provenance, "mechanical_kind": "update_branch",
            "mechanical_outcome": "unknown",
            "pr_attempt": {"kind": "mechanical", "accounted": False,
                           "expected": {"ownership_token": token},
                           "result": {"ownership_token": token, **RESULT}}}
    return token


async def _pr_doctor_row() -> dict:
    return await AutomationContext("pr-doctor").get_target_state("o/r#101")


def test_unknown_mechanical_outcome_settles_on_merged_closeout(tmp_path):
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'term1.db'}")
        await _seed_row()
        await _seed_product()
        loop, rec, ctx = build_loop(candidates=[], wall_clock=lambda: NOW,
                                    close_out_result="closed",
                                    pr_status={"o/r#1": _pr(1, "merged", idle_days=20)})
        await _seed_token_handoff("o/r#1", phase="uncertain")
        await _seed_unknown_mechanical_attempt("o/r#1")
        _real_work_stopped(loop)
        await loop.tick()
        assert [t for t, _ in rec.close_outs] == ["o/r#1"]
        assert await ctx.get_target_state("o/r#1") == {}
        # Counted (it may have landed) and detached: no token left behind.
        assert await _pr_doctor_row() == RESULT

    asyncio.run(run())


def test_unknown_mechanical_outcome_still_blocks_an_open_pr_detach(tmp_path):
    """Without a terminal PR the unknown outcome is still unaccountable."""
    async def run():
        from server.automations.pr_accounting import account, detach
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'term2.db'}")
        await _seed_row()
        await _seed_product()
        await _seed_token_handoff("o/r#1", phase="uncertain")
        token = await _seed_unknown_mechanical_attempt("o/r#1")
        async with owned(token) as tx:
            assert not await account(tx, "o/r#101")
            assert not await detach(tx)
        assert await _pr_doctor_row() == {"ownership_token": token}

    asyncio.run(run())


def test_handoff_with_no_visible_pr_releases_once_its_issue_is_closed(tmp_path):
    """KinDash#17284: the closed PR is invisible to the sweep; a closed issue
    releases the row, an open (or unknown) one keeps it."""
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'term3.db'}")
        await _seed_row()
        await _seed_product()
        states = {"o/r#1": {"issue_state": "closed"}, "o/r#2": {"issue_state": "open"},
                  "o/r#3": {}}
        loop, rec, ctx = build_loop(candidates=[], wall_clock=lambda: NOW,
                                    investigation_state=lambda t, _s: states[t])
        for target in states:
            await _seed_token_handoff(target, phase="uncertain")
        _real_work_stopped(loop)
        kept = {t: await ctx.get_target_state(t) for t in ("o/r#2", "o/r#3")}
        await loop.tick()
        assert await ctx.get_target_state("o/r#1") == {}
        assert {t: await ctx.get_target_state(t) for t in kept} == kept
        events = await ctx.list_events(kind="closed_handoff_released")
        assert [e["target"] for e in events] == ["o/r#1"]
        assert [t for t, _ in rec.retracted] == ["o/r#1"]
        assert not await ctx.list_events(kind="gave_up")

    asyncio.run(run())
