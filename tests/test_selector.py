import unittest
from datetime import datetime, timezone
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
