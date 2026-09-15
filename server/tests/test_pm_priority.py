"""Derived initiative priority (spec 2026-09-14 D6/§10): ONE pure rule."""
from server.pm.priority import initiative_priority


def test_epic_score_wins_when_present():
    assert initiative_priority(12.5, [99.0, 1.0]) == 12.5


def test_falls_back_to_the_highest_open_leaf_score():
    assert initiative_priority(None, [3.0, None, 42.0, 7.5]) == 42.0


def test_none_when_nothing_is_scored():
    assert initiative_priority(None, []) is None
    assert initiative_priority(None, [None, None]) is None
