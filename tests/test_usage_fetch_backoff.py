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
    path = paths.ledger_path()
    if not path.exists():
        return None, None
    data = json.loads(path.read_text())
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
            self.assertEqual(u.window("session").used_pct, 33.0)
            fails, until = _cooldown("alpha")
            self.assertIsNone(fails)
            self.assertIsNone(until)

    def test_dead_refresh_token_with_setup_token_reports_unsupported(self):
        """The kindash shape: dead refresh token + setup-token refused for usage.

        A narrow-scope setup-token (``user:inference``, no ``user:profile``) is
        refused by the usage endpoint *permanently* — retrying can never earn it
        the scope it lacks. So this is a capability gap, not a fetch failure: the
        snapshot says UNSUPPORTED and names the reason, the failure cooldown is
        NOT grown, and neither endpoint is re-hit because the snapshot caches
        under the normal TTL.

        The usage endpoint's 429 alone does not prove the gap (it is the same
        status a real throttle sends), so the fallback confirms it against the
        profile endpoint, which refuses a scope-less token explicitly.
        """
        with temp_env():
            from ccswitch import accounts, crypto, oauth
            from ccswitch.quota import REASON_SETUP_TOKEN_SCOPE, UNSUPPORTED
            _seed_account("alpha", expires_in_ms=-1)  # expired → refresh attempted
            with mock.patch.object(
                    oauth, "refresh_access_token",
                    side_effect=oauth.AuthError("dead")) as rt, \
                 mock.patch.object(crypto, "read_oauth_token", return_value="sk-ant-oat01-x"), \
                 mock.patch.object(oauth, "has_usage_scope", return_value=False) as hs, \
                 mock.patch.object(
                    oauth, "fetch_usage",
                    side_effect=oauth.UsageError("usage endpoint returned 429")) as fu:
                u = accounts.usage_for("alpha")
                self.assertIsNotNone(u)
                self.assertEqual(u.availability, UNSUPPORTED)
                self.assertEqual(u.reason, REASON_SETUP_TOKEN_SCOPE)
                self.assertEqual(u.windows, ())   # never a fabricated 0%
                hs.assert_called_once_with("sk-ant-oat01-x")
                self.assertEqual(rt.call_count, 1)
                self.assertEqual(fu.call_count, 1)
                # Account stays ok — the setup token keeps it launch-capable.
                self.assertEqual(accounts.list_accounts()[0]["status"], "ok")
                # No unbounded failure cooldown for a permanent capability gap.
                fails, until = _cooldown("alpha")
                self.assertIsNone(fails)
                self.assertIsNone(until)
                # The cached snapshot stops the loop just as the cooldown did.
                self.assertEqual(accounts.usage_for("alpha").availability, UNSUPPORTED)
                self.assertEqual(rt.call_count, 1)
                self.assertEqual(fu.call_count, 1)

    def test_setup_token_with_usage_scope_treats_a_429_as_transient(self):
        """A setup-token that HAS ``user:profile`` and still fails is throttled, not
        capability-gapped: back off (bounded, ledger-shared) and try again later.
        Recording UNSUPPORTED here would tell the user to re-authenticate an
        account whose credential is fine — the wrong instruction.
        """
        with temp_env():
            from ccswitch import accounts, crypto, oauth
            _seed_account("alpha", expires_in_ms=-1)
            with mock.patch.object(oauth, "refresh_access_token",
                                   side_effect=oauth.AuthError("dead")), \
                 mock.patch.object(crypto, "read_oauth_token", return_value="sk-ant-oat01-x"), \
                 mock.patch.object(oauth, "has_usage_scope", return_value=True), \
                 mock.patch.object(oauth, "fetch_usage",
                                   side_effect=oauth.UsageError("usage endpoint returned 429")) as fu:
                self.assertIsNone(accounts.usage_for("alpha"))
                self.assertEqual(accounts.list_accounts()[0]["status"], "ok")
                fails, until = _cooldown("alpha")
                self.assertEqual(fails, 1)
                self.assertAlmostEqual(until - time.time(), 60.0, delta=5.0)
                # Cooldown honoured: no second usage call until it lapses.
                self.assertIsNone(accounts.usage_for("alpha"))
                self.assertEqual(fu.call_count, 1)
                _expire_cooldown("alpha")
                accounts.usage_for("alpha")
                self.assertEqual(fu.call_count, 2)

    def test_setup_token_scope_probe_failure_is_transient_not_unsupported(self):
        """If the scope oracle itself cannot answer (network, 5xx), we do not know —
        and 'do not know' is a backoff, never a permanent UNSUPPORTED verdict."""
        with temp_env():
            from ccswitch import accounts, crypto, oauth, paths
            _seed_account("alpha", expires_in_ms=-1)
            with mock.patch.object(oauth, "refresh_access_token",
                                   side_effect=oauth.AuthError("dead")), \
                 mock.patch.object(crypto, "read_oauth_token", return_value="sk-ant-oat01-x"), \
                 mock.patch.object(oauth, "has_usage_scope",
                                   side_effect=oauth.TransientError("profile 503")), \
                 mock.patch.object(oauth, "fetch_usage",
                                   side_effect=oauth.UsageError("usage endpoint returned 429")):
                self.assertIsNone(accounts.usage_for("alpha"))
                fails, _ = _cooldown("alpha")
                self.assertEqual(fails, 1)
                data = json.loads(paths.ledger_path().read_text())
                self.assertNotIn("usage", data["accounts"]["alpha"])

    def test_unsupported_setup_token_usage_keeps_account_out_of_scored_tiers(self):
        """UNSUPPORTED must read to the selector exactly as None did.

        The snapshot is now non-None, so this pins the thing that could silently
        regress: an account with unreadable quota must not be scored as if it had
        headroom.
        """
        from ccswitch import selector
        from ccswitch.quota import UNSUPPORTED, unknown_snapshot
        from datetime import datetime, timezone
        snap = unknown_snapshot("anthropic", fetched_at=datetime.now(timezone.utc),
                                availability=UNSUPPORTED)
        self.assertFalse(selector.usage_known(snap))
        self.assertFalse(selector.usage_known(None))

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
                self.assertEqual(u.window("session").used_pct, 42.0)
                fu.assert_not_called()


# Copilot fetch-failure backoff moved with the proxy read itself to
# server/usage/process_quota.py (server/tests/test_process_quota.py): the core
# no longer reads a process, so it has nothing to back off from
# (tests/test_process_quota_ttl.py).


if __name__ == "__main__":
    unittest.main()
