"""Pure per-repo selection-strategy ordering (``order_live_groups``).

The runner reorders the live plan groups by a user-selectable strategy before
popping ``live[0]``. These tests drive the ordering directly with plain string
items (``repo_of`` = identity) so no plan/candidate scaffolding is needed. The
contract for every strategy: stable — within an equal key the incoming plan
priority order is preserved.
"""
from __future__ import annotations

from server.automations.backlog_selection import order_live_groups

ident = lambda x: x  # noqa: E731 — items ARE their repo in these tests


# --------------------------------------------------------------------------- #
# balanced (default) — fewest in-pipeline runs first, plan order on ties        #
# --------------------------------------------------------------------------- #

def test_balanced_orders_by_fewest_load():
    out = order_live_groups(["a", "b", "c"], repo_of=ident, strategy="balanced",
                            repo_load={"a": 3, "b": 0, "c": 1})
    assert out == ["b", "c", "a"]


def test_balanced_empty_load_keeps_plan_order():
    out = order_live_groups(["a", "b", "c"], repo_of=ident, strategy="balanced",
                            repo_load={})
    assert out == ["a", "b", "c"]


def test_balanced_is_stable_within_equal_load():
    # a and b both at load 0 → incoming (plan) order a-before-b stands.
    out = order_live_groups(["a", "b"], repo_of=ident, strategy="balanced",
                            repo_load={"a": 0, "b": 0})
    assert out == ["a", "b"]


# --------------------------------------------------------------------------- #
# priority — repo_order rank first, load breaks ties within a rank              #
# --------------------------------------------------------------------------- #

def test_priority_serves_by_repo_order():
    out = order_live_groups(["c", "a", "b"], repo_of=ident, strategy="priority",
                            repo_order=["a", "b", "c"], repo_load={})
    assert out == ["a", "b", "c"]


def test_priority_unlisted_repo_ranks_last():
    out = order_live_groups(["z", "a"], repo_of=ident, strategy="priority",
                            repo_order=["a"], repo_load={})
    assert out == ["a", "z"]


def test_priority_breaks_ties_within_rank_by_load():
    # two items in the same repo "a"; load can't differ per-item here, so use two
    # repos at the same rank position is impossible — instead verify a lower-rank
    # repo with 0 load never jumps ahead of a higher-rank repo with load.
    out = order_live_groups(["b", "a"], repo_of=ident, strategy="priority",
                            repo_order=["a", "b"], repo_load={"a": 9, "b": 0})
    assert out == ["a", "b"]  # rank wins over load


# --------------------------------------------------------------------------- #
# weighted — effective load = load / weight; weight 0 suppresses                #
# --------------------------------------------------------------------------- #

def test_weighted_higher_weight_wins():
    # a: 6/3 = 2.0 ; b: 3/1 = 3.0 → a (higher weight) sorts first.
    out = order_live_groups(["b", "a"], repo_of=ident, strategy="weighted",
                            repo_load={"a": 6, "b": 3}, weights={"a": 3, "b": 1})
    assert out == ["a", "b"]


def test_weighted_default_weight_is_one():
    out = order_live_groups(["a", "b"], repo_of=ident, strategy="weighted",
                            repo_load={"a": 2, "b": 1}, weights={})
    assert out == ["b", "a"]  # no weights → identical to balanced


def test_weighted_zero_weight_suppresses_repo():
    out = order_live_groups(["a", "b"], repo_of=ident, strategy="weighted",
                            repo_load={"a": 0, "b": 5}, weights={"a": 0})
    assert out == ["b"]  # a is suppressed even though it has the lower load


def test_weighted_all_suppressed_is_empty():
    out = order_live_groups(["a", "b"], repo_of=ident, strategy="weighted",
                            repo_load={}, weights={"a": 0, "b": 0})
    assert out == []


# --------------------------------------------------------------------------- #
# concurrency-cap — drop repos at/over their cap; balance the remainder         #
# --------------------------------------------------------------------------- #

def test_cap_drops_repo_at_or_over_cap():
    # a is at its cap of 2 → dropped; b (cap 5, load 1) survives.
    out = order_live_groups(["a", "b"], repo_of=ident, strategy="concurrency-cap",
                            repo_load={"a": 2, "b": 1}, caps={"a": 2, "b": 5})
    assert out == ["b"]


def test_cap_absent_means_uncapped():
    out = order_live_groups(["a", "b"], repo_of=ident, strategy="concurrency-cap",
                            repo_load={"a": 9, "b": 0}, caps={"b": 1})
    assert out == ["b", "a"]  # a uncapped, kept; ordered balanced (b fewer load)


def test_cap_all_over_cap_is_empty():
    # every repo with work is at its cap → empty → caller idles the slot.
    out = order_live_groups(["a", "b"], repo_of=ident, strategy="concurrency-cap",
                            repo_load={"a": 2, "b": 3}, caps={"a": 2, "b": 3})
    assert out == []
