"""Backlog Runner ⇄ Codex compatibility acceptance suite (Phase 7, Task 7.2).

The plan gates widening ``BacklogRunner.engine_policy`` past ``claude_only`` on
this suite. It covers the parts of that gate that are decidable without a live
OpenAI account: the prompt contract, the launch preconditions, the
claim → spawn → reconcile → completion/give-up state machine under a Codex
launch, and shared-account concurrency. The one item it deliberately does NOT
cover — a real Codex session landing a real PR with sanitized evidence recorded
— is live-boundary work (constitution III) that remains outstanding. The owner
reviewed that gap and explicitly accepted it to widen the ceiling to
``codex_allowed`` now rather than block on it (#1264); the assertion at the end
of this file is the reviewable record of that decision, not a claim that the
live run happened.

Full-auto grants unattended filesystem and network access. Restrictive modes
remain refused before spending a session.
"""
from __future__ import annotations

from server.tests.test_backlog_runner_loop import (
    isolated_ownership_identity as isolated_ownership_identity,
)

import asyncio

import pytest

from ccswitch.engines import AgentEngine
from server.automations.backlog_prompt import build_implement_prompt
from server.automations.backlog_selection import Batch
from server.automations.engine_policy import (
    EnginePolicy,
    unattended_launch_blocker,
)
from server.db import db_session, init_db
from server.sessions.completion import (
    COMPLETION_MARKER,
    NO_PR_MARKER,
    commit_trailer,
    resolution_from_text,
)

# A batch carries the slug of a product whose dispatch is ON — the queue read is
# policy-scoped, so `_refill` is entitled to refuse anything else.
PRODUCT = "viberpit"
BATCH = Batch(primary="o/r#1", issues=["o/r#1", "o/r#2"], repo="o/r",
              titles={"o/r#1": "one", "o/r#2": "two"}, product=PRODUCT)


def _policies():
    """The loop's ``read_policies`` seam: ``PRODUCT`` dispatches automatically,
    with a per-product ceiling well clear of ``max_concurrent`` so the fleet-wide
    slot budget stays the thing under test."""
    from server.automations.product_policy import effective_policy
    return _some({PRODUCT: effective_policy({"dispatch": "auto",
                                             "max_in_flight": 100})})


# --------------------------------------------------------------------------- #
# 1. Prompt contract — engine-neutral except the attribution trailer.          #
# --------------------------------------------------------------------------- #

def test_the_codex_prompt_carries_the_whole_issue_contract():
    """Scope, repo, branch rule, PR completion workflow and closing keywords must
    all survive the engine switch — the runner's contract is git/gh facts, not
    Claude syntax."""
    prompt = build_implement_prompt(BATCH, "main", engine=AgentEngine.CODEX)
    for required in ("o/r#1", "o/r#2", "`o/r`", "Fixes #1", "Fixes #2",
                     "origin/main", "never a draft", "ready for review",
                     "--squash --auto"):
        assert required in prompt, f"Codex prompt lost {required!r}"


def test_the_codex_prompt_names_no_claude_only_syntax():
    """A Codex session must not be told to co-author as Claude, and must not be
    handed Claude-harness vocabulary (slash commands, skills, hooks, CLAUDE.md)
    that means nothing to it."""
    prompt = build_implement_prompt(BATCH, "main", engine=AgentEngine.CODEX)
    assert commit_trailer(AgentEngine.CODEX) in prompt
    assert commit_trailer(AgentEngine.CLAUDE_CODE) not in prompt
    lowered = prompt.lower()
    for claude_ism in ("claude.md", "anthropic", "subagent", "superpowers",
                       "skill", "hook"):
        assert claude_ism not in lowered, f"Codex prompt leaks {claude_ism!r}"


def test_both_engines_get_the_same_contract_apart_from_the_trailer():
    """One semantic template, two attributions — not two prompt builders."""
    claude = build_implement_prompt(BATCH, "main", engine=AgentEngine.CLAUDE_CODE)
    codex = build_implement_prompt(BATCH, "main", engine=AgentEngine.CODEX)

    def normalize(text: str, engine: AgentEngine) -> str:
        return text.replace(commit_trailer(engine), "TRAILER")

    assert normalize(claude, AgentEngine.CLAUDE_CODE) == \
        normalize(codex, AgentEngine.CODEX)


def test_the_completion_and_no_pr_markers_are_engine_neutral():
    """The loop consumes ONE result contract; the engine adapter owns delivery."""
    assert resolution_from_text(f"done\n{NO_PR_MARKER}") == "not-needed"
    assert resolution_from_text(f"cancelled the work\n{NO_PR_MARKER}") == "cancelled"
    assert resolution_from_text(f"shipped\n{COMPLETION_MARKER}") is None


# --------------------------------------------------------------------------- #
# 2. Launch preconditions — the sandbox finding.                                #
# --------------------------------------------------------------------------- #

def test_full_auto_can_complete_the_automation_contract():
    assert unattended_launch_blocker(AgentEngine.CODEX, "full-auto") is None


@pytest.mark.parametrize("mode", ["interactive", "acceptEdits", "auto", "allowlist", "plan"])
def test_every_sandboxed_mode_is_refused(mode):
    assert unattended_launch_blocker(AgentEngine.CODEX, mode) is not None


def test_bypass_permissions_can_also_complete_the_contract():
    assert unattended_launch_blocker(AgentEngine.CODEX, "bypassPermissions") is None


def test_claude_automations_are_unaffected_by_the_precondition():
    """Codex isolation must not regress the engine that works today."""
    for mode in ("full-auto", "acceptEdits", "interactive", "bypassPermissions"):
        assert unattended_launch_blocker(AgentEngine.CLAUDE_CODE, mode) is None


def test_an_unmappable_mode_is_refused_rather_than_assumed_fine():
    assert unattended_launch_blocker(AgentEngine.CODEX, "nonesuch") is not None


# --------------------------------------------------------------------------- #
# 3. The spawn path: a refused launch must cost nothing and claim nothing.     #
# --------------------------------------------------------------------------- #

async def _fake_create_session(*, origin=None, origin_ref=None, **kw):
    from server.db.models import Session
    async with db_session() as db:
        s = Session(id=f"sess-{origin_ref}", name="auto", account_name="a",
                    cwd="/tmp", status="working", origin=origin,
                    origin_ref=origin_ref)
        db.add(s)
    s.launch_kwargs = kw  # type: ignore[attr-defined]
    return s


async def _codex_runner_ctx(tmp_path, db_name, monkeypatch, *, mode="full-auto"):
    from server.automations.context import AutomationContext
    from server.automations.registry import registry
    from server.db.models import Automation

    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / db_name}")
    async with db_session() as db:
        db.add(Automation(id="backlog-runner", enabled=True, mode="auto",
                          config={"engine": "codex"}))
    monkeypatch.setattr(registry.get("backlog-runner"), "engine_policy",
                        EnginePolicy.CODEX_ALLOWED, raising=False)
    monkeypatch.setenv("CCSWITCH_CODEX_AUTOMATIONS_ENABLED", "true")
    ctx = AutomationContext("backlog-runner", create_session=_fake_create_session)
    return ctx


def test_a_codex_spawn_under_full_auto_proceeds_without_escalation(tmp_path, monkeypatch):
    async def run():
        ctx = await _codex_runner_ctx(tmp_path, "cx_a.db", monkeypatch)
        rec = await ctx.spawn_session(origin_ref="o/r#1", name="backlog o/r#1")
        assert rec is not None
        assert rec.launch_kwargs["permission_mode"] == "full-auto"
        kinds = [e["kind"] for e in await ctx.list_events()]
        assert "engine_launch_refused" not in kinds

    asyncio.run(run())


def test_a_codex_spawn_with_an_egress_capable_mode_proceeds(tmp_path, monkeypatch):
    """The precondition is a gate, not a ban: an operator who grants the mode the
    contract needs still gets a Codex session."""

    async def run():
        ctx = await _codex_runner_ctx(tmp_path, "cx_b.db", monkeypatch)
        rec = await ctx.spawn_session(origin_ref="o/r#1", name="backlog o/r#1",
                                      permission_mode="bypassPermissions")
        assert rec is not None
        assert rec.launch_kwargs["engine"] is AgentEngine.CODEX
        assert rec.launch_kwargs["permission_mode"] == "bypassPermissions"

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["interactive", "acceptEdits", "auto", "allowlist", "plan"])
def test_a_codex_spawn_with_a_restrictive_mode_stays_refused(tmp_path, monkeypatch, mode):
    async def run():
        ctx = await _codex_runner_ctx(tmp_path, "cx_f.db", monkeypatch)
        rec = await ctx.spawn_session(origin_ref="o/r#1", name="backlog o/r#1",
                                      permission_mode=mode)
        assert rec is None
        kinds = [e["kind"] for e in await ctx.list_events()]
        assert "engine_launch_refused" in kinds
    asyncio.run(run())


def test_a_claude_spawn_is_unchanged_by_the_precondition(tmp_path, monkeypatch):
    async def run():
        from server.automations.context import AutomationContext
        from server.db.models import Automation

        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'cx_c.db'}")
        async with db_session() as db:
            db.add(Automation(id="backlog-runner", enabled=True, mode="auto"))
        ctx = AutomationContext("backlog-runner",
                                create_session=_fake_create_session)
        rec = await ctx.spawn_session(origin_ref="o/r#1", name="backlog o/r#1")
        assert rec is not None
        assert rec.launch_kwargs["engine"] is AgentEngine.CLAUDE_CODE
        assert rec.launch_kwargs["permission_mode"] == "full-auto"

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# 4. The loop's state machine under a refused / failed Codex launch.           #
# --------------------------------------------------------------------------- #

def _loop(ctx, *, spawn, **overrides):
    """A BacklogRunnerLoop with every seam stubbed except the ones under test."""
    from server.automations.backlog_runner import BacklogRunnerLoop

    clock = overrides.pop("clock", lambda: 1000.0)
    announced: list[str] = []
    escalated: list[str] = []

    async def _announce(batch):
        announced.append(batch.primary)

    async def _needs_human(target, _reason):
        escalated.append(target)

    async def admission(batch, **_kwargs):
        from types import SimpleNamespace
        policies = await _policies()
        return (SimpleNamespace(permitted=True, product_slug=batch.product,
                                configured_dispatch=policies[batch.product]["dispatch"]),
                policies[batch.product])

    seams = dict(
        admission=admission,        select_batch=overrides.pop("select_batch", lambda _c: _some(BATCH)),
        announce=overrides.pop("announce", _announce),
        spawn=spawn,
        count_in_flight=lambda: _some(0),
        healthy_account=overrides.pop("healthy_account", lambda: _some("acct")),
        pr_status_for=lambda _t: _some(None),
        pr_doctor_active=lambda _r: _some(False),
        read_config=overrides.pop("read_config", lambda: _some(("on", _CONFIG))),
        read_policies=overrides.pop("read_policies", _policies),
        needs_human=_needs_human,
        clock=clock,
    )
    seams.update(overrides)
    loop = BacklogRunnerLoop(ctx, **seams)
    return loop, announced, escalated


_CONFIG = {"max_concurrent": 1, "max_spawns_per_day": 10}


def _some(value):
    async def _coro():
        return value
    return _coro()


def test_a_refused_launch_releases_the_claim_instead_of_holding_a_slot(
        tmp_path, monkeypatch):
    """A spawn that returns ``None`` (policy or precondition refusal) used to be
    recorded as a successful spawn with a null session id: the claim row occupied
    a concurrency slot and the sweep could later escalate it to needs-human on the
    evidence of a session that never existed. The claim IS the row (Phase D), so
    releasing it means deleting it."""

    async def run():
        ctx = await _codex_runner_ctx(tmp_path, "cx_d.db", monkeypatch)

        async def refusing_spawn(_batch, _account, **_kw):
            return None

        loop, announced, escalated = _loop(ctx, spawn=refusing_spawn)
        await loop.tick()

        assert announced == ["o/r#1"], "the claim was announced before the spawn"
        assert escalated == [], "a refusal is not a needs-human escalation"
        assert await ctx.list_target_states() == {}, "no slot may be held"

    asyncio.run(run())


def test_a_refused_launch_does_not_burn_the_daily_spawn_cap(tmp_path, monkeypatch):
    async def run():
        ctx = await _codex_runner_ctx(tmp_path, "cx_e.db", monkeypatch)

        async def refusing_spawn(_batch, _account, **_kw):
            return None

        loop, _announced, _escalated = _loop(ctx, spawn=refusing_spawn)
        await loop.tick()
        assert loop._spawns_today == []

    asyncio.run(run())


def test_a_successful_codex_spawn_records_the_session_and_holds_the_slot(
        tmp_path, monkeypatch):
    async def run():
        ctx = await _codex_runner_ctx(tmp_path, "cx_f.db", monkeypatch)

        class Rec:
            id = "sess-1"
            branch = "feat/x"

        async def ok_spawn(_batch, _account, **_kw):
            return Rec()

        loop, announced, _escalated = _loop(ctx, spawn=ok_spawn)
        await loop.tick()
        states = await ctx.list_target_states()
        assert states["o/r#1"]["session_id"] == "sess-1"
        assert states["o/r#1"]["issues"] == ["o/r#1", "o/r#2"]
        assert announced == ["o/r#1"]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# 4b. The two human-triggered spawn paths report a refusal honestly.           #
# --------------------------------------------------------------------------- #

async def _orch_with_refusing_spawn(tmp_path, monkeypatch):
    """Use real durable ownership/state with an external launch refusal."""
    from server.sessions.orchestrator import SessionOrchestrator

    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    ctx = await _codex_runner_ctx(tmp_path, "cx_manual_refusal.db", monkeypatch)

    async def read_config():
        return "on", _CONFIG

    async def announce(_batch):
        return None

    async def account(*_a, **_k):
        return "acct"

    async def spawn(_batch, _account, **_kw):
        return None

    monkeypatch.setattr(orch, "_backlog_ctx", ctx, raising=False)
    monkeypatch.setattr(orch, "_backlog_read_config", read_config, raising=False)
    monkeypatch.setattr(orch, "_backlog_announce", announce, raising=False)
    monkeypatch.setattr(orch, "_automation_account_for", account, raising=False)
    monkeypatch.setattr(orch, "_backlog_spawn", spawn, raising=False)
    monkeypatch.setattr(orch, "_gh_run", lambda *a, **k: "", raising=False)
    from unittest.mock import AsyncMock
    orch._backlog_count_in_flight = AsyncMock(return_value=0)
    orch._backlog_loop = _loop(orch._backlog_ctx, spawn=spawn)[0]
    return orch, ctx


def _queue_of(monkeypatch, orch, candidates: list[dict]) -> None:
    """Pin the runner's candidate pool — the priority-queue read both manual
    entry points resolve repo/title through (spec 003 §7a)."""
    async def _queue(_config, *, skipped_out=None):
        return list(candidates)

    monkeypatch.setattr(orch, "_backlog_query_queue", _queue, raising=False)


def test_the_propose_tap_reports_a_refusal_instead_of_a_phantom_spawn(tmp_path, monkeypatch):
    """A refused tap must report refusal and release its durable batch claim."""
    async def run():
        orch, ctx = await _orch_with_refusing_spawn(tmp_path, monkeypatch)
        _queue_of(monkeypatch, orch, [{"ref": "o/r#1", "repo": "o/r", "title": "one"}])
        status = await orch.backlog_runner_implement_core("o/r#1")
        assert status == "refused"
        assert await ctx.list_target_states() == {}, "a refused tap must not retain its claim"
        assert orch._backlog_loop._spawns_today == []
        from sqlalchemy import select
        from server.db.models import AutomationOwnedTarget
        async with db_session() as db:
            assert list(await db.scalars(select(AutomationOwnedTarget).where(
                AutomationOwnedTarget.operation_token.is_not(None)))) == []
    asyncio.run(run())


def test_manual_dispatch_reports_a_refusal_instead_of_a_phantom_spawn(tmp_path, monkeypatch):
    async def run():
        orch, ctx = await _orch_with_refusing_spawn(tmp_path, monkeypatch)
        _queue_of(monkeypatch, orch, [{"ref": "o/r#1", "repo": "o/r", "title": "one"}])
        out = await orch.backlog_runner_dispatch(["o/r#1"])
        assert out["status"] == "refused"
        assert out["session_id"] is None
        assert await ctx.list_target_states() == {}
        assert orch._backlog_loop._spawns_today == []
        from sqlalchemy import select
        from server.db.models import AutomationOwnedTarget
        async with db_session() as db:
            assert list(await db.scalars(select(AutomationOwnedTarget).where(
                AutomationOwnedTarget.operation_token.is_not(None)))) == []
    asyncio.run(run())


def test_the_dashboard_has_a_message_for_a_refused_action():
    """An unmapped status renders the generic "Action failed" — accurate but
    useless. A refusal has a specific, actionable cause."""
    from server.automations.action_providers import _BACKLOG_STATUS_MAP

    assert "refused" in _BACKLOG_STATUS_MAP


# --------------------------------------------------------------------------- #
# 5. Shared-account concurrency: two runners, one OpenAI account.              #
# --------------------------------------------------------------------------- #

def test_two_runners_on_one_account_cannot_claim_the_same_batch(tmp_path, monkeypatch):
    """OpenAI allows one managed auth home per machine, so the same account
    legitimately serves sessions on several runners. The collision that must not
    happen is at the CLAIM, and it is engine-independent. Phase D moved the claim
    off the ``in-progress`` label onto the runner's durable target-state row: the
    priority queue excludes every ref a claim row already holds
    (``_backlog_query_queue``), so the second runner is handed no batch at all and
    spawns nothing rather than opening a second PR."""

    async def run():
        ctx = await _codex_runner_ctx(tmp_path, "cx_g.db", monkeypatch)
        spawned: list[str] = []

        class Rec:
            id = "sess-1"
            branch = None

        async def ok_spawn(batch, _account, **_kw):
            spawned.append(batch.primary)
            return Rec()

        async def claimed_aware_select(_config):
            # What the real queue does: a ref any claim row holds is not a candidate.
            states = await ctx.list_target_states()
            held = {ref for target, st in states.items() if st.get("claimed")
                    for ref in (st.get("issues") or [target])}
            return None if BATCH.primary in held else BATCH

        # The first runner claims the batch (durable row), then the second runs.
        first, _a, _e = _loop(ctx, spawn=ok_spawn, select_batch=claimed_aware_select)
        await first.tick()
        assert spawned == ["o/r#1"]

        second, _a2, _e2 = _loop(ctx, spawn=ok_spawn, select_batch=claimed_aware_select)
        await second.tick()
        assert spawned == ["o/r#1"], "the runner that lost the claim must not spawn"

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# 6. The gate itself.                                                          #
# --------------------------------------------------------------------------- #

def test_the_backlog_runner_ceiling_is_codex_allowed():
    """Widened by the owner (#1264): everything decidable offline passes, and the
    remaining live-boundary item — a real Codex session landing a real PR with
    sanitized evidence recorded — was explicitly accepted as outstanding rather
    than blocking the ceiling.

    The global automation kill switch and each origin’s engine opt-in remain off.
    """
    from server.automations.registry import registry

    assert registry.get("backlog-runner").engine_policy is EnginePolicy.CODEX_ALLOWED
