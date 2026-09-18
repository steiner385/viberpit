"""Dispatch-outcome pure helpers (spec 2026-09-12 product automation policy §6/§7).

These are the ONE fingerprint, the ONE bounce predicate and the ONE bounce query
builder (constitution VI): the queue's ``needs_spec`` skip, the roadmap's leaf
reason and the runner's status card all go through them, so none of the three can
drift into its own idea of "the same issue body, bounced too often".
"""
from __future__ import annotations

import pytest

from server.db import db_session, init_db
from server.db.models import DISPATCH_OUTCOMES, DispatchOutcome
from server.pm.outcomes import (
    FINALISED_BOUNCE_OUTCOMES, TierStats, body_fingerprint, bounced_out, bounces_stmt,
    rows_to_bounces, tier_floor_from_stats)


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
    assert "dispatch_kind" in sql


@pytest.mark.asyncio
async def test_bounces_stmt_excludes_investigate_dispatches(tmp_path):
    """An ``investigate`` dispatch never opens a PR by design (series item 4b), so
    it must not count as a bounce — only the ``None``/``implement`` row does
    (spec 2026-09-18 §3/§5)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'bounces.db'}"
    await init_db(db_url=db_url)
    async with db_session() as db:
        db.add(DispatchOutcome(target_ref="o/r#1", body_fingerprint="fp", outcome="no_pr",
                               dispatch_kind="investigate"))
        db.add(DispatchOutcome(target_ref="o/r#1", body_fingerprint="fp", outcome="no_pr",
                               dispatch_kind=None))
    async with db_session() as db:
        rows = (await db.execute(bounces_stmt([("o/r#1", "fp")]))).all()
    assert rows_to_bounces(rows, [("o/r#1", "fp")]) == {"o/r#1": 1}


# --- D9 tier floor (phase 4) -------------------------------------------------
#
# ``tier_floor_from_stats`` is the ONE model-pick floor rule: "the lowest tier at
# which that capability's issues merged first-pass in >= 2 outcomes, when the lower
# tiers' first-pass rate was 0 across >= 2 outcomes". The >= 2 thresholds live
# nowhere else, and the walk order is the catalog's ``TIER_ORDER`` — never the
# caller's input order and never a string compare.


def _s(tier, outcomes, first_pass):
    return TierStats(tier=tier, outcomes=outcomes, first_pass=first_pass)


def test_floor_is_the_lowest_tier_that_merged_first_pass_when_cheaper_tiers_failed():
    assert tier_floor_from_stats([_s("sonnet", 3, 0), _s("opus", 2, 2)]) == "opus"


def test_no_floor_without_two_failed_outcomes_below():
    """One cheap failure is not evidence the cheap tier cannot do the work."""
    assert tier_floor_from_stats([_s("sonnet", 1, 0), _s("opus", 2, 2)]) is None
    assert tier_floor_from_stats([_s("opus", 2, 2)]) is None


def test_no_floor_when_a_cheaper_tier_ever_merged_first_pass():
    """A cheaper tier that does the work at all keeps the floor off — the runner's
    auto pick is only ever raised on evidence the cheap tier cannot deliver."""
    assert tier_floor_from_stats([_s("sonnet", 4, 1), _s("opus", 2, 2)]) is None


def test_floor_needs_two_first_pass_merges_at_the_higher_tier():
    assert tier_floor_from_stats([_s("sonnet", 2, 0), _s("opus", 3, 1)]) is None


def test_floor_skips_a_middle_tier_with_too_little_evidence():
    """A tier with < 2 outcomes is evidence in NEITHER role: it cannot establish
    that the cheap tiers fail, and it cannot be the floor."""
    assert tier_floor_from_stats(
        [_s("haiku", 2, 0), _s("sonnet", 1, 0), _s("opus", 2, 2)]) == "opus"


def test_floor_orders_by_the_catalog_not_by_input_order():
    assert tier_floor_from_stats([_s("opus", 2, 2), _s("sonnet", 2, 0)]) == "opus"


def test_empty_stats_have_no_floor():
    assert tier_floor_from_stats([]) is None
