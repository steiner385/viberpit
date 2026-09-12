"""Dispatch-outcome pure helpers (spec 2026-09-12 product automation policy §6/§7).

These are the ONE fingerprint, the ONE bounce predicate and the ONE bounce query
builder (constitution VI): the queue's ``needs_spec`` skip, the roadmap's leaf
reason and the runner's status card all go through them, so none of the three can
drift into its own idea of "the same issue body, bounced too often".
"""
from __future__ import annotations

from server.db.models import DISPATCH_OUTCOMES
from server.pm.outcomes import (
    FINALISED_BOUNCE_OUTCOMES, body_fingerprint, bounced_out, bounces_stmt, rows_to_bounces)


def test_fingerprint_is_stable_and_body_sensitive():
    a = body_fingerprint("T", "body")
    assert a == body_fingerprint("T", "body") and len(a) == 64
    assert a != body_fingerprint("T", "body edited")
    assert body_fingerprint(None, None) == body_fingerprint("", "")


def test_bounced_out_threshold_and_zero_means_never():
    assert bounced_out(2, 2) and bounced_out(3, 2)
    assert not bounced_out(1, 2)
    assert not bounced_out(99, 0)


def test_finalised_bounce_outcomes_exclude_running_merged_and_superseded():
    """A bounce is a dispatch that ENDED without a merge. ``running`` has not ended,
    ``merged`` did not bounce, and ``superseded`` is a dispatch that was replaced
    before it could answer — a double tap is not evidence the issue is unworkable
    (R3-9)."""
    assert FINALISED_BOUNCE_OUTCOMES == (
        frozenset(DISPATCH_OUTCOMES) - {"running", "merged", "superseded"})
    assert "superseded" in DISPATCH_OUTCOMES
    assert "superseded" not in FINALISED_BOUNCE_OUTCOMES


def test_rows_to_bounces_matches_only_the_current_fingerprint():
    pairs = [("o/r#1", "fp-now"), ("o/r#2", "fp-now2")]
    rows = [("o/r#1", "fp-now", 2), ("o/r#1", "fp-old", 5), ("o/r#2", "fp-other", 1)]
    assert rows_to_bounces(rows, pairs) == {"o/r#1": 2, "o/r#2": 0}


def test_bounces_stmt_filters_on_finalised_outcomes():
    sql = str(bounces_stmt([("o/r#1", "fp")]))
    assert "dispatch_outcomes" in sql and "outcome" in sql and "count" in sql.lower()
