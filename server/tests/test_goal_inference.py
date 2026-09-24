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


def test_infer_goal_for_claude_session_uses_verified_helper_config_dir(monkeypatch):
    """A Claude-engine session (including Copilot, which also runs CLAUDE_CODE) must
    take its config dir from the engine-safe, readiness-checked helper picker — not
    compose the session's own account unconditionally. Composing a disabled,
    de-authed or org-denied account spawned a call certain to fail as the opaque
    "Claude Code returned an error result: success" (#1841, sibling of #1817)."""
    import server.sessions.orchestrator as orch_mod

    def _boom(name):
        raise AssertionError("compose() must go through _autopilot_helper_config_dir")
    monkeypatch.setattr(orch_mod, "compose", _boom)

    calls = {}
    async def fake_infer_goal(name, tail, *, config_dir, cwd):
        calls["args"] = (name, config_dir, cwd)
        return "Ship the fix"
    monkeypatch.setattr(
        "server.sessions.goal_inference.infer_goal", fake_infer_goal)

    orch = _bare_orch()
    orch._records = {"s1": _rec(engine=AgentEngine.CLAUDE_CODE, account="a", cwd="/work")}
    orch._autopilot_helper_config_dir = lambda account: f"/helper/{account}"

    async def fake_tail(sid, **kw):
        return [{"role": "user", "text": "fix it"}]
    orch._autopilot_transcript_tail = fake_tail

    async def run():
        return await orch._infer_goal_for("s1")
    result = asyncio.run(run())

    assert result == "Ship the fix"
    assert calls["args"] == ("Fix login bug", "/helper/a", "/work")


def test_infer_goal_for_skips_when_no_helper_account_is_ready(monkeypatch):
    """No live Anthropic account → no call at all (keep the prior goal), rather than
    a call against a dead identity that fails and files an auto-error (#1841)."""
    async def fake_infer_goal(*a, **kw):
        raise AssertionError("infer_goal must not run without a ready helper account")
    monkeypatch.setattr(
        "server.sessions.goal_inference.infer_goal", fake_infer_goal)

    orch = _bare_orch()
    orch._records = {"s1": _rec(engine=AgentEngine.CLAUDE_CODE, account="a", cwd="/work")}
    orch._autopilot_helper_config_dir = lambda account: None

    async def fake_tail(sid, **kw):
        return [{"role": "user", "text": "fix it"}]
    orch._autopilot_transcript_tail = fake_tail

    assert asyncio.run(orch._infer_goal_for("s1")) is None


@pytest.mark.asyncio
async def test_infer_goal_failure_log_carries_kind_and_cli_detail(caplog):
    """The SDK's "error result: success" says nothing; the log must carry the
    classified kind and the CLI's own words (read off the yielded messages) so the
    auto-error is diagnosable without digging out a helper transcript (#1841)."""
    from server.sessions import helper_call

    helper_call.reset_breakers()

    async def denied(*, prompt, options):
        class _Result:
            is_error = True
            errors: list = []
            result = "Your organization has disabled Claude subscription access"
            api_error_status = 403
        yield _Result()
        raise RuntimeError("Claude Code returned an error result: success")

    try:
        with caplog.at_level("WARNING", logger="server.sessions.goal_inference"):
            g = await infer_goal("n", [], config_dir="/cfg/x", cwd="/tmp", query_fn=denied)
    finally:
        helper_call.reset_breakers()
    assert g is None
    [rec] = [r for r in caplog.records if r.name == "server.sessions.goal_inference"]
    msg = rec.getMessage()
    assert "kind=auth_forbidden" in msg
    assert "'Your organization has disabled Claude subscription access'" in msg


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
    calls = {"infer": 0, "window_kwargs": []}

    async def fake_window(sid, *, before=None, limit=20, **kw):
        calls["window_kwargs"].append(kw)
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
        # #1712: the sweep's read is best-effort, so a slow replay must not WARN.
        assert calls["window_kwargs"] == [{"background": True}]
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


@pytest.mark.asyncio
@pytest.mark.parametrize("version, expected_path", [
    ((2, 2, 0), "/test/claude"),
    ((2, 0, 0), None),
    (None, "/test/claude"),
])
async def test_goal_options_probe_off_loop(monkeypatch, version, expected_path):
    """Exercise real option construction and the canonical resolver, without a turn."""
    import threading
    import claude_agent_sdk
    from server.sessions import claude_bin, goal_inference

    loop_thread = threading.get_ident()
    probe_threads = []
    options_seen = []
    monkeypatch.setattr(claude_bin, "_min_version", lambda: (2, 1, 0))
    monkeypatch.setattr(claude_bin.shutil, "which", lambda _: "/test/claude")

    def probe(path):
        probe_threads.append(threading.get_ident())
        return version

    def env(config_dir):
        assert threading.get_ident() == loop_thread
        return {"CLAUDE_CONFIG_DIR": config_dir}

    async def query(*, prompt, options):
        assert threading.get_ident() == loop_thread
        options_seen.append(options)
        async for message in _fake_query("Ship it")(prompt=prompt, options=options):
            yield message

    monkeypatch.setattr(claude_bin, "_probe_version", probe)
    monkeypatch.setattr(goal_inference, "helper_env", env)
    monkeypatch.setattr(claude_agent_sdk, "query", query)

    result = await infer_goal("goal", [], config_dir="/test/config", cwd="/test/repo")

    assert result == "Ship it"
    assert probe_threads and all(t != loop_thread for t in probe_threads)
    assert len(options_seen) == 1
    assert options_seen[0].cli_path == expected_path
    assert options_seen[0].cwd == "/test/repo"
    assert options_seen[0].env == {"CLAUDE_CONFIG_DIR": "/test/config"}


@pytest.mark.asyncio
async def test_injected_goal_query_skips_options_and_probe(monkeypatch):
    from server.sessions import goal_inference

    def unexpected():
        pytest.fail("injected query must not resolve the binary")

    monkeypatch.setattr(goal_inference, "cli_path_kwargs", unexpected)
    assert await infer_goal("goal", [], config_dir="", cwd="/tmp",
                            query_fn=_fake_query("Ship it")) == "Ship it"


@pytest.mark.asyncio
async def test_goal_probe_cancellation_propagates(monkeypatch):
    import threading
    from server.aio import cancel_and_reap
    from server.sessions import goal_inference

    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()

    def probe():
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(timeout=5)
            return {}
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(goal_inference, "cli_path_kwargs", probe)
    monkeypatch.setattr(goal_inference, "helper_env", lambda _: {})
    task = asyncio.create_task(infer_goal("goal", [], config_dir="", cwd="/tmp"))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await cancel_and_reap(task, phase="goal-probe-test")
        await asyncio.wait_for(finished.wait(), timeout=2)
