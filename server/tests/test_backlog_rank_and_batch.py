"""Pure unit tests for rank_and_batch — no I/O, no fixtures."""
from __future__ import annotations

from server.automations.backlog_selection import Batch, rank_and_batch

C = [  # candidate issues (repo-qualified refs + numbers for fallback ordering)
    {"ref": "o/r#5", "number": 5, "repo": "o/r"},
    {"ref": "o/r#2", "number": 2, "repo": "o/r"},
    {"ref": "o/r#9", "number": 9, "repo": "o/r"},
]


def test_empty_is_none():
    assert rank_and_batch([], 1, lambda cs: []) is None


def test_single_candidate_skips_ranking():
    b = rank_and_batch([C[0]], 3, lambda cs: (_ for _ in ()).throw(AssertionError("no rank")))
    assert b.primary == "o/r#5" and b.issues == ["o/r#5"]


def test_k1_takes_ranked_top():
    b = rank_and_batch(C, 1, lambda cs: ["o/r#9", "o/r#5", "o/r#2"])
    assert b.primary == "o/r#9" and b.issues == ["o/r#9"]


def test_kN_takes_coherent_group_up_to_cap():
    b = rank_and_batch(C, 2, lambda cs: ["o/r#9", "o/r#5", "o/r#2"])
    assert b.primary == "o/r#9" and b.issues == ["o/r#9", "o/r#5"]  # capped at 2


def test_malformed_ranking_falls_back_to_lowest_number():
    b = rank_and_batch(C, 1, lambda cs: ["nonsense", "also-bad"])
    assert b.primary == "o/r#2"  # deterministic fallback: lowest issue number


def test_batch_carries_repo():
    b = rank_and_batch(C, 1, lambda cs: ["o/r#9"])
    assert b.repo == "o/r"


def test_batch_is_frozen_dataclass():
    b = rank_and_batch([C[0]], 1, lambda cs: [])
    assert isinstance(b, Batch)
    import dataclasses
    assert dataclasses.is_dataclass(b)


def test_empty_ranking_falls_back_to_lowest_number():
    """ranking returns [] (not malformed refs, just empty) → same fallback."""
    b = rank_and_batch(C, 2, lambda cs: [])
    assert b.primary == "o/r#2"
    assert b.issues == ["o/r#2", "o/r#5"]


def test_partial_malformed_ranking_keeps_known_refs():
    """ranking returns mix of valid and unknown refs → keep known, drop unknown."""
    b = rank_and_batch(C, 3, lambda cs: ["o/r#9", "bogus", "o/r#2"])
    assert b.issues == ["o/r#9", "o/r#2"]
    assert b.primary == "o/r#9"
