from server.pipeline.track import StageNode, StageModel, NodeStatus


def test_stage_node_roundtrip():
    n = StageNode(id="ci", label="CI", kind="ci", order=0)
    assert n.to_dict() == {"id": "ci", "label": "CI", "kind": "ci", "order": 0, "source": "derived"}
    assert StageNode.from_dict(n.to_dict()) == n


def test_stage_model_roundtrip():
    m = StageModel(repo="o/r", nodes=(StageNode("ci", "CI", "ci", 0), StageNode("merged", "Merged", "merged", 1)))
    d = m.to_dict()
    assert d["repo"] == "o/r"
    assert [x["id"] for x in d["nodes"]] == ["ci", "merged"]
    assert StageModel.from_dict(d) == m


def test_node_status_roundtrip():
    s = NodeStatus(id="ci", state="active")
    assert s.to_dict() == {"id": "ci", "state": "active"}
    assert NodeStatus.from_dict(s.to_dict()) == s
