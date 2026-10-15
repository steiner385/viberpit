"""Pure SDLC stage engine: next_action over the intent→spec→plan→build chain.

Exhaustive, I/O-free — the way test_pr_doctor_engine tests plan_recovery. Covers
every stage transition and the propose-only v1 gate.
"""
from __future__ import annotations

import pytest

from server.sdlc.engine import (
    Advance,
    ArtifactView,
    AwaitApproval,
    CloseEpic,
    CloseInitiative,
    Done,
    HandoffToBuild,
    InitiativeView,
    Materialize,
    MergeArtifact,
    NeedsHuman,
    ProgramView,
    Propose,
    STAGE_ARTIFACT,
    STAGE_SEQUENCE,
    advance_stage,
    next_action,
    program_action,
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                           #
# --------------------------------------------------------------------------- #

def _init(**kw) -> InitiativeView:
    base = dict(slug="widget", stage="intake", status="active",
                repo="owner/repo", issue=None)
    base.update(kw)
    return InitiativeView(**base)  # type: ignore[arg-type]


def _art(kind: str, *, sha: str | None = None, pr: int | None = None) -> ArtifactView:
    return ArtifactView(kind=kind, commit_sha=sha, pr_number=pr)


# --------------------------------------------------------------------------- #
# Terminal / override states (checked before stage logic)                      #
# --------------------------------------------------------------------------- #

def test_stage_done_is_done():
    assert next_action(_init(stage="done"), [], "propose", "cautious") == Done()


def test_status_done_is_done():
    assert next_action(_init(status="done"), [], "propose", "cautious") == Done()


def test_blocked_needs_human():
    assert isinstance(next_action(_init(status="blocked"), [], "propose", "cautious"), NeedsHuman)


def test_off_mode_needs_human():
    # Automation disabled — a human must drive the chain.
    assert isinstance(next_action(_init(), [], "off", "cautious"), NeedsHuman)


# --------------------------------------------------------------------------- #
# Front-stage propose / gate (intake→intent, design→spec, plan→plan)           #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("stage,kind", [
    ("intake", "intent"),
    ("design", "spec"),
    ("plan", "plan"),
])
def test_no_artifact_proposes_stage_kind(stage, kind):
    assert next_action(_init(stage=stage), [], "propose", "cautious") == Propose(kind)


@pytest.mark.parametrize("stage,kind", [
    ("intake", "intent"),
    ("design", "spec"),
    ("plan", "plan"),
])
def test_proposed_pr_awaits_approval(stage, kind):
    # Artifact PR is open (commit_sha None) → propose-only gate: wait for human.
    arts = [_art(kind, sha=None, pr=7)]
    assert next_action(_init(stage=stage), arts, "propose", "cautious") == AwaitApproval(kind)


@pytest.mark.parametrize("stage,kind", [
    ("intake", "intent"),
    ("design", "spec"),
])
def test_merged_artifact_still_awaits_human_advance(stage, kind):
    # Merged (commit_sha set) but stage not yet advanced: the engine NEVER
    # advances the stage itself in propose-only — the human tap does. So it keeps
    # returning AwaitApproval until the orchestrator bumps the stage.
    # The plan stage is the one exception (materialization); it is covered by
    # test_plan_merged_{with,without}_epic below.
    arts = [_art(kind, sha="deadbeef", pr=7)]
    assert next_action(_init(stage=stage), arts, "propose", "cautious") == AwaitApproval(kind)


# --------------------------------------------------------------------------- #
# Plan materialization (spec 003 §7b): a merged plan with no epic yet          #
# --------------------------------------------------------------------------- #

def test_plan_merged_without_epic_materializes():
    assert next_action(_init(stage="plan"), [_art("plan", sha="abc")], "propose", "cautious") == Materialize()


def test_plan_merged_with_epic_awaits_advance_as_before():
    assert next_action(_init(stage="plan", epic="o/r#42"), [_art("plan", sha="abc")],
                       "propose", "cautious") == AwaitApproval("plan")


def test_plan_still_open_awaits_before_materializing():
    assert next_action(_init(stage="plan"), [_art("plan", pr=7)], "propose", "cautious") == AwaitApproval("plan")


def test_only_current_stage_kind_drives_action():
    # A merged intent while we are in the design stage → design proposes spec;
    # the presence of the upstream intent artifact does not change the decision.
    arts = [_art("intent", sha="cafe", pr=1)]
    assert next_action(_init(stage="design"), arts, "propose", "cautious") == Propose("spec")


# --------------------------------------------------------------------------- #
# Build handoff                                                                 #
# --------------------------------------------------------------------------- #

def test_build_stage_hands_off_issue():
    act = next_action(_init(stage="build", issue="owner/repo#42"), [], "propose", "cautious")
    assert act == HandoffToBuild("owner/repo#42")


def test_build_stage_without_issue_needs_human():
    assert isinstance(next_action(_init(stage="build", issue=None), [], "propose", "cautious"),
                      NeedsHuman)


def test_build_stage_without_issue_hands_off_the_epic():
    """#1545: a materialized initiative with no origin_ref (every control-band one
    is unstamped) reaches build right after the accept that created its epic —
    that epic is the handoff target, not a NeedsHuman dead end."""
    act = next_action(_init(stage="build", issue=None, epic="owner/repo#101"), [],
                      "propose", "cautious")
    assert act == HandoffToBuild("owner/repo#101")


def test_build_stage_prefers_the_origin_issue_over_the_epic():
    act = next_action(_init(stage="build", issue="owner/repo#42", epic="owner/repo#101"), [],
                      "propose", "cautious")
    assert act == HandoffToBuild("owner/repo#42")


# --------------------------------------------------------------------------- #
# Out-of-scope / unknown stages                                                #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("stage", ["deploy", "maintain"])
def test_back_stages_need_human(stage):
    # Deploy/Maintain are carried by existing pipeline/monitor flows, not this
    # front-stage engine — nothing to propose here.
    assert isinstance(next_action(_init(stage=stage), [], "propose", "cautious"), NeedsHuman)


def test_unknown_stage_needs_human():
    assert isinstance(next_action(_init(stage="bogus"), [], "propose", "cautious"), NeedsHuman)


# --------------------------------------------------------------------------- #
# Purity / determinism / constants                                             #
# --------------------------------------------------------------------------- #

def test_deterministic():
    init, arts = _init(stage="design"), [_art("spec", sha=None, pr=5)]
    a = next_action(init, arts, "propose", "cautious")
    b = next_action(init, arts, "propose", "cautious")
    assert a == b == AwaitApproval("spec")


def test_does_not_mutate_inputs():
    init = _init(stage="intake")
    arts = [_art("intent", sha=None, pr=1)]
    before = (init, tuple(arts))
    next_action(init, arts, "propose", "cautious")
    assert (init, tuple(arts)) == before  # frozen dataclasses — nothing mutated


def test_stage_artifact_map_matches_sequence():
    # Every artifact-producing stage is in the sequence, in order.
    assert list(STAGE_ARTIFACT) == ["intake", "design", "plan"]
    for stage in STAGE_ARTIFACT:
        assert stage in STAGE_SEQUENCE
    assert STAGE_SEQUENCE.index("intake") < STAGE_SEQUENCE.index("design") < \
        STAGE_SEQUENCE.index("plan") < STAGE_SEQUENCE.index("build")


def test_step_back_stage_walks_the_sequence_backwards():
    from server.sdlc.engine import step_back_stage
    assert step_back_stage("design") == "intake"
    assert step_back_stage("done") == "maintain"
    assert step_back_stage("intake") is None
    with pytest.raises(ValueError):
        step_back_stage("nope")


# --------------------------------------------------------------------------- #
# Tolerance axis (spec 2026-09-14 §5): cautious == today; balanced/bold act    #
# --------------------------------------------------------------------------- #

def test_cautious_keeps_every_human_gate():
    assert next_action(_init(stage="intake"), [_art("intent", pr=1)], "on", "cautious") == AwaitApproval("intent")
    assert next_action(_init(stage="intake"), [_art("intent", sha="cafe", pr=1)], "on", "cautious") == AwaitApproval("intent")
    built = _init(stage="build", issue="owner/repo#9", epic_total=3, epic_done=3)
    assert next_action(built, [], "on", "cautious") == HandoffToBuild("owner/repo#9")


def test_balanced_arms_merge_on_an_open_artifact_pr():
    assert next_action(_init(stage="design"), [_art("spec", pr=7)], "on", "balanced") == MergeArtifact("spec", 7)


def test_open_pr_without_a_known_number_still_waits():
    # Enrichment has not filled pr_number yet: nothing to arm, so wait a tick.
    assert next_action(_init(stage="design"), [_art("spec")], "on", "balanced") == AwaitApproval("spec")


def test_balanced_advances_a_merged_artifact_one_stage():
    assert next_action(_init(stage="intake"), [_art("intent", sha="cafe", pr=1)], "on", "balanced") == Advance("design")
    assert next_action(_init(stage="design"), [_art("spec", sha="cafe", pr=2)], "on", "balanced") == Advance("plan")
    with_epic = _init(stage="plan", issue="owner/repo#9", epic="owner/repo#9")
    assert next_action(with_epic, [_art("plan", sha="cafe", pr=3)], "on", "balanced") == Advance("build")


def test_materialize_precedes_advance_at_every_tolerance():
    arts = [_art("plan", sha="cafe", pr=3)]
    for tol in ("cautious", "balanced", "bold"):
        assert next_action(_init(stage="plan", issue="owner/repo#9"), arts, "on", tol) == Materialize()


def test_balanced_advances_a_built_initiative_to_deploy():
    built = _init(stage="build", issue="owner/repo#9", epic_total=3, epic_done=3)
    assert next_action(built, [], "on", "balanced") == Advance("deploy")


def test_an_unfinished_epic_never_advances():
    building = _init(stage="build", issue="owner/repo#9", epic_total=3, epic_done=2)
    assert next_action(building, [], "on", "bold") == HandoffToBuild("owner/repo#9")
    no_epic = _init(stage="build", issue="owner/repo#9")
    assert next_action(no_epic, [], "on", "bold") == HandoffToBuild("owner/repo#9")


def test_off_and_blocked_win_over_any_tolerance():
    arts = [_art("intent", sha="cafe", pr=1)]
    assert next_action(_init(stage="intake"), arts, "off", "bold") == NeedsHuman(reason="SDLC automation is off")
    assert next_action(_init(stage="intake", status="blocked"), arts, "on", "bold") == NeedsHuman(reason="initiative is blocked")


def test_legacy_modes_are_simply_not_off():
    # Intake still passes propose/auto; the engine treats anything but off as on.
    arts = [_art("intent", sha="cafe", pr=1)]
    assert next_action(_init(stage="intake"), arts, "propose", "balanced") == Advance("design")
    assert next_action(_init(stage="intake"), arts, "auto", "cautious") == AwaitApproval("intent")


# --------------------------------------------------------------------------- #
# Lifecycle sweep (#3108): finished work closes, in-progress epics skip gates  #
# --------------------------------------------------------------------------- #

def _shipped(stage: str, *, closed: bool, done: int = 3, **kw) -> InitiativeView:
    return _init(stage=stage, issue="owner/repo#9", epic="owner/repo#5",
                 epic_total=3, epic_done=done, epic_closed=closed, **kw)


@pytest.mark.parametrize("stage", ["deploy", "maintain"])
def test_a_finished_epic_past_build_is_closed_then_the_initiative(stage):
    assert next_action(_shipped(stage, closed=False), [], "on", "balanced") == \
        CloseEpic("owner/repo#5")
    assert next_action(_shipped(stage, closed=True), [], "on", "balanced") == CloseInitiative()
    assert next_action(_shipped(stage, closed=True), [], "on", "bold") == CloseInitiative()


def test_closing_needs_the_tolerance_every_sub_issue_and_an_unblocked_row():
    # cautious: today's behaviour — the existing flows (a human) close it.
    assert isinstance(next_action(_shipped("deploy", closed=True), [], "on", "cautious"), NeedsHuman)
    # one sub-issue still open: nothing closes.
    assert isinstance(next_action(_shipped("deploy", closed=False, done=2), [], "on", "bold"),
                      NeedsHuman)
    # a blocked row is a human's, whatever the epic says.
    assert isinstance(next_action(_shipped("deploy", closed=True, status="blocked"), [], "on",
                                  "bold"), NeedsHuman)
    # an epic with no sub-issues proves nothing.
    empty = _init(stage="deploy", epic="owner/repo#5", epic_total=0, epic_done=0, epic_closed=True)
    assert isinstance(next_action(empty, [], "on", "bold"), NeedsHuman)


def test_a_finished_build_still_advances_to_deploy_first():
    """Build keeps its own gate: the close sequence starts past it."""
    assert next_action(_shipped("build", closed=True), [], "on", "balanced") == Advance("deploy")


@pytest.mark.parametrize("stage,arts", [
    ("plan", []),                                       # would propose a plan
    ("plan", [_art("plan", pr=7)]),                     # would wait on the plan PR
    ("design", [_art("spec")]),                         # would wait on the spec session
])
def test_an_adopted_epic_already_in_progress_skips_the_document_gate(stage, arts):
    ini = _init(stage=stage, epic="owner/repo#5", epic_total=4, epic_done=1)
    assert next_action(ini, arts, "on", "balanced") == Advance(advance_stage(stage))


def test_an_epic_with_nothing_closed_yet_still_takes_the_document_gate():
    ini = _init(stage="plan", epic="owner/repo#5", epic_total=4, epic_done=0)
    assert next_action(ini, [], "on", "balanced") == Propose("plan")
    started = _init(stage="plan", epic="owner/repo#5", epic_total=4, epic_done=2)
    assert next_action(started, [], "on", "cautious") == Propose("plan")


def _program(**kw) -> ProgramView:
    base = dict(slug="prog", status="active", stage="deploy", epic="owner/repo#1",
                epic_closed=False, epic_total=2, epic_done=2, phases_total=2, phases_done=2)
    base.update(kw)
    return ProgramView(**base)  # type: ignore[arg-type]


def test_a_program_whose_phases_are_all_done_rolls_up():
    assert program_action(_program(), "on", "balanced") == CloseEpic("owner/repo#1")
    assert program_action(_program(epic_closed=True), "on", "balanced") == CloseInitiative()
    assert program_action(_program(epic=None, epic_total=0, epic_done=0), "on", "bold") == \
        CloseInitiative()


@pytest.mark.parametrize("kw,mode,tolerance", [
    ({"phases_done": 1}, "on", "bold"),                 # a phase is still running
    ({"phases_total": 0, "phases_done": 0}, "on", "bold"),
    ({"epic_done": 1}, "on", "bold"),                   # the epic tracks other open work
    ({"status": "blocked"}, "on", "bold"),
    ({"status": "done"}, "on", "bold"),
    ({}, "off", "bold"),
    ({}, "on", "cautious"),
])
def test_a_program_still_running_or_held_is_left_alone(kw, mode, tolerance):
    assert program_action(_program(**kw), mode, tolerance) is None
