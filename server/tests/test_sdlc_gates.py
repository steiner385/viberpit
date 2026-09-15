"""Gate registry (spec 2026-09-14 §4): one inventory of where the roadmap waits on
a human, one rule for whether a product's tolerance lets the machine act."""
import pytest

from server.sdlc.gates import (
    GATE_BY_ID, GATES, RISK_CLASSES, RISK_TOLERANCES, auto_resolves,
    effective_tolerance,
)


def test_vocabularies():
    assert RISK_CLASSES == ("low", "medium", "high")
    assert RISK_TOLERANCES == ("cautious", "balanced", "bold")


def test_registry_lists_every_spec_gate_with_its_risk():
    risks = {g.id: g.risk for g in GATES}
    assert risks == {
        "artifact.merge": "low",
        "stage.advance": "low",
        "stage.advance_built": "low",
        "materialization.accept": "medium",
        "model.upgrade": "medium",
        "dispatch": None,
        "triage.accept": None,
        "issue.needs_spec": "high",
        "session.hand_back": "high",
        "initiative.blocked": "high",
        "triage.no_fit": "high",
        "deploy.production": "high",
        "issue.close": "high",
    }
    assert GATE_BY_ID["stage.advance"].reverse == "Step back"
    for g in GATES:
        assert g.label and g.action and g.reverse


@pytest.mark.parametrize("tolerance,expected", [
    ("cautious", {"artifact.merge": False, "materialization.accept": False, "issue.close": False}),
    ("balanced", {"artifact.merge": True, "stage.advance": True, "stage.advance_built": True,
                  "materialization.accept": False, "model.upgrade": False, "issue.close": False}),
    ("bold", {"artifact.merge": True, "materialization.accept": True, "model.upgrade": True,
              "issue.close": False, "deploy.production": False}),
])
def test_auto_resolves_matrix(tolerance, expected):
    for gate_id, want in expected.items():
        assert auto_resolves(gate_id, tolerance) is want, (gate_id, tolerance)


def test_policy_owned_gates_and_unknowns_never_auto_resolve_here():
    # dispatch / triage.accept have their own knobs; this rule must not double-decide them.
    assert auto_resolves("dispatch", "bold") is False
    assert auto_resolves("triage.accept", "bold") is False
    assert auto_resolves("no.such.gate", "bold") is False
    assert auto_resolves("artifact.merge", "turbo") is False


def test_effective_tolerance_kill_switch_and_garbage():
    assert effective_tolerance("bold", enabled=True) == "bold"
    assert effective_tolerance("bold", enabled=False) == "cautious"
    assert effective_tolerance(None, enabled=True) == "cautious"
    assert effective_tolerance("turbo", enabled=True) == "cautious"


def test_settings_carry_the_kill_switch(monkeypatch):
    from server.config import Settings
    monkeypatch.delenv("VIBERPIT_GATE_AUTORESOLVE_ENABLED", raising=False)
    assert Settings().gate_autoresolve_enabled is True
    monkeypatch.setenv("VIBERPIT_GATE_AUTORESOLVE_ENABLED", "false")
    assert Settings().gate_autoresolve_enabled is False
