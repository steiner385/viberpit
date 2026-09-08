import email.message
import io
import json
import unittest
import urllib.error
import urllib.parse
from datetime import timezone
from unittest import mock


class _FakeResp:
    """Minimal context-manager stand-in for an urllib response."""

    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _http_error(code, retry_after=None, body=b"{}"):
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("https://x", code, "err", hdrs, io.BytesIO(body))


USAGE_PAYLOAD = {
    "five_hour": {"utilization": 11.0, "resets_at": "2026-06-19T18:00:00.6+00:00"},
    "seven_day": {"utilization": 69.0, "resets_at": "2026-06-22T20:00:00.6+00:00"},
    "extra_usage": {"is_enabled": False},
    "limits": [
        {"kind": "session", "group": "session", "percent": 11, "resets_at": "2026-06-19T18:00:00.6+00:00", "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 69, "resets_at": "2026-06-22T20:00:00.6+00:00", "is_active": True},
    ],
}


class TestOAuth(unittest.TestCase):
    def test_parse_usage_prefers_limits_array(self):
        from ccswitch.oauth import parse_usage
        u = parse_usage(USAGE_PAYLOAD)
        self.assertEqual(u.window("session").used_pct, 11.0)
        self.assertEqual(u.window("weekly").used_pct, 69.0)
        self.assertFalse(u.extra_usage_enabled)
        self.assertEqual(u.window("session").resets_at.tzinfo, timezone.utc)

    def test_parse_usage_falls_back_to_objects(self):
        from ccswitch.oauth import parse_usage
        payload = {k: v for k, v in USAGE_PAYLOAD.items() if k != "limits"}
        u = parse_usage(payload)
        self.assertEqual(u.window("session").used_pct, 11.0)
        self.assertEqual(u.window("weekly").used_pct, 69.0)

    def test_fetch_usage_ok(self):
        from ccswitch import oauth
        with mock.patch.object(oauth, "http_request", return_value=(200, json.dumps(USAGE_PAYLOAD).encode())):
            u = oauth.fetch_usage("tok")
            self.assertEqual(u.window("session").used_pct, 11.0)

    def test_fetch_usage_401_raises_autherror(self):
        from ccswitch import oauth
        with mock.patch.object(oauth, "http_request", return_value=(401, b"{}")):
            with self.assertRaises(oauth.AuthError):
                oauth.fetch_usage("tok")

    def test_refresh_maps_response_to_claudeaioauth(self):
        from ccswitch import oauth
        resp = {"access_token": "newA", "refresh_token": "newR", "expires_in": 3600, "scope": "user:inference user:profile"}
        with mock.patch.object(oauth, "http_request", return_value=(200, json.dumps(resp).encode())):
            prior = {"subscriptionType": "max", "rateLimitTier": "default_claude_max"}
            blob = oauth.refresh_access_token("oldR", prior)
            self.assertEqual(blob["accessToken"], "newA")
            self.assertEqual(blob["refreshToken"], "newR")
            self.assertEqual(blob["subscriptionType"], "max")  # carried from prior
            self.assertGreater(blob["expiresAt"], 0)
            self.assertEqual(blob["scopes"], ["user:inference", "user:profile"])

    def test_refresh_429_raises_transient_not_autherror(self):
        """A 429 on the token endpoint is rate-limiting, not a dead token.
        It must surface as TransientError, never AuthError (which marks needs-reauth)."""
        from ccswitch import oauth
        with mock.patch.object(oauth, "http_request", return_value=(429, b'{"error":"rate_limited"}')):
            with self.assertRaises(oauth.TransientError):
                oauth.refresh_access_token("oldR", {})

    def test_refresh_5xx_raises_transient_not_autherror(self):
        """A 503 from the token endpoint is a server blip — retryable, not terminal."""
        from ccswitch import oauth
        with mock.patch.object(oauth, "http_request", return_value=(503, b"upstream unavailable")):
            with self.assertRaises(oauth.TransientError):
                oauth.refresh_access_token("oldR", {})

    def test_refresh_transient_is_not_an_autherror(self):
        """TransientError must NOT be a subclass of AuthError, so `except AuthError`
        cannot swallow it and re-introduce the needs-reauth misclassification."""
        from ccswitch import oauth
        self.assertFalse(issubclass(oauth.TransientError, oauth.AuthError))

    def test_refresh_400_invalid_grant_raises_autherror(self):
        """A 400/401 is a genuinely dead refresh token — must stay terminal (AuthError)."""
        from ccswitch import oauth
        with mock.patch.object(oauth, "http_request", return_value=(400, b'{"error":"invalid_grant"}')):
            with self.assertRaises(oauth.AuthError):
                oauth.refresh_access_token("oldR", {})

    def test_refresh_401_raises_autherror(self):
        """A 401 from the token endpoint is a rejected grant — terminal (AuthError)."""
        from ccswitch import oauth
        with mock.patch.object(oauth, "http_request", return_value=(401, b'{"error":"unauthorized"}')):
            with self.assertRaises(oauth.AuthError):
                oauth.refresh_access_token("oldR", {})

    def test_refresh_403_raises_transient_not_autherror(self):
        """A 403 (e.g. a Cloudflare edge block — `error code: 1010`) is NOT proof the
        refresh token is dead: it's environmental and hits every account identically.
        It must surface as TransientError so a shared edge block cannot mass-flip all
        accounts to needs-reauth. Only 400/401 mean a genuinely rejected grant."""
        from ccswitch import oauth
        with mock.patch.object(oauth, "http_request", return_value=(403, b"error code: 1010\n")):
            with self.assertRaises(oauth.TransientError):
                oauth.refresh_access_token("oldR", {})

    def test_parse_usage_limits_missing_resets_at_falls_back(self):
        """limits session entry without resets_at must be skipped; fallback fills in."""
        from ccswitch.oauth import parse_usage
        payload = {
            "five_hour": {"utilization": 42.0, "resets_at": "2026-06-19T18:00:00.0+00:00"},
            "seven_day": {"utilization": 5.0, "resets_at": "2026-06-22T20:00:00.0+00:00"},
            "extra_usage": {"is_enabled": False},
            "limits": [
                # session entry is malformed — no resets_at
                {"kind": "session", "group": "session", "percent": 99},
                # weekly entry also malformed
                {"kind": "weekly_all", "group": "weekly", "percent": 88},
            ],
        }
        # Must not raise KeyError; must fall back to five_hour/seven_day values
        u = parse_usage(payload)
        self.assertEqual(u.window("session").used_pct, 42.0)
        self.assertEqual(u.window("weekly").used_pct, 5.0)

    def test_refresh_url_encodes_special_chars_in_token(self):
        """refresh token containing &, =, + must be properly URL-encoded in the body."""
        from ccswitch import oauth
        special_token = "a&b=c+d"
        resp = {"access_token": "newA", "expires_in": 3600}
        captured = {}

        def fake_http(method, url, headers=None, data=None, timeout=15, **kwargs):
            captured["data"] = data
            return 200, json.dumps(resp).encode()

        with mock.patch.object(oauth, "http_request", side_effect=fake_http):
            oauth.refresh_access_token(special_token, {})

        body_str = captured["data"].decode("utf-8")
        parsed = urllib.parse.parse_qs(body_str)
        self.assertEqual(parsed["refresh_token"], ["a&b=c+d"])

    def test_refresh_no_scope_in_response_defaults_scopes_to_empty(self):
        """When response has no 'scope' and prior has no scopes, blob['scopes'] == []."""
        from ccswitch import oauth
        resp = {"access_token": "newA", "expires_in": 3600}
        with mock.patch.object(oauth, "http_request", return_value=(200, json.dumps(resp).encode())):
            blob = oauth.refresh_access_token("tok", {})
        self.assertEqual(blob["scopes"], [])

    def test_refresh_400_autherror_carries_status_and_body(self):
        from unittest import mock
        import pytest
        from ccswitch import oauth
        body = b'{"error":"invalid_grant","error_description":"expired"}'
        with mock.patch.object(oauth, "http_request", return_value=(400, body)):
            with pytest.raises(oauth.AuthError) as ei:
                oauth.refresh_access_token("oldR", {})
        assert ei.value.status == 400
        assert "invalid_grant" in ei.value.body

    def test_refresh_400_body_is_truncated(self):
        from unittest import mock
        import pytest
        from ccswitch import oauth
        big = b'{"error":"' + b"x" * 5000 + b'"}'
        with mock.patch.object(oauth, "http_request", return_value=(400, big)):
            with pytest.raises(oauth.AuthError) as ei:
                oauth.refresh_access_token("oldR", {})
        assert len(ei.value.body) <= 2048


class TestUserAgent(unittest.TestCase):
    def test_http_request_sends_ccswitch_user_agent(self):
        """Every outbound request must carry a real User-Agent. The token endpoint
        (platform.claude.com) sits behind a Cloudflare bot rule that 403s clients
        with the default `Python-urllib` UA (`error code: 1010`); a header-less
        refresh is therefore blocked before Anthropic ever sees it."""
        from ccswitch import oauth
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["ua"] = req.get_header("User-agent")
            return _FakeResp(200, b"{}")

        with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=fake_urlopen):
            oauth.http_request("GET", "https://x")
        self.assertIsNotNone(captured["ua"], "no User-Agent header sent")
        self.assertTrue(
            captured["ua"].startswith("viberpit/"),
            f"expected a viberpit/<version> UA, got {captured['ua']!r}",
        )

    def test_explicit_user_agent_is_not_overridden(self):
        """A caller that sets its own User-Agent keeps it (default only fills the gap)."""
        from ccswitch import oauth
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["ua"] = req.get_header("User-agent")
            return _FakeResp(200, b"{}")

        with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=fake_urlopen):
            oauth.http_request("GET", "https://x", headers={"User-Agent": "custom/9"})
        self.assertEqual(captured["ua"], "custom/9")


class TestHttpRequestRetry(unittest.TestCase):
    def test_retries_on_429_then_succeeds(self):
        from ccswitch import oauth
        slept = []
        responses = [_http_error(429), _http_error(429), _FakeResp(200, b'{"ok":1}')]
        with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=responses):
            status, body = oauth.http_request(
                "GET", "https://x", max_retries=3, sleep=slept.append, rng=lambda: 0.5
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"ok":1}')
        self.assertEqual(len(slept), 2)  # two transient failures → two backoff sleeps

    def test_retries_on_5xx_then_succeeds(self):
        from ccswitch import oauth
        slept = []
        responses = [_http_error(503), _FakeResp(200, b'{"ok":1}')]
        with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=responses):
            status, _ = oauth.http_request(
                "GET", "https://x", max_retries=3, sleep=slept.append, rng=lambda: 0.5
            )
        self.assertEqual(status, 200)
        self.assertEqual(len(slept), 1)

    def test_honors_retry_after_header(self):
        from ccswitch import oauth
        slept = []
        responses = [_http_error(429, retry_after=7), _FakeResp(200)]
        with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=responses):
            oauth.http_request("GET", "https://x", max_retries=3, sleep=slept.append, rng=lambda: 0.5)
        self.assertEqual(slept, [7.0])  # server-specified delay wins over computed backoff

    def test_gives_up_after_max_retries_and_returns_status(self):
        from ccswitch import oauth
        slept = []
        responses = [_http_error(429), _http_error(429), _http_error(429)]
        with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=responses):
            status, _ = oauth.http_request(
                "GET", "https://x", max_retries=2, sleep=slept.append, rng=lambda: 0.5
            )
        self.assertEqual(status, 429)        # exhausted retries → surface the status
        self.assertEqual(len(slept), 2)      # max_retries sleeps, then give up

    def test_does_not_retry_terminal_4xx(self):
        from ccswitch import oauth
        slept = []
        with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=[_http_error(400)]):
            status, _ = oauth.http_request(
                "GET", "https://x", max_retries=3, sleep=slept.append, rng=lambda: 0.5
            )
        self.assertEqual(status, 400)
        self.assertEqual(slept, [])  # 400 is terminal — no retry

    def test_retries_on_403_then_succeeds(self):
        """403 is a Cloudflare edge block (error 1010), not a dead credential — retry
        with backoff so an intermittent block self-heals instead of surfacing as a
        spurious transient failure on the first hit."""
        from ccswitch import oauth
        slept = []
        responses = [_http_error(403), _FakeResp(200, b'{"ok":1}')]
        with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=responses):
            status, _ = oauth.http_request(
                "GET", "https://x", max_retries=3, sleep=slept.append, rng=lambda: 0.5
            )
        self.assertEqual(status, 200)
        self.assertEqual(len(slept), 1)  # one 403 → one backoff sleep, then success

    def test_retries_on_network_error_then_succeeds(self):
        """A connection-level failure (URLError) is transient — retry, don't crash."""
        from ccswitch import oauth
        slept = []
        responses = [urllib.error.URLError("connection refused"), _FakeResp(200, b'{"ok":1}')]
        with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=responses):
            status, body = oauth.http_request(
                "GET", "https://x", max_retries=3, sleep=slept.append, rng=lambda: 0.5
            )
        self.assertEqual(status, 200)
        self.assertEqual(len(slept), 1)

    def test_timeout_error_is_retried(self):
        from ccswitch import oauth
        slept = []
        responses = [TimeoutError("timed out"), _FakeResp(200, b'{"ok":1}')]
        with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=responses):
            status, _ = oauth.http_request(
                "GET", "https://x", max_retries=3, sleep=slept.append, rng=lambda: 0.5
            )
        self.assertEqual(status, 200)
        self.assertEqual(len(slept), 1)

    def test_network_error_exhausted_raises_transient(self):
        """Persistent network failure must surface as TransientError (token presumed
        valid), NOT propagate a raw URLError that crashes callers."""
        from ccswitch import oauth
        slept = []
        responses = [urllib.error.URLError("down")] * 4
        with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=responses):
            with self.assertRaises(oauth.TransientError):
                oauth.http_request(
                    "GET", "https://x", max_retries=3, sleep=slept.append, rng=lambda: 0.5
                )
        self.assertEqual(len(slept), 3)  # 3 retries, then give up

    def test_wall_clock_budget_stops_retrying(self):
        """max_total_sec bounds total time so a stalling endpoint can't turn
        max_retries x socket-timeout into minutes of blocking."""
        from ccswitch import oauth
        slept = []
        # clock: start=0, then elapsed 4, 8, 12 ... — budget 10s allows 2 retries.
        times = iter([0.0, 4.0, 8.0, 12.0, 16.0, 20.0])
        responses = [_http_error(503) for _ in range(10)]
        with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=responses) as uo:
            status, _ = oauth.http_request(
                "GET", "https://x", max_retries=9, max_total_sec=10.0,
                sleep=slept.append, rng=lambda: 0.5, clock=lambda: next(times),
            )
        self.assertEqual(status, 503)
        self.assertEqual(uo.call_count, 3)   # elapsed 12 >= 10 → stop after 3rd attempt
        self.assertEqual(len(slept), 2)

    def test_config_defaults_match_oauth_retry_defaults(self):
        """Single source of truth: Config retry knobs and oauth defaults must not drift."""
        from ccswitch import oauth
        from ccswitch.config import Config
        self.assertEqual(Config().retry_max_retries, oauth._DEFAULT_MAX_RETRIES)
        self.assertEqual(Config().retry_max_total_sec, oauth._DEFAULT_MAX_TOTAL_SEC)


class TestNonIdempotentRetry(unittest.TestCase):
    def test_http_request_non_idempotent_does_not_retry_5xx(self):
        from unittest import mock
        from ccswitch import oauth
        calls = {"n": 0}
        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            raise oauth.urllib.error.HTTPError("https://x", 503, "err", {}, __import__("io").BytesIO(b"down"))
        with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=fake_urlopen):
            status, body = oauth.http_request("POST", "https://x", data=b"f", idempotent=False,
                                              max_retries=3, sleep=lambda *_: None)
        assert status == 503
        assert calls["n"] == 1   # NOT resent — would risk consuming a rotated token

    def test_http_request_non_idempotent_still_retries_429(self):
        from unittest import mock
        from ccswitch import oauth
        seq = [(429, b"slow"), (200, b"{}")]
        calls = {"n": 0}
        def fake_urlopen(req, timeout=None):
            i = calls["n"]; calls["n"] += 1
            st, bd = seq[min(i, len(seq) - 1)]
            if st != 200:
                raise oauth.urllib.error.HTTPError("https://x", st, "err", {}, __import__("io").BytesIO(bd))
            class R:
                status = 200
                def read(self): return b"{}"
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()
        with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=fake_urlopen):
            status, _ = oauth.http_request("POST", "https://x", data=b"f", idempotent=False,
                                           max_retries=3, sleep=lambda *_: None)
        assert status == 200
        assert calls["n"] == 2   # 429 is an edge reject → safe to retry

    def test_http_request_non_idempotent_network_error_raises_without_retry(self):
        from unittest import mock
        import pytest
        from ccswitch import oauth
        calls = {"n": 0}
        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            raise TimeoutError("read timed out")
        with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=fake_urlopen):
            with pytest.raises(oauth.TransientError):
                oauth.http_request("POST", "https://x", data=b"f", idempotent=False,
                                   max_retries=3, sleep=lambda *_: None)
        assert calls["n"] == 1

    def test_refresh_passes_idempotent_false(self):
        from unittest import mock
        from ccswitch import oauth
        with mock.patch.object(oauth, "http_request", return_value=(200, b'{"access_token":"a","expires_in":1}')) as hr:
            oauth.refresh_access_token("oldR", {})
        assert hr.call_args.kwargs.get("idempotent") is False


class TestUsageNullReset(unittest.TestCase):
    def test_zero_usage_null_resets_at_does_not_crash(self):
        from ccswitch.oauth import parse_usage
        # a freshly-added account with no recent usage: utilization 0, resets_at null,
        # and no session/weekly entry in limits[]
        payload = {
            "five_hour": {"utilization": 0.0, "resets_at": None},
            "seven_day": {"utilization": 0.0, "resets_at": None},
            "extra_usage": {"is_enabled": False},
            "limits": [],
        }
        u = parse_usage(payload)
        self.assertEqual(u.window("session").used_pct, 0.0)
        self.assertEqual(u.window("weekly").used_pct, 0.0)
        self.assertIsNone(u.window("session").resets_at)
        self.assertIsNone(u.window("weekly").resets_at)

    def test_limits_session_null_reset_falls_back_to_null(self):
        from ccswitch.oauth import parse_usage
        payload = {
            "five_hour": {"utilization": 0.0, "resets_at": None},
            "seven_day": {"utilization": 40.0, "resets_at": "2026-06-22T20:00:00+00:00"},
            "limits": [{"kind": "weekly_all", "group": "weekly", "percent": 40,
                        "resets_at": "2026-06-22T20:00:00+00:00"}],
        }
        u = parse_usage(payload)
        self.assertEqual(u.window("session").used_pct, 0.0)
        self.assertIsNone(u.window("session").resets_at)
        self.assertEqual(u.window("weekly").used_pct, 40.0)
        self.assertIsNotNone(u.window("weekly").resets_at)


class TestHasUsageScope(unittest.TestCase):
    """``has_usage_scope`` — the authoritative "can this token read usage?" oracle.

    Verified against the real API on 2026-09-07: ``GET /api/oauth/usage`` refuses
    a setup-token minted without ``user:profile`` with a 429 (a rate-limit shape,
    indistinguishable from a genuine throttle), while ``GET /api/oauth/profile``
    answers the same token with an explicit 403 whose body carries
    ``oauth_scope_insufficient`` — and answers a ``user:profile`` token 200. The
    profile endpoint is therefore the scope oracle; the usage endpoint's 429 is not.
    """

    SCOPE_403 = (b'{"type":"error","error":{"type":"permission_error","message":"OAuth token '
                 b'does not meet scope requirement any_of(user:profile, user:office)","details":'
                 b'{"required_scopes":["user:profile","user:office"],"match":"any",'
                 b'"error_code":"oauth_scope_insufficient"}}}')

    def test_200_means_the_scope_is_present(self):
        from ccswitch import oauth
        with mock.patch.object(oauth, "http_request", return_value=(200, b'{"account":{}}')) as hr:
            self.assertTrue(oauth.has_usage_scope("tok"))
        self.assertEqual(hr.call_args.args[:2], ("GET", oauth.PROFILE_URL))

    def test_explicit_scope_refusal_means_absent(self):
        from ccswitch import oauth
        with mock.patch.object(oauth, "http_request", return_value=(403, self.SCOPE_403)):
            self.assertFalse(oauth.has_usage_scope("tok"))

    def test_scope_refusal_is_not_retried(self):
        """403 is on the shared transient list (Cloudflare 1010), but a SCOPE 403 is
        terminal: retrying it would burn the whole retry budget per probe."""
        from ccswitch import oauth
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            raise _http_error(403, body=self.SCOPE_403)

        with mock.patch.object(oauth.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(oauth.time, "sleep") as slept:
            self.assertFalse(oauth.has_usage_scope("tok"))
        self.assertEqual(len(calls), 1)
        slept.assert_not_called()

    def test_edge_403_without_scope_error_is_transient(self):
        """A Cloudflare block is a 403 too — it must not be read as 'scope absent'."""
        from ccswitch import oauth
        with mock.patch.object(oauth, "http_request", return_value=(403, b"error code: 1010\n")):
            with self.assertRaises(oauth.TransientError):
                oauth.has_usage_scope("tok")

    def test_401_is_an_auth_error_and_429_is_transient(self):
        from ccswitch import oauth
        with mock.patch.object(oauth, "http_request", return_value=(401, b"{}")):
            with self.assertRaises(oauth.AuthError):
                oauth.has_usage_scope("tok")
        with mock.patch.object(oauth, "http_request", return_value=(429, b"{}")):
            with self.assertRaises(oauth.TransientError):
                oauth.has_usage_scope("tok")
