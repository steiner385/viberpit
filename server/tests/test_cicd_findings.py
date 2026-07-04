"""Finding assembly from analyzer outputs."""
from __future__ import annotations

from server.automations.cicd.findings import (
    check_transition_findings, flaky_test_findings, missing_junit_finding,
)
from server.automations.cicd.types import GateThresholds

TH = GateThresholds()


def test_flaky_test_findings_threshold_and_volume():
    out = flaky_test_findings("o/r", {
        "t::hot": (8.0, 12),   # above threshold, enough shas -> finding
        "t::ok": (0.0, 30),    # clean
        "t::thin": (50.0, 2),  # too few shas to trust
    }, th=TH)
    assert [f.key for f in out] == ["t::hot"]
    assert out[0].kind == "flaky_test" and out[0].detail["flake_pct"] == 8.0


def test_check_transition_findings_kinds():
    out = check_transition_findings("o/r", {"e2e": "graduate", "frontend": "demote"})
    assert {(f.kind, f.key) for f in out} == {
        ("graduate_check", "e2e"), ("demote_check", "frontend")}


def test_missing_junit_finding():
    f = missing_junit_finding("o/r")
    assert f.kind == "missing_junit" and f.fingerprint == "missing_junit:o/r:junit"
