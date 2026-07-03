"""Inferred session goals: the one-shot infer_goal helper and the orchestrator
sweep that maintains Session.inferred_goal as LLM session intelligence."""
from __future__ import annotations

import asyncio

import pytest

from server.sessions.goal_inference import infer_goal
from server.sessions.types import SessionRecord


def _fake_query(reply_text):
    async def gen(*, prompt, options):
        class _Block:
            text = reply_text
        class _Msg:
            content = [_Block()]
        yield _Msg()
    return gen


@pytest.mark.asyncio
async def test_infer_goal_returns_one_line():
    fn = _fake_query("Ship the ReferralReward schema as a merged PR closing #10273.\n")
    g = await infer_goal("ReferralReward schema", [{"role": "user", "text": "implement it"}],
                         config_dir="", cwd="/tmp", query_fn=fn)
    assert g == "Ship the ReferralReward schema as a merged PR closing #10273."


@pytest.mark.asyncio
async def test_infer_goal_empty_reply_is_none():
    g = await infer_goal("n", [], config_dir="", cwd="/tmp", query_fn=_fake_query("   "))
    assert g is None


@pytest.mark.asyncio
async def test_infer_goal_model_failure_is_none():
    async def boom(*, prompt, options):
        raise RuntimeError("down")
        yield  # pragma: no cover
    g = await infer_goal("n", [], config_dir="", cwd="/tmp", query_fn=boom)
    assert g is None


# ---- orchestrator sweep -------------------------------------------------- #

def _bare_orch():
    import server.sessions.orchestrator as orch_mod
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._draining = False
    orch._records = {}
    orch._goal_marker = {}
    return orch


def _rec(**kw):
    base = dict(id="s1", name="Fix login bug", account="a", claude_session_id="",
                cols=80, rows=24, cwd="/tmp", status="done")
    base.update(kw)
    return SessionRecord(**base)


class _Window:
    def __init__(self, total_turns):
        self.events = []
        self.earliest_turn = 0
        self.total_turns = total_turns


def _wire(orch, *, turns=3, inferred="Land the login fix as PR #7"):
    calls = {"infer": 0}

    async def fake_window(sid, *, before=None, limit=20):
        return _Window(turns)
    orch.transcript_window = fake_window

    async def fake_tail(sid, **kw):
        return [{"role": "user", "text": "fix the login bug"}]
    orch._autopilot_transcript_tail = fake_tail

    async def fake_infer(sid):
        calls["infer"] += 1
        return inferred
    orch._infer_goal_for = fake_infer
    return calls


def test_goal_sweep_infers_and_persists(tmp_path):
    async def run():
        from server.db import init_db, db_session
        from server.db.models import Session as DbSession
        from sqlalchemy import select
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'g.db'}")
        async with db_session() as db:
            db.add(DbSession(id="s1", name="Fix login bug", account_name="a", cwd="/tmp"))

        orch = _bare_orch()
        orch._records = {"s1": _rec()}
        calls = _wire(orch)

        await orch._goal_sweep()

        assert calls["infer"] == 1
        assert orch._records["s1"].inferred_goal == "Land the login fix as PR #7"
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            assert row.inferred_goal == "Land the login fix as PR #7"

        # Second sweep with an UNCHANGED transcript: no re-inference (cost gate).
        await orch._goal_sweep()
        assert calls["infer"] == 1
    asyncio.run(run())


def test_goal_sweep_skips_explicit_goal_sessions(tmp_path):
    async def run():
        from server.db import init_db
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'g2.db'}")
        orch = _bare_orch()
        orch._records = {"s1": _rec(active_goal="ship it")}
        calls = _wire(orch)
        await orch._goal_sweep()
        assert calls["infer"] == 0          # explicit goal wins — inferring is wasted
    asyncio.run(run())


def test_goal_sweep_failure_keeps_prior_value(tmp_path):
    async def run():
        from server.db import init_db
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'g3.db'}")
        orch = _bare_orch()
        orch._records = {"s1": _rec(inferred_goal="previous good goal")}
        calls = _wire(orch, inferred=None)   # inference fails/returns None
        await orch._goal_sweep()
        assert calls["infer"] == 1
        assert orch._records["s1"].inferred_goal == "previous good goal"  # never clobbered
    asyncio.run(run())
