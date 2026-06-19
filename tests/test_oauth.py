import json
import unittest
import urllib.parse
from datetime import datetime, timezone
from unittest import mock


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
