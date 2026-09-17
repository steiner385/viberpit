"""Consequence explainer (spec 2026-09-17 §3): every sentence the UI shows about a
control is the real engine run on the post-tap state, described. Pure and total."""
from __future__ import annotations

from dataclasses import replace

import pytest

from server.sdlc.engine import ArtifactView, InitiativeView, next_action
from server.sdlc.explain import (
    ACTOR_MACHINE, ACTOR_NOBODY, ACTOR_YOU, CONTROLS, WHEN_NEVER, WHEN_NEXT_TICK, WHEN_NOW,
    ExplainContext, describe, dispatch_sentence, explain, gates_to_wire, guidance_to_wire,
)
from server.sdlc.gates import GATES, RISK_TOLERANCES
from server.tests.test_sdlc_paths import assert_pure


def _view(**kw) -> InitiativeView:
    base = dict(slug="widget", stage="design", status="active", repo="o/r", issue=None)
    base.update(kw)
    return InitiativeView(**base)  # type: ignore[arg-type]


def _art(kind: str, *, sha: str | None = None, pr: int | None = None) -> ArtifactView:
    return ArtifactView(kind=kind, commit_sha=sha, pr_number=pr)


def _ctx(view: InitiativeView, arts: list[ArtifactView] | None = None, **kw) -> ExplainContext:
    base = dict(initiative=view, artifacts=list(arts or []), mode="on", tolerance="cautious",
                configured_tolerance="cautious", autoresolve_enabled=True, has_children=False,
                proposal_pending=False, proposal_tasks=0, dispatch="off",
                queue_min_status="ready", session_model="auto", tick_seconds=60)
    base.update(kw)
    return ExplainContext(**base)  # type: ignore[arg-type]


def test_module_is_pure():
    assert_pure("server.sdlc.explain")


# --- the simulation property: `then` IS the engine on the post-tap state --------

@pytest.mark.parametrize("tolerance", RISK_TOLERANCES)
def test_advance_then_equals_engine_on_advanced_state(tolerance):
    view = _view(stage="design")
    arts = [_art("intent", sha="a"), _art("spec", sha="b")]
    ctx = _ctx(view, arts, tolerance=tolerance, configured_tolerance=tolerance)
    g = explain(ctx)
    after = replace(view, stage="plan", status="active")
    expected = describe(next_action(after, arts, "on", tolerance), ctx, after, arts)
    assert g.actions["advance"].then == expected
    assert g.actions["advance"].immediate[0].text == "Moves the gate to plan."


def test_advance_from_design_spawns_a_plan_session_and_names_the_file():
    view = _view(stage="design")
    arts = [_art("intent", sha="a"), _art("spec", sha="b")]
    g = explain(_ctx(view, arts, session_model="opus"))
    step = g.actions["advance"].then[0]
    assert step.spawns_session is True
    assert "plan session on opus" in step.text
    assert "docs/sdlc/widget/plan.md" in step.text
    assert "reads the merged spec" in step.text
    assert g.actions["advance"].when == WHEN_NEXT_TICK
    assert g.actions["advance"].reversal == "Step back"
    assert g.actions["advance"].gate == "stage.advance"


def test_step_back_then_equals_engine_on_previous_stage():
    view = _view(stage="plan")
    arts = [_art("intent", sha="a"), _art("spec", sha="b")]
    ctx = _ctx(view, arts)
    g = explain(ctx)
    after = replace(view, stage="design", status="active")
    assert g.actions["step_back"].then == describe(next_action(after, arts, "on", "cautious"), ctx, after, arts)
    # A merged spec at design under cautious: nothing spawns, the human re-advances.
    assert g.actions["step_back"].then[0].spawns_session is False
    assert "tap Advance to move to plan" in g.actions["step_back"].then[0].text
    assert g.actions["step_back"].when == WHEN_NEVER


def test_unblock_clears_the_block_and_then_describes_the_stage():
    view = _view(stage="design", status="blocked")
    ctx = _ctx(view, [])
    g = explain(ctx)
    c = g.actions["unblock"]
    assert c.available is True
    assert c.immediate[0].text == "Clears the block; the gate stays at design."
    after = replace(view, status="awaiting_human")
    assert c.then == describe(next_action(after, [], "on", "cautious"), ctx, after, [])
    assert c.then[0].spawns_session is True   # no spec yet → a spec session


def test_materialize_accept_writes_github_moves_to_build_and_hands_to_the_board():
    view = _view(stage="plan")
    arts = [_art("intent", sha="a"), _art("spec", sha="b"), _art("plan", sha="c")]
    g = explain(_ctx(view, arts, proposal_pending=True, proposal_tasks=3, dispatch="auto",
                     queue_min_status="ready"))
    c = g.actions["materialize_accept"]
    assert c.available is True
    assert c.when == WHEN_NOW
    assert c.immediate[0].writes_github is True
    assert "3 sub-issues" in c.immediate[0].text
    assert c.immediate[1].text == "Moves the gate to build."
    assert c.then[0].spawns_session is True
    assert "dispatch it to a coding session" in c.then[0].text
    assert c.reversal == "Close the created issues"
    assert c.gate == "materialization.accept"


def test_materialize_accept_singular_sub_issue():
    view = _view(stage="plan")
    arts = [_art("plan", sha="c")]
    g = explain(_ctx(view, arts, proposal_pending=True, proposal_tasks=1))
    assert "1 sub-issue " in g.actions["materialize_accept"].immediate[0].text


def test_materialize_reject_frees_a_new_decompose_session():
    view = _view(stage="plan")
    arts = [_art("plan", sha="c")]
    g = explain(_ctx(view, arts, proposal_pending=True, proposal_tasks=2))
    c = g.actions["materialize_reject"]
    assert c.immediate[0].text == "Rejects this decomposition; nothing is created on GitHub."
    assert c.then[0].spawns_session is True
    assert "decompose session" in c.then[0].text
    assert c.when == WHEN_NEXT_TICK


def test_materialize_controls_unavailable_without_a_pending_proposal():
    g = explain(_ctx(_view(stage="plan"), [_art("plan", sha="c")]))
    for k in ("materialize_accept", "materialize_reject"):
        assert g.actions[k].available is False
        assert g.actions[k].unavailable_reason == "No decomposition is waiting."


# --- availability mirrors the endpoints' 409 rules ------------------------------

def test_program_has_no_outlook_and_every_control_is_unavailable():
    g = explain(_ctx(_view(), [], has_children=True))
    assert g.machine is None
    assert set(g.actions) == set(CONTROLS)
    for c in g.actions.values():
        assert c.available is False
        assert "phases" in (c.unavailable_reason or "")


def test_blocked_row_disables_advance_and_step_back_but_offers_unblock():
    g = explain(_ctx(_view(stage="design", status="blocked"), []))
    assert g.actions["advance"].unavailable_reason == "Clear the block first."
    assert g.actions["step_back"].unavailable_reason == "Clear the block first."
    assert g.actions["unblock"].available is True
    assert g.machine.actor == ACTOR_YOU
    assert g.machine.summary == "Blocked: unblock or step back."


def test_unblock_waits_for_a_pending_materialization():
    g = explain(_ctx(_view(stage="plan", status="blocked"), [], proposal_pending=True, proposal_tasks=1))
    assert g.actions["unblock"].available is False
    assert g.actions["unblock"].unavailable_reason == "Accept or reject the pending materialization first."


def test_edges_of_the_track():
    first = explain(_ctx(_view(stage="intake"), []))
    assert first.actions["step_back"].unavailable_reason == "Already at the first stage."
    last = explain(_ctx(_view(stage="done"), []))
    assert last.actions["advance"].unavailable_reason == "Already at the final stage."
    assert last.machine.actor == ACTOR_NOBODY
    assert last.machine.summary == "Nothing left to do."


def test_unknown_stage_is_total():
    g = explain(_ctx(_view(stage="weird"), []))
    assert g.actions["advance"].available is False
    assert g.actions["step_back"].available is False
    assert g.machine is not None


# --- the machine outlook ---------------------------------------------------------

def test_outlook_under_balanced_says_the_machine_advances_then_spawns():
    view = _view(stage="design")
    arts = [_art("intent", sha="a"), _art("spec", sha="b")]
    g = explain(_ctx(view, arts, tolerance="balanced", configured_tolerance="balanced"))
    assert g.machine.actor == ACTOR_MACHINE
    assert g.machine.when == WHEN_NEXT_TICK
    assert g.machine.steps[0].text == "Advances to plan automatically."
    assert g.machine.steps[1].spawns_session is True      # one level of lookahead
    assert len(g.machine.steps) == 2
    assert "balanced" in g.machine.because
    assert "advance a stage once its artifact merged" in g.machine.because


def test_outlook_under_cautious_waits_for_you():
    view = _view(stage="design")
    arts = [_art("intent", sha="a"), _art("spec", sha="b")]
    g = explain(_ctx(view, arts))
    assert g.machine.actor == ACTOR_YOU
    assert g.machine.when == WHEN_NEVER
    assert g.machine.summary == "The spec is merged; tap Advance to move to plan."
    assert g.machine.because == "Risk tolerance cautious: every gate waits for you."


def test_open_pr_under_cautious_names_the_review():
    g = explain(_ctx(_view(stage="design"), [_art("spec", pr=12)]))
    assert g.machine.summary == "The spec PR is waiting for you to review and merge it; under cautious the loop does not merge it."


def test_open_pr_under_balanced_arms_auto_merge():
    g = explain(_ctx(_view(stage="design"), [_art("spec", pr=12)], tolerance="balanced",
                     configured_tolerance="balanced"))
    assert g.machine.actor == ACTOR_MACHINE
    assert g.machine.steps[0].writes_github is True
    assert "PR #12" in g.machine.steps[0].text


def test_kill_switch_off_is_said_out_loud():
    g = explain(_ctx(_view(stage="design"), [_art("spec", sha="b")], tolerance="cautious",
                     configured_tolerance="balanced", autoresolve_enabled=False))
    assert g.machine.because == ("Risk tolerance is balanced, but auto-resolve is switched off for "
                                 "this deployment, so every gate waits for you.")
    assert g.tolerance == {"effective": "cautious", "configured": "balanced",
                           "autoresolveEnabled": False, "synthesizerMode": "on"}


def test_mode_off_means_nobody_acts():
    g = explain(_ctx(_view(stage="design"), [], mode="off"))
    assert g.machine.actor == ACTOR_NOBODY
    assert g.machine.when == WHEN_NEVER
    assert "SDLC automation is off" in g.machine.summary
    assert g.actions["advance"].when == WHEN_NEVER


def test_build_stage_defers_to_the_board_whatever_the_issue_pointer():
    for issue in (None, "o/r#5"):
        g = explain(_ctx(_view(stage="build", issue=issue, epic="o/r#9"), [], dispatch="off"))
        assert g.machine.actor == ACTOR_YOU
        assert g.machine.summary.startswith("Nothing more from the roadmap loop.")
        assert "Dispatch is off" in g.machine.summary


def test_deploy_stage_is_carried_by_other_flows():
    g = explain(_ctx(_view(stage="deploy"), []))
    assert g.machine.summary == "Deploy and maintain are carried by the pipeline and control bands, not the roadmap loop."


def test_pending_proposal_is_your_move():
    g = explain(_ctx(_view(stage="plan"), [_art("plan", sha="c")], proposal_pending=True, proposal_tasks=2))
    assert g.machine.actor == ACTOR_YOU
    assert g.machine.summary == "A decomposition is waiting for your Accept or Reject."


# --- helpers and wire shapes -----------------------------------------------------

def test_dispatch_sentence():
    assert dispatch_sentence("off", "ready") == "nothing is dispatched: this product's Dispatch is off."
    assert dispatch_sentence("propose", "ready") == "the Backlog Runner may include it in a proposed batch; you approve each batch."
    assert dispatch_sentence("auto", "triaged") == ("the Backlog Runner may dispatch it to a coding session once it is "
                                                    "at or above 'triaged' on the board.")


def test_guidance_wire_shape():
    g = explain(_ctx(_view(stage="design"), [_art("spec", sha="b")]))
    w = guidance_to_wire(g)
    assert set(w) == {"machine", "actions", "tolerance", "tickSeconds"}
    assert set(w["machine"]) == {"actor", "when", "summary", "steps", "because"}
    a = w["actions"]["advance"]
    assert set(a) == {"available", "unavailableReason", "immediate", "then", "when", "reversal", "gate"}
    assert set(a["then"][0]) == {"text", "spawnsSession", "writesGithub"}
    assert w["tickSeconds"] == 60
    assert guidance_to_wire(explain(_ctx(_view(), [], has_children=True)))["machine"] is None


def test_gates_wire_lists_every_gate_with_auto_resolution_per_tolerance():
    w = gates_to_wire(autoresolve_enabled=True)
    assert [g["id"] for g in w["gates"]] == [g.id for g in GATES]
    assert w["tolerances"] == list(RISK_TOLERANCES)
    assert w["autoresolveEnabled"] is True
    by_id = {g["id"]: g for g in w["gates"]}
    assert by_id["stage.advance"]["autoResolvesUnder"] == {"cautious": False, "balanced": True, "bold": True}
    assert by_id["materialization.accept"]["autoResolvesUnder"] == {"cautious": False, "balanced": False, "bold": True}
    assert by_id["dispatch"]["autoResolvesUnder"] == {"cautious": False, "balanced": False, "bold": False}
    assert by_id["dispatch"]["risk"] is None
    assert by_id["deploy.production"]["autoResolvesUnder"]["bold"] is False
    assert set(by_id["stage.advance"]) == {"id", "label", "risk", "action", "reverse", "autoResolvesUnder"}
