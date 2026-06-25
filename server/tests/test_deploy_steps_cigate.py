import json
from server.deploy import steps


def _runs(*concs, status="completed"):
    return json.dumps({"check_runs": [
        {"status": status, "conclusion": c} for c in concs]})


def test_all_success_is_green():
    assert steps.ci_verdict("s", gh_api=lambda p: _runs("success", "skipped")) == "green"


def test_zero_runs_is_pending():
    assert steps.ci_verdict("s", gh_api=lambda p: json.dumps({"check_runs": []})) == "pending"


def test_incomplete_run_is_pending():
    assert steps.ci_verdict("s", gh_api=lambda p: _runs("success", None, status="in_progress")) == "pending"


def test_failure_is_red():
    assert steps.ci_verdict("s", gh_api=lambda p: _runs("success", "failure")) == "red"


def test_exception_is_unavailable():
    def boom(p):
        raise RuntimeError("network")
    assert steps.ci_verdict("s", gh_api=boom) == "unavailable"
