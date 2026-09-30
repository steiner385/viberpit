"""Pure tests for the decision-escalation rule (spec 2026-09-30 §5)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from server.automations.decision_escalation import (Thresholds, decide, digest_text,
                                                    fingerprint, should_send)
from server.pm.escalation import HeldItem, ProductEscalation

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=3)
T = Thresholds()


def _item(ref, *, kind="decision", downstream=0, age=1.0, recommended="b"):
    return HeldItem(ref=ref, repo="o/k", number=int(ref.rsplit("#", 1)[1]), product="kindash",
                    title=f"Issue {ref}", url=f"https://x/{ref}", hold_kind=kind,
                    hold_reason="why", hold_by="session:s1", hold_set_at=None, age_hours=age,
                    proposal_id="dp-1" if kind == "decision" else None, options=(),
                    recommended_key=recommended if kind == "decision" else None,
                    downstream=downstream, downstream_dispatchable=downstream,
                    downstream_refs=(), dispatchable_on_release=True, released_by="x")


def _esc(*, dispatchable=5, held=(), starving=False, oldest=None):
    return ProductEscalation(slug="kindash", dispatchable=dispatchable,
                             skipped={"held": len(held)}, held=tuple(held),
                             starving=starving, oldest_hold_hours=oldest)


def test_decide_truth_table():
    quiet = decide(_esc(held=[_item("o/k#1")]), T)
    assert quiet.escalate is False and quiet.reasons == ()
    assert decide(_esc(dispatchable=0, held=[_item("o/k#1")], starving=True), T).reasons == ("starving",)
    assert decide(_esc(held=[_item("o/k#1", age=49.0)], oldest=49.0), T).reasons == ("aged",)
    assert decide(_esc(held=[_item("o/k#1", downstream=3)]), T).reasons == ("downstream",)
    both = decide(_esc(dispatchable=0, held=[_item("o/k#1", downstream=9, age=100)],
                       starving=True, oldest=100.0), T)
    assert both.escalate and both.reasons == ("starving", "aged", "downstream")
    # nothing held: never, whatever the queue looks like
    assert decide(_esc(dispatchable=0), T).escalate is False
    # a new proposal on a healthy product changes the fingerprint but not the verdict
    assert decide(_esc(held=[_item("o/k#1"), _item("o/k#2")]), T).escalate is False


def test_fingerprint_changes_with_held_set_starvation_or_impact_only():
    a = fingerprint(_esc(held=[_item("o/k#1")]))
    assert a == fingerprint(_esc(held=[_item("o/k#1", age=99.0)])), "age alone is not a change"
    assert a != fingerprint(_esc(held=[_item("o/k#1"), _item("o/k#2")]))
    assert a != fingerprint(_esc(held=[_item("o/k#1")], starving=True, dispatchable=0))
    assert a != fingerprint(_esc(held=[_item("o/k#1", downstream=4)]))
    assert fingerprint(_esc(held=[_item("o/k#2"), _item("o/k#1")])) == \
        fingerprint(_esc(held=[_item("o/k#1"), _item("o/k#2")]))


def test_should_send_dedupes_on_fingerprint_and_repeats_after_repeat_hours():
    v = decide(_esc(dispatchable=0, held=[_item("o/k#1")], starving=True), T)
    assert should_send(v, None, now=NOW, t=T) is True
    same = (v.fingerprint, NOW - timedelta(hours=2))
    assert should_send(v, same, now=NOW, t=T) is False
    old = (v.fingerprint, NOW - timedelta(hours=25))
    assert should_send(v, old, now=NOW, t=T) is True
    other = ("different", NOW - timedelta(minutes=1))
    assert should_send(v, other, now=NOW, t=T) is True
    calm = decide(_esc(held=[_item("o/k#1")]), T)
    assert should_send(calm, None, now=NOW, t=T) is False, "no verdict, no send"


def test_digest_text_names_counts_and_the_top_three_loudest():
    held = [_item(f"o/k#{i}", downstream=10 - i, age=float(i)) for i in range(1, 6)]
    held[4] = _item("o/k#5", kind="owner_action", downstream=0, age=200.0)
    esc = _esc(dispatchable=3, held=held, starving=False, oldest=200.0)
    v = decide(esc, T)
    title, body = digest_text(esc, v)
    assert title == "kindash: 5 held (4 decisions), 3 dispatchable"
    lines = body.splitlines()
    assert lines[0] == "#1 Issue o/k#1 — blocks 9 (recommended: b)"
    assert lines[1].startswith("#2 Issue o/k#2 — blocks 8")
    assert lines[2].startswith("#3 Issue o/k#3 — blocks 7")
    assert lines[3] == "2 more · oldest 8d · why: aged, downstream"
