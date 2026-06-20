"""Tests for per-account nickname storage (core layer)."""
import time
import unittest
from tests.helpers import temp_env


def _seed_account(name, expires_in_ms=10_000_000):
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    now_ms = int(time.time() * 1000)
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-A", "refreshToken": f"{name}-R",
        "expiresAt": now_ms + expires_in_ms, "subscriptionType": "max",
    })
    write_json_secure(paths.shared_mcp_path(), {})


class TestNicknames(unittest.TestCase):
    def test_set_nickname_appears_in_list_accounts(self):
        """accounts.set_nickname persists and list_accounts returns it."""
        with temp_env():
            from ccswitch import accounts
            _seed_account("work")
            accounts.set_nickname("work", "Work")
            result = accounts.list_accounts()
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["nickname"], "Work")

    def test_set_nickname_clear_with_empty_string(self):
        """set_nickname('', ...) clears an existing nickname."""
        with temp_env():
            from ccswitch import accounts
            _seed_account("work")
            accounts.set_nickname("work", "Work")
            # Clear with empty string
            accounts.set_nickname("work", "")
            result = accounts.list_accounts()
            self.assertIsNone(result[0]["nickname"])

    def test_set_nickname_clear_with_none(self):
        """set_nickname(None) clears an existing nickname."""
        with temp_env():
            from ccswitch import accounts
            _seed_account("personal")
            accounts.set_nickname("personal", "Personal")
            accounts.set_nickname("personal", None)
            result = accounts.list_accounts()
            self.assertIsNone(result[0]["nickname"])

    def test_no_nickname_returns_none(self):
        """list_accounts returns nickname=None when none has been set."""
        with temp_env():
            from ccswitch import accounts
            _seed_account("alpha")
            result = accounts.list_accounts()
            self.assertIsNone(result[0]["nickname"])

    def test_nickname_survives_ledger_round_trip(self):
        """Nickname persists across locked_ledger open/close cycles."""
        with temp_env():
            from ccswitch import accounts, ledger
            _seed_account("alpha")
            accounts.set_nickname("alpha", "Alpha Account")
            # Read directly from ledger
            with ledger.locked_ledger() as led:
                self.assertEqual(led["accounts"]["alpha"]["nickname"], "Alpha Account")


class TestLedgerSetNickname(unittest.TestCase):
    def test_ledger_set_nickname_direct(self):
        """ledger.set_nickname sets and clears nickname on led dict."""
        with temp_env():
            from ccswitch import ledger
            with ledger.locked_ledger() as led:
                ledger.set_nickname(led, "x", "Work")
                self.assertEqual(led["accounts"]["x"]["nickname"], "Work")
                ledger.set_nickname(led, "x", None)
                self.assertNotIn("nickname", led["accounts"]["x"])

    def test_ledger_set_nickname_blank_clears(self):
        """ledger.set_nickname with falsy string removes the key."""
        with temp_env():
            from ccswitch import ledger
            with ledger.locked_ledger() as led:
                ledger.set_nickname(led, "y", "Label")
                ledger.set_nickname(led, "y", "")
                self.assertNotIn("nickname", led["accounts"]["y"])
