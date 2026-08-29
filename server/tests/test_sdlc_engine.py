"""Pure SDLC stage engine: next_action over the intent→spec→plan→build chain.

Exhaustive, I/O-free — the way test_pr_doctor_engine tests plan_recovery. Covers
every stage transition and the propose-only v1 gate.
"""
from __future__ import annotations

import pytest

from server.sdlc.engine import (
    ArtifactView,
    AwaitApproval,
    Done,
    HandoffToBuild,
    InitiativeView,
    NeedsHuman,
    Propose,
    STAGE_ARTIFACT,
    STAGE_SEQUENCE,
    next_action,
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
    assert next_action(_init(stage="done"), [], "propose", {}) == Done()


def test_status_done_is_done():
    assert next_action(_init(status="done"), [], "propose", {}) == Done()


def test_blocked_needs_human():
    assert isinstance(next_action(_init(status="blocked"), [], "propose", {}), NeedsHuman)


def test_off_mode_needs_human():
    # Automation disabled — a human must drive the chain.
    assert isinstance(next_action(_init(), [], "off", {}), NeedsHuman)


# --------------------------------------------------------------------------- #
# Front-stage propose / gate (intake→intent, design→spec, plan→plan)           #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("stage,kind", [
    ("intake", "intent"),
    ("design", "spec"),
    ("plan", "plan"),
])
def test_no_artifact_proposes_stage_kind(stage, kind):
    assert next_action(_init(stage=stage), [], "propose", {}) == Propose(kind)


@pytest.mark.parametrize("stage,kind", [
    ("intake", "intent"),
    ("design", "spec"),
    ("plan", "plan"),
])
def test_proposed_pr_awaits_approval(stage, kind):
    # Artifact PR is open (commit_sha None) → propose-only gate: wait for human.
    arts = [_art(kind, sha=None, pr=7)]
    assert next_action(_init(stage=stage), arts, "propose", {}) == AwaitApproval(kind)


@pytest.mark.parametrize("stage,kind", [
    ("intake", "intent"),
    ("design", "spec"),
    ("plan", "plan"),
])
def test_merged_artifact_still_awaits_human_advance(stage, kind):
    # Merged (commit_sha set) but stage not yet advanced: the engine NEVER
    # advances the stage itself in propose-only — the human tap does. So it keeps
    # returning AwaitApproval until the orchestrator bumps the stage.
    arts = [_art(kind, sha="deadbeef", pr=7)]
    assert next_action(_init(stage=stage), arts, "propose", {}) == AwaitApproval(kind)


def test_only_current_stage_kind_drives_action():
    # A merged intent while we are in the design stage → design proposes spec;
    # the presence of the upstream intent artifact does not change the decision.
    arts = [_art("intent", sha="cafe", pr=1)]
    assert next_action(_init(stage="design"), arts, "propose", {}) == Propose("spec")


# --------------------------------------------------------------------------- #
# Build handoff                                                                 #
# --------------------------------------------------------------------------- #

def test_build_stage_hands_off_issue():
    act = next_action(_init(stage="build", issue="owner/repo#42"), [], "propose", {})
    assert act == HandoffToBuild("owner/repo#42")


def test_build_stage_without_issue_needs_human():
    assert isinstance(next_action(_init(stage="build", issue=None), [], "propose", {}),
                      NeedsHuman)


# --------------------------------------------------------------------------- #
# Out-of-scope / unknown stages                                                #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("stage", ["deploy", "maintain"])
def test_back_stages_need_human(stage):
    # Deploy/Maintain are carried by existing pipeline/monitor flows, not this
    # front-stage engine — nothing to propose here.
    assert isinstance(next_action(_init(stage=stage), [], "propose", {}), NeedsHuman)


def test_unknown_stage_needs_human():
    assert isinstance(next_action(_init(stage="bogus"), [], "propose", {}), NeedsHuman)


# --------------------------------------------------------------------------- #
# mode="auto" + config: the future auto-advance seam (v1 default = propose-only) #
# --------------------------------------------------------------------------- #

def test_auto_default_still_gates():
    # auto mode with the default propose_only gate ON behaves like propose:
    # a merged artifact still awaits a human advance.
    arts = [_art("intent", sha="cafe", pr=1)]
    assert next_action(_init(stage="intake"), arts, "auto", {}) == AwaitApproval("intent")


def test_auto_advance_intent_to_spec_when_gate_disabled():
    arts = [_art("intent", sha="cafe", pr=1)]
    cfg = {"propose_only": False}
    assert next_action(_init(stage="intake"), arts, "auto", cfg) == Propose("spec")


def test_auto_advance_design_to_plan_when_gate_disabled():
    arts = [_art("spec", sha="cafe", pr=2)]
    cfg = {"propose_only": False}
    assert next_action(_init(stage="design"), arts, "auto", cfg) == Propose("plan")


def test_auto_advance_plan_to_build_when_gate_disabled():
    arts = [_art("plan", sha="cafe", pr=3)]
    cfg = {"propose_only": False}
    act = next_action(_init(stage="plan", issue="owner/repo#9"), arts, "auto", cfg)
    assert act == HandoffToBuild("owner/repo#9")


def test_auto_advance_only_applies_in_auto_mode():
    # Even with the gate disabled in config, propose mode never auto-advances.
    arts = [_art("intent", sha="cafe", pr=1)]
    cfg = {"propose_only": False}
    assert next_action(_init(stage="intake"), arts, "propose", cfg) == AwaitApproval("intent")


# --------------------------------------------------------------------------- #
# Purity / determinism / constants                                             #
# --------------------------------------------------------------------------- #

def test_deterministic():
    init, arts = _init(stage="design"), [_art("spec", sha=None, pr=5)]
    a = next_action(init, arts, "propose", {})
    b = next_action(init, arts, "propose", {})
    assert a == b == AwaitApproval("spec")


def test_does_not_mutate_inputs():
    init = _init(stage="intake")
    arts = [_art("intent", sha=None, pr=1)]
    before = (init, tuple(arts))
    next_action(init, arts, "propose", {})
    assert (init, tuple(arts)) == before  # frozen dataclasses — nothing mutated


def test_stage_artifact_map_matches_sequence():
    # Every artifact-producing stage is in the sequence, in order.
    assert list(STAGE_ARTIFACT) == ["intake", "design", "plan"]
    for stage in STAGE_ARTIFACT:
        assert stage in STAGE_SEQUENCE
    assert STAGE_SEQUENCE.index("intake") < STAGE_SEQUENCE.index("design") < \
        STAGE_SEQUENCE.index("plan") < STAGE_SEQUENCE.index("build")
