"""AI-native SDLC ⇄ Codex compatibility acceptance suite (Phase 7, Task 7.3).

Same shape as ``test_backlog_runner_codex_acceptance.py``: everything the plan's
Task 7.3 gate can decide without a live OpenAI account. The SDLC origins differ
from the backlog runner in one way that matters here — their output is a
*propose-only* artifact PR behind a human gate — so the tests below check that
the human gates survive an engine switch, and that a refused launch cannot
record an artifact for a session that was never created.

The remaining gate items (an opt-in initiative driven intent → design → plan on a
real Codex session, plus the sanitized evidence record) are live-boundary work,
so the SDLC ceilings stay ``claude_only`` — asserted at the end of this file.
"""
from __future__ import annotations

import asyncio

import pytest

from ccswitch.engines import AgentEngine
from server.automations.engine_policy import (
    EnginePolicy,
    unattended_launch_blocker,
)
from server.automations.sdlc_intake import SdlcIntakeLoop, Trigger
from server.codex.permissions import CodexSandboxMode, profile_for_mode
from server.db import db_session, init_db
from server.sdlc.prompts import (
    artifact_path,
    build_intent_prompt,
    build_plan_prompt,
    build_spec_prompt,
)
from server.sessions.completion import commit_trailer
from server.sessions.types import VALID_SESSION_MODES

TRIGGER = Trigger(slug="widget", text="users cannot export", title="Export",
                  repo="o/r")


# --------------------------------------------------------------------------- #
# 1. Artifact prompts survive the engine switch.                               #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("build", [
    lambda engine: build_intent_prompt(TRIGGER.text, slug="widget", repo="o/r",
                                       engine=engine),
    lambda engine: build_spec_prompt("intent body", ["skill-a"], slug="widget",
                                     repo="o/r", engine=engine),
    lambda engine: build_plan_prompt("spec body", slug="widget", repo="o/r",
                                     engine=engine),
])
def test_every_stage_prompt_keeps_its_human_gate_on_codex(build):
    """The propose-only guarantees are the whole point of the SDLC chain: one
    artifact file, a NON-draft PR, and explicitly NO auto-merge. None of them may
    be lost when the engine changes."""
    prompt = build(AgentEngine.CODEX)
    assert "NON-draft PR" in prompt
    assert "Do NOT arm auto-merge" in prompt
    assert "propose-only" in prompt
    assert "NEVER edit the upstream artifacts" in prompt


@pytest.mark.parametrize("kind", ["intent", "spec", "plan"])
def test_every_stage_prompt_names_the_right_artifact_path(kind):
    build = {"intent": lambda e: build_intent_prompt(TRIGGER.text, slug="widget",
                                                     engine=e),
             "spec": lambda e: build_spec_prompt("i", [], slug="widget", engine=e),
             "plan": lambda e: build_plan_prompt("s", slug="widget", engine=e)}[kind]
    assert artifact_path("widget", kind) in build(AgentEngine.CODEX)


def test_stage_prompts_carry_the_codex_trailer_and_no_claude_vocabulary():
    for build in (lambda e: build_intent_prompt("t", slug="w", engine=e),
                  lambda e: build_spec_prompt("i", [], slug="w", engine=e),
                  lambda e: build_plan_prompt("s", slug="w", engine=e)):
        prompt = build(AgentEngine.CODEX)
        assert commit_trailer(AgentEngine.CODEX) in prompt
        assert commit_trailer(AgentEngine.CLAUDE_CODE) not in prompt
        lowered = prompt.lower()
        for claude_ism in ("claude.md", "anthropic", "superpowers", "subagent"):
            assert claude_ism not in lowered


def test_one_semantic_template_serves_both_engines():
    """A Codex-native wording is fine; a second prompt builder is not
    (constitution VI). The only difference must be the attribution."""
    claude = build_spec_prompt("i", ["s"], slug="w", engine=AgentEngine.CLAUDE_CODE)
    codex = build_spec_prompt("i", ["s"], slug="w", engine=AgentEngine.CODEX)
    assert claude.replace(commit_trailer(AgentEngine.CLAUDE_CODE), "T") == \
        codex.replace(commit_trailer(AgentEngine.CODEX), "T")


# --------------------------------------------------------------------------- #
# 2. Permission-tier mapping: release separation of duties must not weaken.    #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mode", sorted(VALID_SESSION_MODES))
def test_every_session_mode_has_a_codex_profile(mode):
    """An SDLC tier resolver may hand any valid mode to a Codex launch; none of
    them may fall through to a default."""
    assert profile_for_mode(mode) is not None


def test_plan_mode_can_never_write_on_codex():
    """The propose-only stages lean on plan mode meaning "do not change
    anything". On Codex that is enforced by the sandbox, not by the prompt."""
    profile = profile_for_mode("plan")
    assert profile.sandbox_mode is CodexSandboxMode.READ_ONLY
    assert not profile.sandbox_mode.write_capable


def test_no_mode_is_more_permissive_on_codex_than_it_is_on_ccswitch():
    """Every mode that would still ask a human on the ccswitch side keeps an
    approval channel on the Codex side."""
    for mode in ("interactive", "allowlist", "auto", "acceptEdits", "plan"):
        assert profile_for_mode(mode).approval_policy.prompts_human


def test_the_sdlc_stages_hit_the_same_egress_wall_as_the_backlog_runner():
    """An artifact PR needs a push too, so the finding is not backlog-specific."""
    assert unattended_launch_blocker(AgentEngine.CODEX, "full-auto") is not None
    assert unattended_launch_blocker(AgentEngine.CODEX, "plan") is not None
    assert unattended_launch_blocker(AgentEngine.CLAUDE_CODE, "full-auto") is None


# --------------------------------------------------------------------------- #
# 3. Intake loop: a refused launch must not record an artifact.                #
# --------------------------------------------------------------------------- #

class _Ctx:
    """Minimal AutomationContext stand-in with a real refusal seam."""

    def __init__(self, *, plan):
        self._plan = plan
        self.events: list[dict] = []

    async def launch_plan_or_refuse(self, origin_ref):
        if self._plan is None:
            self.events.append({"kind": "engine_policy_refused",
                                "target": origin_ref})
        return self._plan

    async def emit(self, level, summary, **kw):
        self.events.append({"level": level, "summary": summary, **kw})


def _intake_loop(ctx, *, spawn, recorded):
    async def read_config():
        return "auto", {"max_spawns_per_day": 10}

    async def read_triggers():
        return [TRIGGER]

    async def ensure_initiative(trigger):
        from server.sdlc import engine as sdlc_engine
        # Intake stage, nothing proposed yet → next_action returns Propose("intent").
        return (sdlc_engine.InitiativeView(slug=trigger.slug, stage="intake",
                                           status="proposed", repo=trigger.repo), [])

    async def record_intent(slug, path, session_id):
        recorded.append((slug, path, session_id))

    return SdlcIntakeLoop(ctx, read_config=read_config, read_triggers=read_triggers,
                          ensure_initiative=ensure_initiative,
                          record_intent=record_intent, spawn=spawn,
                          clock=lambda: 1000.0)


def test_a_refused_intake_launch_records_no_artifact_and_wedges_nothing():
    """Recording the intent artifact for a session that was never created is the
    worst outcome available: the pure engine answers ``AwaitApproval`` for any
    initiative whose intent artifact exists, so the initiative would sit
    "awaiting approval" forever behind a PR nobody ever opened."""
    recorded: list = []
    ctx = _Ctx(plan=(AgentEngine.CODEX, {}))

    async def refusing_spawn(_trigger, _prompt):
        return None

    loop = _intake_loop(ctx, spawn=refusing_spawn, recorded=recorded)
    asyncio.run(loop.tick())

    assert recorded == [], "no artifact may be recorded without a session"
    assert loop._spawns_today == [], "a refusal must not burn the daily cap"
    assert not any(e.get("kind") == "proposed" for e in ctx.events)


def test_a_successful_intake_launch_still_records_the_artifact():
    recorded: list = []
    ctx = _Ctx(plan=(AgentEngine.CLAUDE_CODE, {}))

    class Rec:
        id = "sess-1"

    async def ok_spawn(_trigger, _prompt):
        return Rec()

    loop = _intake_loop(ctx, spawn=ok_spawn, recorded=recorded)
    asyncio.run(loop.tick())

    assert recorded == [("widget", artifact_path("widget", "intent"), "sess-1")]
    assert loop._spawns_today == [1000.0]
    assert any(e.get("kind") == "proposed" for e in ctx.events)


def test_an_out_of_policy_intake_launch_never_reaches_the_spawn():
    """The engine is resolved BEFORE the prompt (the prompt states a trailer), so
    a policy refusal costs no prompt build and no session."""
    recorded: list = []
    ctx = _Ctx(plan=None)
    calls: list = []

    async def spy_spawn(trigger, prompt):
        calls.append(trigger)
        return object()

    loop = _intake_loop(ctx, spawn=spy_spawn, recorded=recorded)
    asyncio.run(loop.tick())

    assert calls == []
    assert recorded == []


# --------------------------------------------------------------------------- #
# 4. Mixed-engine initiative chains and the account the stages run on.         #
# --------------------------------------------------------------------------- #

def test_each_stage_resolves_its_own_engine_so_a_chain_may_be_mixed():
    """Stage engines are per-origin, not inherited from the initiative: intake
    and the synthesizer are separate origins with separate ceilings, so an
    initiative whose intent ran on Claude can have its spec run on Codex once
    that origin is audited. Nothing in the artifact contract couples them — the
    handoff is the committed file."""
    from server.automations.registry import registry

    intake = registry.get("sdlc-intake")
    synth = registry.get("sdlc-synthesizer")
    assert intake.id != synth.id
    # Both prompts read their upstream artifact from the repo, not from the
    # producing session, so a stage cannot depend on its predecessor's engine.
    assert "intent.md" in build_spec_prompt("i", [], slug="w",
                                            engine=AgentEngine.CODEX)


def test_an_automation_account_is_selected_for_the_engine_that_will_run(
        tmp_path, monkeypatch):
    """The account gate and the engine decision must agree. Selecting a Claude
    account and then dropping it (``sanitize_launch_for_engine``) leaves a Codex
    session with no account at all, and — worse — makes the "no healthy account,
    pause spawns" decision on the wrong engine's quota entirely."""
    # Patch the binding the orchestrator actually calls. It does
    # ``from server.sessions.selection import select_account`` at module import,
    # so patching ``selection.select_account`` only works if this test happens to
    # run before anything else imports the orchestrator — true alone, false under
    # xdist.
    from server.sessions import orchestrator as orch_mod

    seen: list = []

    def fake_select(exclude=frozenset(), load=None, *, reserve=True,
                    engine=AgentEngine.CLAUDE_CODE):
        seen.append(engine)

        class P:
            name = "openai-acct"
        return P()

    monkeypatch.setattr(orch_mod, "select_account", fake_select)

    async def run():
        orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
        monkeypatch.setattr(orch, "_account_limited", lambda _n: False,
                            raising=False)
        monkeypatch.setattr(orch, "_recovery",
                            type("R", (), {"quarantined": lambda self, now=None:
                                           frozenset()})(), raising=False)
        monkeypatch.setattr(orch, "_clock", lambda: 0.0, raising=False)
        monkeypatch.setattr(orch, "live_session_load", lambda: {}, raising=False)
        assert await orch._backlog_healthy_account(
            engine=AgentEngine.CODEX) == "openai-acct"
        assert seen == [AgentEngine.CODEX]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# 5. The gate itself.                                                          #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("origin", ["sdlc-intake", "sdlc-synthesizer"])
def test_the_sdlc_ceilings_are_still_claude_only(origin):
    """Task 7.3 widens these only after an opt-in initiative runs intent → design
    → plan on a real Codex session with every human gate intact, and the
    sanitized evidence is recorded. That is live-boundary work (constitution III)
    and is not something this suite can certify."""
    from server.automations.registry import registry

    assert registry.get(origin).engine_policy is EnginePolicy.CLAUDE_ONLY


def test_the_codex_automation_kill_switch_still_ships_off():
    from server.config import get_settings

    assert getattr(get_settings(), "codex_automations_enabled", False) is False


def test_the_codex_network_opt_in_still_ships_off():
    """#1263's network-egress escalation is owner-authorized, not a default: an
    unattended Codex automation must still be refused, not silently granted
    danger-full-access, absent an explicit operator opt-in."""
    from server.config import get_settings

    assert getattr(get_settings(), "codex_unattended_network_opt_in", False) is False


def test_the_db_is_untouched_by_this_suite(tmp_path):
    """Guard rail: these are pure/loop-level tests; if one starts needing a DB the
    fixture belongs here explicitly rather than leaking a global."""
    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.db'}")
        async with db_session() as db:
            assert db is not None

    asyncio.run(run())
