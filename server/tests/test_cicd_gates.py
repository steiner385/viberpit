"""Gate evaluation: graduate / demote / retire verdicts with hysteresis."""
from __future__ import annotations

from server.automations.cicd.gates import check_verdict
from server.automations.cicd.types import CheckStats, GateThresholds

TH = GateThresholds()


def _stats(**kw) -> CheckStats:
    base: dict = dict(check="e2e", runs_total=50, pr_runs_total=50, window_days=30.0,
                      flake_pct=0.5, recent_flake_pct=0.0, default_branch_failures=0,
                      p95_duration_s=300.0, flake_rising=False)
    base.update(kw)
    return CheckStats(**base)


def test_graduates_when_all_gates_pass():
    assert check_verdict(_stats(), is_required=False, th=TH) == "graduate"


def test_each_gate_blocks_graduation():
    for kw in (dict(window_days=10.0), dict(runs_total=10), dict(flake_pct=3.0),
               dict(default_branch_failures=1), dict(p95_duration_s=700.0),
               dict(flake_rising=True), dict(pr_runs_total=10)):
        assert check_verdict(_stats(**kw), is_required=False, th=TH) is None, kw


def test_graduation_evidence_must_come_from_pr_runs():
    """A job that only ever runs on schedule/workflow_run/push (`nightly`, `probe`,
    `rerun` …) can never be a required PR check, however stable: 50 runs with
    none on a pull_request/merge_group event is no evidence at all."""
    assert check_verdict(_stats(pr_runs_total=0), is_required=False, th=TH) is None
    # A check that runs on PRs *and* on a schedule still qualifies.
    assert check_verdict(_stats(runs_total=80, pr_runs_total=30),
                         is_required=False, th=TH) == "graduate"


def test_folded_child_gets_no_verdict():
    """A job another job `needs:` is already folded into its dependent's result —
    its tier is decided by the parent (the aggregator), so neither graduation nor
    retirement is proposed for it. A required check keeps its demote path."""
    assert check_verdict(_stats(), is_required=False, th=TH, folded=True) is None
    noisy = _stats(window_days=70.0, flake_pct=8.0)
    assert check_verdict(noisy, is_required=False, th=TH, folded=True) is None
    assert check_verdict(_stats(recent_flake_pct=6.0), is_required=True, th=TH,
                         folded=True) == "demote"


def test_demote_needs_sustained_recent_flake():
    assert check_verdict(_stats(recent_flake_pct=6.0), is_required=True, th=TH) == "demote"
    assert check_verdict(_stats(recent_flake_pct=3.0), is_required=True, th=TH) is None


def test_hysteresis_no_oscillation():
    # 3% flake: too flaky to graduate, not flaky enough to demote.
    s = _stats(flake_pct=3.0, recent_flake_pct=3.0)
    assert check_verdict(s, is_required=False, th=TH) is None
    assert check_verdict(s, is_required=True, th=TH) is None


def test_retire_after_long_noisy_advisory():
    s = _stats(window_days=70.0, flake_pct=8.0)
    assert check_verdict(s, is_required=False, th=TH) == "retire"
