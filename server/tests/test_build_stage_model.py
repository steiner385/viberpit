from server.pipeline.track import build_stage_model


def _ids(m):
    return [n.id for n in m.nodes]


def test_minimal_repo_collapses_to_ci_ready_merged():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, has_deploy=False)
    assert _ids(m) == ["ci", "ready", "merged"]
    assert [n.order for n in m.nodes] == [0, 1, 2]
    assert m.nodes[0].label == "CI"  # default when no ciGraph


def test_ci_label_from_cigraph_workflow_name():
    m = build_stage_model("o/r", ci_graph={"workflowName": "Tests"}, has_queue=False, has_deploy=False)
    assert m.nodes[0].label == "Tests"


def test_queue_node_only_when_has_queue():
    m = build_stage_model("o/r", ci_graph=None, has_queue=True, has_deploy=False)
    assert _ids(m) == ["ci", "ready", "queue", "merged"]


def test_deploy_node_only_when_has_deploy():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, has_deploy=True)
    assert _ids(m) == ["ci", "ready", "deploy:production", "merged"]
    assert m.nodes[2].kind == "deploy"


def test_full_track_order():
    m = build_stage_model("o/r", ci_graph={"workflowName": "CI"}, has_queue=True, has_deploy=True)
    assert _ids(m) == ["ci", "ready", "queue", "deploy:production", "merged"]
    assert [n.order for n in m.nodes] == [0, 1, 2, 3, 4]
