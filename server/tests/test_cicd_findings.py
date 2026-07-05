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
    assert "message" not in out[0].detail  # none supplied


def test_flaky_test_findings_attaches_message():
    """#450: a parsed failure message rides along in the finding detail."""
    out = flaky_test_findings("o/r", {"t::hot": (8.0, 12)}, th=TH,
                              messages={"t::hot": "AssertionError: 1 != 2",
                                        "t::cold": "ignored — not flaky"})
    assert out[0].detail["message"] == "AssertionError: 1 != 2"


def test_check_transition_findings_kinds_and_actionability():
    out = check_transition_findings(
        "o/r", {"e2e": "graduate", "frontend": "demote", "slow": "retire"})
    by_kind = {f.kind: f for f in out}
    assert set(by_kind) == {"graduate_check", "demote_check", "retire_check"}
    # #450: a graduation candidate is informational — it must NOT be actionable
    # (no ci-doctor issue filed); demote/retire are actionable.
    assert by_kind["graduate_check"].actionable is False
    assert by_kind["demote_check"].actionable is True
    assert by_kind["retire_check"].actionable is True


def test_missing_junit_finding():
    f = missing_junit_finding("o/r")
    assert f.kind == "missing_junit" and f.fingerprint == "missing_junit:o/r:junit"
