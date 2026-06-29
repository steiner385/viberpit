# server/tests/test_merge_group_checks.py
from server.github.pr_status import build_eta_checks_query, parse_merge_group_response


def test_query_includes_merge_queue_entry_block():
    q = build_eta_checks_query([("octo/repo", 7)])
    assert "mergeQueueEntry" in q
    assert "estimatedTimeToMerge" in q
    # merge-group checks come from the entry's headCommit rollup, not the PR head commit
    assert "headCommit" in q


def test_parse_merge_group_extracts_checks_and_eta():
    resp = {"data": {"r0": {"pullRequest": {"mergeQueueEntry": {
        "estimatedTimeToMerge": 300,
        "headCommit": {"statusCheckRollup": {"contexts": {"nodes": [
            {"__typename": "CheckRun", "name": "build", "status": "COMPLETED",
             "conclusion": "SUCCESS", "startedAt": "2026-06-28T12:00:00Z",
             "completedAt": "2026-06-28T12:05:00Z",
             "checkSuite": {"workflowRun": {"workflow": {"name": "CI"}}}},
        ]}}},
    }}}}}
    out = parse_merge_group_response(resp, [("octo/repo", 7)])
    entry = out[("octo/repo", 7)]
    assert entry["eta_seconds"] == 300
    assert len(entry["checks"]) == 1
    c = entry["checks"][0]
    assert c["name"] == "build" and c["event"] == "merge_group" and c["isRequired"] is False
    assert c["workflowName"] == "CI"


def test_parse_merge_group_absent_entry_is_empty():
    resp = {"data": {"r0": {"pullRequest": {"mergeQueueEntry": None}}}}
    out = parse_merge_group_response(resp, [("octo/repo", 7)])
    assert out[("octo/repo", 7)] == {"checks": [], "eta_seconds": None}
