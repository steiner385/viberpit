"""The owner digest, pure half (design 2026-10-14 §5): which item gets which ONE
recommended action, how items group and merge, and when the daily push is due."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server.pm import digest as dg
from server.pm.escalation import HeldItem
from server.pm.outcomes import Escalation

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


OPTIONS = ({"key": "a", "label": "Option A", "detail": None},
           {"key": "b", "label": "Option B", "detail": "the other"})


def _held(ref: str = "o/r#1", *, kind: str = "owner_action", reason: str = "owner call",
          escalation: Escalation | None = None, attention: str = "owner_action",
          wid: str | None = "wi1", proposal_id: str | None = None,
          options: tuple[dict, ...] = (), recommended_key: str | None = None,
          product: str = "p") -> HeldItem:
    return HeldItem(ref=ref, repo="o/r", number=int(ref.split("#")[1]), product=product,
                    title=f"issue {ref}", url=f"https://github.com/{ref.replace('#', '/issues/')}",
                    hold_kind=kind, hold_reason=reason, hold_by="runner", hold_set_at=None,
                    age_hours=None, proposal_id=proposal_id, options=options,
                    recommended_key=recommended_key,
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


def test_a_gate_without_its_stage_evidence_is_only_linked():
    item = dg.from_initiative("p", {"slug": "ini-1", "title": "Ship it", "reason": "advance",
                                    "status": "active"})
    assert (item.kind, item.recommendation, item.url) == ("gate", None, "/initiatives/ini-1")
    assert "gate" in item.why


@pytest.mark.parametrize("node", [{"reason": "advance", "status": "active"},
                                  {"reason": None, "status": "awaiting_human"}])
def test_a_gate_whose_stage_is_complete_recommends_advancing_it(node):
    item = dg.from_initiative("p", {"slug": "ini-1", "title": "Ship it", **node},
                              stage_complete=True)
    assert item.recommendation is not None
    assert item.recommendation.to_wire() == {"action": "advance_gate",
                                             "label": "Advance the stage gate",
                                             "params": {"slug": "ini-1"}}


@pytest.mark.parametrize("status,reason,complete,expected", [
    ("awaiting_human", None, True, True),
    ("active", "advance", True, True),
    # Needs-you only because its epic has held issues: the gate is not the ask.
    ("active", None, True, False),
    ("awaiting_human", None, False, False),
    ("active", "advance", False, False),
    ("blocked", None, True, False),
])
def test_gate_advance_is_recommended_only_for_a_waiting_gate_with_its_evidence(
        status, reason, complete, expected):
    assert dg.gate_advance_recommended(status=status, reason=reason,
                                       stage_complete=complete) is expected


def test_a_pending_decision_recommends_accepting_its_recommended_option():
    held = dg.from_held(_held(kind="decision", attention="decision", proposal_id="dp1",
                              options=OPTIONS, recommended_key="b"))
    assert held.recommendation is not None
    assert held.recommendation.to_wire() == {"action": "accept_decision",
                                             "label": "Accept: Option B",
                                             "params": {"proposalId": "dp1", "key": "b"}}
    loose = dg.from_decision(proposal_id="dp2", product="p", ref="o/r#2", title="t", url=None,
                             rationale="r", options=OPTIONS, recommended_key="a")
    assert loose.recommendation is not None
    assert (loose.recommendation.label, dict(loose.recommendation.params)) == (
        "Accept: Option A", {"proposalId": "dp2", "key": "a"})


@pytest.mark.parametrize("proposal_id,options,key", [
    ("dp1", OPTIONS, "z"),            # the recommended key is not one of the options
    ("dp1", OPTIONS, None),           # no recommendation at all
    (None, OPTIONS, "b"),             # no proposal to accept
    ("dp1", ({"label": "keyless"},), "b"),
])
def test_a_decision_without_a_valid_recommended_option_recommends_nothing(
        proposal_id, options, key):
    assert dg.decision_recommendation(proposal_id, options, key) is None
    held = dg.from_held(_held(kind="decision", attention="decision", proposal_id=proposal_id,
                              options=options, recommended_key=key))
    assert held.recommendation is None
    if proposal_id is not None:
        assert dg.from_decision(proposal_id=proposal_id, product="p", ref=None, title=None,
                                url=None, rationale=None, options=options,
                                recommended_key=key).recommendation is None


def test_a_releasable_system_hold_recommends_releasing_it():
    item = dg.from_held(_held(), releasable=True)
    assert item.recommendation is not None
    assert item.recommendation.to_wire() == {"action": "release_hold",
                                             "label": "Release the hold",
                                             "params": {"workItemId": "wi1"}}
    # A climb the approve tap would refuse (the queue skips the issue) is released.
    climb = dg.from_held(_held(escalation=Escalation("opus", "fable"),
                               attention="model_approval"), releasable=True)
    assert climb.recommendation is not None and climb.recommendation.action == "release_hold"


@pytest.mark.parametrize("attention,kind", [("checklist", "owner_action"),
                                            ("decision", "decision")])
def test_a_checklist_or_decision_is_never_released_from_the_digest(attention, kind):
    item = dg.from_held(_held(kind=kind, attention=attention,
                              reason="Left for you:\n- [ ] physical check"), releasable=True)
    assert item.recommendation is None or item.recommendation.action != "release_hold"


def test_a_digest_shaped_like_2026_10_15_mostly_carries_a_recommendation():
    """#3112's acceptance: 2 decisions with a recommended key, gates whose stage is
    complete, system hand-offs on trackers the policy now skips — beside what only a
    person can do (a checklist, a person's park, a gate with no evidence)."""
    items = [
        dg.from_held(_held("o/r#1", kind="decision", attention="decision", wid="w1",
                           proposal_id="dp1", options=OPTIONS, recommended_key="a")),
        dg.from_decision(proposal_id="dp2", product="p", ref="o/r#2", title="t", url=None,
                         rationale="r", options=OPTIONS, recommended_key="b"),
        *(dg.from_held(_held(f"o/r#{n}", wid=f"w{n}"), releasable=True)
          for n in range(10, 22)),
        *(dg.from_held(_held(f"o/r#{n}", wid=f"w{n}", attention="checklist",
                             reason="Left for you:\n- [ ] check the hardware"))
          for n in range(30, 34)),
        *(dg.from_held(_held(f"o/r#{n}", wid=f"w{n}")) for n in range(40, 43)),
        *(dg.from_initiative("p", {"slug": f"g{n}", "title": "G", "status": "awaiting_human"},
                             stage_complete=True) for n in range(5)),
        *(dg.from_initiative("p", {"slug": f"h{n}", "title": "H", "status": "awaiting_human"})
          for n in range(4)),
    ]
    out = dg.assemble(items, products=["p"], now=NOW)
    assert out["total"] == 30
    assert out["withRecommendation"] > out["total"] / 2


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


def test_an_owner_recommendation_stands_in_even_for_an_item_with_its_own_action():
    """The hygiene pass's ``release_hold`` recommendation and the held item's own
    release are one action on one thing: the digest lists it once (#3112)."""
    held = dg.from_held(_held("o/r#4", wid="wi4"), releasable=True)
    assert held.recommendation is not None
    rec = dg.from_recommendation({"id": "r1", "product": "p", "rule": "stale_holds",
                                  "ref": "o/r#4", "action": "release_hold", "evidence": "e",
                                  "detail": {}})
    out = dg.assemble([held, rec], products=["p"], now=NOW)
    assert [i["id"] for i in out["products"][0]["items"]] == ["rec:r1"]
