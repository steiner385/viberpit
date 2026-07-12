import unittest
from datetime import datetime, timedelta, timezone
from tests.helpers import temp_env, usage


class TestSelector(unittest.TestCase):
    def setUp(self):
        with temp_env():
            from ccswitch.config import load_config
            self.cfg = load_config()
        self.now = datetime.now(timezone.utc)

    def test_picks_lowest_session_utilization(self):
        from ccswitch.selector import select_best
        usages = {"alpha": usage(session=80.0), "beta": usage(session=10.0), "gamma": usage(session=40.0)}
        self.assertEqual(select_best(usages, set(), self.cfg, self.now), "beta")

    def test_weekly_breaks_ties(self):
        from ccswitch.selector import select_best
        usages = {"alpha": usage(session=10.0, weekly=90.0), "beta": usage(session=10.0, weekly=20.0)}
        self.assertEqual(select_best(usages, set(), self.cfg, self.now), "beta")

    def test_excludes_at_ceiling_and_reserved_and_reauth(self):
        from ccswitch.selector import select_best
        usages = {
            "alpha": usage(session=100.0),       # at session ceiling
            "beta": usage(session=10.0, weekly=100.0),  # at weekly ceiling
            "gamma": None,                       # needs-reauth
            "delta": usage(session=5.0),         # reserved by another
            "echo": usage(session=50.0),         # the only valid pick
        }
        self.assertEqual(select_best(usages, {"delta"}, self.cfg, self.now), "echo")

    def test_all_busy_raises_with_earliest_reset(self):
        from ccswitch.selector import select_best, AllAccountsBusy
        usages = {"alpha": usage(session=100.0, mins=60), "beta": usage(session=100.0, mins=30)}
        with self.assertRaises(AllAccountsBusy) as ctx:
            select_best(usages, set(), self.cfg, self.now)
        self.assertIsNotNone(ctx.exception.earliest_reset)

    def test_earliest_reset_reflects_only_dimension_at_ceiling(self):
        # Session is fine (10%, resets soon) but weekly is at ceiling (100%, resets far out).
        # The earliest_reset must reflect the WEEKLY reset (the dimension actually at ceiling),
        # NOT the sooner session reset — otherwise --wait sleeps until the wrong time and busy-loops.
        from ccswitch.oauth import Usage
        from ccswitch.selector import select_best, AllAccountsBusy
        session_reset = self.now + timedelta(minutes=30)
        weekly_reset = self.now + timedelta(days=2)
        u = Usage(
            session_pct=10.0, session_resets_at=session_reset,
            weekly_pct=100.0, weekly_resets_at=weekly_reset,
            extra_usage_enabled=False,
        )
        with self.assertRaises(AllAccountsBusy) as ctx:
            select_best({"solo": u}, set(), self.cfg, self.now)
        self.assertEqual(ctx.exception.earliest_reset, weekly_reset)

    # --- perishability-weighted selection (prefer_soon_reset, default on) ---

    def test_prefers_soon_resetting_at_equal_usage(self):
        # Equal usage on both accounts; the one whose window resets sooner has the more
        # "perishable" capacity, so we should drain it first to not waste that quota.
        from ccswitch.oauth import Usage
        from ccswitch.selector import select_best
        soon = self.now + timedelta(minutes=20)
        later = self.now + timedelta(hours=5)
        usages = {
            "later": Usage(50.0, later, 20.0, later, False),
            "soon": Usage(50.0, soon, 20.0, soon, False),
        }
        self.assertEqual(select_best(usages, set(), self.cfg, self.now), "soon")

    def test_fresh_account_saved_for_last(self):
        # A fully fresh account (no active window) has no perishable capacity, so an account
        # with real usage that resets soon should be preferred — even though it has *more* usage.
        from ccswitch.oauth import Usage
        from ccswitch.selector import select_best
        soon = self.now + timedelta(minutes=30)
        usages = {
            "fresh": Usage(0.0, None, 0.0, None, False),
            "used": Usage(40.0, soon, 20.0, soon, False),
        }
        self.assertEqual(select_best(usages, set(), self.cfg, self.now), "used")

    def test_weekly_can_be_the_binding_perishable_dimension(self):
        # Session has tons of headroom & resets soon, but weekly is the tighter constraint and
        # resets sooner-relative-to-its-headroom — the binding dimension must drive the score.
        from ccswitch.oauth import Usage
        from ccswitch.selector import select_best
        soon = self.now + timedelta(hours=1)
        far = self.now + timedelta(days=3)
        usages = {
            # weekly nearly spent and resetting in 1h -> highly perishable
            "weekly_tight": Usage(10.0, far, 90.0, soon, False),
            # plenty of weekly headroom, far reset -> not perishable
            "weekly_loose": Usage(10.0, far, 20.0, far, False),
        }
        self.assertEqual(select_best(usages, set(), self.cfg, self.now), "weekly_tight")

    def test_degrades_to_lowest_usage_when_resets_far_and_equal(self):
        # When every reset is far out and identical, perishability ranking collapses to
        # "most remaining headroom" == lowest usage == the legacy behavior.
        from ccswitch.selector import select_best
        usages = {
            "high": usage(session=80.0, mins=300),
            "low": usage(session=10.0, mins=300),
            "mid": usage(session=40.0, mins=300),
        }
        self.assertEqual(select_best(usages, set(), self.cfg, self.now), "low")

    def test_all_fresh_is_deterministic(self):
        # All candidates fresh (None resets): no perishability signal -> deterministic name order.
        from ccswitch.oauth import Usage
        from ccswitch.selector import select_best
        usages = {"b": Usage(0.0, None, 0.0, None, False),
                  "a": Usage(0.0, None, 0.0, None, False),
                  "c": Usage(0.0, None, 0.0, None, False)}
        self.assertEqual(select_best(usages, set(), self.cfg, self.now), "a")

    def test_identical_accounts_tiebreak_by_name(self):
        from ccswitch.oauth import Usage
        from ccswitch.selector import select_best
        soon = self.now + timedelta(minutes=45)
        usages = {"zulu": Usage(30.0, soon, 10.0, soon, False),
                  "alpha": Usage(30.0, soon, 10.0, soon, False),
                  "mike": Usage(30.0, soon, 10.0, soon, False)}
        self.assertEqual(select_best(usages, set(), self.cfg, self.now), "alpha")

    def test_toggle_off_reproduces_legacy_lowest_usage(self):
        # With prefer_soon_reset disabled, reset time is ignored: equal usage -> name tiebreak.
        from ccswitch.oauth import Usage
        from ccswitch.selector import select_best
        self.cfg.prefer_soon_reset = False
        soon = self.now + timedelta(minutes=20)
        later = self.now + timedelta(hours=5)
        usages = {
            "later": Usage(50.0, later, 20.0, later, False),
            "soon": Usage(50.0, soon, 20.0, soon, False),
        }
        self.assertEqual(select_best(usages, set(), self.cfg, self.now), "later")

    # --- load-aware selection (live-session count discounts perishability) ---

    def test_load_demotes_busy_perishable_account(self):
        # Two equally-perishable accounts; the one already carrying live load is demoted.
        from ccswitch.oauth import Usage
        from ccswitch.selector import select_best
        soon = self.now + timedelta(minutes=30)
        usages = {
            "busy": Usage(40.0, soon, 20.0, soon, False),
            "free": Usage(40.0, soon, 20.0, soon, False),
        }
        self.assertEqual(
            select_best(usages, set(), self.cfg, self.now, load={"busy": 2.0}), "free")

    def test_empty_load_reproduces_legacy_order(self):
        # load={} must rank identically to the no-load call (regression guard).
        from ccswitch.oauth import Usage
        from ccswitch.selector import select_best
        soon = self.now + timedelta(minutes=20)
        later = self.now + timedelta(hours=5)
        usages = {
            "later": Usage(50.0, later, 20.0, later, False),
            "soon": Usage(50.0, soon, 20.0, soon, False),
        }
        self.assertEqual(
            select_best(usages, set(), self.cfg, self.now, load={}),
            select_best(usages, set(), self.cfg, self.now))

    def test_load_breaks_ties_when_perishability_off(self):
        # prefer_soon_reset off -> all composite scores 0; load (fewest sessions) decides.
        from ccswitch.selector import select_best
        self.cfg.prefer_soon_reset = False
        usages = {"alpha": usage(session=30.0), "beta": usage(session=30.0)}
        self.assertEqual(
            select_best(usages, set(), self.cfg, self.now,
                        load={"alpha": 1.0}), "beta")

    def test_heavier_load_loses_even_with_better_usage(self):
        # 'low' has better usage but heavy load; 'high' is idle and resets equally soon.
        # The load discount must outweigh the small usage edge.
        from ccswitch.oauth import Usage
        from ccswitch.selector import select_best
        soon = self.now + timedelta(minutes=30)
        usages = {
            "low": Usage(20.0, soon, 10.0, soon, False),
            "high": Usage(30.0, soon, 10.0, soon, False),
        }
        self.assertEqual(
            select_best(usages, set(), self.cfg, self.now, load={"low": 3.0}), "high")

    def test_large_load_is_bounded_no_crash(self):
        # A huge load value must not raise / produce NaN; it just sorts last.
        from ccswitch.oauth import Usage
        from ccswitch.selector import select_best
        soon = self.now + timedelta(minutes=30)
        usages = {
            "swamped": Usage(40.0, soon, 20.0, soon, False),
            "calm": Usage(40.0, soon, 20.0, soon, False),
        }
        self.assertEqual(
            select_best(usages, set(), self.cfg, self.now,
                        load={"swamped": 1e9}), "calm")


# --- rank_accounts() tests ---

from ccswitch.config import Config
from ccswitch.oauth import Usage

NOW = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
SOON = NOW + timedelta(minutes=30)


def _cfg(**kw):
    return Config(**kw)


def test_rank_accounts_orders_like_select_best():
    from ccswitch.selector import rank_accounts, select_best, RankedAccount
    usages = {
        "a": Usage(40.0, SOON, 20.0, SOON, False),
        "b": Usage(10.0, SOON, 5.0, SOON, False),
    }
    ranked, earliest = rank_accounts(usages, set(), _cfg(), NOW)
    names = [r.name for r in ranked]
    assert names[0] == select_best(usages, set(), _cfg(), NOW)
    assert [r.rank for r in ranked] == [1, 2]
    assert earliest is None


def test_rank_accounts_excludes_at_ceiling_and_reports_reset():
    from ccswitch.selector import rank_accounts
    reset = NOW + timedelta(hours=2)
    usages = {
        "busy": Usage(100.0, reset, 50.0, reset, False),
        "ok": Usage(10.0, SOON, 5.0, SOON, False),
    }
    ranked, earliest = rank_accounts(usages, set(), _cfg(), NOW)
    assert [r.name for r in ranked] == ["ok"]
    assert earliest == reset


def test_rank_accounts_all_busy_returns_empty_with_earliest():
    from ccswitch.selector import rank_accounts
    reset = NOW + timedelta(hours=1)
    usages = {"x": Usage(100.0, reset, 0.0, None, False)}
    ranked, earliest = rank_accounts(usages, set(), _cfg(), NOW)
    assert ranked == []
    assert earliest == reset


def test_rank_reason_perishable_lowest_load():
    from ccswitch.selector import rank_accounts
    usages = {"a": Usage(40.0, SOON, 20.0, SOON, False)}
    ranked, _ = rank_accounts(usages, set(), _cfg(), NOW, load={})
    assert ranked[0].reason == "perishable quota, lowest load"


def test_rank_reason_perishable_sharing_load():
    from ccswitch.selector import rank_accounts
    usages = {"a": Usage(40.0, SOON, 20.0, SOON, False)}
    ranked, _ = rank_accounts(usages, set(), _cfg(), NOW, load={"a": 2.0})
    assert ranked[0].reason == "perishable quota, sharing live load"


def test_rank_reason_fresh_saved_for_last():
    from ccswitch.selector import rank_accounts
    # No reset window on the binding dimension -> composite 0 -> "fresh".
    usages = {"a": Usage(0.0, None, 0.0, None, False)}
    ranked, _ = rank_accounts(usages, set(), _cfg(), NOW)
    assert ranked[0].reason == "fresh quota — saved for last"


def test_select_best_still_raises_all_busy():
    import pytest
    from ccswitch.selector import select_best, AllAccountsBusy
    reset = NOW + timedelta(hours=1)
    usages = {"x": Usage(100.0, reset, 0.0, None, False)}
    with pytest.raises(AllAccountsBusy) as ei:
        select_best(usages, set(), _cfg(), NOW)
    assert ei.value.earliest_reset == reset


# --- optimistic fallback + kind (new) ---

def test_select_picked_readable_is_not_optimistic():
    from ccswitch.selector import select_picked, Picked
    usages = {"a": Usage(10.0, SOON, 5.0, SOON, False)}
    assert select_picked(usages, set(), _cfg(), NOW) == Picked(name="a", optimistic=False)


def test_optimistic_fallback_off_excludes_none_usage():
    # Legacy behavior: with the flag off, an all-None set has no candidates.
    from ccswitch.selector import rank_accounts
    ranked, earliest = rank_accounts({"a": None, "b": None}, set(), _cfg(), NOW)
    assert ranked == []
    assert earliest is None


def test_optimistic_fallback_picks_least_load_when_all_unreadable():
    from ccswitch.selector import select_picked, Picked
    picked = select_picked({"a": None, "b": None}, set(), _cfg(), NOW,
                           load={"a": 2.0, "b": 0.0}, optimistic_fallback=True)
    assert picked == Picked(name="b", optimistic=True)


def test_optimistic_fallback_skips_unhealthy():
    from ccswitch.selector import select_picked, AllAccountsBusy
    import pytest
    with pytest.raises(AllAccountsBusy) as ei:
        select_picked({"a": None}, set(), _cfg(), NOW,
                      unhealthy=frozenset({"a"}), optimistic_fallback=True)
    assert ei.value.kind == "no_usable_accounts"
    assert ei.value.earliest_reset is None


def test_readable_outranks_optimistic():
    from ccswitch.selector import select_picked, Picked
    picked = select_picked({"a": Usage(10.0, SOON, 5.0, SOON, False), "b": None},
                           set(), _cfg(), NOW, optimistic_fallback=True)
    assert picked == Picked(name="a", optimistic=False)


def test_mixed_ceiling_plus_unreadable_picks_optimistic():
    from ccswitch.selector import select_picked, Picked
    reset = NOW + timedelta(hours=2)
    picked = select_picked({"a": Usage(100.0, reset, 0.0, None, False), "b": None},
                           set(), _cfg(), NOW, optimistic_fallback=True)
    assert picked == Picked(name="b", optimistic=True)


def test_all_at_ceiling_raises_at_ceiling_kind():
    from ccswitch.selector import select_picked, AllAccountsBusy
    import pytest
    reset = NOW + timedelta(hours=1)
    with pytest.raises(AllAccountsBusy) as ei:
        select_picked({"x": Usage(100.0, reset, 0.0, None, False)}, set(), _cfg(), NOW,
                      optimistic_fallback=True)
    assert ei.value.kind == "at_ceiling"
    assert ei.value.earliest_reset == reset


def test_optimistic_ranked_after_readable_in_list():
    from ccswitch.selector import rank_accounts
    ranked, _ = rank_accounts({"a": Usage(10.0, SOON, 5.0, SOON, False), "b": None},
                              set(), _cfg(), NOW, optimistic_fallback=True)
    assert [r.name for r in ranked] == ["a", "b"]
    assert ranked[0].optimistic is False and ranked[1].optimistic is True


# --- per-account weekly reserve (soft fallback tier) ---

LATER = NOW + timedelta(hours=5)


def test_reserve_demotes_over_reserve_account_to_last():
    from ccswitch.selector import rank_accounts
    # Perishability = binding-dimension headroom / hours-to-reset (higher = pick first).
    usages = {
        "hot": Usage(10.0, SOON, 91.0, SOON, False),    # weekly binding: 9%/0.5h = 18/h
        "cool": Usage(10.0, LATER, 20.0, LATER, False),  # weekly binding: 80%/5h = 16/h
    }
    # WITHOUT a reserve, hot is the more perishable account and wins…
    ranked_no_reserve, _ = rank_accounts(usages, set(), _cfg(), NOW)
    assert [r.name for r in ranked_no_reserve] == ["hot", "cool"]
    # …so the flip below is attributable to the reserve alone.
    ranked, _ = rank_accounts(usages, set(), _cfg(), NOW, reserves={"hot": 90.0})
    assert [r.name for r in ranked] == ["cool", "hot"]
    assert ranked[1].reason == "over weekly reserve — fallback"
    assert ranked[1].optimistic is False
    assert [r.rank for r in ranked] == [1, 2]


def test_reserve_boundary_is_inclusive():
    from ccswitch.selector import rank_accounts
    # "at" (20/h) outranks "under" (2.02/h) without reserves; with weekly_pct == reserve
    # it must be blocked (>= semantics), flipping the order.
    usages = {
        "at": Usage(10.0, SOON, 90.0, SOON, False),      # exactly at reserve → blocked
        "under": Usage(10.0, LATER, 89.9, LATER, False),  # just under → normal candidate
    }
    ranked_no_reserve, _ = rank_accounts(usages, set(), _cfg(), NOW)
    assert [r.name for r in ranked_no_reserve] == ["at", "under"]
    ranked, _ = rank_accounts(usages, set(), _cfg(), NOW,
                              reserves={"at": 90.0, "under": 90.0})
    assert [r.name for r in ranked] == ["under", "at"]
    assert ranked[1].reason == "over weekly reserve — fallback"


def test_reserve_soft_fallback_picks_least_weekly_used():
    from ccswitch.selector import select_picked, Picked
    usages = {
        "worse": Usage(10.0, SOON, 97.0, SOON, False),
        "better": Usage(10.0, SOON, 92.0, SOON, False),
    }
    picked = select_picked(usages, set(), _cfg(), NOW,
                           reserves={"worse": 90.0, "better": 90.0})
    assert picked == Picked(name="better", optimistic=False)


def test_reserve_hard_ceiling_still_excludes_outright():
    from ccswitch.selector import rank_accounts
    reset = NOW + timedelta(hours=2)
    usages = {
        "maxed": Usage(10.0, SOON, 100.0, reset, False),  # at ceiling — NOT fallback
        "ok": Usage(10.0, SOON, 5.0, SOON, False),
    }
    ranked, earliest = rank_accounts(usages, set(), _cfg(), NOW,
                                     reserves={"maxed": 90.0})
    assert [r.name for r in ranked] == ["ok"]
    assert earliest == reset


def test_optimistic_tier_outranks_reserve_blocked():
    from ccswitch.selector import rank_accounts
    usages = {"r": Usage(10.0, SOON, 95.0, SOON, False), "o": None}
    ranked, _ = rank_accounts(usages, set(), _cfg(), NOW,
                              optimistic_fallback=True, reserves={"r": 90.0})
    assert [r.name for r in ranked] == ["o", "r"]


def test_no_reserves_behavior_unchanged():
    from ccswitch.selector import rank_accounts
    usages = {
        "a": Usage(40.0, SOON, 20.0, SOON, False),
        "b": Usage(10.0, SOON, 95.0, SOON, False),
    }
    base, _ = rank_accounts(usages, set(), _cfg(), NOW)
    empty, _ = rank_accounts(usages, set(), _cfg(), NOW, reserves={})
    under, _ = rank_accounts(usages, set(), _cfg(), NOW, reserves={"a": 99.0})
    assert [r.name for r in base] == [r.name for r in empty] == [r.name for r in under]
