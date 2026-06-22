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
        self.assertEqual(u.session_pct, 11.0)
        self.assertEqual(u.weekly_pct, 69.0)
        self.assertFalse(u.extra_usage_enabled)
        self.assertEqual(u.session_resets_at.tzinfo, timezone.utc)

    def test_parse_usage_falls_back_to_objects(self):
        from ccswitch.oauth import parse_usage
        payload = {k: v for k, v in USAGE_PAYLOAD.items() if k != "limits"}
        u = parse_usage(payload)
        self.assertEqual(u.session_pct, 11.0)
        self.assertEqual(u.weekly_pct, 69.0)

    def test_fetch_usage_ok(self):
        from ccswitch import oauth
        with mock.patch.object(oauth, "http_request", return_value=(200, json.dumps(USAGE_PAYLOAD).encode())):
            u = oauth.fetch_usage("tok")
            self.assertEqual(u.session_pct, 11.0)

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
        self.assertEqual(u.session_pct, 42.0)
        self.assertEqual(u.weekly_pct, 5.0)

    def test_refresh_url_encodes_special_chars_in_token(self):
        """refresh token containing &, =, + must be properly URL-encoded in the body."""
        from ccswitch import oauth
        special_token = "a&b=c+d"
        resp = {"access_token": "newA", "expires_in": 3600}
        captured = {}

        def fake_http(method, url, headers=None, data=None, timeout=15):
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
        self.assertEqual(u.session_pct, 0.0)
        self.assertEqual(u.weekly_pct, 0.0)
        self.assertIsNone(u.session_resets_at)
        self.assertIsNone(u.weekly_resets_at)

    def test_limits_session_null_reset_falls_back_to_null(self):
        from ccswitch.oauth import parse_usage
        payload = {
            "five_hour": {"utilization": 0.0, "resets_at": None},
            "seven_day": {"utilization": 40.0, "resets_at": "2026-06-22T20:00:00+00:00"},
            "limits": [{"kind": "weekly_all", "group": "weekly", "percent": 40,
                        "resets_at": "2026-06-22T20:00:00+00:00"}],
        }
        u = parse_usage(payload)
        self.assertEqual(u.session_pct, 0.0)
        self.assertIsNone(u.session_resets_at)
        self.assertEqual(u.weekly_pct, 40.0)
        self.assertIsNotNone(u.weekly_resets_at)
