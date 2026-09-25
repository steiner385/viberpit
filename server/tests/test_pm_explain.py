"""Inbox consequence guidance (spec 2026-09-17 §4.3): what Accept and Reject do to
one proposal under the owning product's dispatch policy. Pure."""
from server.automations.product_policy import effective_policy
from server.pm.explain import triage_guidance


def test_accept_under_auto_dispatch_says_a_session_may_follow():
    g = triage_guidance(feature_slug="sessions", score=12.5,
                        policy=effective_policy({"dispatch": "auto", "queue_min_status": "Ready"}))
    a = g["accept"]
    assert a["available"] is True
    assert a["immediate"][0]["text"] == "Files it under sessions with score 12.5; it leaves the inbox."
    assert a["then"][0]["spawnsSession"] is True
    assert "dispatch it to a coding session" in a["then"][0]["text"]
    assert a["then"][0]["text"][0].isupper()
    assert a["when"] == "next_tick"
    assert a["reversal"] == "Undo within the window"
    assert a["gate"] == "triage.accept"


def test_accept_under_off_dispatch_dispatches_nothing():
    g = triage_guidance(feature_slug="sessions", score=None, policy=effective_policy(None))
    a = g["accept"]
    assert a["immediate"][0]["text"] == "Files it under sessions; it leaves the inbox."
    assert a["then"][0]["spawnsSession"] is False
    assert "Dispatch is off" in a["then"][0]["text"]
    assert a["when"] == "never"


def test_accept_under_propose_names_the_batch_approval():
    g = triage_guidance(feature_slug="sessions", score=3.0, policy=effective_policy({"dispatch": "propose"}))
    assert "proposed batch" in g["accept"]["then"][0]["text"]


def test_no_fit_accept_says_so():
    g = triage_guidance(feature_slug=None, score=None, policy=effective_policy(None))
    assert g["accept"]["immediate"][0]["text"] == "Files it with no capability (a no-fit accept); it leaves the inbox."


def test_reject_is_immediate_and_dispatches_nothing():
    r = triage_guidance(feature_slug="x", score=None, policy=effective_policy(None))["reject"]
    assert r["immediate"][0]["text"] == "Marks it needs-human; it leaves the inbox and nothing is dispatched."
    assert r["then"] == []
    assert r["when"] == "now"


def test_hold_guidance_only_for_owner_action_and_evidence():
    from server.pm.explain import hold_guidance
    from server.automations.product_policy import effective_policy
    assert hold_guidance(hold_kind=None, policy=effective_policy(None)) is None
    assert hold_guidance(hold_kind="stronger_model", policy=effective_policy(None)) is None
    assert hold_guidance(hold_kind="decision", policy=effective_policy(None)) is None
    g = hold_guidance(hold_kind="owner_action", policy={**effective_policy(None), "dispatch": "auto"})
    assert set(g) == {"retry", "implement"}
    assert g["retry"]["when"] == "next_tick" and g["retry"]["then"][0]["spawnsSession"] is True
    assert g["implement"]["when"] == "now" and g["implement"]["immediate"][0]["spawnsSession"] is True
    off = hold_guidance(hold_kind="owner_action", policy={**effective_policy(None), "dispatch": "off"})
    assert off["implement"]["available"] is False and "dispatch is off" in off["implement"]["unavailableReason"]
    assert off["retry"]["when"] == "never"


def test_hold_guidance_evidence_is_button_less_and_policy_independent():
    from server.pm.explain import hold_guidance
    from server.automations.product_policy import effective_policy
    g = hold_guidance(hold_kind="evidence", policy=effective_policy(None))
    e = g["evidence"]
    assert e["available"] is True
    assert e["immediate"] == []
    assert e["when"] == "next_tick"
    assert "investigates this report instead of implementing it" in e["then"][0]["text"]
    assert "No pull request" in e["then"][0]["text"]
    assert e["then"][0]["spawnsSession"] is False
    assert e["reversal"] is None and e["gate"] is None
    # Policy-independent: the copy does not vary with dispatch mode.
    g2 = hold_guidance(hold_kind="evidence", policy={**effective_policy(None), "dispatch": "auto"})
    assert g2["evidence"] == e


def test_hold_guidance_investigate_now_dispatches_immediately_under_auto():
    """#1963: the Investigate-now tap's own consequence, gated on the owning
    product's dispatch policy exactly like `implement` is for owner_action."""
    from server.pm.explain import hold_guidance
    from server.automations.product_policy import effective_policy
    g = hold_guidance(hold_kind="evidence",
                      policy={**effective_policy(None), "dispatch": "auto"})
    assert set(g) == {"evidence", "investigate"}
    i = g["investigate"]
    assert i["available"] is True
    assert i["when"] == "now"
    assert i["immediate"][0]["spawnsSession"] is True
    assert "investigates this report now" in i["immediate"][0]["text"]


def test_hold_guidance_investigate_now_is_unavailable_under_dispatch_off():
    from server.pm.explain import hold_guidance
    from server.automations.product_policy import effective_policy
    off = hold_guidance(hold_kind="evidence",
                        policy={**effective_policy(None), "dispatch": "off"})
    assert off["investigate"]["available"] is False
    assert "dispatch is off" in off["investigate"]["unavailableReason"]
    # The automatic column is unaffected — the Backlog Runner's own schedule
    # does not depend on whether a PERSON is entitled to trigger it manually.
    assert off["evidence"]["available"] is True


# --------------------------------------------------------------------------- #
# Decision proposals (spec 2026-09-18 §6)                                      #
# --------------------------------------------------------------------------- #

def test_decision_guidance_is_two_columns_under_auto_dispatch():
    from server.pm.explain import decision_guidance
    g = decision_guidance(policy={**effective_policy(None), "dispatch": "auto"})
    assert set(g) == {"accept", "reject"}
    a = g["accept"]
    assert a["available"] is True
    assert a["immediate"][0]["text"] == \
        "The hold clears; the next run implements with the chosen option."
    assert a["then"][0]["spawnsSession"] is True
    assert "dispatch it to a coding session" in a["then"][0]["text"]
    assert a["when"] == "next_tick"


def test_decision_accept_under_off_dispatch_spawns_nothing():
    from server.pm.explain import decision_guidance
    a = decision_guidance(policy=effective_policy(None))["accept"]
    assert a["then"][0]["spawnsSession"] is False
    assert "Dispatch is off" in a["then"][0]["text"]
    assert a["when"] == "never"


def test_decision_reject_names_the_hold_reason_it_actually_writes():
    """The sentence the explainer promises and the reason ``decisions.reject``
    writes are ONE constant — a second spelling here would go stale silently."""
    from server.pm.decisions import reject  # noqa: F401 — the writer this pins
    from server.pm.explain import decision_guidance
    from server.pm.holds import DECISION_REJECT_REASON
    r = decision_guidance(policy=effective_policy(None))["reject"]
    assert r["immediate"][0]["text"] == (
        f"The issue is held for you with '{DECISION_REJECT_REASON}'; "
        "Retry or Implement now release it.")
    assert DECISION_REJECT_REASON == "decision proposal rejected"
    assert r["then"] == [] and r["when"] == "now"


def test_the_re_hold_reversal_is_one_sentence_in_both_cards():
    """Accepting a decision and tapping Retry on an owner-action hold promise the
    same thing, so they say it with the same string — not two literals."""
    from server.pm.explain import REHOLD_REVERSAL, decision_guidance, hold_guidance
    policy = effective_policy(None)
    assert decision_guidance(policy=policy)["accept"]["reversal"] == REHOLD_REVERSAL
    assert hold_guidance(hold_kind="owner_action", policy=policy)["retry"]["reversal"] \
        == REHOLD_REVERSAL


def test_decision_guidance_shares_the_dispatch_sentence_with_triage():
    """No second consequence builder: the machine half of both cards is the one
    ``dispatch_sentence``."""
    from server.pm.explain import decision_guidance
    policy = {**effective_policy(None), "dispatch": "propose"}
    assert (decision_guidance(policy=policy)["accept"]["then"][0]["text"]
            == triage_guidance(feature_slug="x", score=None,
                               policy=policy)["accept"]["then"][0]["text"])
