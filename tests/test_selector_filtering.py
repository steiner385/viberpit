"""Selector: filter by engine/provider/model BEFORE scoring, and the five tiers.

The tier order (candidates -> optimistic -> reserve-blocked -> extra-paid) and the
conservative unknown-usage rule are load-bearing: rotation, crash recovery and the
UI's rank tooltip all read them.
"""
import unittest
from datetime import datetime, timedelta, timezone

from ccswitch.quota import (AUTH_ERROR, QuotaSnapshot, QuotaWindow, SCOPE_MODEL,
                            UNKNOWN, anthropic_snapshot, copilot_snapshot,
                            unknown_snapshot)
from ccswitch.selector import (AllAccountsBusy, at_ceiling, binding_window,
                               exhausted, rank_accounts, select_best,
                               select_picked, usage_known)
from tests.helpers import temp_env, usage


class _Base(unittest.TestCase):
    def setUp(self):
        with temp_env():
            from ccswitch.config import load_config
            self.cfg = load_config()
        self.now = datetime.now(timezone.utc)


class TestEngineAndProviderFiltering(_Base):
    """Ineligible accounts are filtered out, not ranked-then-dropped."""

    def _mixed(self):
        return (
            {"claude1": usage(session=50.0), "cp1": copilot_snapshot(5.0, self.now),
             "gpt1": usage(session=1.0)},
            {"claude1": "anthropic", "cp1": "copilot", "gpt1": "openai"},
            {"claude1": None, "cp1": None, "gpt1": "codex"},
        )

    def test_require_engine_keeps_only_that_engine(self):
        usages, providers, engines = self._mixed()
        ranked, _ = rank_accounts(usages, set(), self.cfg, self.now,
                                  providers=providers, engines=engines,
                                  require_engine="codex")
        self.assertEqual([r.name for r in ranked], ["gpt1"])

    def test_absent_engine_declaration_counts_as_the_default_engine(self):
        """engine=None means "the registry's default" (claude_code), so an account
        that predates the manifest still matches a claude_code request."""
        usages, providers, engines = self._mixed()
        ranked, _ = rank_accounts(usages, set(), self.cfg, self.now,
                                  providers=providers, engines=engines,
                                  require_engine="claude_code",
                                  default_engine="claude_code")
        self.assertEqual(sorted(r.name for r in ranked), ["claude1", "cp1"])

    def test_require_provider_keeps_only_that_provider(self):
        usages, providers, engines = self._mixed()
        ranked, _ = rank_accounts(usages, set(), self.cfg, self.now,
                                  providers=providers, engines=engines,
                                  require_provider="copilot")
        self.assertEqual([r.name for r in ranked], ["cp1"])

    def test_a_filtered_out_account_is_not_busy(self):
        """An at-ceiling account of the WRONG engine must not supply earliest_reset:
        waiting for its window would never make the request satisfiable."""
        reset = self.now + timedelta(hours=4)
        usages = {"claude1": anthropic_snapshot(100.0, reset, 100.0, reset)}
        with self.assertRaises(AllAccountsBusy) as ctx:
            select_picked(usages, set(), self.cfg, self.now,
                          providers={"claude1": "anthropic"},
                          engines={"claude1": None}, require_engine="codex")
        self.assertIsNone(ctx.exception.earliest_reset)
        self.assertEqual(ctx.exception.kind, "no_usable_accounts")

    def test_no_filter_ranks_every_provider_together(self):
        usages, providers, engines = self._mixed()
        ranked, _ = rank_accounts(usages, set(), self.cfg, self.now,
                                  providers=providers, engines=engines)
        self.assertEqual(len(ranked), 3)


class TestModelScopedWindows(_Base):
    def _snap(self, session_pct, opus_pct):
        reset = self.now + timedelta(hours=2)
        return QuotaSnapshot(
            provider="anthropic", fetched_at=self.now,
            windows=(
                QuotaWindow(key="session", label="session", used_pct=session_pct,
                            resets_at=reset, duration_seconds=18000),
                QuotaWindow(key="opus_weekly", label="opus weekly", used_pct=opus_pct,
                            resets_at=reset, duration_seconds=604800,
                            scope=SCOPE_MODEL, applies_to_models=("opus",)),
            ))

    def test_model_scoped_ceiling_only_blocks_that_model(self):
        u = self._snap(session_pct=5.0, opus_pct=100.0)
        self.assertTrue(at_ceiling(u, 100.0, "opus"))
        self.assertFalse(at_ceiling(u, 100.0, "sonnet"))

    def test_selection_honours_the_model_scope(self):
        usages = {"a": self._snap(5.0, 100.0), "b": self._snap(60.0, 10.0)}
        self.assertEqual(select_best(usages, set(), self.cfg, self.now, model="opus"), "b")
        self.assertEqual(select_best(usages, set(), self.cfg, self.now, model="sonnet"), "a")


class TestUnknownAndAuthErrorUsage(_Base):
    def test_none_usage_is_excluded_without_optimistic_fallback(self):
        usages = {"a": None, "b": usage(session=90.0)}
        ranked, _ = rank_accounts(usages, set(), self.cfg, self.now)
        self.assertEqual([r.name for r in ranked], ["b"])

    def test_unavailable_snapshot_is_treated_exactly_like_none(self):
        """A snapshot that says 'unknown' must not read as a 0%-used fresh account."""
        for availability in (UNKNOWN, AUTH_ERROR):
            with self.subTest(availability=availability):
                usages = {"a": unknown_snapshot("anthropic", fetched_at=self.now,
                                                availability=availability),
                          "b": usage(session=90.0)}
                ranked, _ = rank_accounts(usages, set(), self.cfg, self.now)
                self.assertEqual([r.name for r in ranked], ["b"])
                self.assertFalse(usage_known(usages["a"]))

    def test_unavailable_snapshot_joins_the_optimistic_tier_when_opted_in(self):
        usages = {"a": unknown_snapshot("anthropic", fetched_at=self.now),
                  "b": usage(session=90.0)}
        ranked, _ = rank_accounts(usages, set(), self.cfg, self.now,
                                  optimistic_fallback=True)
        self.assertEqual([r.name for r in ranked], ["b", "a"])
        self.assertTrue(ranked[1].optimistic)

    def test_auth_error_account_marked_unhealthy_is_dropped_even_optimistically(self):
        usages = {"a": unknown_snapshot("anthropic", fetched_at=self.now,
                                        availability=AUTH_ERROR)}
        ranked, earliest = rank_accounts(usages, set(), self.cfg, self.now,
                                         unhealthy={"a"}, optimistic_fallback=True)
        self.assertEqual(ranked, [])
        self.assertIsNone(earliest)


class TestTiersSurviveTheRewrite(_Base):
    def test_reserve_tier_measures_the_longest_window(self):
        usages = {"over": usage(session=10.0, weekly=80.0),
                  "under": usage(session=40.0, weekly=10.0)}
        ranked, _ = rank_accounts(usages, set(), self.cfg, self.now,
                                  reserves={"over": 50.0})
        self.assertEqual([r.name for r in ranked], ["under", "over"])
        self.assertEqual(ranked[1].reason, "over weekly reserve — fallback")

    def test_reserve_on_a_single_window_provider_uses_that_window(self):
        usages = {"cp": copilot_snapshot(80.0, self.now + timedelta(hours=5)),
                  "other": usage(session=10.0, weekly=10.0)}
        ranked, _ = rank_accounts(usages, set(), self.cfg, self.now,
                                  reserves={"cp": 50.0})
        self.assertEqual([r.name for r in ranked], ["other", "cp"])

    def test_extra_paid_tier_needs_BOTH_the_optin_and_the_provider_flag(self):
        reset = self.now + timedelta(hours=1)
        opted_no_flag = anthropic_snapshot(100.0, reset, 10.0, reset, False)
        flag_no_optin = anthropic_snapshot(100.0, reset, 10.0, reset, True)
        both = anthropic_snapshot(100.0, reset, 10.0, reset, True)

        ranked, _ = rank_accounts({"a": opted_no_flag}, set(), self.cfg, self.now,
                                  extra_usage_allowed={"a"})
        self.assertEqual(ranked, [])
        ranked, _ = rank_accounts({"a": flag_no_optin}, set(), self.cfg, self.now)
        self.assertEqual(ranked, [])
        ranked, _ = rank_accounts({"a": both}, set(), self.cfg, self.now,
                                  extra_usage_allowed={"a"})
        self.assertEqual([r.name for r in ranked], ["a"])
        self.assertEqual(ranked[0].reason, "at ceiling — burning extra usage credits")

    def test_full_tier_order(self):
        """candidates -> optimistic -> reserve-blocked -> extra-paid, in that order."""
        reset = self.now + timedelta(hours=1)
        usages = {
            "cand": usage(session=10.0, weekly=10.0),
            "opt": None,
            "res": usage(session=10.0, weekly=80.0),
            "paid": anthropic_snapshot(100.0, reset, 10.0, reset, True),
        }
        ranked, _ = rank_accounts(usages, set(), self.cfg, self.now,
                                  optimistic_fallback=True,
                                  reserves={"res": 50.0},
                                  extra_usage_allowed={"paid"})
        self.assertEqual([r.name for r in ranked], ["cand", "opt", "res", "paid"])
        self.assertEqual([r.rank for r in ranked], [1, 2, 3, 4])

    def test_all_accounts_busy_carries_the_binding_window_reset(self):
        session_reset = self.now + timedelta(minutes=30)
        weekly_reset = self.now + timedelta(days=2)
        u = anthropic_snapshot(10.0, session_reset, 100.0, weekly_reset, False)
        with self.assertRaises(AllAccountsBusy) as ctx:
            select_best({"solo": u}, set(), self.cfg, self.now)
        # Only the window actually AT ceiling supplies the reset.
        self.assertEqual(ctx.exception.earliest_reset, weekly_reset)
        self.assertEqual(ctx.exception.error_data["kind"], "at_ceiling")

    def test_exhausted_still_needs_both_halves(self):
        reset = self.now + timedelta(hours=1)
        u = anthropic_snapshot(100.0, reset, 10.0, reset, True)
        self.assertFalse(exhausted(u, 100.0, extra_usage_allowed=True))
        self.assertTrue(exhausted(u, 100.0, extra_usage_allowed=False))

    def test_binding_window_helper_is_the_one_definition(self):
        u = usage(session=10.0, weekly=80.0)
        self.assertIs(binding_window(u), u.binding_window())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
