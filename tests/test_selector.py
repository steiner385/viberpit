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
