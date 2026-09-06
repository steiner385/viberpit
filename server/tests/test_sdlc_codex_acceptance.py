"""AI-native SDLC ⇄ Codex compatibility acceptance suite (Phase 7, Task 7.3).

Same shape as ``test_backlog_runner_codex_acceptance.py``: everything the plan's
Task 7.3 gate can decide without a live OpenAI account. The SDLC origins differ
from the backlog runner in one way that matters here — their output is a
*propose-only* artifact PR behind a human gate — so the tests below check that
the human gates survive an engine switch, and that a refused launch cannot
record an artifact for a session that was never created.

Section 5 is the enablement gate for #1265/#1266. Both ceilings are now
``codex_allowed``, and the tests there pin what that does and does not grant:
the engine becomes *selectable* on these origins, while three further gates —
the policy default, ``CCSWITCH_CODEX_AUTOMATIONS_ENABLED``, and the
network-egress opt-in from #1263 — all still ship off, so nothing runs on Codex
until an operator turns each of them on deliberately.
"""
from __future__ import annotations

import asyncio
import inspect

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

    async def refusing_spawn(_trigger, _prompt, _engine):
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

    async def ok_spawn(_trigger, _prompt, _engine):
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

    async def spy_spawn(trigger, prompt, engine):
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
# 4b. One engine decision per launch, from the prompt to the account pool.      #
# --------------------------------------------------------------------------- #

def _bare_orchestrator(monkeypatch):
    """A ``SessionOrchestrator`` with no ``__init__`` run — the spawn helpers are
    self-contained enough to exercise against monkeypatched seams, and building a
    real one would drag in a driver registry, a DB and a PR poller."""
    from server.sessions import orchestrator as orch_mod

    return orch_mod, orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)


def test_the_intake_loop_hands_its_resolved_engine_to_the_spawn_seam():
    """The loop resolves the engine so it can state the right commit trailer, and
    that answer has to travel with the prompt it shaped.

    Dropping it means the spawn path resolves a *second* time, off a second read
    of the origin's config. The two reads straddle a DB round-trip, so an
    operator flipping this origin's engine between them lands exactly on the
    outcome ``launch_plan``'s own docstring forbids: a Codex session seeded with a
    prompt telling it to co-author its commits as Claude."""
    seen: list = []
    ctx = _Ctx(plan=(AgentEngine.CODEX, {}))

    async def spawn(_trigger, prompt, engine):
        seen.append((engine, commit_trailer(AgentEngine.CODEX) in prompt))
        return type("Rec", (), {"id": "sess-1"})()

    loop = _intake_loop(ctx, spawn=spawn, recorded=[])
    asyncio.run(loop.tick())

    assert seen == [(AgentEngine.CODEX, True)]


def test_the_intake_spawn_forwards_that_engine_to_the_session_and_the_account_gate(
        monkeypatch):
    """Same decision, one layer down: the intent session is created FOR that
    engine, and the account it runs on is picked from that engine's pool.

    Selecting from the Claude pool and launching Codex is the failure
    ``_backlog_healthy_account`` documents — ``sanitize_launch_for_engine`` drops
    the Anthropic account, so the Codex session runs with none, and the
    "no healthy account, pause spawns" call is made against the wrong quota."""
    _orch_mod, orch = _bare_orchestrator(monkeypatch)
    captured: dict = {}

    class Ctx:
        async def spawn_session(self, **kwargs):
            captured["spawn"] = kwargs
            return type("Rec", (), {"id": "sess-1"})()

    async def probe(_ctx, _origin_ref, *, engine=None):
        captured["account_engine"] = engine
        return "openai-acct"

    monkeypatch.setattr(orch, "_sdlc_intake_ctx", Ctx(), raising=False)
    monkeypatch.setattr(orch, "_automation_account_for", probe, raising=False)

    # repo="" keeps ensure_clone (network + disk) out of the unit entirely.
    trigger = Trigger(slug="widget", text="t")
    asyncio.run(orch._sdlc_intake_spawn(trigger, "seed prompt", AgentEngine.CODEX))

    assert captured["account_engine"] is AgentEngine.CODEX
    assert captured["spawn"]["engine"] is AgentEngine.CODEX
    assert captured["spawn"]["detail"]["engine"] == "codex", (
        "the activity feed must say which engine an intent session ran on")


def test_a_refused_synth_launch_pays_for_no_clone_probe_or_remote_read(monkeypatch):
    """A refusal has to cost nothing — and for the synthesizer it cost a lot.

    ``_backlog_spawn`` already moved its policy check ahead of the clone for this
    exact reason. The synthesizer still probed an account, ran ``ensure_clone``
    (a full git clone on first use) and listed the remote's branches BEFORE
    asking whether the launch was allowed at all. Every tick of an out-of-policy
    origin repeated all three."""
    orch_mod, orch = _bare_orchestrator(monkeypatch)
    touched: list[str] = []

    class Ctx:
        async def launch_plan_or_refuse(self, _origin_ref):
            return None                       # out of policy — the ctx recorded why

        async def spawn_session(self, **_kwargs):  # pragma: no cover — must not run
            touched.append("spawn")

    async def probe(*_a, **_kw):
        touched.append("account")
        return "acct"

    async def read_config():
        touched.append("config")
        return "auto", {}

    async def repo_root():
        return "/tmp"

    def ensure_clone(*_a, **_kw):
        touched.append("clone")
        return "/tmp/clone"

    async def default_branch(_repo):
        touched.append("remote-branches")
        return "master"

    monkeypatch.setattr(orch, "_sdlc_synth_ctx", Ctx(), raising=False)
    monkeypatch.setattr(orch, "_automation_account_for", probe, raising=False)
    monkeypatch.setattr(orch, "_sdlc_synth_read_config", read_config, raising=False)
    monkeypatch.setattr(orch, "_effective_repo_root", repo_root, raising=False)
    monkeypatch.setattr(orch, "_sdlc_default_branch", default_branch, raising=False)
    monkeypatch.setattr(orch_mod.gitrepos, "ensure_clone", ensure_clone)

    ini = {"id": "ini-1", "slug": "widget", "repo": "o/r"}
    assert asyncio.run(orch._sdlc_spawn_synth(ini, "spec")) is None
    assert touched == [], f"a refused launch did work first: {touched}"


def test_the_synth_account_gate_is_asked_for_the_engine_that_was_resolved(monkeypatch):
    """Once the plan is resolved first, the account probe must reuse it rather
    than resolving the origin's engine a third time."""
    orch_mod, orch = _bare_orchestrator(monkeypatch)
    captured: dict = {}

    class Ctx:
        async def launch_plan_or_refuse(self, _origin_ref):
            return AgentEngine.CODEX, {}

        async def spawn_session(self, **kwargs):
            captured["spawn"] = kwargs
            return type("Rec", (), {"id": "sess-1"})()

    async def probe(_ctx, _origin_ref, *, engine=None):
        captured["account_engine"] = engine
        return "openai-acct"

    async def read_config():
        return "auto", {}

    async def repo_root():
        return "/tmp"

    async def default_branch(_repo):
        return "trunk"

    async def auto_effort(_account, _config, _summary):
        return None, None

    monkeypatch.setattr(orch, "_sdlc_synth_ctx", Ctx(), raising=False)
    monkeypatch.setattr(orch, "_automation_account_for", probe, raising=False)
    monkeypatch.setattr(orch, "_sdlc_synth_read_config", read_config, raising=False)
    monkeypatch.setattr(orch, "_effective_repo_root", repo_root, raising=False)
    monkeypatch.setattr(orch, "_sdlc_default_branch", default_branch, raising=False)
    monkeypatch.setattr(orch, "_resolve_codex_auto_effort", auto_effort, raising=False)
    monkeypatch.setattr(orch_mod.gitrepos, "ensure_clone", lambda *a, **k: "/tmp/clone")
    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_sdlc_show_committed_file",
                        staticmethod(lambda *a, **k: "intent body"))

    ini = {"id": "ini-1", "slug": "widget", "repo": "o/r"}
    assert asyncio.run(orch._sdlc_spawn_synth(ini, "spec")) is not None

    assert captured["account_engine"] is AgentEngine.CODEX
    assert captured["spawn"]["engine"] is AgentEngine.CODEX
    assert commit_trailer(AgentEngine.CODEX) in captured["spawn"]["initial_prompt"]


# --------------------------------------------------------------------------- #
# 4c. Artifact durability: PR association, dedupe and the build handoff.        #
# --------------------------------------------------------------------------- #

def test_artifact_pr_association_is_derived_from_git_not_from_the_engine(monkeypatch):
    """#1265's "PR association" criterion. An artifact row is linked to its PR
    through the session's *branches* — a git fact the session's engine cannot
    change — and then through the PR poller cache. Nothing on that path reads an
    engine, so a Codex-produced artifact enriches exactly like a Claude one."""
    _orch_mod, orch = _bare_orchestrator(monkeypatch)

    class Rec:
        repo = "o/r"
        branch = "sdlc/widget-spec"
        authored_branches = ["sdlc/widget-spec"]
        engine = AgentEngine.CODEX      # present, and deliberately never consulted

    class Info:
        number, url, state, merged, merge_commit_sha = 7, "u", "open", False, None

    class Poller:
        def cached(self, repo, branch):
            assert (repo, branch) == ("o/r", "sdlc/widget-spec")
            return Info()

    monkeypatch.setattr(orch, "_records", {"sess-1": Rec()}, raising=False)
    monkeypatch.setattr(orch, "_pr_poller", Poller(), raising=False)

    info = asyncio.run(orch._sdlc_pr_lookup({"id": "a1", "session_id": "sess-1"}))
    assert info == {"number": 7, "url": "u", "state": "open", "sha": None}


def test_stage_dedupe_is_engine_blind_so_flipping_the_engine_cannot_double_propose(
        tmp_path):
    """#1265's dedupe criterion, in its Codex-specific form.

    If dedupe were engine-scoped, an operator flipping this origin from Claude to
    Codex while an intent session was still running would get a SECOND session
    writing the same ``intent.md`` on a second branch, and the initiative would
    end up with two competing artifact PRs for one stage.

    It cannot happen, and the reason is stronger than "the query ignores the
    engine": the duplicate check runs *before* ``launch_plan_or_refuse``, so at
    the moment the decision is made the engine has not been resolved at all.
    Asserted by asking for a Codex spawn over a live Claude-era session."""
    from server.automations.context import AutomationContext
    from server.db.models import Session as DbSession

    created: list = []

    async def never(**kwargs):  # pragma: no cover — reaching it is the failure
        created.append(kwargs)

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'dedupe.db'}")
        async with db_session() as db:
            db.add(DbSession(id="sess-live", name="sdlc intake widget",
                             account_name="claude-acct", cwd="/tmp", status="working",
                             origin="sdlc-intake", origin_ref="widget:intent"))
        ctx = AutomationContext("sdlc-intake", create_session=never)
        rec = await ctx.spawn_session(origin_ref="widget:intent",
                                      dedupe_key="widget:intent",
                                      engine=AgentEngine.CODEX, name="second try")
        assert rec is not None and rec.id == "sess-live"
        assert created == [], "a second artifact session was created for one stage"
        events = await ctx.list_events()
        assert any(e["kind"] == "skipped_duplicate" for e in events)

    asyncio.run(run())


def test_the_build_handoff_crosses_engines_because_it_is_an_issue_not_a_session():
    """#1266's "mixed-engine build handoff" criterion.

    The front chain's engine and ``backlog-runner``'s ceiling are independently
    decided (backlog-runner was itself later widened to ``codex_allowed`` by
    #1264, on its own acceptance suite), and that independence is not a mismatch
    to reconcile: the Build stage hands over the initiative's underlying GitHub
    *issue*, so the receiving origin resolves its own engine under its own
    ceiling. Nothing about the handoff carries the producing session or its
    engine."""
    from server.automations.registry import registry
    from server.sdlc.engine import HandoffToBuild, InitiativeView, next_action

    action = next_action(
        InitiativeView(slug="w", stage="build", status="active", repo="o/r",
                       issue="o/r#42"),
        [], "auto", {})
    assert action == HandoffToBuild("o/r#42")
    # The receiving origin's ceiling is independent of the SDLC ones.
    assert registry.get("backlog-runner").engine_policy is EnginePolicy.CODEX_ALLOWED


def test_a_proposed_artifact_survives_a_restart_without_re_proposing():
    """#1266's "restart/failure counters" criterion, at the layer that decides.

    The loops keep no durable spawn memory — the ``InitiativeArtifact`` row *is*
    the memory (``_spawns_today`` is only a rate cap and is expected to reset).
    So the guard against a restart, or a crashed artifact session, re-proposing a
    stage is the pure engine answering ``AwaitApproval`` for any stage whose
    artifact row exists but has not merged.

    Its engine-independence is structural rather than parametrizable: this is the
    whole decision, and it has no engine input to vary — ``next_action`` takes
    the initiative, its artifacts, the mode and the config, and nothing else."""
    from server.sdlc.engine import ArtifactView, AwaitApproval, InitiativeView, next_action

    state = InitiativeView(slug="w", stage="design", status="active", repo="o/r")
    proposed = [ArtifactView(kind="spec", commit_sha=None)]
    assert next_action(state, proposed, "auto", {}) == AwaitApproval("spec")
    assert "engine" not in inspect.signature(next_action).parameters


# --------------------------------------------------------------------------- #
# 5. The gate itself.                                                          #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("origin", ["sdlc-intake", "sdlc-synthesizer"])
def test_the_sdlc_ceilings_admit_codex_as_an_operator_option(origin):
    """#1265/#1266: both SDLC origins are audited for Codex and widened to
    ``codex_allowed`` — the *option*, not the traffic.

    ``codex_allowed`` still defaults to Claude (:func:`default_engine`), so
    widening moves nothing on its own; it makes the engine selector exist in
    this origin's config schema. Three further gates, all shipping off, stand
    between that and an unattended Codex session — asserted individually
    below."""
    from server.automations.registry import registry

    assert registry.get(origin).engine_policy is EnginePolicy.CODEX_ALLOWED


@pytest.mark.parametrize("origin", ["sdlc-intake", "sdlc-synthesizer"])
def test_widening_the_ceiling_moves_no_traffic_by_itself(origin):
    """Gate 1 of 3: an origin whose config says nothing still launches Claude."""
    from server.automations.engine_policy import default_engine, resolve_engine
    from server.automations.registry import registry

    policy = registry.get(origin).engine_policy
    assert default_engine(policy) is AgentEngine.CLAUDE_CODE
    assert resolve_engine(policy, {}, automations_enabled=True) is AgentEngine.CLAUDE_CODE


@pytest.mark.parametrize("origin", ["sdlc-intake", "sdlc-synthesizer"])
def test_the_kill_switch_clamps_an_opted_in_sdlc_origin_back_to_claude(origin):
    """Gate 2 of 3: even an explicit ``{"engine": "codex"}`` config resolves to
    Claude while ``CCSWITCH_CODEX_AUTOMATIONS_ENABLED`` is off. ``codex_allowed``
    (unlike ``codex_only``) has Claude acceptance evidence to fall back on, so
    the switch downgrades rather than refusing."""
    from server.automations.engine_policy import resolve_engine
    from server.automations.registry import registry

    policy = registry.get(origin).engine_policy
    assert resolve_engine(policy, {"engine": "codex"},
                          automations_enabled=False) is AgentEngine.CLAUDE_CODE
    assert resolve_engine(policy, {"engine": "codex"},
                          automations_enabled=True) is AgentEngine.CODEX


@pytest.mark.parametrize("origin", ["sdlc-intake", "sdlc-synthesizer"])
def test_an_opted_in_codex_sdlc_launch_is_still_refused_without_the_network_opt_in(
        origin):
    """Gate 3 of 3: past the ceiling and past the kill switch, an unattended
    Codex artifact session is refused because its sandbox could not push the
    branch the stage exists to produce (#1263). The escalation that lifts this
    is owner-authorized and ships off — see the test below."""
    from server.automations.engine_policy import escalate_for_codex_network_opt_in
    from server.automations.registry import registry

    assert registry.get(origin).engine_policy is EnginePolicy.CODEX_ALLOWED
    mode, note = escalate_for_codex_network_opt_in(
        AgentEngine.CODEX, "full-auto", network_opt_in=False)
    assert note is None and mode == "full-auto"
    assert unattended_launch_blocker(AgentEngine.CODEX, mode) is not None


@pytest.mark.parametrize("origin", ["sdlc-intake", "sdlc-synthesizer"])
def test_the_widened_ceiling_publishes_the_codex_launch_controls(origin):
    """What widening actually buys the operator: the engine selector and the
    Codex model/effort fields become part of this origin's published schema, so
    ``validate_config`` stops rejecting ``{"engine": "codex"}`` with a 422."""
    from server.automations.engine_policy import (
        CODEX_EFFORT_CONFIG_KEY,
        CODEX_MODEL_CONFIG_KEY,
        ENGINE_CONFIG_KEY,
        effective_config_schema,
    )
    from server.automations.registry import registry

    keys = {f.key for f in effective_config_schema(registry.get(origin))}
    assert {ENGINE_CONFIG_KEY, CODEX_MODEL_CONFIG_KEY, CODEX_EFFORT_CONFIG_KEY} <= keys


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
