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


# --- required-check gating: only the checks that gate a MERGE gate a DEPLOY ---
# When branch protection is readable, ci_verdict must mirror it — a non-required or
# flaky check (e.g. e2e) can't wedge a deploy the merge queue would let through (#620).

def _named(*pairs, status="completed"):
    """check-runs JSON from (name, conclusion) pairs, all sharing one status."""
    return json.dumps({"check_runs": [
        {"name": n, "status": status, "conclusion": c} for n, c in pairs]})


def _gh(check_runs_json, *, contexts):
    """gh_api stub routing by path: the branch-protection endpoint returns the
    required contexts; every other path returns the check-runs payload."""
    def api(path):
        if "protection/required_status_checks" in path:
            return json.dumps({"contexts": list(contexts)})
        return check_runs_json
    return api


def test_non_required_failure_ignored_when_required_pass():
    runs = _named(("lint-and-test", "success"), ("frontend", "success"), ("e2e", "failure"))
    assert steps.ci_verdict("s", gh_api=_gh(runs, contexts=["lint-and-test", "frontend"])) == "green"


def test_required_failure_is_red():
    runs = _named(("lint-and-test", "failure"), ("frontend", "success"), ("e2e", "success"))
    assert steps.ci_verdict("s", gh_api=_gh(runs, contexts=["lint-and-test", "frontend"])) == "red"


def test_missing_required_check_is_pending():
    runs = _named(("lint-and-test", "success"), ("e2e", "success"))  # 'frontend' hasn't reported
    assert steps.ci_verdict("s", gh_api=_gh(runs, contexts=["lint-and-test", "frontend"])) == "pending"


def test_required_incomplete_is_pending():
    runs = json.dumps({"check_runs": [
        {"name": "lint-and-test", "status": "completed", "conclusion": "success"},
        {"name": "frontend", "status": "in_progress", "conclusion": None}]})
    assert steps.ci_verdict("s", gh_api=_gh(runs, contexts=["lint-and-test", "frontend"])) == "pending"


def test_unreadable_protection_falls_back_to_all_checks():
    # No admin scope → protection read fails → count every check (fail-safe, old behavior):
    # a non-required failing check still blocks, never the reverse.
    runs = _named(("lint-and-test", "success"), ("e2e", "failure"))

    def api(path):
        if "protection/required_status_checks" in path:
            raise RuntimeError("HTTP 403: admin required")
        return runs
    assert steps.ci_verdict("s", gh_api=api) == "red"
