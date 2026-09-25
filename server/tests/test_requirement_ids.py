"""Unit tests for server.pm.requirement_ids (issue #2163) — pure, no I/O."""
from __future__ import annotations

from server.pm.requirement_ids import parse_covers, sort_key


def test_no_body_returns_empty():
    assert parse_covers(None) == ()
    assert parse_covers("") == ()


def test_body_with_no_covers_line_returns_empty():
    assert parse_covers("Do the thing.\n\nFiles: a.py, b.py\n") == ()


def test_single_covers_line():
    body = "Do the thing.\n\nCovers: FR-001, AC-012\n"
    assert parse_covers(body) == ("FR-001", "AC-012")


def test_covers_line_is_case_insensitive_and_ids_are_upper_cased():
    body = "covers: fr-003, us-1\n"
    assert parse_covers(body) == ("FR-003", "US-1")


def test_dedupes_preserving_first_seen_order():
    body = "Covers: FR-001, FR-002, FR-001\n"
    assert parse_covers(body) == ("FR-001", "FR-002")


def test_multiple_covers_lines_accumulate():
    body = "Covers: FR-001\n\nSome other text.\n\nCovers: AC-002\n"
    assert parse_covers(body) == ("FR-001", "AC-002")


def test_ignores_text_that_is_not_covers_prefixed():
    body = "This task covers FR-999 informally but has no Covers: line.\n"
    assert parse_covers(body) == ()


def test_sort_key_orders_by_prefix_then_numerically():
    ids = ["FR-010", "FR-002", "AC-001", "US-3", "US-10"]
    assert sorted(ids, key=sort_key) == ["AC-001", "FR-002", "FR-010", "US-3", "US-10"]
