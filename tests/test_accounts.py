import json
import time
import unittest
from datetime import datetime, timezone
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


class TestAccounts(unittest.TestCase):
    def test_ensure_valid_token_refreshes_when_expired(self):
        with temp_env():
            from ccswitch import accounts, oauth
            _seed_account("alpha", expires_in_ms=-1)  # already expired
            new_blob = {"accessToken": "fresh", "refreshToken": "r2", "expiresAt": int(time.time()*1000)+9_000_000, "subscriptionType": "max"}
            with mock.patch.object(oauth, "refresh_access_token", return_value=new_blob) as rt:
                blob = accounts.ensure_valid_token("alpha")
                rt.assert_called_once()
                self.assertEqual(blob["accessToken"], "fresh")
            # persisted to slot
            from ccswitch import paths
            self.assertEqual(json.loads(paths.slot_path("alpha").read_text())["accessToken"], "fresh")

    def test_usage_for_uses_cache_then_fetches(self):
        with temp_env():
            from ccswitch import accounts, oauth
            _seed_account("alpha")
            with mock.patch.object(oauth, "fetch_usage", return_value=usage(session=33.0)) as fu:
                u1 = accounts.usage_for("alpha")
                u2 = accounts.usage_for("alpha")  # second call served from cache
                self.assertEqual(u1.session_pct, 33.0)
                self.assertEqual(fu.call_count, 1)

    def test_usage_for_autherror_marks_needs_reauth(self):
        with temp_env():
            from ccswitch import accounts, oauth
            _seed_account("alpha")
            with mock.patch.object(oauth, "fetch_usage", side_effect=oauth.AuthError("401")), \
                 mock.patch.object(oauth, "refresh_access_token", side_effect=oauth.AuthError("dead")):
                self.assertIsNone(accounts.usage_for("alpha"))
                self.assertEqual(accounts.list_accounts()[0]["status"], "needs-reauth")

    def test_pick_account_reserves_winner(self):
        with temp_env():
            from ccswitch import accounts, oauth
            _seed_account("alpha"); _seed_account("beta")
            def fake_usage(name):
                return usage(session=80.0) if name == "alpha" else usage(session=5.0)
            with mock.patch.object(accounts, "usage_for", side_effect=fake_usage):
                self.assertEqual(accounts.pick_account(accounts.load_config()), "beta")
                # beta now reserved → excluded on the next pick, alpha wins
                self.assertEqual(accounts.pick_account(accounts.load_config()), "alpha")

    def test_sync_back_splits_live_creds(self):
        with temp_env():
            from ccswitch import accounts, paths, configdir
            _seed_account("alpha")
            cfg_dir = configdir.compose("alpha")
            # simulate claude refreshing the token + an MCP login in the live file
            live = json.loads((cfg_dir / ".credentials.json").read_text())
            live["claudeAiOauth"]["accessToken"] = "refreshed"
            live["claudeAiOauth"]["expiresAt"] = 9_999_999_999_999
            live["mcpOAuth"] = {"sentry": {"t": 1}}
            (cfg_dir / ".credentials.json").write_text(json.dumps(live))
            accounts.sync_back("alpha")
            self.assertEqual(json.loads(paths.slot_path("alpha").read_text())["accessToken"], "refreshed")
            self.assertEqual(json.loads(paths.shared_mcp_path().read_text()), {"sentry": {"t": 1}})
