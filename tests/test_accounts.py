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
                self.assertEqual(u1.window("session").used_pct, 33.0)
                self.assertEqual(fu.call_count, 1)

    def test_usage_for_autherror_marks_needs_reauth(self):
        with temp_env():
            from ccswitch import accounts, oauth
            _seed_account("alpha")
            with mock.patch.object(oauth, "fetch_usage", side_effect=oauth.AuthError("401")), \
                 mock.patch.object(oauth, "refresh_access_token", side_effect=oauth.AuthError("dead")):
                self.assertIsNone(accounts.usage_for("alpha"))
                self.assertEqual(accounts.list_accounts()[0]["status"], "needs-reauth")

    def test_usage_for_refresh_transient_does_not_mark_needs_reauth(self):
        """Expired token + transient refresh failure (429/5xx) must NOT mark needs-reauth.

        This is the overnight bug: the background poller refreshed idle accounts'
        expired tokens, Anthropic rate-limited the *token* endpoint, and a 429 was
        misclassified as a dead credential. The token is fine — leave status alone.
        """
        with temp_env():
            from ccswitch import accounts, oauth, ledger
            _seed_account("alpha", expires_in_ms=-1)  # expired → forces a refresh
            with ledger.locked_ledger() as led:
                ledger.set_status(led, "alpha", "ok")
            with mock.patch.object(
                oauth, "refresh_access_token",
                side_effect=oauth.TransientError("token refresh failed with 429"),
            ):
                self.assertIsNone(accounts.usage_for("alpha"))
            self.assertEqual(accounts.list_accounts()[0]["status"], "ok")

    def test_usage_for_refresh_403_does_not_mark_needs_reauth(self):
        """Expired token + a 403 from the token endpoint (Cloudflare edge block) must
        NOT mark needs-reauth. This is the mass-reauth incident: a header-less refresh
        drew `error code: 1010` from Cloudflare, the 403 was misclassified as a dead
        grant, and every idle account in the sweep flipped to needs-reauth in the same
        second. A 403 is environmental — leave the status alone."""
        with temp_env():
            from ccswitch import accounts, oauth, ledger
            _seed_account("alpha", expires_in_ms=-1)  # expired → forces a refresh
            with ledger.locked_ledger() as led:
                ledger.set_status(led, "alpha", "ok")
            # Mock at the HTTP layer so the REAL refresh_access_token classifier runs:
            # a 403 must become TransientError, not AuthError. (Mocking refresh itself
            # would bypass the very classification this regression-tests.)
            with mock.patch.object(oauth, "http_request", return_value=(403, b"error code: 1010\n")):
                self.assertIsNone(accounts.usage_for("alpha"))
            self.assertEqual(accounts.list_accounts()[0]["status"], "ok")

    def test_usage_for_usageerror_returns_none_without_changing_status(self):
        """A transient usage-endpoint error (429/5xx) returns None and leaves status
        unchanged — it must NOT mark needs-reauth or propagate (which 500s the list)."""
        with temp_env():
            from ccswitch import accounts, oauth, ledger
            _seed_account("alpha")
            with ledger.locked_ledger() as led:
                ledger.set_status(led, "alpha", "ok")
            with mock.patch.object(oauth, "fetch_usage", side_effect=oauth.UsageError("usage endpoint returned 429")):
                # Returns None rather than raising — no 500.
                self.assertIsNone(accounts.usage_for("alpha"))
            # Status stays "ok" (a rate-limit is not an auth failure).
            self.assertEqual(accounts.list_accounts()[0]["status"], "ok")

    def test_usage_for_fetch_transient_returns_none_without_changing_status(self):
        """A persistent network failure exhausts retries inside http_request and
        surfaces as TransientError from fetch_usage. usage_for must treat it like a
        rate-limit: return None, leave status unchanged, never 500 or mark reauth."""
        with temp_env():
            from ccswitch import accounts, oauth, ledger
            _seed_account("alpha")
            with ledger.locked_ledger() as led:
                ledger.set_status(led, "alpha", "ok")
            with mock.patch.object(oauth, "fetch_usage", side_effect=oauth.TransientError("network error after 4 attempt(s)")):
                self.assertIsNone(accounts.usage_for("alpha"))
            self.assertEqual(accounts.list_accounts()[0]["status"], "ok")

    def test_usage_for_ensure_valid_token_autherror_no_double_refresh(self):
        """Expired token + dead refresh: ensure_valid_token raises AuthError → needs-reauth,
        fetch_usage is never called, refresh_access_token called exactly once."""
        with temp_env():
            from ccswitch import accounts, oauth
            _seed_account("alpha", expires_in_ms=-1)  # force token expiry
            fetch_spy = mock.patch.object(oauth, "fetch_usage", side_effect=AssertionError("fetch_usage must not be called"))
            refresh_mock = mock.patch.object(oauth, "refresh_access_token", side_effect=oauth.AuthError("dead"))
            with fetch_spy, refresh_mock as rm:
                result = accounts.usage_for("alpha")
                self.assertIsNone(result)
                rm.assert_called_once()
            self.assertEqual(accounts.list_accounts()[0]["status"], "needs-reauth")

    def test_usage_for_ensure_valid_token_autherror_logs_account(self):
        """The dead-refresh-token branch must log WHICH account died and why.

        This set_status(needs-reauth) is otherwise silent, which makes a recurring
        re-auth loss undebuggable from the service logs (the original kindash bug)."""
        with temp_env():
            from ccswitch import accounts, oauth
            _seed_account("alpha", expires_in_ms=-1)  # force a refresh attempt
            with mock.patch.object(oauth, "refresh_access_token", side_effect=oauth.AuthError("invalid_grant")):
                with self.assertLogs("ccswitch.accounts", level="WARNING") as cm:
                    self.assertIsNone(accounts.usage_for("alpha"))
            joined = "\n".join(cm.output)
            self.assertIn("alpha", joined)
            self.assertIn("needs-reauth", joined)
            self.assertIn("invalid_grant", joined)

    def test_usage_for_post_401_refresh_autherror_logs_account(self):
        """The post-401 second-refresh-failure branch must also log account + cause."""
        with temp_env():
            from ccswitch import accounts, oauth
            _seed_account("alpha")  # token fresh → ensure_valid_token passes
            with mock.patch.object(oauth, "fetch_usage", side_effect=oauth.AuthError("401")), \
                 mock.patch.object(oauth, "refresh_access_token", side_effect=oauth.AuthError("invalid_grant")):
                with self.assertLogs("ccswitch.accounts", level="WARNING") as cm:
                    self.assertIsNone(accounts.usage_for("alpha"))
            joined = "\n".join(cm.output)
            self.assertIn("alpha", joined)
            self.assertIn("needs-reauth", joined)
            self.assertIn("invalid_grant", joined)

    def test_pick_account_reserves_winner(self):
        with temp_env():
            from ccswitch import accounts
            _seed_account("alpha"); _seed_account("beta")
            def fake_usage(name):
                return usage(session=80.0) if name == "alpha" else usage(session=5.0)
            with mock.patch.object(accounts, "usage_for", side_effect=fake_usage):
                self.assertEqual(accounts.pick_account(accounts.load_config()), "beta")
                # beta now reserved → excluded on the next pick, alpha wins
                self.assertEqual(accounts.pick_account(accounts.load_config()), "alpha")

    def test_pick_account_falls_back_to_reserved_when_only_option(self):
        """When the only non-ceiling account is reserved, it is used as last resort.

        120s reservation TTL is a concurrent-pick dedup guard, not a hard cap on
        sequential spawns. When only one account has headroom, a second call must
        succeed by ignoring the reservation rather than raising AllAccountsBusy.
        """
        with temp_env():
            from ccswitch import accounts
            _seed_account("solo")
            _seed_account("maxed")
            def fake_usage(name):
                if name == "solo":
                    return usage(session=22.0)  # healthy
                return usage(session=100.0)     # at ceiling
            with mock.patch.object(accounts, "usage_for", side_effect=fake_usage):
                # First pick reserves "solo" for 120s
                first = accounts.pick_account(accounts.load_config())
                self.assertEqual(first, "solo")
                # Second pick: "solo" is reserved, "maxed" is at ceiling.
                # The fallback must relax the reservation and pick "solo" anyway.
                second = accounts.pick_account(accounts.load_config())
                self.assertEqual(second, "solo")

    def test_pick_account_forwards_load_to_selector(self):
        with temp_env():
            from ccswitch import accounts
            _seed_account("alpha"); _seed_account("beta")
            # Equal usage so load is the deciding factor; alpha carries live load.
            def fake_usage(name):
                return usage(session=30.0)
            with mock.patch.object(accounts, "usage_for", side_effect=fake_usage):
                picked = accounts.pick_account(
                    accounts.load_config(), load={"alpha": 2.0})
                self.assertEqual(picked, "beta")

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


def test_usage_for_emits_auth_failure_on_dead_token(monkeypatch, tmp_path):
    from ccswitch import accounts, oauth
    from tests.helpers import temp_env
    captured = []
    accounts.set_auth_failure_sink(captured.append)
    try:
        # Force acquire_valid_token (the Step-1 call in usage_for) to raise a
        # dead-grant AuthError with a body — recovery already exhausted.
        def boom(name):
            raise oauth.AuthError("token refresh failed with 400", status=400,
                                  body='{"error":"invalid_grant"}')
        monkeypatch.setattr(accounts, "acquire_valid_token", boom)
        with temp_env():
            from tests.helpers import register_accounts
            register_accounts("primary")
            accounts.usage_for("primary", path="poller-idle")
    finally:
        accounts.set_auth_failure_sink(None)
    assert len(captured) == 1
    f = captured[0]
    assert f.account == "primary" and f.status == 400 and f.path == "poller-idle"
    assert "invalid_grant" in f.body

def test_usage_for_400_in_step2_emits_auth_failure(monkeypatch):
    """When the ONE retry-refresh in Step 2 returns 400, emit_auth_failure must fire
    so the frontend gets the reauth prompt without waiting for the next poller cycle."""
    from ccswitch import accounts, oauth
    from tests.helpers import temp_env
    captured = []
    accounts.set_auth_failure_sink(captured.append)
    try:
        with temp_env():
            _seed_account("alpha")  # fresh token → Step 1 passes
            with mock.patch.object(oauth, "fetch_usage", side_effect=oauth.AuthError("401")), \
                 mock.patch.object(oauth, "refresh_access_token",
                                   side_effect=oauth.AuthError("token refresh failed with 400",
                                                               status=400, body='{"error":"invalid_grant"}')):
                result = accounts.usage_for("alpha", path="poller-test")
    finally:
        accounts.set_auth_failure_sink(None)
    assert result is None
    assert len(captured) == 1
    f = captured[0]
    assert f.account == "alpha" and f.status == 400 and f.path == "poller-test" and f.attempts == 2
    assert "invalid_grant" in f.body


def test_usage_for_400_adds_to_dead_accounts(monkeypatch):
    """After a 400 on the refresh token, the account lands in _dead_accounts so
    subsequent calls in the same process skip the network immediately."""
    from ccswitch import accounts, oauth
    from tests.helpers import temp_env
    with temp_env():
        _seed_account("alpha")
        accounts._dead_accounts.discard("alpha")
        fetch_calls = []
        def spy_fetch(*a, **kw):
            fetch_calls.append(1)
            raise oauth.AuthError("401")
        with mock.patch.object(oauth, "fetch_usage", side_effect=spy_fetch), \
             mock.patch.object(oauth, "refresh_access_token",
                               side_effect=oauth.AuthError("token refresh failed with 400", status=400)):
            accounts.usage_for("alpha")
        # Second call: _dead_accounts membership must short-circuit before any network call.
        with mock.patch.object(oauth, "fetch_usage", side_effect=AssertionError("must not fetch")):
            result = accounts.usage_for("alpha")
    assert result is None
    accounts._dead_accounts.discard("alpha")  # cleanup


def test_mark_reauthenticated_clears_dead_accounts():
    """mark_reauthenticated() must evict the account from _dead_accounts so the
    next usage_for call re-tries the network after a successful re-login."""
    from ccswitch import accounts
    from tests.helpers import temp_env
    with temp_env():
        _seed_account("alpha")
        accounts._dead_accounts.add("alpha")
        accounts.mark_reauthenticated("alpha")
        assert "alpha" not in accounts._dead_accounts


def test_usage_for_401_survives_refresh_emits_auth_failure(monkeypatch):
    """When the refresh succeeds but the usage endpoint still returns 401, the
    Step-2 handler must emit_auth_failure (it was missing before this fix)."""
    from ccswitch import accounts, oauth
    from tests.helpers import temp_env
    captured = []
    accounts.set_auth_failure_sink(captured.append)
    new_blob = {"accessToken": "refreshed", "refreshToken": "r2",
                "expiresAt": int(1e13), "subscriptionType": "max"}
    try:
        with temp_env():
            _seed_account("alpha")
            call_count = {"n": 0}
            def fetch_side_effect(*a, **kw):
                call_count["n"] += 1
                raise oauth.AuthError("401")
            with mock.patch.object(oauth, "fetch_usage", side_effect=fetch_side_effect), \
                 mock.patch.object(oauth, "refresh_access_token", return_value=new_blob):
                result = accounts.usage_for("alpha", path="poller-step2")
    finally:
        accounts.set_auth_failure_sink(None)
    assert result is None
    assert len(captured) == 1
    f = captured[0]
    assert f.account == "alpha" and f.path == "poller-step2" and f.attempts == 2
    accounts._dead_accounts.discard("alpha")  # cleanup


def test_set_sink_none_is_noop(monkeypatch):
    from ccswitch import accounts
    accounts.set_auth_failure_sink(None)  # unset
    # emit must not raise even with no sink:
    accounts.emit_auth_failure(accounts.AuthFailure(
        account="x", status=400, body="b", path="cli", token_expires_at=None, attempts=1))


def test_acquire_valid_token_recovers_via_sync_back(monkeypatch):
    from ccswitch import accounts, oauth
    state = {"synced": False}
    def boom(name):
        raise oauth.AuthError("token refresh failed with 400", status=400, body="{}")
    def fake_sync_back(name):
        state["synced"] = True
    fresh = {"accessToken": "new", "refreshToken": "newR", "expiresAt": 9_999_999_999_000}
    def fake_read(path):
        return fresh if state["synced"] else {"accessToken": "old", "expiresAt": 0}
    monkeypatch.setattr(accounts, "ensure_valid_token", boom)
    monkeypatch.setattr(accounts, "sync_back", fake_sync_back)
    monkeypatch.setattr(accounts, "read_credentials", fake_read)
    monkeypatch.setattr(accounts, "_is_fresh", lambda blob, cfg: blob.get("expiresAt", 0) > 1)
    out = accounts.acquire_valid_token("primary")
    assert out["accessToken"] == "new" and state["synced"] is True

def test_acquire_valid_token_reraises_when_no_fresher_copy(monkeypatch):
    from ccswitch import accounts, oauth
    import pytest
    def boom(name):
        raise oauth.AuthError("token refresh failed with 400", status=400, body="{}")
    monkeypatch.setattr(accounts, "ensure_valid_token", boom)
    monkeypatch.setattr(accounts, "sync_back", lambda name: None)
    monkeypatch.setattr(accounts, "read_credentials", lambda path: {"accessToken": "old", "expiresAt": 0})
    monkeypatch.setattr(accounts, "_is_fresh", lambda blob, cfg: False)
    with pytest.raises(oauth.AuthError):
        accounts.acquire_valid_token("primary")


class TestRefreshSerialization(unittest.TestCase):
    def test_concurrent_refresh_happens_once(self):
        import threading, time as _t
        with temp_env():
            from ccswitch import accounts, oauth, paths
            from ccswitch.creds import write_credentials
            now_ms = int(_t.time() * 1000)
            write_credentials(paths.slot_path("alpha"), {
                "accessToken": "old", "refreshToken": "r",
                "expiresAt": now_ms - 1, "subscriptionType": "max"})
            calls = []
            def fake_refresh(rt, prior, **kwargs):
                calls.append(rt); _t.sleep(0.05)
                return {"accessToken": "new", "refreshToken": "r2",
                        "expiresAt": now_ms + 10_000_000, "subscriptionType": "max"}
            with mock.patch.object(oauth, "refresh_access_token", side_effect=fake_refresh):
                results = []
                def worker(): results.append(accounts.ensure_valid_token("alpha"))
                ts = [threading.Thread(target=worker) for _ in range(2)]
                for t in ts: t.start()
                for t in ts: t.join()
            self.assertEqual(len(calls), 1)  # serialized: second caller re-read the fresh token
            self.assertTrue(all(r["accessToken"] == "new" for r in results))


class TestUsageForSetupTokenFallback(unittest.TestCase):
    """A setup-token (sk-ant-oat01) makes an account launch-capable via
    CLAUDE_CODE_OAUTH_TOKEN, so usage_for must NOT flip it to needs-reauth when
    the slot's browser-OAuth token is dead. Best-effort: fetch usage with the
    setup-token; keep status 'ok' (usage unknown) if that fails. Regression:
    'Generate long-lived token' wrote the setup-token but usage_for re-stamped
    needs-reauth on the very next poll, so the account flipped straight back to
    'needs authentication'. (Bug 2)
    """

    OAT = "sk-ant-oat01-SECRET"

    def test_dead_refresh_with_setup_token_fetches_usage_and_stays_ok(self):
        # site 1: expired slot token + dead refresh; setup-token exists and the
        # usage API accepts it → real usage, status ok.
        with temp_env():
            from ccswitch import accounts, oauth, crypto
            _seed_account("alpha", expires_in_ms=-1)
            with mock.patch.object(crypto, "read_oauth_token", return_value=self.OAT), \
                 mock.patch.object(oauth, "refresh_access_token", side_effect=oauth.AuthError("dead")), \
                 mock.patch.object(oauth, "fetch_usage", return_value=usage(session=42.0)) as fu:
                u = accounts.usage_for("alpha")
            self.assertIsNotNone(u)
            self.assertEqual(u.window("session").used_pct, 42.0)
            self.assertEqual(accounts.list_accounts()[0]["status"], "ok")
            # Usage was fetched with the setup-token, not the dead slot token.
            fu.assert_called_once_with(self.OAT, max_retries=mock.ANY, max_total_sec=mock.ANY)

    def test_dead_refresh_with_setup_token_usage_unknown_stays_ok(self):
        # site 1: setup-token exists but the usage API rejects it (or is down).
        # Must NOT flip to needs-reauth; status stays ok, usage None.
        with temp_env():
            from ccswitch import accounts, oauth, crypto
            _seed_account("alpha", expires_in_ms=-1)
            with mock.patch.object(crypto, "read_oauth_token", return_value=self.OAT), \
                 mock.patch.object(oauth, "refresh_access_token", side_effect=oauth.AuthError("dead")), \
                 mock.patch.object(oauth, "fetch_usage", side_effect=oauth.AuthError("401")):
                u = accounts.usage_for("alpha")
            self.assertIsNone(u)
            self.assertEqual(accounts.list_accounts()[0]["status"], "ok")

    def test_no_setup_token_still_marks_needs_reauth(self):
        # Guardrail: without a setup-token the dead-refresh path is unchanged.
        with temp_env():
            from ccswitch import accounts, oauth, crypto
            _seed_account("alpha", expires_in_ms=-1)
            with mock.patch.object(crypto, "read_oauth_token", return_value=None), \
                 mock.patch.object(oauth, "refresh_access_token", side_effect=oauth.AuthError("dead")):
                self.assertIsNone(accounts.usage_for("alpha"))
                self.assertEqual(accounts.list_accounts()[0]["status"], "needs-reauth")

    def test_fetch_401_survives_refresh_with_setup_token_stays_ok(self):
        # site 2: fresh slot token, usage 401s, forced refresh+refetch also 401s.
        # With a setup-token present, keep ok instead of needs-reauth.
        with temp_env():
            from ccswitch import accounts, oauth, crypto
            _seed_account("alpha")  # fresh
            with mock.patch.object(crypto, "read_oauth_token", return_value=self.OAT), \
                 mock.patch.object(oauth, "fetch_usage", side_effect=oauth.AuthError("401")), \
                 mock.patch.object(oauth, "refresh_access_token", side_effect=oauth.AuthError("dead")):
                u = accounts.usage_for("alpha")
            self.assertIsNone(u)
            self.assertEqual(accounts.list_accounts()[0]["status"], "ok")


class TestRemoveAccount(unittest.TestCase):
    def test_remove_account_drops_ledger_entry(self):
        """A removed account must not linger in the ledger: the health watchdog
        alerts on ledger status, so residue (e.g. needs-reauth) alerts forever
        for an account that no longer exists (2026-07-21 primary/secondary)."""
        with temp_env():
            from ccswitch import accounts, ledger, paths
            _seed_account("doomed")
            paths.account_cfg_dir("doomed").mkdir(parents=True)
            with ledger.locked_ledger() as led:
                led["accounts"]["doomed"] = {"status": "needs-reauth"}

            accounts.remove_account("doomed")

            self.assertFalse(paths.slot_path("doomed").exists())
            self.assertFalse(paths.account_cfg_dir("doomed").exists())
            with ledger.locked_ledger() as led:
                self.assertNotIn("doomed", led["accounts"])

    def test_remove_account_preserves_other_ledger_entries(self):
        with temp_env():
            from ccswitch import accounts, ledger
            _seed_account("doomed")
            _seed_account("kept")
            with ledger.locked_ledger() as led:
                led["accounts"]["doomed"] = {"status": "needs-reauth"}
                led["accounts"]["kept"] = {"status": "ok"}

            accounts.remove_account("doomed")

            with ledger.locked_ledger() as led:
                self.assertNotIn("doomed", led["accounts"])
                self.assertEqual(led["accounts"]["kept"], {"status": "ok"})
