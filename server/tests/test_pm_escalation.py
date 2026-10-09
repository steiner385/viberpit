"""Pure tests for the escalation read model (spec 2026-09-30 §3)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from server.pm.escalation import (HeldItem, downstream, held_items, product_escalation,
                                  rank_held, reverse_index)
from server.pm.queue import QueueRow

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=1)  # relative to a fixed anchor


def _row(**over) -> QueueRow:
    base: dict[str, Any] = dict(ref="o/r#1", repo="o/r", number=1, product="viberpit", title="t", body="",
                url="u", status="Ready", score=1.0, gh_created_at=NOW - timedelta(days=2),
                gh_updated_at=NOW, assignees=(), author="tony", labels=(), open_children=0,
                open_blockers=(), live_session=False, claimed=False, hold_kind=None)
    base.update(over)
    return QueueRow(**base)


def _held(ref, kind="owner_action", *, set_at=None, **over) -> QueueRow:
    number = int(ref.rsplit("#", 1)[1])
    return _row(ref=ref, number=number, hold_kind=kind, hold_reason="why",
                hold_by="tony@example.com", hold_set_at=set_at, **over)


def _policy(dispatch="auto"):
    return {"dispatch": dispatch, "queue_min_status": "Ready", "respect_dependencies": True,
            "give_up_after_bounces": 0, "skip_labels": [],
            "trusted_authors": ["tony"]}


KW = dict(now=NOW, min_status="Ready", respect_dependencies=True,
          trusted_authors=frozenset({"tony"}))


def test_reverse_index_maps_blocker_to_dependents():
    rows = [_row(ref="o/r#2", number=2, open_blockers=("o/r#1",)),
            _row(ref="o/r#3", number=3, open_blockers=("o/r#1", "o/r#2"))]
    assert reverse_index(rows) == {"o/r#1": {"o/r#2", "o/r#3"}, "o/r#2": {"o/r#3"}}


def test_downstream_is_transitive_and_cycle_safe():
    rev = {"o/r#1": {"o/r#2"}, "o/r#2": {"o/r#3", "o/r#1"}, "o/r#3": {"o/r#1"}}
    assert downstream("o/r#1", rev) == ("o/r#2", "o/r#3")
    assert downstream("o/r#9", rev) == ()


def test_held_items_count_downstream_and_what_would_become_dispatchable():
    rows = [_held("o/r#1"),
            _row(ref="o/r#2", number=2, open_blockers=("o/r#1",)),                 # frees
            _row(ref="o/r#3", number=3, open_blockers=("o/r#2",)),                 # frees (chain)
            _row(ref="o/r#4", number=4, open_blockers=("o/r#1",), assignees=("bob",)),  # assigned
            _row(ref="o/r#5", number=5, open_blockers=("o/r#1", "o/r#8",)),        # still blocked by #8
            _row(ref="o/r#8", number=8, status="Blocked")]
    (item,) = held_items(rows, {}, **KW)
    assert item.ref == "o/r#1" and item.hold_kind == "owner_action"
    assert item.downstream == 4
    assert set(item.downstream_refs) == {"o/r#2", "o/r#3", "o/r#4", "o/r#5"}
    assert item.downstream_dispatchable == 2
    assert item.dispatchable_on_release is True
    assert item.released_by == "Retry, a board move to Ready/Backlog, or an issue-body edit"


def test_held_items_only_lists_parked_kinds_and_joins_the_pending_proposal():
    prop = SimpleNamespace(id="dp-1", options=[{"key": "a", "label": "A", "detail": None},
                                               {"key": "b", "label": "B", "detail": "d"}],
                           recommended_key="b")
    rows = [_held("o/r#1", "decision", set_at=NOW - timedelta(hours=30)),
            _held("o/r#2", "evidence"), _held("o/r#3", "stronger_model"),
            _held("o/r#4", "decision")]  # no proposal row: still listed
    items = {i.ref: i for i in held_items(rows, {"o/r#1": prop}, **KW)}
    assert set(items) == {"o/r#1", "o/r#4"}
    assert items["o/r#1"].proposal_id == "dp-1"
    assert items["o/r#1"].recommended_key == "b" and len(items["o/r#1"].options) == 2
    assert items["o/r#1"].age_hours == 30.0
    assert items["o/r#1"].released_by == "Accept an option in the Inbox"
    assert items["o/r#4"].proposal_id is None and items["o/r#4"].options == ()
    assert items["o/r#4"].age_hours is None


def test_dispatchable_on_release_respects_other_skip_reasons():
    (item,) = held_items([_held("o/r#1", assignees=("bob",))], {}, **KW)
    assert item.dispatchable_on_release is False


def test_rank_held_orders_by_downstream_then_age_then_ref():
    def item(ref, downstream, age):
        return HeldItem(ref=ref, repo="o/r", number=int(ref[-1]), product="p", title="t",
                        url="u", hold_kind="owner_action", hold_reason="r", hold_by="b",
                        hold_set_at=None, age_hours=age, proposal_id=None, options=(),
                        recommended_key=None, downstream=downstream,
                        downstream_dispatchable=0, downstream_refs=(),
                        dispatchable_on_release=True, released_by="x")
    ranked = rank_held([item("o/r#1", 0, 50.0), item("o/r#2", 3, 1.0),
                        item("o/r#3", 3, 9.0), item("o/r#4", 0, None)])
    assert [i.ref for i in ranked] == ["o/r#3", "o/r#2", "o/r#1", "o/r#4"]


def test_product_escalation_starving_truth_table():
    held = _held("o/r#1", set_at=NOW - timedelta(hours=100))
    free = _row(ref="o/r#2", number=2)
    on = product_escalation("p", [held], {}, _policy(), now=NOW)
    assert on.starving is True and on.dispatchable == 0 and on.skipped == {"held": 1}
    assert on.oldest_hold_hours == 100.0 and [i.ref for i in on.held] == ["o/r#1"]
    # one dispatchable still counts as starving at the default threshold …
    assert product_escalation("p", [held, free], {}, _policy(), now=NOW).starving is True
    # … two do not
    assert product_escalation("p", [held, free, _row(ref="o/r#3", number=3)], {},
                              _policy(), now=NOW).starving is False
    # nothing held: not starving however empty the queue
    assert product_escalation("p", [], {}, _policy(), now=NOW).starving is False
    # dispatch off: never starving
    assert product_escalation("p", [held], {}, _policy("off"), now=NOW).starving is False


def test_product_escalation_reads_reach_from_the_policy_unless_overridden():
    backlog = _held("o/r#1", status="Backlog")
    pol = dict(_policy(), queue_min_status="Backlog")
    e = product_escalation("p", [backlog, _row(ref="o/r#2", number=2, status="Backlog")], {},
                           pol, now=NOW)
    assert e.dispatchable == 1
    e = product_escalation("p", [backlog, _row(ref="o/r#2", number=2, status="Backlog")], {},
                           pol, now=NOW, min_status="Ready")
    assert e.dispatchable == 0 and e.skipped == {"held": 1, "status": 1}


def test_release_simulation_undoes_the_reconcilers_held_stamp():
    classified = _held("o/r#1", status="Held", score=1.0)
    assert held_items([classified], {}, **KW)[0].dispatchable_on_release is True
    unscored = _held("o/r#1", status="Held", score=None)
    assert held_items([unscored], {}, **KW)[0].dispatchable_on_release is False
    kw = dict(KW, min_status="Backlog")
    assert held_items([unscored], {}, **kw)[0].dispatchable_on_release is True


def test_chain_simulation_on_a_reconciled_board_with_blocked_stamps():
    rows = [_held("o/r#1", status="Held"),
            _row(ref="o/r#2", number=2, status="Blocked", open_blockers=("o/r#1",)),
            _row(ref="o/r#5", number=5, status="Blocked",
                 open_blockers=("o/r#1", "o/r#8")),
            _row(ref="o/r#8", number=8, status="Blocked", open_blockers=("o/r#9",)),
            _row(ref="o/r#9", number=9, status="In progress"),
            _row(ref="o/r#6", number=6, status="Done", open_blockers=("o/r#1",))]
    (item,) = held_items(rows, {}, **KW)
    assert item.downstream_dispatchable == 1  # only #2; #5 keeps #8, #6 is Done


def test_held_items_carry_the_work_item_id_and_the_approvable_escalation():
    """The Roadmap's held list acts on the item (Retry / Approve escalation by
    work-item id), so the read model has to say WHICH row and whether the hold is
    one the runner wrote at an approval-clamped top. ``released_by`` names the
    approval too, so the digest and the assistant say the same thing the card does."""
    from server.pm.outcomes import Escalation
    rows = [_row(ref="o/r#1", number=1, hold_kind="owner_action", hold_by="runner",
                 hold_reason="bounced at opus; fable needs approval", work_item_id="w1"),
            _row(ref="o/r#2", number=2, hold_kind="owner_action", hold_by="runner",
                 hold_reason="needs the keyring", work_item_id="w2"),
            _held("o/r#3", "decision", work_item_id=None)]
    items = {i.ref: i for i in held_items(rows, {}, **KW)}
    assert items["o/r#1"].work_item_id == "w1"
    assert items["o/r#1"].escalation == Escalation(bounced_at="opus", needs_approval="fable")
    assert items["o/r#1"].released_by == (
        "Retry, a board move to Ready/Backlog, an issue-body edit, "
        "or approving the escalation to fable")
    assert items["o/r#2"].escalation is None
    assert items["o/r#2"].released_by == "Retry, a board move to Ready/Backlog, or an issue-body edit"
    assert items["o/r#3"].work_item_id is None and items["o/r#3"].escalation is None
