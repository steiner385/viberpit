"""PR Doctor ⇄ Codex compatibility acceptance suite (Phase 7, #1267).

The plan gates widening ``PrDoctor.engine_policy`` past ``claude_only`` on this
suite. It covers the parts of that gate that are decidable without a live
OpenAI account: the prompt contract (including the commit-trailer wiring added
to ``server/sessions/pr_fix.py`` by this same change), the launch preconditions
shared with every other origin, and the two fresh-spawn call sites
(``_pr_doctor_session_fix``'s no-live-driver branch, ``_pr_doctor_consolidate_spawn``)
resolving engine before their clone/model-assessment tail. The one item it
deliberately does NOT cover — a live Codex session actually restoring or
consolidating a real PR — is live-boundary work (constitution III) that remains
outstanding. The owner reviewed that gap and explicitly accepted it to widen the
ceiling to ``codex_allowed`` now rather than block on it (#1267), the same
decision already made for backlog-runner (#1264) and the SDLC/PM origins.

Distinct from those origins, PR Doctor also has an INJECTION path: a live
session (owning session still attached) gets a fix/conflict prompt injected via
the shared, already engine-neutral ``AgentDriver.send_prompt`` contract rather
than a fresh spawn. That path needs no engine-policy gate of its own — the
engine was decided once, at the live session's own original spawn — but the
prompt it injects must still name the RIGHT engine's commit trailer, which is
what section 1b covers.
"""
from __future__ import annotations

import asyncio

import pytest

from ccswitch.engines import AgentEngine
from server.automations.engine_policy import (
    CODEX_NETWORK_OPT_IN_MODE,
    EnginePolicy,
    escalate_for_codex_network_opt_in,
    unattended_launch_blocker,
)
from server.db import db_session, init_db
from server.github.pr_status import PullRequestInfo
from server.sessions.completion import commit_trailer
from server.sessions.pr_fix import (
    build_conflict_fix_prompt,
    build_consolidation_prompt,
    build_pr_fix_prompt,
)


def _pr(number=7, title="fix the widget", url="https://x/pull/7"):
    return PullRequestInfo(number=number, url=url, state="open", checks="failing",
                           title=title, head_sha="sha1", is_draft=False,
                           mergeable="UNKNOWN", merge_state_status="")


# --------------------------------------------------------------------------- #
# 1. Prompt contract — engine-neutral except the attribution trailer.          #
# --------------------------------------------------------------------------- #

def test_the_fix_prompt_carries_its_contract_on_either_engine():
    pr = _pr()
    prompt = build_pr_fix_prompt("o/r", pr, engine=AgentEngine.CODEX)
    for required in ("o/r#7", "gh pr checkout 7", "gh pr checks 7", pr.url):
        assert required in prompt


def test_the_conflict_prompt_carries_its_contract_on_either_engine():
    pr = _pr()
    prompt = build_conflict_fix_prompt("o/r", pr, engine=AgentEngine.CODEX)
    for required in ("o/r#7", "gh pr checkout 7", "MERGE CONFLICT",
                     "git push --force-with-lease", pr.url):
        assert required in prompt


def test_the_consolidation_prompt_carries_its_contract_on_either_engine():
    prompt = build_consolidation_prompt(
        "o/r", "main", [_pr(1, "one", "https://x/1"), _pr(2, "two", "https://x/2")],
        engine=AgentEngine.CODEX)
    for required in ("#1", "#2", "origin/main", "Closes #1", "Closes #2",
                     "--squash --auto"):
        assert required in prompt


@pytest.mark.parametrize("builder,args", [
    (build_pr_fix_prompt, ("o/r", _pr())),
    (build_conflict_fix_prompt, ("o/r", _pr())),
    (build_consolidation_prompt, ("o/r", "main", [_pr()])),
])
def test_the_codex_prompt_carries_the_codex_trailer_not_the_claude_one(builder, args):
    prompt = builder(*args, engine=AgentEngine.CODEX)
    assert commit_trailer(AgentEngine.CODEX) in prompt
    assert commit_trailer(AgentEngine.CLAUDE_CODE) not in prompt


@pytest.mark.parametrize("builder,args", [
    (build_pr_fix_prompt, ("o/r", _pr())),
    (build_conflict_fix_prompt, ("o/r", _pr())),
    (build_consolidation_prompt, ("o/r", "main", [_pr()])),
])
def test_the_default_engine_is_still_claude_for_callers_that_omit_it(builder, args):
    """Every existing call site (and every pre-#1267 test) built these prompts
    without an ``engine=`` kwarg; the default must stay Claude so none of them
    silently start asking for a Codex trailer."""
    prompt = builder(*args)
    assert commit_trailer(AgentEngine.CLAUDE_CODE) in prompt
    assert commit_trailer(AgentEngine.CODEX) not in prompt


@pytest.mark.parametrize("builder,args", [
    (build_pr_fix_prompt, ("o/r", _pr())),
    (build_conflict_fix_prompt, ("o/r", _pr())),
    (build_consolidation_prompt, ("o/r", "main", [_pr()])),
])
def test_both_engines_get_the_same_contract_apart_from_the_trailer(builder, args):
    claude = builder(*args, engine=AgentEngine.CLAUDE_CODE)
    codex = builder(*args, engine=AgentEngine.CODEX)

    def normalize(text: str, engine: AgentEngine) -> str:
        return text.replace(commit_trailer(engine), "TRAILER")

    assert normalize(claude, AgentEngine.CLAUDE_CODE) == normalize(codex, AgentEngine.CODEX)


# --------------------------------------------------------------------------- #
# 2. Launch preconditions — shared with every other codex_allowed origin.      #
# --------------------------------------------------------------------------- #

def test_a_full_auto_codex_pr_doctor_launch_is_refused():
    """``full-auto`` is the automation default and maps to Codex's
    network-isolated ``workspace-write`` sandbox: a fix/consolidation session
    would run to completion and then fail at ``git push``."""
    blocker = unattended_launch_blocker(AgentEngine.CODEX, "full-auto")
    assert blocker is not None
    assert "network" in blocker.lower()


def test_bypass_permissions_is_the_one_mode_that_can_complete_the_contract():
    assert unattended_launch_blocker(AgentEngine.CODEX, "bypassPermissions") is None


def test_claude_pr_doctor_launches_are_unaffected_by_the_precondition():
    for mode in ("full-auto", "acceptEdits", "interactive", "bypassPermissions"):
        assert unattended_launch_blocker(AgentEngine.CLAUDE_CODE, mode) is None


def test_the_network_opt_in_escalates_only_the_full_auto_fallback():
    mode, note = escalate_for_codex_network_opt_in(
        AgentEngine.CODEX, "full-auto", network_opt_in=True)
    assert mode == CODEX_NETWORK_OPT_IN_MODE
    assert note is not None
    assert unattended_launch_blocker(AgentEngine.CODEX, mode) is None


# --------------------------------------------------------------------------- #
# 3. AutomationContext.spawn_session under the "pr-doctor" id — the same       #
#    generic machinery every codex_allowed origin relies on.                   #
# --------------------------------------------------------------------------- #

async def _fake_create_session(*, origin=None, origin_ref=None, **kw):
    from server.db.models import Session
    async with db_session() as db:
        s = Session(id=f"sess-{origin_ref}", name="auto", account_name="a",
                    cwd="/tmp", status="working", origin=origin, origin_ref=origin_ref)
        db.add(s)
    s.launch_kwargs = kw  # type: ignore[attr-defined]
    return s


async def _codex_pr_doctor_ctx(tmp_path, db_name, monkeypatch, *, config=None):
    from server.automations.context import AutomationContext
    from server.automations.registry import registry
    from server.db.models import Automation

    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / db_name}")
    async with db_session() as db:
        db.add(Automation(id="pr-doctor", enabled=True, mode="auto",
                          config=config or {"engine": "codex"}))
    monkeypatch.setattr(registry.get("pr-doctor"), "engine_policy",
                        EnginePolicy.CODEX_ALLOWED, raising=False)
    monkeypatch.setenv("CCSWITCH_CODEX_AUTOMATIONS_ENABLED", "true")
    return AutomationContext("pr-doctor", create_session=_fake_create_session)


def test_a_codex_pr_doctor_spawn_under_full_auto_refuses_and_records_why(
        tmp_path, monkeypatch):
    async def run():
        ctx = await _codex_pr_doctor_ctx(tmp_path, "cx_a.db", monkeypatch)
        rec = await ctx.spawn_session(origin_ref="o/r#7", name="pr-doctor fix o/r#7")
        assert rec is None
        kinds = [e["kind"] for e in await ctx.list_events()]
        assert "engine_launch_refused" in kinds

    asyncio.run(run())


def test_a_codex_pr_doctor_spawn_with_an_egress_capable_mode_proceeds(
        tmp_path, monkeypatch):
    async def run():
        ctx = await _codex_pr_doctor_ctx(tmp_path, "cx_b.db", monkeypatch)
        rec = await ctx.spawn_session(origin_ref="o/r#7", name="pr-doctor fix o/r#7",
                                      permission_mode="bypassPermissions")
        assert rec is not None
        assert rec.launch_kwargs["engine"] is AgentEngine.CODEX
        assert rec.launch_kwargs["permission_mode"] == "bypassPermissions"

    asyncio.run(run())


def test_a_claude_pr_doctor_spawn_is_unchanged_by_the_precondition(tmp_path, monkeypatch):
    async def run():
        from server.automations.context import AutomationContext
        from server.db.models import Automation

        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'cx_c.db'}")
        async with db_session() as db:
            db.add(Automation(id="pr-doctor", enabled=True, mode="auto"))
        ctx = AutomationContext("pr-doctor", create_session=_fake_create_session)
        rec = await ctx.spawn_session(origin_ref="o/r#7", name="pr-doctor fix o/r#7")
        assert rec is not None
        assert rec.launch_kwargs["engine"] is AgentEngine.CLAUDE_CODE
        assert rec.launch_kwargs["permission_mode"] == "full-auto"

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# 4. Orchestrator spawn tails: a refused launch must be reported honestly,     #
#    not recorded as a spawn with a null session id (the exact defect class   #
#    #1264's own acceptance suite found and named).                           #
# --------------------------------------------------------------------------- #

def _build_orch(pr):
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext

    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = {}
    orch._drivers = {}
    orch._pr_doctor_ctx = AutomationContext("pr-doctor", create_session=_fake_create_session)
    orch._pr_population = type("P", (), {"cached": staticmethod(lambda repo: [pr])})()
    orch._consolidation_inflight = set()
    return orch


async def _init_codex_pr_doctor_db(tmp_path, monkeypatch, db_name="orch.db"):
    from server.automations.registry import registry
    from server.db.models import Automation

    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / db_name}")
    async with db_session() as db:
        db.add(Automation(id="pr-doctor", enabled=True, mode="auto",
                          config={"engine": "codex"}))
    monkeypatch.setattr(registry.get("pr-doctor"), "engine_policy",
                        EnginePolicy.CODEX_ALLOWED, raising=False)
    monkeypatch.setenv("CCSWITCH_CODEX_AUTOMATIONS_ENABLED", "true")


@pytest.mark.asyncio
async def test_a_refused_fresh_spawn_reports_refused_not_a_phantom_spawn(
        tmp_path, monkeypatch):
    """No live driver + a Codex engine resolved under the default (full-auto)
    permission mode → spawn_session's own sandbox-blocker check refuses inside
    the clone/model tail. Before this fix, the caller assumed any return value
    from spawn_session was a real record and reported "spawned" with a null
    session id and an 'attempted' feed event — exactly the phantom-spawn defect
    #1264's suite found for backlog-runner (test_a_refused_launch_releases_the_
    claim_instead_of_holding_a_slot)."""
    import server.sessions.orchestrator as orch_mod

    await _init_codex_pr_doctor_db(tmp_path, monkeypatch)
    pr = _pr()
    orch = _build_orch(pr)
    monkeypatch.setattr(orch_mod.gitrepos, "ensure_clone",
                        lambda *a, **kw: "/fake/clone/o__r", raising=True)

    status, sid = await orch._pr_doctor_session_fix("o/r", "feat/x", pr, "failing_ci", None)

    assert status == "refused"
    assert sid is None
    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    assert not any(e["kind"] == "attempted" for e in events)
    assert any(e["kind"] == "engine_launch_refused" for e in events)


@pytest.mark.asyncio
async def test_a_refused_consolidation_spawn_emits_no_phantom_attempted_event(
        tmp_path, monkeypatch):
    import server.sessions.orchestrator as orch_mod

    await _init_codex_pr_doctor_db(tmp_path, monkeypatch)
    pr1, pr2 = _pr(1, "one", "https://x/1"), _pr(2, "two", "https://x/2")
    orch = _build_orch(pr1)
    monkeypatch.setattr(orch_mod.gitrepos, "ensure_clone",
                        lambda *a, **kw: "/fake/clone/o__r", raising=True)
    monkeypatch.setattr(orch_mod.gitrepos, "list_remote_branches",
                        lambda repo: {"default": "main"}, raising=True)

    await orch._pr_doctor_consolidate_spawn("o/r", "consolidate:1-2", [pr1, pr2])

    events = await orch._pr_doctor_ctx.list_events(target="consolidate:1-2")
    assert not any(e["kind"] == "attempted" for e in events)
    assert any(e["kind"] == "engine_launch_refused" for e in events)
    assert orch._consolidation_inflight == set()  # guard always released


@pytest.mark.asyncio
async def test_a_codex_engine_fresh_spawn_skips_the_claude_only_model_assessor(
        tmp_path, monkeypatch):
    """resolve_model is a Haiku complexity assessor returning Claude catalog
    aliases; calling it for a Codex spawn and then dropping the answer is the
    exact waste Phase 7 Task 7.1 audits against for every other origin."""
    import server.automations.model_pick as model_pick_mod
    import server.sessions.orchestrator as orch_mod

    await _init_codex_pr_doctor_db(tmp_path, monkeypatch)
    pr = _pr()
    orch = _build_orch(pr)
    monkeypatch.setattr(orch_mod.gitrepos, "ensure_clone",
                        lambda *a, **kw: "/fake/clone/o__r", raising=True)
    calls: list[str] = []

    async def fail_if_called(*a, **kw):
        calls.append("called")
        raise AssertionError("resolve_model must not run for a Codex spawn")

    monkeypatch.setattr(model_pick_mod, "resolve_model", fail_if_called, raising=True)

    # Default permission mode (full-auto) still refuses the Codex spawn below —
    # that is fine, this test only cares that resolve_model was never reached on
    # the way there (it runs, if at all, BEFORE the clone/spawn_session tail).
    status, _sid = await orch._pr_doctor_session_fix("o/r", "feat/x", pr, "failing_ci", None)

    assert status == "refused"
    assert calls == []


# --------------------------------------------------------------------------- #
# 5. The gate itself.                                                          #
# --------------------------------------------------------------------------- #

def test_the_pr_doctor_ceiling_is_codex_allowed():
    """Widened by the owner (#1267): everything decidable offline passes above,
    and the remaining live-boundary item — a real Codex session actually
    restoring or consolidating a real PR — was explicitly accepted as
    outstanding rather than blocking the ceiling.

    Three independent gates still stand between this and an actual live Codex
    launch, none of which this test flips: ``CCSWITCH_CODEX_AUTOMATIONS_ENABLED``
    (global kill switch, default false), the automation's own ``engine`` config
    opt-in (default Claude), and ``CCSWITCH_CODEX_UNATTENDED_NETWORK_OPT_IN``
    (default false — without it, ``unattended_launch_blocker`` still refuses an
    unattended Codex launch outright because ``full-auto`` cannot reach the
    network)."""
    from server.automations.registry import registry

    assert registry.get("pr-doctor").engine_policy is EnginePolicy.CODEX_ALLOWED
