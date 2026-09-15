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


def test_auto_gate_record_has_the_one_shape():
    from datetime import datetime, timezone
    from server.sdlc.gates import auto_gate_record
    at = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    rec = auto_gate_record("stage.advance", "balanced", at, detail={"from": "design", "to": "plan"})
    assert rec == {"gate": "stage.advance", "at": "2026-09-15T12:00:00+00:00",
                   "tolerance": "balanced", "outcome": "resolved",
                   "detail": {"from": "design", "to": "plan"}}
    assert auto_gate_record("artifact.merge", "bold", at, outcome="refused")["outcome"] == "refused"


def test_auto_resolved_view_inside_and_outside_the_window():
    from datetime import datetime, timedelta, timezone
    from server.sdlc.gates import AUTO_RESOLVED_VISIBLE_S, auto_gate_record, auto_resolved_view
    at = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    rec = auto_gate_record("stage.advance", "balanced", at, detail={"from": "design", "to": "plan"})
    view = auto_resolved_view(rec, at + timedelta(hours=1))
    assert view == {"gate": "stage.advance", "summary": "advanced to plan", "reverse": "Step back",
                    "at": "2026-09-15T12:00:00+00:00", "outcome": "resolved", "tolerance": "balanced"}
    # The chip leaves the row after the window — that is the point, not an error.
    assert auto_resolved_view(rec, at + timedelta(seconds=AUTO_RESOLVED_VISIBLE_S + 1)) is None
    assert auto_resolved_view(None, at) is None
    assert auto_resolved_view({"gate": "stage.advance", "at": "garbage"}, at) is None
    refused = auto_gate_record("artifact.merge", "balanced", at, outcome="refused",
                               detail={"kind": "spec", "pr": 7, "message": "auto-merge is not allowed"})
    assert auto_resolved_view(refused, at)["summary"] == "auto-merge unavailable"
    assert auto_resolved_view(auto_gate_record("artifact.merge", "balanced", at,
                                               detail={"kind": "spec"}), at)["summary"] == "merged spec"
