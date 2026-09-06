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

The headline finding is :func:`test_a_full_auto_codex_automation_is_refused`:
Codex's ``full-auto`` profile is network-isolated, so an unattended session
would run to completion and then fail at ``git push``. That is not a Codex bug
and not something the runner can prompt its way around — it is the sandbox — so
the launch path refuses up front instead of spending a session to discover it.
"""
from __future__ import annotations

import asyncio

import pytest

from ccswitch.engines import AgentEngine
from server.automations.backlog_prompt import build_implement_prompt
from server.automations.backlog_selection import Batch
from server.automations.engine_policy import (
    CODEX_NETWORK_OPT_IN_MODE,
    EnginePolicy,
    escalate_for_codex_network_opt_in,
    unattended_launch_blocker,
)
from server.db import db_session, init_db
from server.sessions.completion import (
    COMPLETION_MARKER,
    NO_PR_MARKER,
    commit_trailer,
    resolution_from_text,
)

BATCH = Batch(primary="o/r#1", issues=["o/r#1", "o/r#2"], repo="o/r",
              titles={"o/r#1": "one", "o/r#2": "two"})


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

def test_a_full_auto_codex_automation_is_refused():
    """``full-auto`` is the automation default (see ``context.py``) and maps to
    Codex's network-isolated ``workspace-write`` sandbox. The run would produce a
    branch it can never push, so the launch is refused with a reason an operator
    can act on."""
    blocker = unattended_launch_blocker(AgentEngine.CODEX, "full-auto")
    assert blocker is not None
    assert "network" in blocker.lower()


@pytest.mark.parametrize("mode", ["interactive", "acceptEdits", "auto",
                                  "allowlist", "plan"])
def test_every_other_sandboxed_mode_is_refused_too(mode):
    assert unattended_launch_blocker(AgentEngine.CODEX, mode) is not None


def test_bypass_permissions_is_the_one_mode_that_can_complete_the_contract():
    assert unattended_launch_blocker(AgentEngine.CODEX, "bypassPermissions") is None


def test_claude_automations_are_unaffected_by_the_precondition():
    """Codex isolation must not regress the engine that works today."""
    for mode in ("full-auto", "acceptEdits", "interactive", "bypassPermissions"):
        assert unattended_launch_blocker(AgentEngine.CLAUDE_CODE, mode) is None


def test_an_unmappable_mode_is_refused_rather_than_assumed_fine():
    assert unattended_launch_blocker(AgentEngine.CODEX, "nonesuch") is not None


# --------------------------------------------------------------------------- #
# 2b. The owner-authorized network opt-in (#1263) — narrow by construction.    #
# --------------------------------------------------------------------------- #

def test_the_opt_in_is_a_no_op_when_unset():
    mode, note = escalate_for_codex_network_opt_in(
        AgentEngine.CODEX, "full-auto", network_opt_in=False)
    assert (mode, note) == ("full-auto", None)
    assert unattended_launch_blocker(AgentEngine.CODEX, mode) is not None


def test_the_opt_in_escalates_only_the_full_auto_fallback():
    mode, note = escalate_for_codex_network_opt_in(
        AgentEngine.CODEX, "full-auto", network_opt_in=True)
    assert mode == CODEX_NETWORK_OPT_IN_MODE
    assert note is not None and "full-auto" in note
    assert unattended_launch_blocker(AgentEngine.CODEX, mode) is None


@pytest.mark.parametrize("mode", ["interactive", "acceptEdits", "auto",
                                  "allowlist", "plan"])
def test_the_opt_in_never_overrides_an_explicit_or_pinned_mode(mode):
    """An operator who pinned or explicitly chose a restrictive mode is making a
    deliberate choice; the opt-in only rescues the automation's OWN unattended
    fallback, it must never read as license to override a human's pick."""
    escalated, note = escalate_for_codex_network_opt_in(
        AgentEngine.CODEX, mode, network_opt_in=True)
    assert (escalated, note) == (mode, None)


def test_the_opt_in_does_not_touch_an_already_egress_capable_mode():
    escalated, note = escalate_for_codex_network_opt_in(
        AgentEngine.CODEX, "bypassPermissions", network_opt_in=True)
    assert (escalated, note) == ("bypassPermissions", None)


def test_the_opt_in_is_codex_only():
    escalated, note = escalate_for_codex_network_opt_in(
        AgentEngine.CLAUDE_CODE, "full-auto", network_opt_in=True)
    assert (escalated, note) == ("full-auto", None)


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


def test_a_codex_spawn_under_full_auto_refuses_and_records_why(tmp_path, monkeypatch):
    async def run():
        ctx = await _codex_runner_ctx(tmp_path, "cx_a.db", monkeypatch)
        rec = await ctx.spawn_session(origin_ref="o/r#1", name="backlog o/r#1")
        assert rec is None, "a session that cannot push must not be spawned"
        kinds = [e["kind"] for e in await ctx.list_events()]
        assert "engine_launch_refused" in kinds

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


def test_a_codex_spawn_under_the_network_opt_in_escalates_and_proceeds(
        tmp_path, monkeypatch):
    """With CCSWITCH_CODEX_UNATTENDED_NETWORK_OPT_IN set, the automation's own
    full-auto fallback is escalated rather than refused, and the escalation is
    recorded — not silent."""

    async def run():
        ctx = await _codex_runner_ctx(tmp_path, "cx_e.db", monkeypatch)
        monkeypatch.setenv("CCSWITCH_CODEX_UNATTENDED_NETWORK_OPT_IN", "true")
        rec = await ctx.spawn_session(origin_ref="o/r#1", name="backlog o/r#1")
        assert rec is not None
        assert rec.launch_kwargs["permission_mode"] == "bypassPermissions"
        kinds = [e["kind"] for e in await ctx.list_events()]
        assert "engine_network_opt_in_escalated" in kinds
        assert "engine_launch_refused" not in kinds

    asyncio.run(run())


def test_a_codex_spawn_pinned_to_a_restrictive_mode_stays_refused_under_opt_in(
        tmp_path, monkeypatch):
    """The opt-in must not read as license to override an operator's own pinned
    account default — only the automation's full-auto fallback is in scope."""

    async def run():
        ctx = await _codex_runner_ctx(tmp_path, "cx_f.db", monkeypatch)
        monkeypatch.setenv("CCSWITCH_CODEX_UNATTENDED_NETWORK_OPT_IN", "true")
        rec = await ctx.spawn_session(origin_ref="o/r#1", name="backlog o/r#1",
                                      permission_mode="acceptEdits")
        assert rec is None
        kinds = [e["kind"] for e in await ctx.list_events()]
        assert "engine_launch_refused" in kinds
        assert "engine_network_opt_in_escalated" not in kinds

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
    released: list[str] = []
    escalated: list[str] = []

    async def _release(target):
        released.append(target)

    async def _needs_human(target):
        escalated.append(target)

    seams = dict(
        select_batch=overrides.pop("select_batch", lambda _c: _some(BATCH)),
        claim=overrides.pop("claim", lambda _b: _some(True)),
        spawn=spawn,
        count_in_flight=lambda: _some(0),
        healthy_account=overrides.pop("healthy_account", lambda: _some("acct")),
        pr_status_for=lambda _t: _some(None),
        pr_doctor_active=lambda _r: _some(False),
        read_config=overrides.pop("read_config", lambda: _some(("auto", _CONFIG))),
        release=_release,
        needs_human=_needs_human,
        clock=clock,
    )
    seams.update(overrides)
    loop = BacklogRunnerLoop(ctx, **seams)
    return loop, released, escalated


_CONFIG = {"max_concurrent": 1, "max_spawns_per_day": 10, "max_attempts": 2}


def _some(value):
    async def _coro():
        return value
    return _coro()


def test_a_refused_launch_releases_the_claim_instead_of_holding_a_slot(
        tmp_path, monkeypatch):
    """A spawn that returns ``None`` (policy or precondition refusal) used to be
    recorded as a successful spawn with a null session id: the claim label stayed
    on the issue, the target state occupied a concurrency slot, and the sweep
    could later escalate it to ``needs-human`` on the evidence of a session that
    never existed."""

    async def run():
        ctx = await _codex_runner_ctx(tmp_path, "cx_d.db", monkeypatch)

        async def refusing_spawn(_batch, _account):
            return None

        loop, released, escalated = _loop(ctx, spawn=refusing_spawn)
        await loop.tick()

        assert released == ["o/r#1"], "a refused launch must return the claim"
        assert escalated == [], "a refusal is not a needs-human escalation"
        assert await ctx.list_target_states() == {}, "no slot may be held"

    asyncio.run(run())


def test_a_refused_launch_does_not_burn_the_daily_spawn_cap(tmp_path, monkeypatch):
    async def run():
        ctx = await _codex_runner_ctx(tmp_path, "cx_e.db", monkeypatch)

        async def refusing_spawn(_batch, _account):
            return None

        loop, _released, _escalated = _loop(ctx, spawn=refusing_spawn)
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

        async def ok_spawn(_batch, _account):
            return Rec()

        loop, released, _escalated = _loop(ctx, spawn=ok_spawn)
        await loop.tick()
        states = await ctx.list_target_states()
        assert states["o/r#1"]["session_id"] == "sess-1"
        assert states["o/r#1"]["issues"] == ["o/r#1", "o/r#2"]
        assert released == []

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# 4b. The two human-triggered spawn paths report a refusal honestly.           #
# --------------------------------------------------------------------------- #

def _orch_with_refusing_spawn(monkeypatch):
    """A SessionOrchestrator shell whose ``_backlog_spawn`` refuses, with the
    claim/state seams stubbed — enough to exercise the two tap paths."""
    from server.sessions.orchestrator import SessionOrchestrator

    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    state: dict[str, dict] = {}

    class Ctx:
        async def get_target_state(self, target):
            return state.get(target, {})

        async def set_target_state(self, target, st):
            state[target] = st

        async def delete_target_state(self, target):
            state.pop(target, None)

        async def emit(self, *a, **k):
            return None

    async def read_config():
        return "auto", _CONFIG

    async def claim(_batch):
        return True

    async def account(*_a, **_k):
        return "acct"

    async def spawn(_batch, _account):
        return None

    monkeypatch.setattr(orch, "_backlog_ctx", Ctx(), raising=False)
    monkeypatch.setattr(orch, "_backlog_read_config", read_config, raising=False)
    monkeypatch.setattr(orch, "_backlog_claim", claim, raising=False)
    monkeypatch.setattr(orch, "_automation_account_for", account, raising=False)
    monkeypatch.setattr(orch, "_backlog_spawn", spawn, raising=False)
    monkeypatch.setattr(orch, "_gh_run", lambda *a, **k: "", raising=False)
    return orch, state


def test_the_propose_tap_reports_a_refusal_instead_of_a_phantom_spawn(monkeypatch):
    """``backlog_runner_implement_core`` returned ``"spawned"`` with a null session
    id, so the dashboard said "Spawned implementation session" for a session that
    does not exist — and the claim row stayed behind holding a slot."""
    import server.automations.backlog_github as bg

    orch, state = _orch_with_refusing_spawn(monkeypatch)
    monkeypatch.setattr(bg, "query_candidates",
                        lambda config, *, gh_run: [{"ref": "o/r#1", "repo": "o/r",
                                                    "title": "one"}])
    status = asyncio.run(orch.backlog_runner_implement_core("o/r#1"))
    assert status == "refused"
    assert state == {}, "a refused tap must not leave a claim row behind"


def test_manual_dispatch_reports_a_refusal_instead_of_a_phantom_spawn(monkeypatch):
    import server.automations.backlog_github as bg

    orch, state = _orch_with_refusing_spawn(monkeypatch)
    monkeypatch.setattr(bg, "query_candidates",
                        lambda config, *, gh_run: [{"ref": "o/r#1", "repo": "o/r",
                                                    "title": "one"}])
    out = asyncio.run(orch.backlog_runner_dispatch(["o/r#1"]))
    assert out["status"] == "refused"
    assert out["session_id"] is None
    assert state == {}


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
    happen is at the CLAIM, and it is engine-independent: the loser's claim
    returns False and it spawns nothing rather than opening a second PR."""

    async def run():
        ctx = await _codex_runner_ctx(tmp_path, "cx_g.db", monkeypatch)
        spawned: list[str] = []

        class Rec:
            id = "sess-1"
            branch = None

        async def ok_spawn(batch, _account):
            spawned.append(batch.primary)
            return Rec()

        async def losing_claim(_batch):
            return False

        loop, _r, _e = _loop(ctx, spawn=ok_spawn, claim=losing_claim)
        await loop.tick()
        assert spawned == [], "the runner that lost the claim must not spawn"
        assert await ctx.list_target_states() == {}, "and must not hold a slot"

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# 6. The gate itself.                                                          #
# --------------------------------------------------------------------------- #

def test_the_backlog_runner_ceiling_is_codex_allowed():
    """Widened by the owner (#1264): everything decidable offline passes, and the
    remaining live-boundary item — a real Codex session landing a real PR with
    sanitized evidence recorded — was explicitly accepted as outstanding rather
    than blocking the ceiling.

    Three independent gates still stand between this and an actual live Codex
    launch, none of which this test flips: ``CCSWITCH_CODEX_AUTOMATIONS_ENABLED``
    (global kill switch, default false), the automation's own ``engine`` config
    opt-in (default Claude), and ``CCSWITCH_CODEX_UNATTENDED_NETWORK_OPT_IN``
    (default false — without it, ``unattended_launch_blocker`` still refuses an
    unattended Codex launch outright because ``full-auto`` cannot reach the
    network).
    """
    from server.automations.registry import registry

    assert registry.get("backlog-runner").engine_policy is EnginePolicy.CODEX_ALLOWED
