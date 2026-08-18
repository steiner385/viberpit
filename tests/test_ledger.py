import time
import unittest
from datetime import datetime, timezone
from tests.helpers import temp_env, usage


class TestLedger(unittest.TestCase):
    def test_record_and_read_back_usage(self):
        with temp_env():
            from ccswitch import ledger
            from ccswitch.config import load_config
            cfg = load_config()
            now = datetime.now(timezone.utc)
            with ledger.locked_ledger() as led:
                ledger.record_usage(led, "alpha", usage(session=42.0), now)
            with ledger.locked_ledger() as led:
                got = ledger.cached_usage(led, "alpha", cfg, now)
            self.assertEqual(got.session_pct, 42.0)

    def test_cache_expires_after_ttl(self):
        with temp_env():
            from ccswitch import ledger
            from ccswitch.config import load_config
            cfg = load_config()
            now = datetime.now(timezone.utc)
            with ledger.locked_ledger() as led:
                ledger.record_usage(led, "alpha", usage(), now)
            from datetime import timedelta
            later = now + timedelta(seconds=cfg.usage_cache_ttl_sec + 1)
            with ledger.locked_ledger() as led:
                self.assertIsNone(ledger.cached_usage(led, "alpha", cfg, later))

    def test_reservation_lifecycle(self):
        with temp_env():
            from ccswitch import ledger
            now = time.time()
            with ledger.locked_ledger() as led:
                ledger.reserve(led, "alpha", now + 100)
                self.assertTrue(ledger.reservation_active(led, "alpha", now))
                self.assertFalse(ledger.reservation_active(led, "alpha", now + 200))


# --- weekly reserve (per-account) ---


def test_weekly_reserve_set_read_clear():
    with temp_env():
        from ccswitch import ledger
        with ledger.locked_ledger() as led:
            ledger.set_weekly_reserve(led, "a", 90.0)
        with ledger.locked_ledger() as led:
            assert ledger.get_weekly_reserve(led, "a") == 90.0
            assert ledger.weekly_reserves(led) == {"a": 90.0}
            ledger.set_weekly_reserve(led, "a", None)
        with ledger.locked_ledger() as led:
            assert ledger.get_weekly_reserve(led, "a") is None
            assert ledger.weekly_reserves(led) == {}
            assert "weeklyReservePct" not in led["accounts"]["a"]


def test_weekly_reserve_non_numeric_treated_as_unset():
    with temp_env():
        from ccswitch import ledger
        with ledger.locked_ledger() as led:
            led["accounts"]["junk"] = {"weeklyReservePct": "ninety"}
            led["accounts"]["boolish"] = {"weeklyReservePct": True}
            assert ledger.get_weekly_reserve(led, "junk") is None
            assert ledger.get_weekly_reserve(led, "boolish") is None
            assert ledger.weekly_reserves(led) == {}


def test_list_accounts_includes_weekly_reserve():
    with temp_env():
        from ccswitch import paths, accounts
        store = paths.store_dir()
        store.mkdir(parents=True, exist_ok=True)
        (store / "a.cred.json").write_text(
            '{"accessToken":"t","refreshToken":"r","expiresAt":9999999999999}')
        accounts.set_weekly_reserve("a", 88.0)
        [acct] = accounts.list_accounts()
        assert acct["weeklyReservePct"] == 88.0
        accounts.set_weekly_reserve("a", None)
        [acct] = accounts.list_accounts()
        assert acct["weeklyReservePct"] is None


# --- enabled / disabled (per-account) ---


def test_account_enabled_defaults_true_when_absent():
    """A ledger entry with no ``enabled`` key is treated as enabled (back-compat:
    existing ledgers predate the field and must not silently disable accounts)."""
    with temp_env():
        from ccswitch import ledger
        with ledger.locked_ledger() as led:
            led["accounts"]["a"] = {}  # no "enabled" key
            assert ledger.is_account_enabled(led, "a") is True
            # An entirely unknown account is enabled by default too.
            assert ledger.is_account_enabled(led, "ghost") is True


def test_set_account_enabled_persists_and_reads_back():
    with temp_env():
        from ccswitch import ledger
        with ledger.locked_ledger() as led:
            ledger.set_account_enabled(led, "a", False)
        with ledger.locked_ledger() as led:
            assert ledger.is_account_enabled(led, "a") is False
            assert led["accounts"]["a"]["enabled"] is False
            ledger.set_account_enabled(led, "a", True)
        with ledger.locked_ledger() as led:
            assert ledger.is_account_enabled(led, "a") is True
            assert led["accounts"]["a"]["enabled"] is True


def test_list_accounts_includes_enabled():
    with temp_env():
        from ccswitch import paths, accounts
        store = paths.store_dir()
        store.mkdir(parents=True, exist_ok=True)
        (store / "a.cred.json").write_text(
            '{"accessToken":"t","refreshToken":"r","expiresAt":9999999999999}')
        # Default (no field written yet) surfaces as enabled.
        [acct] = accounts.list_accounts()
        assert acct["enabled"] is True
        accounts.set_account_enabled("a", False)
        [acct] = accounts.list_accounts()
        assert acct["enabled"] is False
