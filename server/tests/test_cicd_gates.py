"""Gate evaluation: graduate / demote / retire verdicts with hysteresis."""
from __future__ import annotations

from server.automations.cicd.gates import check_verdict
from server.automations.cicd.types import CheckStats, GateThresholds

TH = GateThresholds()


def _stats(**kw) -> CheckStats:
    base = dict(check="e2e", runs_total=50, window_days=30.0, flake_pct=0.5,
                recent_flake_pct=0.0, default_branch_failures=0,
                p95_duration_s=300.0, flake_rising=False)
    base.update(kw)
    return CheckStats(**base)  # type: ignore[arg-type]


def test_graduates_when_all_gates_pass():
    assert check_verdict(_stats(), is_required=False, th=TH) == "graduate"


def test_each_gate_blocks_graduation():
    for kw in (dict(window_days=10.0), dict(runs_total=10), dict(flake_pct=3.0),
               dict(default_branch_failures=1), dict(p95_duration_s=700.0),
               dict(flake_rising=True)):
        assert check_verdict(_stats(**kw), is_required=False, th=TH) is None, kw


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
