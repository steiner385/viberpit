"""Backfilled provenance: what it recovers, and what it refuses to guess.

The pure matcher is tested directly. Its contract is asymmetric on purpose: leaving an
automated prompt unmarked is a cosmetic miss, while marking something the operator
typed as "Autopilot" rewrites their own history at them — so every ambiguous case must
resolve to "leave it alone".
"""
from __future__ import annotations

from server.sessions import events as ev
from server.sessions.origin_backfill import _apply


def _user(text: str, ts: float | None) -> ev.UserMessage:
    return ev.UserMessage(turn_id=text, text=text, ts=ts)


SEC = 1000.0  # events carry epoch MILLIseconds


# ── pass 1: exact text is proof ─────────────────────────────────────────────────

def test_exact_text_match_marks_without_the_inferred_flag():
    e = _user("Re-run the failing test.", 1_000 * SEC)
    assert _apply([e], {"Re-run the failing test.": "autopilot"}, []) == 1
    assert e.origin == {"kind": "autopilot"}
    assert "inferred" not in e.origin


def test_exact_text_match_ignores_surrounding_whitespace():
    e = _user("  do the thing\n", 1_000 * SEC)
    _apply([e], {"do the thing": "autopilot"}, [])
    assert e.origin == {"kind": "autopilot"}


def test_a_prompt_with_no_recorded_match_is_left_alone():
    e = _user("something the human typed", 1_000 * SEC)
    assert _apply([e], {"a recorded nudge": "autopilot"}, []) == 0
    assert e.origin is None


# ── pass 2: timestamp is inference ──────────────────────────────────────────────

def test_timestamp_match_marks_inferred():
    e = _user("an old nudge, text not retained", 1_000 * SEC)
    assert _apply([e], {}, [(1_002 * SEC, "autopilot")]) == 1
    assert e.origin == {"kind": "autopilot", "inferred": True}


def test_prompt_outside_the_window_is_not_claimed():
    e = _user("unrelated", 1_000 * SEC)
    assert _apply([e], {}, [(2_000 * SEC, "autopilot")]) == 0
    assert e.origin is None


def test_a_prompt_well_after_the_nudge_still_matches():
    # The prompt can sit in the driver's FIFO queue behind a live turn before the CLI
    # journals it, so the window is deliberately generous in this direction.
    e = _user("queued behind a live turn", 1_120 * SEC)
    assert _apply([e], {}, [(1_000 * SEC, "autopilot")]) == 1
    assert e.origin == {"kind": "autopilot", "inferred": True}


def test_an_ambiguous_window_claims_nothing():
    """THE safety property: a human message near a nudge must not be relabelled."""
    nudge = _user("probably the nudge", 1_001 * SEC)
    human = _user("wait, stop", 1_004 * SEC)
    assert _apply([nudge, human], {}, [(1_000 * SEC, "autopilot")]) == 0
    assert nudge.origin is None and human.origin is None


def test_one_nudge_cannot_claim_two_prompts():
    a = _user("first", 1_001 * SEC)
    b = _user("second", 5_000 * SEC)
    # Two stamps, each unambiguous in its own window → one match apiece, not two each.
    assert _apply([a, b], {}, [(1_000 * SEC, "autopilot"), (4_999 * SEC, "autopilot")]) == 2
    assert a.origin == {"kind": "autopilot", "inferred": True}
    assert b.origin == {"kind": "autopilot", "inferred": True}


def test_a_prompt_already_claimed_by_text_is_not_reclaimed_by_timestamp():
    e = _user("known nudge", 1_000 * SEC)
    assert _apply([e], {"known nudge": "autopilot"}, [(1_000 * SEC, "autopilot")]) == 1
    assert e.origin == {"kind": "autopilot"}          # exact wins; not downgraded


def test_a_prompt_with_no_timestamp_is_never_timestamp_matched():
    e = _user("no ts on this entry", None)
    assert _apply([e], {}, [(1_000 * SEC, "autopilot")]) == 0
    assert e.origin is None


# ── the forward path always wins ────────────────────────────────────────────────

def test_a_prompt_already_marked_forward_is_untouched():
    e = _user("marked at send time", 1_000 * SEC)
    e.origin = {"kind": "completion"}
    assert _apply([e], {"marked at send time": "autopilot"}, [(1_000 * SEC, "autopilot")]) == 0
    assert e.origin == {"kind": "completion"}


def test_assistant_and_tool_events_are_ignored():
    turn = ev.TurnStart(turn_id="a1", src_uuid="a1")
    e = _user("nudge", 1_000 * SEC)
    assert _apply([turn, e], {"nudge": "autopilot"}, []) == 1
    assert e.origin == {"kind": "autopilot"}
