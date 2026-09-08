"""Inferred session goals: the one-shot infer_goal helper and the orchestrator
sweep that maintains Session.inferred_goal as LLM session intelligence."""
from __future__ import annotations

import asyncio

import pytest

from server.sessions.goal_inference import (
    _INSTRUCTION,
    _SYSTEM_PROMPT,
    build_codex_goal_prompt,
    infer_goal,
    normalize_goal_text,
)
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def test_prompt_targets_terminal_shipped_state():
    """The inferred goal must be the terminal shipped outcome (implemented + PR armed),
    not an intermediate artifact — a design doc / spec / plan is a means, not the goal.
    Guards against reverting to 'the deliverable it is driving toward', which let the
    model report a design-doc step as the end goal (screenshot report, 2026-07)."""
    sys_l = _SYSTEM_PROMPT.lower()
    assert "terminal" in sys_l
    assert "pull request" in sys_l or "pr " in sys_l
    # Intermediate artifacts are explicitly demoted to means, not goal.
    assert "design doc" in sys_l and "means" in sys_l
    # The only escape hatch is an explicitly narrower initial request/title.
    assert "explicitly" in sys_l and "scope" in sys_l
    assert "terminal" in _INSTRUCTION.lower()


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


# ---- shared normalization / Codex prompt builder (#1280) ------------------ #

def test_normalize_goal_text_collapses_whitespace_and_quotes():
    assert normalize_goal_text('  "Ship it\nnow"  ') == "Ship it now"


def test_normalize_goal_text_none_on_empty():
    assert normalize_goal_text("") is None
    assert normalize_goal_text("   ") is None
    assert normalize_goal_text(None) is None


def test_normalize_goal_text_caps_length():
    assert len(normalize_goal_text("x" * 500)) == 200


def test_build_codex_goal_prompt_folds_system_prompt_into_turn_text():
    """Codex threads have no separate system-prompt slot, so the instruction that
    tells the model to state the TERMINAL shipped outcome must travel in the turn
    text itself, not get silently dropped."""
    prompt = build_codex_goal_prompt("Fix login bug", [{"role": "user", "text": "fix it"}])
    assert "terminal" in prompt.lower()
    assert "Fix login bug" in prompt
    assert "fix it" in prompt


# ---- _infer_goal_for engine routing (#1280) -------------------------------- #

def test_infer_goal_for_codex_session_never_touches_claude_compose(monkeypatch):
    """A Codex-engine session must route to the native Codex helper and must never
    call configdir.compose() — that builds an Anthropic CLAUDE_CONFIG_DIR, which is
    meaningless (and potentially destructive) for an OpenAI-only account identity."""
    import server.sessions.orchestrator as orch_mod

    def _boom(name):
        raise AssertionError("compose() must not be called for a Codex-engine session")
    monkeypatch.setattr(orch_mod, "compose", _boom)

    orch = _bare_orch()
    orch._records = {"s1": _rec(engine=AgentEngine.CODEX, account="openai-main")}

    async def fake_tail(sid, **kw):
        return [{"role": "user", "text": "fix it"}]
    orch._autopilot_transcript_tail = fake_tail

    calls = {}
    async def fake_codex(account, name, tail):
        calls["args"] = (account, name, tail)
        return "Ship the fix as a merged PR"
    orch.infer_goal_codex = fake_codex

    async def run():
        return await orch._infer_goal_for("s1")
    result = asyncio.run(run())

    assert result == "Ship the fix as a merged PR"
    assert calls["args"][0] == "openai-main"
    assert calls["args"][1] == "Fix login bug"


def test_infer_goal_for_claude_session_keeps_existing_behavior(monkeypatch):
    """A Claude-engine session (including Copilot, which also runs CLAUDE_CODE) must
    keep composing the Anthropic config dir and calling infer_goal exactly as before."""
    import server.sessions.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "compose", lambda name: f"/cfg/{name}")

    calls = {}
    async def fake_infer_goal(name, tail, *, config_dir, cwd):
        calls["args"] = (name, config_dir, cwd)
        return "Ship the fix"
    monkeypatch.setattr(
        "server.sessions.goal_inference.infer_goal", fake_infer_goal)

    orch = _bare_orch()
    orch._records = {"s1": _rec(engine=AgentEngine.CLAUDE_CODE, account="a", cwd="/work")}

    async def fake_tail(sid, **kw):
        return [{"role": "user", "text": "fix it"}]
    orch._autopilot_transcript_tail = fake_tail

    async def run():
        return await orch._infer_goal_for("s1")
    result = asyncio.run(run())

    assert result == "Ship the fix"
    assert calls["args"] == ("Fix login bug", "/cfg/a", "/work")


# ---- orchestrator sweep -------------------------------------------------- #

def _bare_orch():
    import server.sessions.orchestrator as orch_mod
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._draining = False
    orch._records = {}
    orch._goal_marker = {}
    return orch


def _rec(**kw):
    base = dict(id="s1", name="Fix login bug", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="",
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
