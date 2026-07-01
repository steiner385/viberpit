# server/tests/test_merge_queue_parse.py
import json
from pathlib import Path
from server.github.merge_queue import build_merge_queue_query, parse_merge_queue_response, QueueEntry

def test_build_query_aliases_one_block_per_repo():
    q = build_merge_queue_query(["cairnea/KinDash", "acme/web"])
    assert q.count("mergeQueue") == 2
    assert "r0: repository(owner: \"cairnea\", name: \"KinDash\")" in q
    assert "r1: repository(owner: \"acme\", name: \"web\")" in q
    assert q.count("{") == q.count("}")

def test_build_query_empty_repos_returns_empty_string():
    assert build_merge_queue_query([]) == ""

def test_parse_maps_entries_per_repo():
    resp = {"data": {"r0": {"mergeQueue": {"entries": {"nodes": [
        {"position": 1, "state": "AWAITING_CHECKS", "enqueuedAt": "2026-06-30T10:00:00Z",
         "estimatedTimeToMerge": 720,
         "headCommit": {"oid": "abc", "statusCheckRollup": {"state": "PENDING",
            "contexts": {"totalCount": 4, "nodes": [
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SKIPPED"},
                {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None},
                {"__typename": "StatusContext", "state": "PENDING"}]}}},
         "pullRequest": {"number": 10824}},
        {"position": 2, "state": "UNMERGEABLE", "enqueuedAt": "2026-06-30T09:30:00Z",
         "estimatedTimeToMerge": None,
         "headCommit": {"oid": "def", "statusCheckRollup": {"state": "FAILURE",
            "contexts": {"totalCount": 4, "nodes": [
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}]}}},
         "pullRequest": {"number": 10805}},
    ]}}}}}
    out = parse_merge_queue_response(resp, ["cairnea/KinDash"])
    entries = out["cairnea/KinDash"]
    assert entries[0] == QueueEntry(
        pr_number=10824, position=1, state="AWAITING_CHECKS",
        enqueued_at=1782813600, batch_head_oid="abc",
        group_checks="pending", checks_total=4, checks_done=2, eta_seconds=720)
    assert entries[1].state == "UNMERGEABLE"
    assert entries[1].group_checks == "failing"
    assert entries[1].checks_done == 1

def test_parse_real_fixture_does_not_raise():
    fix = json.loads(Path("server/tests/fixtures/merge_queue_kindash.json").read_text())
    out = parse_merge_queue_response(fix, ["cairnea/KinDash"])
    assert isinstance(out["cairnea/KinDash"], tuple)

def test_parse_null_rollup_is_unknown():
    # synthetic fixture (Task 1) covers UNMERGEABLE + null rollup + a failing batch.
    fix = json.loads(Path("server/tests/fixtures/merge_queue_synthetic.json").read_text())
    by_num = {e.pr_number: e for e in parse_merge_queue_response(fix, ["cairnea/KinDash"])["cairnea/KinDash"]}
    assert by_num[10805].group_checks is None      # statusCheckRollup: null → unknown
    assert by_num[10805].checks_total is None
    assert by_num[10823].group_checks == "failing"
    assert by_num[10824].checks_done == 2          # 2 COMPLETED of 4
