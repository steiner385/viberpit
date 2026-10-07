"""Phantom claims: a Backlog Runner claim whose holder session is archived,
ended, complete or gone must never consume capacity (incident 2026-10-06).

What happened: the runner's tick raised every minute for 14 h, so its sweep never
settled the claims of runs the completion path had already archived ("archived
completed run …"). Eight of them pinned kindash at its ``max_in_flight`` of 8 and
every KinDash session — interactive ones included — sat in the session queue behind
the product-capacity gate, with free hosts and accounts.

Three layers, one test group each:
  * accounting — a dead-holder claim counts toward NO cap (``claim_liveness``);
  * settle on terminal — archive/close/complete/delete settles the session's claim
    through the runner's own sweep decision, independent of the tick;
  * governor auto-heal — a defect the readout finds is settled, not only reported.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

from server.automations import product_admission as admission
from server.automations.backlog_runner import in_flight_by_product, is_in_flight
from server.automations.claim_liveness import (DEAD_CLAIM_STATUSES, holder_session_id,
                                               mark_dead_claims)
from server.db import db_session, init_db
from server.db.models import Project, ProjectRepo, Session as DbSession
from server.tests.test_backlog_runner_loop import (  # noqa: F401 — autouse fixture
    _seed_row, _set_owned_state, build_loop, isolated_ownership_identity)
from server.tests.test_capacity_governor_loop import _DEFECT, _build_loop, _readout


async def _session(sid: str, status: str) -> None:
    async with db_session() as db:
        db.add(DbSession(id=sid, name=sid, account_name="a", cwd="/tmp", status=status))


async def _product(slug: str = "kindash", repo: str = "o/r", max_in_flight: int = 1) -> None:
    async with db_session() as db:
        db.add(Project(id=f"proj-{slug}", slug=slug, automation_policy={
            "dispatch": "auto", "max_in_flight": max_in_flight}))
        db.add(ProjectRepo(project_id=f"proj-{slug}", repo=repo))


def _claim(sid: str | None, **extra) -> dict:
    return {"product": "kindash", "repo": "o/r", "ownership_token": "tok",
            "session_id": sid, **extra}


# --------------------------------------------------------------------------- #
# Accounting                                                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("status", sorted(DEAD_CLAIM_STATUSES) + [None])
def test_a_dead_holder_claim_counts_toward_no_cap(tmp_path, status):
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'acct.db'}")
        if status is not None:              # None = the row is gone entirely
            await _session("s-dead", status)
        await _session("s-live", "in_progress")
        states = {"o/r#1": _claim("s-dead"), "o/r#2": _claim("s-live")}
        marked = await mark_dead_claims(states)
        assert in_flight_by_product(marked) == {"kindash": 1}
        assert sum(is_in_flight(s) for s in marked.values()) == 1
        # Accounting copies only: the stored rows are never touched, so a marked
        # copy can never be written back through a compare_target_state fence.
        assert states["o/r#1"] == _claim("s-dead")

    asyncio.run(run())


@pytest.mark.parametrize("status", ["queued", "pending_approval", "idle", "done",
                                    "in_progress", "handed_back"])
def test_a_live_or_pending_holder_still_counts(tmp_path, status):
    """`queued`/`pending_approval` hold no runtime slot yet but WILL start, and a
    `done` row can be resumed — all legitimate load (D7's narrower set)."""
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'live.db'}")
        await _session("s1", status)
        marked = await mark_dead_claims({"o/r#1": _claim("s1")})
        assert in_flight_by_product(marked) == {"kindash": 1}

    asyncio.run(run())


def test_rows_with_no_running_holder_are_never_judged():
    """A proposed row, a token-less legacy row and a row with no session yet are
    not phantom load — the marker leaves their accounting exactly as it was."""
    assert holder_session_id(_claim("s1", proposed=True)) is None
    assert holder_session_id({"product": "kindash", "session_id": "s1"}) is None
    assert holder_session_id(_claim(None)) is None
    assert holder_session_id(_claim("s1", handed_off=True)) is None
    assert holder_session_id(_claim("s1")) == "s1"


@pytest.mark.asyncio
async def test_the_product_capacity_gate_ignores_phantom_claims(isolated_db):
    """The incident's gate: ``repo_capacity_status`` is what held every KinDash
    session in the queue (queue_reason=product_capacity) — 8/8 on dead claims."""
    await _product(max_in_flight=1)
    await _session("s-archived", "archived")
    context = AsyncMock()
    context.list_target_states.return_value = {"o/r#1": _claim("s-archived")}
    assert await admission.repo_capacity_status("o/r", context=context) == ("kindash", 0, 1)
    assert await admission.queued_claim_slot_free(
        "o/r", session_id="queued-one", context=context) is True


def test_the_fleet_in_flight_count_ignores_phantom_claims(tmp_path):
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext
    from server.automations.registry import registry

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'fleet.db'}")
        await registry.ensure_rows()
        await _session("s-complete", "complete")
        await _session("s-live", "idle")
        orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
        orch._backlog_ctx = AutomationContext("backlog-runner")
        await orch._backlog_ctx.set_target_state("o/r#1", _claim("s-complete"))
        await orch._backlog_ctx.set_target_state("o/r#2", _claim("s-live"))
        assert await orch._backlog_count_in_flight() == 1

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Settle on terminal                                                          #
# --------------------------------------------------------------------------- #

def test_settle_session_claims_settles_only_that_sessions_claim(tmp_path):
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'settle.db'}")
        await _seed_row()
        loop, _rec, ctx = build_loop(candidates=[])
        # ``_ended`` is the harness's "this session is over" (``_mark_session_ended``).
        for tgt, sid in (("o/r#1", "done-sid"), ("o/r#2", "other-sid")):
            await _set_owned_state(ctx, tgt, {"repo": "o/r", "claimed": True, "attempts": 0,
                                              "issues": [tgt], "session_id": sid,
                                              "_ended": True})
        out = await loop.settle_session_claims("done-sid")
        assert out == {"o/r#1": "settled"}
        assert await ctx.get_target_state("o/r#1") == {}
        assert (await ctx.get_target_state("o/r#2")).get("ownership_token")

    asyncio.run(run())


def test_settle_runs_even_when_the_tick_itself_is_broken(tmp_path):
    """The incident: ``_release_give_ups`` raised on one poison row every tick, so
    the sweep never ran. The tick now sweeps past that failure (#2731), so the
    wedge is modelled as the sweep stage itself failing: whatever breaks the
    tick, the terminal settle must not depend on it."""
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'broken.db'}")
        await _seed_row()
        loop, _rec, ctx = build_loop(candidates=[])
        await _set_owned_state(ctx, "o/r#1", {"repo": "o/r", "claimed": True, "attempts": 0,
                                              "issues": ["o/r#1"], "session_id": "s1",
                                              "_ended": True})
        loop._sweep = AsyncMock(side_effect=RuntimeError(  # type: ignore[method-assign]
            "sweep stage is broken"))
        await loop.tick()                                   # the tick is wedged…
        assert (await ctx.get_target_state("o/r#1")).get("ownership_token")
        assert await loop.settle_session_claims("s1") == {"o/r#1": "settled"}  # …the settle is not
        assert await ctx.get_target_state("o/r#1") == {}

    asyncio.run(run())


def test_settle_reports_a_claim_whose_work_has_not_stopped_as_held(tmp_path):
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'held.db'}")
        await _seed_row()
        loop, _rec, ctx = build_loop(candidates=[])
        loop._work_stopped = AsyncMock(return_value=False)  # type: ignore[method-assign]
        await _set_owned_state(ctx, "o/r#1", {"repo": "o/r", "claimed": True, "attempts": 0,
                                              "issues": ["o/r#1"], "session_id": "s1"})
        assert await loop.settle_session_claims("s1") == {"o/r#1": "held"}
        assert (await ctx.get_target_state("o/r#1")).get("ownership_token")

    asyncio.run(run())


def test_run_completes_runner_archives_it_claim_gone_and_capacity_free(tmp_path):
    """The exact incident sequence through the REAL orchestrator: a runner claim's
    session completes, the completion path archives it — capacity must be free at
    once (accounting) and the claim row itself gone right after (settle), with no
    runner tick in between."""
    from server.automations.claim_liveness import mark_dead_claims as _mark
    from server.automations.registry import registry
    from server.sessions.engine import AgentEngine
    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions.types import SessionRecord
    from server.tests.fakes.fake_factory import fake_sdk_factory

    cwd = tmp_path / "cwd"
    cwd.mkdir()

    async def run():
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
            await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'seq.db'}")
            await registry.ensure_rows()
            await _product(max_in_flight=1)
            async with db_session() as db:
                db.add(DbSession(id="run-1", name="run", account_name="a", cwd=str(cwd),
                                 status="complete", origin="backlog-runner"))
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            orch._records["run-1"] = SessionRecord(
                id="run-1", name="run", account="a", engine=AgentEngine.CLAUDE_CODE,
                engine_session_id="", cols=80, rows=24, cwd=str(cwd), status="complete",
                origin="backlog-runner")
            ctx = orch._backlog_ctx
            await _set_owned_state(ctx, "o/r#1", {"repo": "o/r", "product": "kindash",
                                                  "claimed": True, "attempts": 0,
                                                  "issues": ["o/r#1"], "session_id": "run-1"})
            orch._backlog_loop._pr_status_for = AsyncMock(return_value=None)

            await orch.archive_session("run-1", archived_by="backlog-runner")

            # Accounting: free the instant the row is archived, on every gate.
            assert await orch._backlog_count_in_flight() == 0
            assert await admission.repo_capacity_status("o/r", context=ctx) == ("kindash", 0, 1)
            # Settle: the transition scheduled the runner's own settle for the claim.
            settles = [t for t in orch._bg_tasks
                       if t.get_name() == "backlog-settle-on-terminal-run-1"]
            assert settles, "archive_session must schedule the claim settle"
            assert await settles[0] == {"o/r#1": "settled"}
            assert await ctx.get_target_state("o/r#1") == {}
            assert in_flight_by_product(await _mark(await ctx.list_target_states())) == {}
            await orch.stop()

    asyncio.run(run())


def test_terminal_settle_is_primary_only(monkeypatch):
    """A worker must never drive the runner against the shared DB (#919/#2106);
    its sessions' claims are settled by the primary's governor heal or sweep."""
    import server.sessions.orchestrator as orch_mod

    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    spawned: list[str] = []

    def fake_spawn(coro, *, name=None):
        coro.close()
        spawned.append(name)

    orch._spawn = fake_spawn  # type: ignore[method-assign]
    orch._backlog_loop = AsyncMock()
    monkeypatch.setattr(orch_mod, "drives_fleet_automations", lambda: False)
    orch._settle_claims_on_terminal("s1")
    assert spawned == []
    monkeypatch.setattr(orch_mod, "drives_fleet_automations", lambda: True)
    orch._settle_claims_on_terminal("s1")
    assert spawned == ["backlog-settle-on-terminal-s1"]


# --------------------------------------------------------------------------- #
# Capacity Governor auto-heal                                                 #
# --------------------------------------------------------------------------- #

def _with_heal(outcome: str | Exception):
    loop, ctx, calls, store = _build_loop(readout=_readout(defects=[dict(_DEFECT)]))
    healed: list[set] = []

    async def heal(targets):
        healed.append(set(targets))
        if isinstance(outcome, Exception):
            raise outcome
        return {t: outcome for t in targets}

    loop._heal_defects = heal
    return loop, ctx, healed


@pytest.mark.asyncio
async def test_governor_heals_a_defect_instead_of_only_warning():
    loop, ctx, healed = _with_heal("settled")
    await loop.tick()
    assert healed == [{"o/r#1"}]
    assert ctx.kinds().count("capacity_defect_healed") == 1
    assert "capacity_defect" not in ctx.kinds()
    assert ctx.capacity_counts["defects"] == 0


@pytest.mark.asyncio
async def test_governor_keeps_warning_when_the_claim_cannot_be_settled_yet():
    loop, ctx, _healed = _with_heal("held")
    await loop.tick()
    warns = [e for e in ctx.events if e["kind"] == "capacity_defect"]
    assert len(warns) == 1 and warns[0]["detail"]["heal"] == "held"
    assert "capacity_defect_healed" not in ctx.kinds()


@pytest.mark.asyncio
async def test_a_failed_heal_never_costs_the_plan_write():
    loop, ctx, _healed = _with_heal(RuntimeError("settle boom"))
    await loop.tick()
    assert ctx.kinds().count("capacity_defect") == 1
    assert ctx.capacity_counts["defects"] == 1
