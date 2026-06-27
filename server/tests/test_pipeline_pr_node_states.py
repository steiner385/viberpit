"""Task 4: PipelinePr carries node_states — serialization round-trip tests."""
from server.github.pr_status import PullRequestInfo, PipelinePr
from server.pipeline.track import NodeStatus


def _pr():
    return PullRequestInfo(number=1, url="u", state="open", checks="none", title="t", repo="o/r")


def test_node_states_in_to_dict_and_roundtrip():
    p = PipelinePr(pr=_pr(), session_id="s", session_name="n",
                   node_states=(NodeStatus("ci", "active"), NodeStatus("merged", "pending")))
    d = p.to_dict()
    assert d["node_states"] == [{"id": "ci", "state": "active"}, {"id": "merged", "state": "pending"}]
    assert PipelinePr.from_dict(d).node_states == p.node_states


def test_node_states_default_empty():
    assert PipelinePr(pr=_pr()).to_dict()["node_states"] == []
