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
