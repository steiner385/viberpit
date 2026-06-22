"""Anthropic OAuth: exact usage + independent token refresh (stdlib urllib)."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
BETA_HEADER = "oauth-2025-04-20"


class AuthError(Exception):
    """Token genuinely rejected (400 invalid_grant / 401) — needs refresh or re-auth."""


class TransientError(Exception):
    """Endpoint returned a retryable status (429 / 5xx).

    NOT a credential failure — the token is presumed still valid and callers must
    leave the account's status unchanged (do not mark needs-reauth). Deliberately
    a sibling of AuthError, not a subclass, so `except AuthError` cannot swallow it
    and re-introduce the rate-limit-as-dead-token misclassification.
    """


class UsageError(Exception):
    """Usage endpoint returned an unexpected status."""


def _is_transient_status(status: int) -> bool:
    """True for statuses that mean 'back off and retry', not 'credential is dead'."""
    return status == 429 or 500 <= status <= 599


@dataclass
class Usage:
    session_pct: float
    session_resets_at: datetime | None
    weekly_pct: float
    weekly_resets_at: datetime | None
    extra_usage_enabled: bool


def http_request(method: str, url: str, headers: dict | None = None,
                 data: bytes | None = None, timeout: int = 15) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _parse_iso(s: str | None) -> datetime | None:
    # resets_at is null when a window has 0% usage (no active window to reset)
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_usage(payload: dict) -> Usage:
    session_pct = weekly_pct = None
    session_reset = weekly_reset = None
    for entry in payload.get("limits", []) or []:
        group = entry.get("group")
        resets_at = entry.get("resets_at")
        if not resets_at:
            continue
        if group == "session" and session_pct is None:
            session_pct = float(entry.get("percent", 0))
            session_reset = _parse_iso(resets_at)
        elif group == "weekly" and weekly_pct is None:
            weekly_pct = float(entry.get("percent", 0))
            weekly_reset = _parse_iso(resets_at)
    if session_pct is None:
        fh = payload.get("five_hour") or {}
        session_pct = float(fh.get("utilization", 0))
        session_reset = _parse_iso(fh.get("resets_at"))
    if weekly_pct is None:
        sd = payload.get("seven_day") or {}
        weekly_pct = float(sd.get("utilization", 0))
        weekly_reset = _parse_iso(sd.get("resets_at"))
    extra = bool((payload.get("extra_usage") or {}).get("is_enabled", False))
    return Usage(session_pct, session_reset, weekly_pct, weekly_reset, extra)


def fetch_usage(access_token: str) -> Usage:
    status, body = http_request(
        "GET", USAGE_URL,
        headers={"Authorization": f"Bearer {access_token}", "anthropic-beta": BETA_HEADER},
    )
    if status == 401:
        raise AuthError("usage endpoint returned 401")
    if status != 200:
        raise UsageError(f"usage endpoint returned {status}")
    return parse_usage(json.loads(body))


def refresh_access_token(refresh_token: str, prior: dict) -> dict:
    form = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    })
    status, body = http_request(
        "POST", TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=form.encode("utf-8"),
    )
    if _is_transient_status(status):
        # Rate-limit / server error: the refresh token is presumed valid. Surface
        # as transient so callers leave the account status untouched instead of
        # flipping it to needs-reauth.
        raise TransientError(f"token refresh rate-limited/unavailable: {status}")
    if status != 200:
        raise AuthError(f"token refresh failed with {status}")
    resp = json.loads(body)
    now_ms = int(time.time() * 1000)
    blob = dict(prior)  # carry subscriptionType / rateLimitTier
    blob["accessToken"] = resp["access_token"]
    blob["refreshToken"] = resp.get("refresh_token", refresh_token)
    blob["expiresAt"] = now_ms + int(resp.get("expires_in", 0)) * 1000
    if "scope" in resp:
        blob["scopes"] = resp["scope"].split()
    blob.setdefault("scopes", [])
    return blob
