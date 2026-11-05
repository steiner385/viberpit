"""Release-milestone definition parser and measure-kind registry."""

from pathlib import Path

import pytest

from server.pm.milestone_def import (
    MEASURE_KINDS,
    MilestoneDefError,
    Platform,
    Ref,
    from_json,
    parse_ladder,
    parse_ref,
    to_json,
)

FIXTURE = Path(__file__).parent / "fixtures" / "milestones" / "ladder.yaml"
PRIMARY = "cairnea/KinDash"


def test_parses_fixture_ladder():
    lad = parse_ladder(FIXTURE.read_text(), primary_repo=PRIMARY)
    assert [m.key for m in lad.milestones] == ["household", "friendly", "community", "public"]
    assert lad.start == "household"
    hh = lad.milestones[0]
    assert hh.epics == (Ref("cairnea/KinDash", 20191),)
    assert [c.key for c in hh.gate] == ["rubric", "lane-fixes", "nightly", "consent"]
    nightly = hh.gate[2].measure
    assert (nightly.kind, nightly.target, nightly.op) == ("workflow_streak", 3, "ge")
    assert lad.milestones[1].platforms[-1] == Platform("Kiosk", True, "not yet")
    assert [c.key for c in lad.milestones[2].gate] == ["paying", "week-four", "support", "crash-free", "p0"]
    assert [c.key for c in lad.numbers] == ["rubric-gap", "leaves", "outside-families", "defects-by-ci"]
    refs = hh.gate[3].measure.params["refs"]
    assert refs[0] == Ref(PRIMARY, 20234)


def test_round_trips_through_json():
    lad = parse_ladder(FIXTURE.read_text(), primary_repo=PRIMARY)
    assert from_json(to_json(lad)) == lad


def test_registry_defaults():
    assert MEASURE_KINDS["workflow_streak"].default_max_age_s == 129600
    assert MEASURE_KINDS["manual"].default_max_age_s is None
    assert MEASURE_KINDS["ladder_leaves"].gate_ok is False
    assert set(MEASURE_KINDS) == {
        "sub_issues", "issue_count", "refs_closed", "workflow_streak",
        "ci_metric", "ladder_leaves", "manual",
    }


def test_parse_ref():
    assert parse_ref("#5", primary_repo="o/r") == Ref("o/r", 5)
    assert parse_ref("a/b#7", primary_repo="o/r").key == "a/b#7"


@pytest.mark.parametrize("yaml_text, msg", [
    ("version: 2\nmilestones: []", "version"),
    ("version: 1\ntitle: t\nmilestones: [{key: a, name: A}, {key: a, name: B}]", "duplicate milestone key 'a'"),
    ("version: 1\ntitle: t\nmilestones: [{key: A!, name: A}]", "key"),
    ("version: 1\ntitle: t\nmilestones: [{key: numbers, name: A}]", "reserved"),
    ("version: 1\ntitle: t\nmilestones: [{key: a, name: A, gate: [{key: g, label: G, measure: {kind: nope}}]}]", "unknown measure kind 'nope'"),
    ("version: 1\ntitle: t\nmilestones: [{key: a, name: A, colour: red}]", "unknown key 'colour'"),
    ("version: 1\ntitle: t\nmilestones: [{key: a, name: A, gate: [{key: g, label: G, measure: {kind: issue_count, query: q}}]}]", "needs op and target"),
    ("version: 1\ntitle: t\nnumbers: [{key: n, label: N, measure: {kind: ladder_leaves, op: eq, target: 1}}]\nmilestones: []", "ladder_leaves"),
    ("version: 1\ntitle: t\nmilestones: [{key: a, name: A, gate: [{key: g, label: G, measure: {kind: ladder_leaves}}]}]", "ladder_leaves"),
    ("version: 1\ntitle: t\nstart: zzz\nmilestones: [{key: a, name: A}]", "start"),
    ("version: 1\ntitle: t\nmilestones: [{key: a, name: A, gate: [{key: g, label: G, measure: {kind: workflow_streak, workflow: n.yml, target: 3}}]}]", "workflow_streak' requires 'branch'"),
    ("version: 1\ntitle: t\nmilestones: [{key: a, name: A, epics: ['nope']}]", "ref"),
    ("version: 1\ntitle: t\nmilestones: [{key: a, name: A, gate: [{key: g, label: G, measure: {kind: manual}}, {key: g, label: H, measure: {kind: manual}}]}]", "duplicate"),
    ("version: 1\ntitle: t\nnumbers: [{key: n, label: N, measure: {kind: manual}}, {key: n, label: M, measure: {kind: manual}}]\nmilestones: []", "duplicate"),
    ("version: 1\ntitle: t\nmilestones: [{key: a, name: A, gate: [{key: g, label: G, measure: {kind: issue_count, query: q, op: eq, target: 0, bogus: 1}}]}]", "unknown key 'bogus'"),
    (": : :", "YAML"),
])
def test_rejects(yaml_text, msg):
    with pytest.raises(MilestoneDefError, match=msg):
        parse_ladder(yaml_text, primary_repo="o/r")


# PyYAML (YAML 1.1) reads a bare ``1e400`` as a string; ``1.0e+400`` is the float spelling, which overflows to inf.
@pytest.mark.parametrize("target", [".inf", "-.inf", ".nan", "1.0e+400"])
def test_rejects_non_finite_target(target):
    text = ("version: 1\ntitle: t\nmilestones: [{key: a, name: A, gate: [{key: g, label: G, "
            f"measure: {{kind: issue_count, query: q, op: le, target: {target}}}}}]}}]")
    with pytest.raises(MilestoneDefError, match="finite"):
        parse_ladder(text, primary_repo="o/r")
