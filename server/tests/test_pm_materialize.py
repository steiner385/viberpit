"""Pure decomposition core for plan materialization (spec 003 §7b, #992).

Everything here is pure: prompt text, parsing a session's fenced JSON, the
dependency chain, and the mutation documents Task 15 journals and applies.
"""
import pytest

from server.pm.gh_mutations import build_batch
from server.pm.materialize import (
    Decomposition,
    MaterializationParseError,
    TaskSpec,
    blocked_by_mutation,
    build_create_issue,
    build_materialize_prompt,
    chain_edges,
    parse_materialization_output,
)
from server.pm.reconcile_diff import DepView, compile_edges

PLAN = "## Phase A\nDo the thing.\n\n## Phase B\nDo the other thing."


# --------------------------------------------------------------------------- #
# Prompt                                                                       #
# --------------------------------------------------------------------------- #

def test_prompt_carries_the_plan_and_the_json_contract():
    p = build_materialize_prompt("acme", "o/acme", PLAN)
    assert PLAN in p
    assert "acme" in p and "o/acme" in p
    assert "```json" in p
    for key in ("epic_title", "epic_body", "tasks", "title", "body", "depends_on"):
        assert key in p
    # The session must know sibling order is the phase order — chain_edges
    # derives the blocked-by chain from it.
    assert "phase" in p.lower()


def test_prompt_asks_for_covers_line_and_epic_requirement_index():
    """Issue #2163: each task body gets a Covers: line copied from the plan's
    requirement IDs; the epic body gets a matching index."""
    p = build_materialize_prompt("acme", "o/acme", PLAN)
    assert "Covers:" in p
    assert "FR-" in p and "AC-" in p and "US-" in p
    assert "## Requirements covered" in p
    assert "verbatim" in p.lower()


# --------------------------------------------------------------------------- #
# Parser                                                                       #
# --------------------------------------------------------------------------- #

def test_parse_round_trips_a_fenced_object():
    text = """
    Here is the decomposition.

    ```json
    {"epic_title": "  Ship it  ", "epic_body": "The epic.",
     "tasks": [{"title": " T1 ", "body": "B1", "depends_on": []},
               {"title": "T2", "body": "B2", "depends_on": ["0"]}]}
    ```
    """
    d = parse_materialization_output(text)
    assert d == Decomposition(
        epic_title="Ship it",
        epic_body="The epic.",
        tasks=(TaskSpec(title="T1", body="B1", depends_on=()),
               TaskSpec(title="T2", body="B2", depends_on=(0,))),
    )


def test_parse_accepts_a_bare_object_without_a_fence():
    d = parse_materialization_output(
        'blah {"epic_title": "E", "epic_body": "", "tasks": [{"title": "T"}]} trailing')
    assert d.epic_title == "E"
    assert d.tasks[0] == TaskSpec(title="T", body="", depends_on=())


def test_parse_ignores_an_echoed_shape_and_takes_the_last_fence():
    """A model that restates the requested shape does so BEFORE it answers.

    Echoing the contract back produces a perfectly valid object — with
    placeholder tasks — so "first fence that parses" would return the echo."""
    text = '''I will answer in exactly this shape:
```json
{"epic_title": "...", "epic_body": "...",
 "tasks": [{"title": "...", "body": "...", "depends_on": []}]}
```
Here is the decomposition:
```json
{"epic_title": "Ship it", "epic_body": "The epic.",
 "tasks": [{"title": "T1", "body": "B1"}]}
```'''
    d = parse_materialization_output(text)
    assert d.epic_title == "Ship it"
    assert d.tasks == (TaskSpec(title="T1", body="B1", depends_on=()),)


def test_parse_rejects_missing_tasks():
    with pytest.raises(MaterializationParseError):
        parse_materialization_output('{"epic_title": "E", "epic_body": "B"}')


def test_parse_rejects_an_empty_task_list():
    with pytest.raises(MaterializationParseError):
        parse_materialization_output('{"epic_title": "E", "epic_body": "B", "tasks": []}')


def test_parse_rejects_a_task_without_a_title():
    with pytest.raises(MaterializationParseError):
        parse_materialization_output('{"tasks": [{"title": "  ", "body": "b"}]}')


def test_parse_rejects_a_non_numeric_dependency():
    with pytest.raises(MaterializationParseError):
        parse_materialization_output(
            '{"tasks": [{"title": "T1"}, {"title": "T2", "depends_on": ["nope"]}]}')


def test_parse_rejects_output_with_no_json_at_all():
    with pytest.raises(MaterializationParseError):
        parse_materialization_output("I could not decompose this plan, sorry.")


# --------------------------------------------------------------------------- #
# chain_edges                                                                  #
# --------------------------------------------------------------------------- #

def _d(*depends: tuple[int, ...]) -> Decomposition:
    return Decomposition(
        epic_title="E", epic_body="B",
        tasks=tuple(TaskSpec(title=f"T{i}", body="", depends_on=dep)
                    for i, dep in enumerate(depends)))


def test_chain_edges_unions_explicit_and_sibling_order():
    assert chain_edges(_d((), (0,), ())) == [(1, 0), (2, 1)]


def test_chain_edges_keeps_an_extra_explicit_edge_and_dedupes():
    # 2 depends on 0 explicitly; sibling order also blocks it on 1.
    assert chain_edges(_d((), (0,), (0,))) == [(1, 0), (2, 0), (2, 1)]


def test_chain_edges_of_a_single_task_is_empty():
    assert chain_edges(_d(())) == []


def test_chain_edges_rejects_a_cycle():
    with pytest.raises(MaterializationParseError):
        chain_edges(_d((2,), (), ()))  # 0←2, 1←0, 2←1


def test_chain_edges_rejects_an_out_of_range_index():
    with pytest.raises(MaterializationParseError):
        chain_edges(_d((), (5,)))
    with pytest.raises(MaterializationParseError):
        chain_edges(_d((), (-1,)))


def test_chain_edges_rejects_a_self_dependency():
    with pytest.raises(MaterializationParseError):
        chain_edges(_d((), (1,)))


# --------------------------------------------------------------------------- #
# Mutation documents                                                           #
# --------------------------------------------------------------------------- #

def test_build_create_issue_without_a_parent():
    assert build_create_issue("R_1", "Epic", "Body") == (
        'mutation { createIssue(input: {repositoryId: "R_1", title: "Epic", '
        'body: "Body"}) { issue { id number url } } }')


def test_build_create_issue_with_a_parent_is_one_call():
    # Probe (phase0-findings "Materialization probe"): CreateIssueInput.parentIssueId
    # makes a task a sub-issue of the epic in the SAME call — no addSubIssue.
    doc = build_create_issue("R_1", 'T "1"', "Body", parent_issue_id="I_epic")
    assert doc == (
        'mutation { createIssue(input: {repositoryId: "R_1", title: "T \\"1\\"", '
        'body: "Body", parentIssueId: "I_epic"}) { issue { id number url } } }')


def test_blocked_by_mutation_key_matches_the_reconcilers_own_edge():
    """Journal suppression depends on this: the reconciler must recognise an edge
    materialization already wrote as one of its own (same key), whatever the
    initiated_by provenance."""
    mine = blocked_by_mutation("I_b", "I_a", initiated_by="materialize:acme")
    (theirs,) = compile_edges(
        [DepView(src_ref="o/r#2", dst_ref="o/r#1", source="plan", active=True)],
        {"o/r#2": "I_b", "o/r#1": "I_a"},
        set(),
    )
    assert mine.key() == theirs.key()
    assert mine.kind == "add_blocked_by"
    assert mine.initiated_by == "materialize:acme"


def test_blocked_by_mutation_renders_through_the_shared_batch_builder():
    doc, aliases = build_batch([blocked_by_mutation("I_b", "I_a", initiated_by="m")],
                               project_node_id="PVT_1", field_cache={})
    assert doc == ('mutation { m0: addBlockedBy(input: {issueId: "I_b", '
                   'blockingIssueId: "I_a"}) { clientMutationId } }')
    assert list(aliases) == ["m0"]
