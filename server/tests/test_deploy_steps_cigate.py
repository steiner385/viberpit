import json
import subprocess

import pytest

from server.deploy import steps


def test_default_gh_api_authenticates_as_app(monkeypatch):
    """The deploy gate's default gh reader authenticates as the deploy repo's App
    installation, not an ambient GITHUB_TOKEN in the process environment (issue #913).
    Non-zero must still raise CalledProcessError (the ci_verdict/publish contract)."""
    from types import SimpleNamespace

    from ccswitch import gitrepos
    monkeypatch.setattr(gitrepos, "_app_token", lambda owner: f"ghs_{owner}")
    seen = {}

    def fake_run_gh(args, token=None, *, input=None, timeout=None):
        seen["token"] = token
        seen["args"] = args
        return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="",
                               args=["gh", *args])

    monkeypatch.setattr(gitrepos, "_run_gh", fake_run_gh)
    out = steps._default_gh_api("repos/{owner}/{repo}/commits/abc/check-runs")
    assert out == '{"ok":true}'
    assert seen["token"] == f"ghs_{steps._DEPLOY_OWNER}"  # App token for steiner385

    # A non-zero gh must surface as CalledProcessError carrying stderr, so
    # _gh_err_reason and the "any raise → unavailable" callers still work.
    def failing(args, token=None, *, input=None, timeout=None):
        return SimpleNamespace(returncode=1, stdout="", stderr="gh: HTTP 403",
                               args=["gh", *args])

    monkeypatch.setattr(gitrepos, "_run_gh", failing)
    with pytest.raises(subprocess.CalledProcessError):
        steps._default_gh_api("repos/{owner}/{repo}/commits/abc/check-runs")


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


# --- a required check that will NEVER report: "unrun", not a forever-"pending" ---
# GitHub keeps at most ONE pending run per concurrency group and cancels any earlier
# pending one — even with cancel-in-progress:false. During a merge burst on master the
# middle commits' CI runs are therefore cancelled before a single job starts, so those
# shas carry NO check-run for the required contexts, ever. On the check-runs endpoint
# alone that is indistinguishable from "the run hasn't started yet", so the gate
# reported "pending" forever and the deploy banner spun on a verdict that could not
# resolve. Consulting the workflow RUNS for the sha separates the two.

def _wf(*pairs):
    """actions/runs JSON from (status, conclusion) pairs."""
    return json.dumps({"workflow_runs": [{"status": s, "conclusion": c} for s, c in pairs]})


def _gh_runs(check_runs_json, *, contexts, runs_json):
    """gh_api stub routing three ways: protection, actions/runs, check-runs."""
    def api(path):
        if "protection/required_status_checks" in path:
            return json.dumps({"contexts": list(contexts)})
        if "actions/runs" in path:
            return runs_json
        return check_runs_json
    return api


_REQUIRED = ["lint-and-test", "frontend", "amendment"]


def test_missing_required_check_with_a_cancelled_run_is_unrun():
    runs = _named(("amendment", "success"))     # ci.yml was cancelled before any job ran
    api = _gh_runs(runs, contexts=_REQUIRED,
                   runs_json=_wf(("completed", "success"), ("completed", "cancelled")))
    assert steps.ci_verdict("s", gh_api=api) == "unrun"


def test_missing_required_check_stays_pending_while_a_run_is_in_flight():
    # A cancelled sibling does not make an in-flight run's verdict final.
    runs = _named(("amendment", "success"))
    api = _gh_runs(runs, contexts=_REQUIRED,
                   runs_json=_wf(("completed", "cancelled"), ("in_progress", None)))
    assert steps.ci_verdict("s", gh_api=api) == "pending"


def test_missing_required_check_stays_pending_when_nothing_was_cancelled():
    # Every run finished cleanly and the context is still absent — the check-run may
    # simply not have propagated yet. Conservative: pending, never "unrun".
    runs = _named(("amendment", "success"))
    api = _gh_runs(runs, contexts=_REQUIRED, runs_json=_wf(("completed", "success")))
    assert steps.ci_verdict("s", gh_api=api) == "pending"


def test_unrun_probe_failure_falls_back_to_pending():
    # The extra probe is best-effort: an unreadable actions API must never upgrade the
    # verdict's severity beyond the historical "pending".
    runs = _named(("amendment", "success"))

    def api(path):
        if "protection/required_status_checks" in path:
            return json.dumps({"contexts": _REQUIRED})
        if "actions/runs" in path:
            raise RuntimeError("HTTP 502")
        return runs
    assert steps.ci_verdict("s", gh_api=api) == "pending"


def test_green_path_never_probes_the_actions_api():
    # The probe is one extra round-trip per detect tick; it must fire only on the rare
    # missing-context branch, not on every healthy commit.
    runs = _named(("lint-and-test", "success"), ("frontend", "success"),
                  ("amendment", "success"))
    paths: list[str] = []

    def api(path):
        paths.append(path)
        if "protection/required_status_checks" in path:
            return json.dumps({"contexts": _REQUIRED})
        return runs
    assert steps.ci_verdict("s", gh_api=api) == "green"
    assert not any("actions/runs" in p for p in paths)


# --- a SUPERSEDED check-run must not outvote the one that replaced it (#1557) ---
# A commit can carry several check-runs of the same name: GitHub double-fires a push
# event, or a concurrency group cancels the earlier run, or an operator re-runs a flaky
# job. Branch protection judges a required context by its LATEST run of that name, which
# is why master tip c20211a3 merged cleanly while the deploy controller refused it:
# `amendment` had a cancelled run from the superseded attempt sitting beside the
# successful one, and folding over BOTH read the commit red (2026-09-11).
#
# Fail-safe in the other direction: collapsing duplicates we cannot order could turn a
# genuine red green, so the newest is picked only on positive evidence (id, then
# started_at) and an unorderable group keeps the strict all-runs reading.

def _dup(*runs, status="completed"):
    """check-runs JSON from explicit run dicts, defaulting status to completed."""
    return json.dumps({"check_runs": [{"status": status, **r} for r in runs]})


def _required(check_runs_json):
    return _gh(check_runs_json, contexts=_REQUIRED)


def test_cancelled_run_superseded_by_a_successful_rerun_is_green():
    runs = _dup(
        {"name": "lint-and-test", "conclusion": "success", "id": 10},
        {"name": "frontend", "conclusion": "success", "id": 11},
        {"name": "amendment", "conclusion": "cancelled", "id": 12},
        {"name": "amendment", "conclusion": "success", "id": 13},
    )
    assert steps.ci_verdict("s", gh_api=_required(runs)) == "green"


def test_latest_per_name_does_not_depend_on_payload_order():
    # The check-runs endpoint makes no ordering promise, so the newest run may arrive
    # first. Recency comes from the run's own id, never from its position in the list.
    runs = _dup(
        {"name": "amendment", "conclusion": "success", "id": 13},
        {"name": "amendment", "conclusion": "cancelled", "id": 12},
        {"name": "lint-and-test", "conclusion": "success", "id": 10},
        {"name": "frontend", "conclusion": "success", "id": 11},
    )
    assert steps.ci_verdict("s", gh_api=_required(runs)) == "green"


def test_a_rerun_that_failed_supersedes_an_earlier_success_and_is_red():
    # The inverse of the fix: newest wins even when newest is the bad one.
    runs = _dup(
        {"name": "lint-and-test", "conclusion": "success", "id": 10},
        {"name": "frontend", "conclusion": "success", "id": 11},
        {"name": "amendment", "conclusion": "success", "id": 12},
        {"name": "amendment", "conclusion": "failure", "id": 13},
    )
    assert steps.ci_verdict("s", gh_api=_required(runs)) == "red"


def test_cancelled_run_superseded_by_an_in_flight_rerun_is_pending():
    # `gh run rerun` against a cancelled run: the replacement is queued, not finished.
    # The verdict follows the LATEST run, so it is pending — not the old run's red.
    runs = json.dumps({"check_runs": [
        {"name": "lint-and-test", "status": "completed", "conclusion": "success", "id": 10},
        {"name": "frontend", "status": "completed", "conclusion": "success", "id": 11},
        {"name": "amendment", "status": "completed", "conclusion": "cancelled", "id": 12},
        {"name": "amendment", "status": "in_progress", "conclusion": None, "id": 13},
    ]})
    assert steps.ci_verdict("s", gh_api=_required(runs)) == "pending"


def test_started_at_orders_duplicates_when_ids_are_absent():
    runs = _dup(
        {"name": "lint-and-test", "conclusion": "success", "started_at": "2026-09-11T21:07:00Z"},
        {"name": "frontend", "conclusion": "success", "started_at": "2026-09-11T21:07:00Z"},
        {"name": "amendment", "conclusion": "cancelled", "started_at": "2026-09-11T21:07:10Z"},
        {"name": "amendment", "conclusion": "success", "started_at": "2026-09-11T21:09:30Z"},
    )
    assert steps.ci_verdict("s", gh_api=_required(runs)) == "green"


def test_unorderable_duplicates_keep_the_strict_all_runs_reading():
    # No id and no started_at: nothing says which run supersedes which. Guessing could
    # turn a real failure green, so an unorderable group stays strict and blocks.
    runs = _dup(
        {"name": "lint-and-test", "conclusion": "success"},
        {"name": "frontend", "conclusion": "success"},
        {"name": "amendment", "conclusion": "cancelled"},
        {"name": "amendment", "conclusion": "success"},
    )
    assert steps.ci_verdict("s", gh_api=_required(runs)) == "red"


def test_duplicates_sharing_a_started_at_stay_strict():
    # Equal timestamps order nothing either — same fail-safe as no timestamps at all.
    runs = _dup(
        {"name": "lint-and-test", "conclusion": "success"},
        {"name": "frontend", "conclusion": "success"},
        {"name": "amendment", "conclusion": "cancelled", "started_at": "2026-09-11T21:07:10Z"},
        {"name": "amendment", "conclusion": "success", "started_at": "2026-09-11T21:07:10Z"},
    )
    assert steps.ci_verdict("s", gh_api=_required(runs)) == "red"


def test_superseded_cancelled_run_is_green_without_branch_protection_too():
    # A non-admin token cannot read required contexts and falls back to counting every
    # check. That fallback narrows WHICH runs are counted, never HOW MANY runs per name,
    # so a superseded duplicate must not wedge the deploy there either.
    runs = _dup(
        {"name": "lint-and-test", "conclusion": "success", "id": 10},
        {"name": "amendment", "conclusion": "cancelled", "id": 12},
        {"name": "amendment", "conclusion": "success", "id": 13},
    )

    def api(path):
        if "protection/required_status_checks" in path:
            raise RuntimeError("HTTP 403: admin required")
        return runs
    assert steps.ci_verdict("s", gh_api=api) == "green"


def test_unnamed_check_runs_are_never_collapsed_together():
    # Legacy/synthetic payloads carry no `name`. They are not duplicates of each other,
    # so every one of them must still count — a failure among them stays red.
    runs = _dup({"conclusion": "failure"}, {"conclusion": "success"})

    def api(path):
        if "protection/required_status_checks" in path:
            raise RuntimeError("HTTP 403: admin required")
        return runs
    assert steps.ci_verdict("s", gh_api=api) == "red"
