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
