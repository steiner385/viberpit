"""usage_for fetch-failure backoff.

A failing account must stop hitting the network on every call. Without this,
any caller that reads usage synchronously (pick_account_detailed probes run
~2x/min from automation) re-hammers Anthropic's usage endpoint through its
Retry-After window, so a transient 429 becomes a permanent one and the account
shows no usage/rank forever (kindash incident 2026-07-08). The backoff lives
in the ledger so every process/caller shares one cooldown.
"""
import json
import time
import unittest
from unittest import mock

from tests.helpers import temp_env, usage


def _seed_account(name, expires_in_ms=10_000_000):
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    now_ms = int(time.time() * 1000)
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-A", "refreshToken": f"{name}-R",
        "expiresAt": now_ms + expires_in_ms, "subscriptionType": "max",
    })
    write_json_secure(paths.shared_mcp_path(), {})


def _cooldown(name):
    """Read the account's backoff fields straight from the ledger file."""
    from ccswitch import paths
    data = json.loads(paths.ledger_path().read_text())
    acct = data["accounts"].get(name, {})
    return acct.get("usageFetchFails"), acct.get("usageFetchCooldownUntil")


def _expire_cooldown(name):
    from ccswitch import ledger
    with ledger.locked_ledger() as led:
        led["accounts"][name]["usageFetchCooldownUntil"] = time.time() - 1


class TestUsageFetchBackoff(unittest.TestCase):
    def test_failed_fetch_sets_cooldown_and_skips_network(self):
        with temp_env():
            from ccswitch import accounts, oauth
            _seed_account("alpha")
            with mock.patch.object(
                    oauth, "fetch_usage",
                    side_effect=oauth.UsageError("usage endpoint returned 429")) as fu:
                self.assertIsNone(accounts.usage_for("alpha"))
                self.assertEqual(fu.call_count, 1)
                # Cooldown recorded: fails=1, until ~now+60s
                fails, until = _cooldown("alpha")
                self.assertEqual(fails, 1)
                self.assertAlmostEqual(until - time.time(), 60.0, delta=5.0)
                # Within the cooldown: no second network attempt.
                self.assertIsNone(accounts.usage_for("alpha"))
                self.assertEqual(fu.call_count, 1)

    def test_cooldown_expiry_allows_refetch(self):
        with temp_env():
            from ccswitch import accounts, oauth
            _seed_account("alpha")
            with mock.patch.object(
                    oauth, "fetch_usage",
                    side_effect=oauth.UsageError("usage endpoint returned 429")) as fu:
                accounts.usage_for("alpha")
                _expire_cooldown("alpha")
                accounts.usage_for("alpha")
                self.assertEqual(fu.call_count, 2)

    def test_backoff_grows_exponentially_and_caps(self):
        with temp_env():
            from ccswitch import accounts, oauth
            _seed_account("alpha")
            with mock.patch.object(
                    oauth, "fetch_usage",
                    side_effect=oauth.UsageError("usage endpoint returned 429")):
                accounts.usage_for("alpha")
                _expire_cooldown("alpha")
                accounts.usage_for("alpha")
                fails, until = _cooldown("alpha")
                self.assertEqual(fails, 2)
                self.assertAlmostEqual(until - time.time(), 120.0, delta=5.0)
                # Drive the count high: the delay must clamp to the 900s cap.
                from ccswitch import ledger
                with ledger.locked_ledger() as led:
                    led["accounts"]["alpha"]["usageFetchFails"] = 50
                _expire_cooldown("alpha")
                accounts.usage_for("alpha")
                _, until = _cooldown("alpha")
                self.assertAlmostEqual(until - time.time(), 900.0, delta=45.0)

    def test_success_clears_backoff(self):
        with temp_env():
            from ccswitch import accounts, oauth
            _seed_account("alpha")
            with mock.patch.object(
                    oauth, "fetch_usage",
                    side_effect=oauth.UsageError("usage endpoint returned 429")):
                accounts.usage_for("alpha")
            _expire_cooldown("alpha")
            with mock.patch.object(oauth, "fetch_usage", return_value=usage(session=33.0)):
                u = accounts.usage_for("alpha")
            self.assertEqual(u.session_pct, 33.0)
            fails, until = _cooldown("alpha")
            self.assertIsNone(fails)
            self.assertIsNone(until)

    def test_dead_refresh_token_with_setup_token_backs_off(self):
        """The kindash shape: dead refresh token + setup-token fallback 429ing.

        Neither the token endpoint nor the usage endpoint may be re-hit while
        the cooldown is active.
        """
        with temp_env():
            from ccswitch import accounts, crypto, oauth
            _seed_account("alpha", expires_in_ms=-1)  # expired → refresh attempted
            with mock.patch.object(
                    oauth, "refresh_access_token",
                    side_effect=oauth.AuthError("dead")) as rt, \
                 mock.patch.object(crypto, "read_oauth_token", return_value="sk-ant-oat01-x"), \
                 mock.patch.object(
                    oauth, "fetch_usage",
                    side_effect=oauth.UsageError("usage endpoint returned 429")) as fu:
                self.assertIsNone(accounts.usage_for("alpha"))
                self.assertEqual(rt.call_count, 1)
                self.assertEqual(fu.call_count, 1)
                # Account stays ok (setup token keeps it launch-capable) …
                self.assertEqual(accounts.list_accounts()[0]["status"], "ok")
                # … but the cooldown stops the loop.
                self.assertIsNone(accounts.usage_for("alpha"))
                self.assertEqual(rt.call_count, 1)
                self.assertEqual(fu.call_count, 1)

    def test_needs_reauth_flip_backs_off_token_endpoint(self):
        """Dead refresh token, no setup token: don't re-refresh every call."""
        with temp_env():
            from ccswitch import accounts, crypto, oauth
            _seed_account("alpha", expires_in_ms=-1)
            with mock.patch.object(
                    oauth, "refresh_access_token",
                    side_effect=oauth.AuthError("dead")) as rt, \
                 mock.patch.object(crypto, "read_oauth_token", return_value=None):
                self.assertIsNone(accounts.usage_for("alpha"))
                self.assertIsNone(accounts.usage_for("alpha"))
                self.assertEqual(rt.call_count, 1)

    def test_mark_reauthenticated_clears_backoff(self):
        """Re-auth must not leave the account usage-dark for the cooldown tail."""
        with temp_env():
            from ccswitch import accounts, oauth
            _seed_account("alpha")
            with mock.patch.object(
                    oauth, "fetch_usage",
                    side_effect=oauth.UsageError("usage endpoint returned 429")) as fu:
                accounts.usage_for("alpha")
                accounts.mark_reauthenticated("alpha")
                accounts.usage_for("alpha")  # cooldown cleared → real fetch
                self.assertEqual(fu.call_count, 2)

    def test_fresh_cache_still_served_during_cooldown(self):
        """A cooldown must never mask a fresh cached value (cache wins first)."""
        with temp_env():
            from ccswitch import accounts, ledger, oauth
            _seed_account("alpha")
            with mock.patch.object(oauth, "fetch_usage", return_value=usage(session=42.0)):
                accounts.usage_for("alpha")  # populates the ledger cache
            with ledger.locked_ledger() as led:
                led["accounts"]["alpha"]["usageFetchFails"] = 1
                led["accounts"]["alpha"]["usageFetchCooldownUntil"] = time.time() + 600
            with mock.patch.object(oauth, "fetch_usage") as fu:
                u = accounts.usage_for("alpha")
                self.assertEqual(u.session_pct, 42.0)
                fu.assert_not_called()


if __name__ == "__main__":
    unittest.main()
