"""The owner digest, pure half (design 2026-10-14 §5): which item gets which ONE
recommended action, how items group and merge, and when the daily push is due."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server.pm import digest as dg
from server.pm.escalation import HeldItem
from server.pm.outcomes import Escalation

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def _held(ref: str = "o/r#1", *, kind: str = "owner_action", reason: str = "owner call",
          escalation: Escalation | None = None, attention: str = "owner_action",
          wid: str | None = "wi1") -> HeldItem:
    return HeldItem(ref=ref, repo="o/r", number=int(ref.split("#")[1]), product="p",
                    title=f"issue {ref}", url=f"https://github.com/{ref.replace('#', '/issues/')}",
                    hold_kind=kind, hold_reason=reason, hold_by="runner", hold_set_at=None,
                    age_hours=None, proposal_id=None, options=(), recommended_key=None,
                    downstream=0, downstream_dispatchable=0, downstream_refs=(),
                    dispatchable_on_release=True, released_by="", work_item_id=wid,
                    escalation=escalation, attention_kind=attention)


def test_a_model_approval_recommends_approving_the_requested_tier():
    item = dg.from_held(_held(escalation=Escalation(bounced_at="opus", needs_approval="fable"),
                              attention="model_approval",
                              reason="bounced at opus; fable needs approval"))
    assert item.id == "hold:wi1" and item.kind == "model_approval"
    assert item.recommendation is not None
    assert item.recommendation.to_wire() == {"action": "approve_escalation",
                                             "label": "Approve fable",
                                             "params": {"workItemId": "wi1", "model": "fable"}}


@pytest.mark.parametrize("attention,kind", [("checklist", "owner_action"),
                                            ("decision", "decision"),
                                            ("owner_action", "owner_action")])
def test_a_checklist_decision_or_hand_off_recommends_nothing(attention, kind):
    item = dg.from_held(_held(kind=kind, attention=attention,
                              reason="Left for you:\n- [ ] prod check"))
    assert item.kind == attention and item.recommendation is None
    assert item.why.startswith("Left for you:")


@pytest.mark.parametrize("level,feature,recommended", [
    ("high", "sessions", True), ("medium", "sessions", False), ("high", None, False)])
def test_triage_is_accepted_only_when_confident_and_onto_a_capability(level, feature,
                                                                      recommended):
    item = dg.from_triage(proposal_id="tp1", product="p", ref="o/r#3", title="t", url=None,
                          feature_slug=feature, confidence_level=level)
    assert item.id == "triage:tp1" and item.kind == "triage"
    assert (item.recommendation is not None) is recommended
    if recommended:
        assert item.recommendation is not None
        assert item.recommendation.action == "accept_triage"
        assert item.recommendation.label == "Accept into sessions"


def test_a_gate_is_never_advanced_from_the_digest_only_linked():
    item = dg.from_initiative("p", {"slug": "ini-1", "title": "Ship it", "reason": "advance",
                                    "status": "active"})
    assert (item.kind, item.recommendation, item.url) == ("gate", None, "/initiatives/ini-1")
    assert "gate" in item.why


def test_a_recommendation_carries_its_action_and_evidence_links():
    rec = {"id": "r1", "product": "p", "rule": "parked_review", "ref": "o/r#9",
           "action": "close", "evidence": "o/r#9 is parked, but a merged PR already does it",
           "detail": {"title": "Old idea", "closeReason": "not_planned",
                      "prs": [{"number": 5, "title": "feat", "url": "https://x/pull/5"}]}}
    item = dg.from_recommendation(rec, url="https://x/issues/9")
    assert item.id == "rec:r1" and item.kind == "recommendation" and item.title == "Old idea"
    assert item.recommendation is not None
    assert (item.recommendation.action, item.recommendation.label) == (
        "apply_recommendation", "Close as not planned")
    assert item.evidence == ({"title": "#5 feat", "url": "https://x/pull/5"},)
    unpark = dg.from_recommendation({**rec, "action": "unpark", "detail": {}})
    assert unpark.recommendation is not None and unpark.recommendation.label == "Unpark"


def test_assemble_groups_by_product_merges_a_recommendation_into_its_item_and_sorts():
    held = dg.from_held(_held("o/r#4"))
    approval = dg.from_held(_held("o/r#5", wid="wi5", attention="model_approval",
                                  escalation=Escalation("opus", "fable")))
    rec = dg.from_recommendation({"id": "r1", "product": "p", "rule": "stale_holds",
                                  "ref": "o/r#4", "action": "release_hold", "evidence": "e",
                                  "detail": {}})
    gate = dg.from_initiative("q", {"slug": "g", "title": "G", "status": "awaiting_human"})
    out = dg.assemble([held, gate, rec, approval], products=["p", "q", "empty"], now=NOW)
    assert out["generatedAt"] == NOW.isoformat()
    assert [p["slug"] for p in out["products"]] == ["p", "q"]          # empty dropped
    p_items = out["products"][0]["items"]
    # The recommendation stands in for the held o/r#4; recommended items first.
    assert [i["id"] for i in p_items] == ["hold:wi5", "rec:r1"]
    assert out["total"] == 3 and out["withRecommendation"] == 2


@pytest.mark.parametrize("value,parsed", [
    ("hold:wi1", ("hold", "wi1")), ("rec:abc", ("rec", "abc")), ("gate:x:y", ("gate", "x:y")),
    ("bogus:1", None), ("hold:", None), ("nocolon", None)])
def test_parse_id(value, parsed):
    assert dg.parse_id(value) == parsed


def test_digest_due_once_a_day_and_the_summary_line():
    assert dg.digest_due(None, NOW)
    assert not dg.digest_due(NOW - timedelta(hours=20), NOW)
    assert dg.digest_due(NOW - timedelta(hours=23, minutes=50), NOW)
    assert dg.digest_due((NOW - timedelta(days=1)).replace(tzinfo=None), NOW)
    assert dg.summary_text("p", 5, 3) == ("p: today's digest",
                                          "5 items need you — 3 have a recommendation")
    assert dg.summary_text("p", 1, 1)[1] == "1 item needs you — 1 has a recommendation"
