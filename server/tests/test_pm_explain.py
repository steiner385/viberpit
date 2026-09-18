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


def test_hold_guidance_only_for_owner_action():
    from server.pm.explain import hold_guidance
    from server.automations.product_policy import effective_policy
    assert hold_guidance(hold_kind=None, policy=effective_policy(None)) is None
    assert hold_guidance(hold_kind="evidence", policy=effective_policy(None)) is None
    g = hold_guidance(hold_kind="owner_action", policy={**effective_policy(None), "dispatch": "auto"})
    assert set(g) == {"retry", "implement"}
    assert g["retry"]["when"] == "next_tick" and g["retry"]["then"][0]["spawnsSession"] is True
    assert g["implement"]["when"] == "now" and g["implement"]["immediate"][0]["spawnsSession"] is True
    off = hold_guidance(hold_kind="owner_action", policy={**effective_policy(None), "dispatch": "off"})
    assert off["implement"]["available"] is False and "dispatch is off" in off["implement"]["unavailableReason"]
    assert off["retry"]["when"] == "never"
