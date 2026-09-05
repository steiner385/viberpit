"""accounts.mark_reauthenticated clears the stale needs-reauth status + cache.

Regression: after a successful reauth, capture_login writes the fresh token to
the slot but never touched the ledger, so the row stayed "Needs re-auth" with
stale cached usage. get_accounts reads the stored status before refreshing, so
the correction must be synchronous.
"""
import time
import unittest
from datetime import datetime, timezone

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


class TestMarkReauthenticated(unittest.TestCase):
    def test_clears_needs_reauth_status_and_cached_usage(self):
        with temp_env():
            from ccswitch import accounts, ledger
            from ccswitch.quota import anthropic_snapshot
            _seed_account("kindash")
            # Simulate a dead account: needs-reauth status + stale cached usage.
            now = datetime.now(timezone.utc)
            with ledger.locked_ledger() as led:
                ledger.set_status(led, "kindash", "needs-reauth")
                ledger.record_usage(led, "kindash", anthropic_snapshot(40.0, now, 28.0, now, False), now)

            accounts.mark_reauthenticated("kindash")

            result = {a["name"]: a for a in accounts.list_accounts()}["kindash"]
            self.assertEqual(result["status"], "ok")
            # cached usage dropped → next read does a real fetch
            self.assertIsNone(result["usage"])

    def test_idempotent_on_healthy_account(self):
        with temp_env():
            from ccswitch import accounts
            _seed_account("primary")
            accounts.mark_reauthenticated("primary")
            result = {a["name"]: a for a in accounts.list_accounts()}["primary"]
            self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
