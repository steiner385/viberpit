"""mergeable / merge_state_status / auto_merge_enabled: query, parse, round-trip."""
from __future__ import annotations

from server.github.pr_status import PullRequestInfo, _to_info, build_pr_query


def test_query_requests_new_fields():
    q = build_pr_query([("owner/repo", "feat/x")])
    assert "mergeable" in q
    assert "autoMergeRequest" in q
    assert "mergeStateStatus" in q


def test_to_info_populates_merge_fields():
    node = {"number": 5, "url": "u", "state": "OPEN", "title": "t", "isDraft": False,
            "headRefOid": "abc", "updatedAt": "", "mergeable": "CONFLICTING",
            "mergeStateStatus": "DIRTY", "autoMergeRequest": {"enabledAt": "2026-06-30T00:00:00Z"},
            "commits": {"nodes": []}}
    info = _to_info(node, {})
    assert info.mergeable == "CONFLICTING"
    assert info.merge_state_status == "DIRTY"
    assert info.auto_merge_enabled is True


def test_to_info_defaults_when_absent():
    node = {"number": 6, "url": "u", "state": "OPEN", "title": "t", "isDraft": False,
            "headRefOid": "", "updatedAt": "", "commits": {"nodes": []}}
    info = _to_info(node, {})
    assert info.mergeable == "UNKNOWN"
    assert info.merge_state_status == ""
    assert info.auto_merge_enabled is False


def test_round_trip_preserves_merge_fields():
    info = PullRequestInfo(number=5, url="u", state="open", checks="failing", title="t",
                           mergeable="CONFLICTING", merge_state_status="DIRTY", auto_merge_enabled=True)
    back = PullRequestInfo.from_dict(info.to_dict())
    assert back.mergeable == "CONFLICTING"
    assert back.merge_state_status == "DIRTY"
    assert back.auto_merge_enabled is True


# --- merge-queue enrollment tests ---

def test_query_requests_merge_queue_entry():
    """_PR_NODE_FIELDS must include mergeQueueEntry so both per-branch and
    per-repo queries fetch it (they share the constant)."""
    from server.github.pr_status import _PR_NODE_FIELDS
    assert "mergeQueueEntry" in _PR_NODE_FIELDS


def test_to_info_sets_in_merge_queue_true_when_present():
    node = {"number": 7, "url": "u", "state": "OPEN", "title": "t", "isDraft": False,
            "headRefOid": "abc", "updatedAt": "",
            "mergeQueueEntry": {"state": "QUEUED"},
            "commits": {"nodes": []}}
    info = _to_info(node, {})
    assert info.in_merge_queue is True


def test_to_info_sets_in_merge_queue_false_when_null():
    node = {"number": 8, "url": "u", "state": "OPEN", "title": "t", "isDraft": False,
            "headRefOid": "", "updatedAt": "",
            "mergeQueueEntry": None,
            "commits": {"nodes": []}}
    info = _to_info(node, {})
    assert info.in_merge_queue is False


def test_to_info_sets_in_merge_queue_false_when_absent():
    node = {"number": 9, "url": "u", "state": "OPEN", "title": "t", "isDraft": False,
            "headRefOid": "", "updatedAt": "", "commits": {"nodes": []}}
    info = _to_info(node, {})
    assert info.in_merge_queue is False


def test_round_trip_preserves_in_merge_queue_true():
    info = PullRequestInfo(number=10, url="u", state="open", checks="passing", title="t",
                           in_merge_queue=True)
    back = PullRequestInfo.from_dict(info.to_dict())
    assert back.in_merge_queue is True


def test_round_trip_preserves_in_merge_queue_false():
    info = PullRequestInfo(number=11, url="u", state="open", checks="passing", title="t",
                           in_merge_queue=False)
    back = PullRequestInfo.from_dict(info.to_dict())
    assert back.in_merge_queue is False


def test_from_dict_defaults_in_merge_queue_false_when_missing():
    """Old serialized dicts without in_merge_queue must deserialize to False."""
    d = {"number": 12, "url": "u", "state": "open", "checks": "passing", "title": "t"}
    info = PullRequestInfo.from_dict(d)
    assert info.in_merge_queue is False


# --- touchesWorkflows (§4): query field, path detection, round-trip ---

def test_query_requests_files_field():
    q = build_pr_query([("owner/repo", "feat/x")])
    assert "files(first: 100)" in q
    assert "path" in q


def _node_with_files(paths):
    return {"number": 20, "url": "u", "state": "OPEN", "title": "t", "isDraft": False,
            "headRefOid": "abc", "updatedAt": "", "commits": {"nodes": []},
            "files": {"nodes": [{"path": p} for p in paths]}}


def test_to_info_touches_workflows_true_for_workflow_path():
    info = _to_info(_node_with_files([".github/workflows/ci.yml", "src/x.py"]), {})
    assert info.touches_workflows is True


def test_to_info_touches_workflows_false_for_non_workflow_paths():
    info = _to_info(_node_with_files(["src/x.py", ".github/dependabot.yml"]), {})
    assert info.touches_workflows is False


def test_to_info_touches_workflows_false_when_files_absent():
    node = {"number": 21, "url": "u", "state": "OPEN", "title": "t", "isDraft": False,
            "headRefOid": "", "updatedAt": "", "commits": {"nodes": []}}
    assert _to_info(node, {}).touches_workflows is False


def test_round_trip_preserves_touches_workflows():
    info = PullRequestInfo(number=22, url="u", state="open", checks="passing", title="t",
                           touches_workflows=True)
    back = PullRequestInfo.from_dict(info.to_dict())
    assert back.touches_workflows is True


def test_from_dict_defaults_touches_workflows_false_when_missing():
    d = {"number": 23, "url": "u", "state": "open", "checks": "passing", "title": "t"}
    assert PullRequestInfo.from_dict(d).touches_workflows is False
