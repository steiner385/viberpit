"""The roadmap node's `autoResolved` field (spec 2026-09-14 §11): what the
synthesizer did with a gate, only while the chip is still worth showing."""
from datetime import datetime, timedelta, timezone
from typing import Any

from server.pm.tree import InitiativeIn, _initiative_node
from server.sdlc.gates import AUTO_RESOLVED_VISIBLE_S, auto_gate_record


def _ini(**kw: Any) -> InitiativeIn:
    base: dict[str, Any] = dict(slug="s", title="S", stage="plan", status="active",
                                primary_slug=None, capability_slugs=(), epic=None)
    base.update(kw)
    return InitiativeIn(**base)


def test_node_carries_a_recent_auto_resolution_and_names_its_reverse():
    rec = auto_gate_record("stage.advance", "balanced", datetime.now(timezone.utc),
                           detail={"from": "design", "to": "plan"})
    node = _initiative_node(_ini(last_auto_gate=rec), "viberpit")
    assert node["autoResolved"]["summary"] == "advanced to plan"
    assert node["autoResolved"]["reverse"] == "Step back"
    assert node["autoResolved"]["outcome"] == "resolved"


def test_node_drops_an_expired_or_missing_record():
    old = auto_gate_record(
        "stage.advance", "balanced",
        datetime.now(timezone.utc) - timedelta(seconds=AUTO_RESOLVED_VISIBLE_S + 60),
        detail={"from": "design", "to": "plan"})
    assert _initiative_node(_ini(last_auto_gate=old), "viberpit")["autoResolved"] is None
    assert _initiative_node(_ini(), "viberpit")["autoResolved"] is None
