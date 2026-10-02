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
    last_attempt_model, prior_attempts, rows_to_bounces, tier_floor_from_stats)


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


# --- The model ladder (spec 2026-09-18 §4, decision 2) -----------------------


def test_next_rung_climbs_one_tier_from_the_previous_attempt():
    from server.pm.outcomes import next_rung
    assert next_rung("sonnet", floor=None, default_model="auto", top="fable") == "opus"
    assert next_rung("claude-opus-4-1", floor=None, default_model="auto", top="fable") == "fable"
    # unresolvable previous → climb from sonnet
    assert next_rung("auto", floor=None, default_model="auto", top="fable") == "opus"
    assert next_rung(None, floor=None, default_model="auto", top="fable") == "opus"
    # never below the capability floor or a pinned default
    assert next_rung("haiku", floor="opus", default_model="auto", top="fable") == "opus"
    assert next_rung("haiku", floor=None, default_model="fable", top="fable") == "fable"
    # bounced at the top
    assert next_rung("fable", floor=None, default_model="auto", top="fable") is None
    assert next_rung("opus", floor=None, default_model="auto", top="opus") is None
    assert next_rung("sonnet", floor=None, default_model="auto", top="best") is None


def test_top_tier_reads_the_policy_and_defaults_to_the_last_tier():
    from server.pm.outcomes import top_tier
    from server.sessions.model_catalog import TIER_ORDER
    assert top_tier({}) == TIER_ORDER[-1]
    assert top_tier({"ladder_top": "opus"}) == "opus"
    assert top_tier({"ladder_top": "bogus"}) == TIER_ORDER[-1]


def test_clamp_ladder_top_walks_down_to_the_first_allowed_tier():
    """Spec §4 decision 2 amendment: the ladder's top is the strongest tier the
    model-approval gate ALLOWS, so a rung can never park a dispatch for approval."""
    from server.pm.outcomes import clamp_ladder_top
    assert clamp_ladder_top("fable", lambda t: t != "fable") == "opus"
    assert clamp_ladder_top("fable", lambda t: True) == "fable"
    assert clamp_ladder_top("opus", lambda t: t == "haiku") == "haiku"
    # Nothing allowed → the cheapest tier: hand off after the first bounce, never
    # climb onto a tier that would park.
    assert clamp_ladder_top("fable", lambda t: False) == "haiku"
    # A tier ABOVE the configured top being gated changes nothing.
    assert clamp_ladder_top("opus", lambda t: t != "fable") == "opus"
    # An unrecognised configured value reads as the strongest tier (top_tier's rule).
    assert clamp_ladder_top("bogus", lambda t: t != "fable") == "opus"


def test_gated_ladder_top_names_the_configured_tier_only_when_it_was_clamped():
    from server.pm.outcomes import gated_ladder_top
    assert gated_ladder_top({"ladder_top": "opus", "ladder_top_configured": "fable"}) == "fable"
    assert gated_ladder_top({"ladder_top": "fable", "ladder_top_configured": "fable"}) is None
    assert gated_ladder_top({"ladder_top": "opus"}) is None   # un-annotated snapshot
    assert gated_ladder_top({}) is None


@pytest.mark.asyncio
async def test_last_attempt_model_reads_the_newest_finalised_implement_row(tmp_path):
    """The ladder climbs from the newest finalised ``implement`` dispatch's model:
    an older bounce doesn't win over a newer one, an unfinished ``running`` row
    carries no verdict yet, and a newer ``investigate`` dispatch is not an attempt
    at all — none of the three may outrank the newer ``no_pr`` on ``opus``."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'last_attempt.db'}"
    await init_db(db_url=db_url)
    from datetime import datetime, timedelta, timezone
    # Relative to now, never an absolute date literal (spec §11): a fixed calendar
    # date is a time bomb the day something starts comparing it to ``now()``.
    base = datetime.now(timezone.utc) - timedelta(days=1)
    async with db_session() as db:
        db.add(DispatchOutcome(target_ref="o/r#1", body_fingerprint="fp", outcome="no_pr",
                               model="sonnet", ended_at=base))
        db.add(DispatchOutcome(target_ref="o/r#1", body_fingerprint="fp", outcome="no_pr",
                               model="opus", ended_at=base + timedelta(hours=1)))
        db.add(DispatchOutcome(target_ref="o/r#1", body_fingerprint="fp", outcome="running",
                               model="fable"))
    async with db_session() as db:
        assert await last_attempt_model(db, "o/r#1") == "opus"

    async with db_session() as db:
        db.add(DispatchOutcome(target_ref="o/r#1", body_fingerprint="fp", outcome="no_pr",
                               model="fable", dispatch_kind="investigate",
                               ended_at=base + timedelta(hours=2)))
    async with db_session() as db:
        assert await last_attempt_model(db, "o/r#1") == "opus"


@pytest.mark.asyncio
async def test_prior_attempts_counts_the_same_rows_last_attempt_model_reads(tmp_path):
    """The ladder's ``attempt_no`` seed: every bounce deletes the runner's claim
    row, so the count of past attempts lives here, not on the claim. It counts
    exactly what ``last_attempt_model`` considers an attempt — finalised
    ``implement`` dispatches for this ref — so the two ladder readers can never
    disagree: unfinished (``running``/``superseded``) rows, ``investigate``
    dispatches and other refs are all excluded. 0-based: a ref never dispatched
    has 0 prior attempts."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'prior_attempts.db'}"
    await init_db(db_url=db_url)
    async with db_session() as db:
        assert await prior_attempts(db, "o/r#1") == 0

    async with db_session() as db:
        for outcome in ("no_pr", "merged"):
            db.add(DispatchOutcome(target_ref="o/r#1", body_fingerprint="fp",
                                   outcome=outcome, model="sonnet"))
        # Neither of these is a finished attempt.
        db.add(DispatchOutcome(target_ref="o/r#1", body_fingerprint="fp",
                               outcome="running", model="sonnet"))
        db.add(DispatchOutcome(target_ref="o/r#1", body_fingerprint="fp",
                               outcome="superseded", model="sonnet"))
        # Not an attempt at all, and not this ref.
        db.add(DispatchOutcome(target_ref="o/r#1", body_fingerprint="fp", outcome="no_pr",
                               model="sonnet", dispatch_kind="investigate"))
        db.add(DispatchOutcome(target_ref="o/r#2", body_fingerprint="fp", outcome="no_pr",
                               model="sonnet"))
    async with db_session() as db:
        assert await prior_attempts(db, "o/r#1") == 2
        assert await prior_attempts(db, "o/r#2") == 1


@pytest.mark.asyncio
async def test_a_decision_proposal_is_not_a_bounce_and_not_an_attempt(tmp_path):
    """Spec 2026-09-18 §6 / review R1 F4: a dispatch that stopped to ask the owner a
    product question and parked the item on a ``decision`` hold neither failed nor
    used up a rung. All three ladder/queue readers go through the ONE
    ``_counts_as_attempt`` predicate, so the row is invisible to each of them —
    otherwise an item decided twice could be given up on for asking, and the next
    dispatch would climb from the tier that merely asked."""
    from datetime import datetime, timedelta, timezone

    from server.db.models import DECISION_PROPOSED_CAUSE
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'decision_rows.db'}"
    await init_db(db_url=db_url)
    # Relative to now, never an absolute date literal (spec §11).
    base = datetime.now(timezone.utc) - timedelta(days=1)
    async with db_session() as db:
        db.add(DispatchOutcome(target_ref="o/r#1", body_fingerprint="fp", outcome="no_pr",
                               model="sonnet", cause="session ended without a mergeable PR",
                               ended_at=base))
        # The proposal: newer, same ref, same fingerprint — and not an attempt.
        db.add(DispatchOutcome(target_ref="o/r#1", body_fingerprint="fp", outcome="no_pr",
                               model="opus", cause=DECISION_PROPOSED_CAUSE,
                               ended_at=base + timedelta(hours=1)))
    async with db_session() as db:
        rows = (await db.execute(bounces_stmt([("o/r#1", "fp")]))).all()
        assert rows_to_bounces(rows, [("o/r#1", "fp")]) == {"o/r#1": 1}
        assert await last_attempt_model(db, "o/r#1") == "sonnet"
        assert await prior_attempts(db, "o/r#1") == 1


@pytest.mark.asyncio
async def test_a_null_cause_row_still_counts(tmp_path):
    """The NULL leg of the predicate, which is the whole population of rows written
    before causes were recorded: SQL's ``cause != '…'`` is NULL — not true — for
    every one of them, so a predicate without the ``IS NULL`` branch would silently
    stop counting the entire history."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'null_cause.db'}"
    await init_db(db_url=db_url)
    async with db_session() as db:
        db.add(DispatchOutcome(target_ref="o/r#1", body_fingerprint="fp", outcome="no_pr",
                               model="sonnet", cause=None))
    async with db_session() as db:
        rows = (await db.execute(bounces_stmt([("o/r#1", "fp")]))).all()
        assert rows_to_bounces(rows, [("o/r#1", "fp")]) == {"o/r#1": 1}
        assert await last_attempt_model(db, "o/r#1") == "sonnet"
        assert await prior_attempts(db, "o/r#1") == 1


def test_needs_approval_suffix_round_trips_through_escalation_for():
    """The hand-off reason the runner writes at an approval-clamped top and the
    structured ``escalation`` the holds API reads back are ONE pair: the writer
    (``needs_approval_suffix``) and the reader (``escalation_for``) live side by
    side so the Roadmap can offer "Approve escalation to fable" for exactly the
    holds the runner wrote that way."""
    from server.pm.outcomes import (Escalation, escalation_for, escalation_to_wire,
                                    needs_approval_suffix)
    reason = f"bounced at opus{needs_approval_suffix('fable')}"
    assert reason == "bounced at opus; fable needs approval"
    assert escalation_for("owner_action", reason) == Escalation(bounced_at="opus",
                                                                needs_approval="fable")
    # PR Doctor's top-of-ladder hand-off names the gate without a bounce tier.
    assert escalation_for("owner_action", f"PR Doctor gave up at the top{needs_approval_suffix('fable')}") \
        == Escalation(bounced_at=None, needs_approval="fable")
    # Only an owner_action hold can be approved into a climb; every other kind,
    # an ordinary give-up and an unknown tier read as "not an escalation".
    assert escalation_for("stronger_model", reason) is None
    assert escalation_for(None, reason) is None
    assert escalation_for("owner_action", "bounced at opus: no mergeable PR after 3 attempts") is None
    assert escalation_for("owner_action", "bounced at opus; gpt-9 needs approval") is None
    assert escalation_for("owner_action", None) is None
    assert escalation_to_wire(Escalation("opus", "fable")) == {"bouncedAt": "opus", "needsApproval": "fable"}
    assert escalation_to_wire(None) is None
