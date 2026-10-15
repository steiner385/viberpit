"""#3109: a Backlog Runner claim that nothing can move must lapse.

Each case reproduces a row found holding a KinDash claim on 2026-10-15:

* ``cairnea/KinDash#15256`` — a pre-ownership ``handed_off`` row whose session row
  is gone, its PR (#15268) a conflicting draft nobody touched for weeks. PR Doctor
  never works a draft, and the legacy settle waited for a merge/close that never
  comes, so the queue skipped the issue as ``claimed`` for 41 days.
* ``#15735``/``#15958``/``#16235`` — token-bearing hand-offs whose PR Doctor
  operation went ``uncertain`` before any session was reserved. ``_work_stopped``
  answered False for want of a session id, so even the merged PR never closed out.
* ``#18812`` — a launch that went ``uncertain`` ("session creation did not return
  a confirmed result"): the reserved session never appeared, and the row has no
  ``session_id`` for the orphan release to probe.

The age (``stale_claim_days``) is POLICY consulted only where the ownership
store's own stopped-work evidence already allows the release; it is never the
proof itself (``server/automations/ownership.py``).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from server.automations.backlog_runner import BacklogRunnerLoop
from server.automations.context import AutomationContext
from server.db import init_db
from server.github.pr_status import PullRequestInfo
from server.tests.test_backlog_runner_loop import (  # noqa: F401 — autouse fixture
    DEFAULT_PRODUCT, _seed_product, _seed_row, _set_owned_state, build_loop,
    isolated_ownership_identity,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def _pr(n: int, state: str, *, idle_days: float) -> PullRequestInfo:
    return PullRequestInfo(number=n, state=state, url=f"u/{n}", title="t", checks="none",
                           is_draft=state == "draft",
                           updated_at=(NOW - timedelta(days=idle_days)).isoformat())


def _real_work_stopped(loop: BacklogRunnerLoop) -> None:
    """``build_loop`` stubs ``_work_stopped`` True; these cases need the real proof."""
    del loop._work_stopped


async def _seed_legacy_handoff(ctx, target: str) -> None:
    await ctx.set_target_state(target, {
        "repo": "o/r", "claimed": True, "handed_off": True, "attempts": 0,
        "issues": [target], "session_id": "gone-sid", "branch": None})


async def _seed_token_handoff(target: str, *, phase: str) -> None:
    """A token-bearing hand-off as ``_backlog_handoff`` writes it: the runner's
    operation transferred to PR Doctor, ``session_id`` nulled. *phase* is the
    successor operation's phase; no session is ever reserved on it."""
    from server.automations.ownership import ReleaseProof, owned
    ctx = AutomationContext("backlog-runner")
    await _set_owned_state(ctx, target, {
        "repo": "o/r", "product": DEFAULT_PRODUCT, "claimed": True, "attempts": 0,
        "issues": [target]})
    st = await ctx.get_target_state(target)
    async with owned(st["ownership_token"]) as tx:
        successor = await tx.transfer(
            owner="pr-doctor", source="runner-handoff", proof=ReleaseProof.NOT_STARTED,
            reason="fixture", provenance={"origin_ref": "o/r#101", "runner_target": target})
    async with owned(successor) as tx:
        tx.operation.phase = phase
    await ctx.set_target_state(target, {
        **st, "ownership_token": successor, "handed_off": True, "session_id": None,
        "implementation_session_id": "gone-impl"})


def test_legacy_handoff_whose_pr_went_idle_lapses_past_the_ttl(tmp_path):
    """KinDash#15256: session gone + a draft idle past ``stale_claim_days`` →
    released with an audit event. A draft touched inside the window is kept."""
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'stale1.db'}")
        await _seed_row()
        await _seed_product()

        async def session_gone(_sid):
            return True

        loop, _rec, ctx = build_loop(
            candidates=[], session_gone=session_gone, wall_clock=lambda: NOW,
            investigation_state=lambda t, s: {"issue_state": "open"},
            pr_status={"o/r#1": _pr(1, "draft", idle_days=18),
                       "o/r#2": _pr(2, "draft", idle_days=2)})
        await _seed_legacy_handoff(ctx, "o/r#1")
        await _seed_legacy_handoff(ctx, "o/r#2")
        await loop.tick()
        assert await ctx.get_target_state("o/r#1") == {}
        assert (await ctx.get_target_state("o/r#2")).get("claimed") is True
        events = await ctx.list_events(kind="legacy_claim_released")
        assert [e["target"] for e in events] == ["o/r#1"]

    asyncio.run(run())


def test_zero_ttl_keeps_an_idle_handoff(tmp_path):
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'stale0.db'}")
        await _seed_row()
        await _seed_product()

        async def session_gone(_sid):
            return True

        loop, _rec, ctx = build_loop(
            candidates=[], session_gone=session_gone, wall_clock=lambda: NOW,
            policies={DEFAULT_PRODUCT: {"dispatch": "auto", "stale_claim_days": 0}},
            pr_status={"o/r#1": _pr(1, "open", idle_days=90)})
        await _seed_token_handoff("o/r#1", phase="claimed")
        before = await ctx.get_target_state("o/r#1")
        await loop.tick()
        assert await ctx.get_target_state("o/r#1") == before

    asyncio.run(run())


def test_token_handoff_with_an_active_open_pr_is_kept(tmp_path):
    """PR Doctor still carries an open PR touched inside the window: the claim
    stays, however old the claim itself is."""
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'stale2.db'}")
        await _seed_row()
        await _seed_product()
        loop, _rec, ctx = build_loop(candidates=[], wall_clock=lambda: NOW,
                                     pr_status={"o/r#1": _pr(1, "open", idle_days=1)})
        await _seed_token_handoff("o/r#1", phase="uncertain")
        _real_work_stopped(loop)
        before = await ctx.get_target_state("o/r#1")
        await loop.tick()
        assert await ctx.get_target_state("o/r#1") == before
        assert not await ctx.list_events(kind="stale_claim_released")

    asyncio.run(run())


def test_token_handoff_whose_pr_went_idle_lapses(tmp_path):
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'stale3.db'}")
        await _seed_row()
        await _seed_product()
        loop, rec, ctx = build_loop(candidates=[], wall_clock=lambda: NOW,
                                    pr_status={"o/r#1": _pr(1, "draft", idle_days=30)})
        await _seed_token_handoff("o/r#1", phase="uncertain")
        _real_work_stopped(loop)
        await loop.tick()
        assert await ctx.get_target_state("o/r#1") == {}
        events = await ctx.list_events(kind="stale_claim_released")
        assert [e["target"] for e in events] == ["o/r#1"]
        assert [t for t, _ in rec.retracted] == ["o/r#1"]

    asyncio.run(run())


def test_sessionless_uncertain_handoff_closes_out_its_merged_pr(tmp_path):
    """KinDash#15735/#15958/#16235: no session was ever reserved on the uncertain
    PR Doctor operation, so no native actor can still be running — the merged
    PR's close-out must run instead of waiting on a session id forever."""
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'stale4.db'}")
        await _seed_row()
        await _seed_product()
        loop, rec, ctx = build_loop(candidates=[], wall_clock=lambda: NOW,
                                    close_out_result="closed",
                                    pr_status={"o/r#1": _pr(1, "merged", idle_days=20)})
        await _seed_token_handoff("o/r#1", phase="uncertain")
        _real_work_stopped(loop)
        await loop.tick()
        assert [t for t, _ in rec.close_outs] == ["o/r#1"]
        assert await ctx.get_target_state("o/r#1") == {}

    asyncio.run(run())


async def _seed_unconfirmed_launch(ctx, target: str, *, claimed_days_ago: float) -> str:
    """KinDash#18812: reserve_session ran, creation's result never came back, the
    operation went ``uncertain`` and the row never learned a ``session_id``."""
    from server.automations.ownership import owned
    await _set_owned_state(ctx, target, {
        "repo": "o/r", "product": DEFAULT_PRODUCT, "claimed": True, "attempts": 0,
        "issues": [target],
        "claimed_at": (NOW - timedelta(days=claimed_days_ago)).isoformat()})
    token = (await ctx.get_target_state(target))["ownership_token"]
    async with owned(token) as tx:
        sid = tx.reserve_session()
        tx.operation.execution_generation = 1
        tx.operation.execution_sessions = {sid: {"current": "e1", "executions": {
            "e1": {"generation": 1, "stopped": None}}}}
        tx.mark_uncertain("session creation did not return a confirmed result")
    return sid


def test_unconfirmed_launch_whose_session_never_appeared_lapses_past_the_ttl(tmp_path):
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'stale5.db'}")
        await _seed_row()
        await _seed_product()

        async def absent(_sid):
            return True

        loop, _rec, ctx = build_loop(candidates=[], wall_clock=lambda: NOW,
                                     session_absent=absent)
        await _seed_unconfirmed_launch(ctx, "o/r#1", claimed_days_ago=8)
        await _seed_unconfirmed_launch(ctx, "o/r#2", claimed_days_ago=0.01)
        await loop.tick()
        assert await ctx.get_target_state("o/r#1") == {}
        # Minutes old: creation may still be answering — never race a live launch.
        assert (await ctx.get_target_state("o/r#2")).get("claimed") is True
        events = await ctx.list_events(kind="orphaned_claim_released")
        assert [e["target"] for e in events] == ["o/r#1"]

    asyncio.run(run())


def test_claim_with_a_live_session_is_kept_however_old(tmp_path):
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'stale6.db'}")
        await _seed_row()
        await _seed_product()

        async def alive(_sid):
            return False

        loop, _rec, ctx = build_loop(candidates=[], wall_clock=lambda: NOW,
                                     session_absent=alive, session_gone=alive)
        await _set_owned_state(ctx, "o/r#1", {
            "repo": "o/r", "product": DEFAULT_PRODUCT, "claimed": True, "attempts": 0,
            "issues": ["o/r#1"], "session_id": "live-sid",
            "claimed_at": (NOW - timedelta(days=60)).isoformat()})
        sid = await _seed_unconfirmed_launch(ctx, "o/r#2", claimed_days_ago=60)
        assert sid
        _real_work_stopped(loop)
        before = {t: await ctx.get_target_state(t) for t in ("o/r#1", "o/r#2")}
        await loop.tick()
        assert {t: await ctx.get_target_state(t) for t in before} == before

    asyncio.run(run())
