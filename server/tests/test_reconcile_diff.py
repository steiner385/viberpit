from datetime import datetime, timezone
from server.pm.desired_state import Decision, DesiredItem
from server.pm.reconcile_diff import (
    DepView, MirrorItem, Mutation, compile_edges, diff_fields, membership, should_push)

T1 = datetime(2026, 9, 1, tzinfo=timezone.utc); T2 = datetime(2026, 9, 2, tzinfo=timezone.utc)


def _mirror(**over):
    base = dict(ref="o/r#5", item_node_id="PVTI_5", status="Backlog", score=None, stage=None,
                gh_updated_at=T1)
    base.update(over)
    return MirrorItem(**base)


def test_ownership_rule():
    assert should_push(T2, T1, "Backlog") is True          # decision newer than GitHub
    assert should_push(T1, T2, "Backlog") is False         # human edit newer -> suppressed
    assert should_push(T1, T1, "Backlog") is False         # strictly newer required
    assert should_push(None, T2, None) is True             # unset field: initial fill
    assert should_push(None, T2, "Ready") is False         # no decision timestamp, value exists


def test_diff_fields_emits_only_changed_and_pushable():
    desired = DesiredItem("o/r#5", Decision("Ready", T2), Decision(480.0, T2), Decision(None, None))
    muts = diff_fields(desired, _mirror(status="Backlog", score=480.0))
    assert [(m.kind, m.field, m.old_value, m.new_value) for m in muts] == [
        ("set_field", "Status", "Backlog", "Ready")]
    assert muts[0].target_node_id == "PVTI_5" and muts[0].initiated_by == "status:o/r#5"
    # set fields suppressed when human-edited, but unset fields always fillable
    muts = diff_fields(desired, _mirror(status="Backlog", gh_updated_at=T2))
    assert [(m.kind, m.field, m.old_value, m.new_value) for m in muts] == [
        ("set_field", "Score", None, 480.0)]


def test_mutation_key_is_stable_and_value_sensitive():
    a = Mutation("set_field", "PVTI_5", "Status", "Backlog", "Ready", "status:o/r#5")
    b = Mutation("set_field", "PVTI_5", "Status", "Blocked", "Ready", "x")
    c = Mutation("set_field", "PVTI_5", "Status", "Backlog", "Done", "status:o/r#5")
    assert a.key() == b.key() and a.key() != c.key()


def test_membership_adds_missing_open_issues():
    muts = membership({"o/r#5": "I_5", "o/r#6": "I_6"}, {"o/r#5"}, "PVT_1")
    assert [(m.kind, m.target_node_id, m.new_value, m.initiated_by) for m in muts] == [
        ("add_item", "PVT_1", "I_6", "membership:o/r#6")]


def test_compile_edges_prose_without_native_twin():
    deps = [DepView("o/r#5", "o/r#3", "prose", True),
            DepView("o/r#6", "o/r#3", "prose", True), DepView("o/r#6", "o/r#3", "native", True),
            DepView("o/r#7", "o/r#3", "order", True), DepView("o/r#8", "o/r#3", "prose", False)]
    nodes = {"o/r#5": "I_5", "o/r#3": "I_3", "o/r#6": "I_6", "o/r#8": "I_8"}
    muts = compile_edges(deps, nodes, already=set())
    assert [(m.target_node_id, m.new_value, m.initiated_by) for m in muts] == [
        ("I_5", "I_3", "compile:prose:o/r#5->o/r#3")]   # #6 has native twin; #7 unresolved node; #8 inactive
    assert compile_edges(deps, nodes, already={muts[0].key()}) == []


def test_initial_fill_pushes_even_when_item_updated_after_decision():
    assert should_push(T1, T2, None) is True
