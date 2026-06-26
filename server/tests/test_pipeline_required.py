from datetime import datetime, timezone
from server.pipeline.required import (
    required_checks, build_expected_set, effective_prefixes, rollup_workflow_for,
    workflow_scope_allows,
)

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)


def test_no_config_collapses():
    assert effective_prefixes("o/n") == []
    assert rollup_workflow_for("o/n") is None
    assert workflow_scope_allows("any", None) is True


def test_required_checks_falls_back_to_all_when_no_signal():
    checks = [{"name": "a", "event": "pull_request", "isRequired": False, "workflowName": "ci"},
              {"name": "b", "event": "pull_request", "isRequired": False, "workflowName": "ci"}]
    assert [c["name"] for c in required_checks(checks, [], None)] == ["a", "b"]


def test_required_checks_uses_isrequired_marks_when_present():
    checks = [{"name": "a", "event": "pull_request", "isRequired": True, "workflowName": "ci"},
              {"name": "b", "event": "pull_request", "isRequired": False, "workflowName": "ci"}]
    assert [c["name"] for c in required_checks(checks, [], None)] == ["a"]


def test_required_checks_drops_merge_group():
    checks = [{"name": "a", "event": "merge_group", "isRequired": True, "workflowName": "ci"},
              {"name": "b", "event": "pull_request", "isRequired": True, "workflowName": "ci"}]
    assert [c["name"] for c in required_checks(checks, [], None)] == ["b"]


def test_expected_set_filters_to_required_when_marked():
    history = ["a", "b", "c"]
    checks = [{"name": "a", "event": "pull_request", "isRequired": True, "workflowName": "ci"},
              {"name": "b", "event": "pull_request", "isRequired": False, "workflowName": "ci"}]
    assert sorted(build_expected_set(history, checks, [], None, NOW)) == ["a"]
