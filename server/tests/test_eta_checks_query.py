from server.github.pr_status import build_eta_checks_query, parse_eta_checks_response


def test_build_eta_query_embeds_number_in_both_places():
    q = build_eta_checks_query([("steiner385/cc-account-switcher", 293)])
    assert "pullRequest(number: 293)" in q
    assert "isRequired(pullRequestNumber: 293)" in q
    assert "startedAt" in q and "status" in q
    assert "completedAt" in q


def test_parse_eta_checks_maps_checkruns():
    resp = {"data": {"r0": {"pullRequest": {"commits": {"nodes": [{"commit": {"statusCheckRollup":
        {"contexts": {"nodes": [
            {"__typename": "CheckRun", "name": "lint-and-test", "status": "IN_PROGRESS",
             "conclusion": None, "startedAt": "2026-06-25T11:59:00Z",
             "completedAt": "2026-06-25T12:05:00Z",
             "isRequired": True,
             "checkSuite": {"workflowRun": {"workflow": {"name": "CI"}}}},
        ]}}}}]}}}}}
    out = parse_eta_checks_response(resp, [("o/n", 293)])
    chk = out[("o/n", 293)][0]
    assert chk == {"name": "lint-and-test", "status": "IN_PROGRESS", "conclusion": None,
                   "started_at": "2026-06-25T11:59:00Z",
                   "completed_at": "2026-06-25T12:05:00Z",
                   "isRequired": True,
                   "workflowName": "CI", "event": "pull_request"}


def test_parse_eta_checks_maps_status_context():
    resp = {"data": {"r0": {"pullRequest": {"commits": {"nodes": [{"commit": {"statusCheckRollup":
        {"contexts": {"nodes": [
            {"__typename": "StatusContext", "context": "legacy-check", "state": "SUCCESS"},
        ]}}}}]}}}}}
    out = parse_eta_checks_response(resp, [("o/n", 293)])
    chk = out[("o/n", 293)][0]
    assert chk == {"name": "legacy-check", "status": "COMPLETED", "conclusion": "SUCCESS",
                   "started_at": None, "completed_at": None,
                   "isRequired": False, "workflowName": None, "event": "pull_request"}


def test_build_eta_query_empty():
    assert build_eta_checks_query([]) == ""


def test_parse_eta_checks_empty_response():
    out = parse_eta_checks_response({}, [("o/n", 1)])
    assert out == {("o/n", 1): []}


def test_parse_eta_checks_multiple_prs():
    resp = {"data": {
        "r0": {"pullRequest": {"commits": {"nodes": [{"commit": {"statusCheckRollup":
            {"contexts": {"nodes": [
                {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED",
                 "conclusion": "SUCCESS", "startedAt": "2026-06-25T10:00:00Z",
                 "completedAt": "2026-06-25T10:10:00Z",
                 "isRequired": True,
                 "checkSuite": {"workflowRun": {"workflow": {"name": "CI"}}}},
            ]}}}}]}}},
        "r1": {"pullRequest": {"commits": {"nodes": [{"commit": {"statusCheckRollup":
            {"contexts": {"nodes": []}}}}]}}},
    }}
    prs = [("owner/repo", 10), ("owner/repo", 20)]
    out = parse_eta_checks_response(resp, prs)
    assert len(out[("owner/repo", 10)]) == 1
    assert out[("owner/repo", 10)][0]["completed_at"] == "2026-06-25T10:10:00Z"
    assert out[("owner/repo", 20)] == []
