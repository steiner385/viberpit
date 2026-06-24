"""Anthropic OAuth: exact usage + independent token refresh (stdlib urllib)."""
import importlib.metadata
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

try:
    _VERSION = importlib.metadata.version("ccswitch")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover — running from a non-installed tree
    _VERSION = "0.0.0+unknown"

# Identify ourselves on every outbound request. The token endpoint
# (platform.claude.com) sits behind a Cloudflare bot rule that returns 403
# `error code: 1010` to the default `Python-urllib/x.y` UA, so a header-less
# refresh is blocked at the edge before Anthropic sees it — and that 403 was
# being misread as a dead grant, mass-flipping every account to needs-reauth.
USER_AGENT = f"ccswitch/{_VERSION}"

# Backoff bounds for transient (429 / 5xx / network) retries.
_BACKOFF_BASE = 0.5   # seconds — first retry waits up to this (with jitter)
_BACKOFF_CAP = 30.0   # seconds — never wait longer than this between retries
_RETRY_AFTER_CAP = 120.0  # ignore absurd server-supplied Retry-After values
# Defaults for the retry budget. Mirrored by Config (retry_max_retries /
# retry_max_total_sec); a drift-guard test keeps the two in sync.
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_MAX_TOTAL_SEC = 15.0  # wall-clock ceiling across all attempts

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
    """True for statuses that mean 'back off and retry', not 'credential is dead'.

    403 is included because the OAuth endpoints sit behind Cloudflare, which returns
    it (error 1010) when it blocks a request at the edge — the token never reached
    Anthropic, so an intermittent block is worth retrying. A genuinely dead grant is
    400 (invalid_grant) / 401, which stay terminal. refresh_access_token still treats
    a 403 that survives retries as TransientError (only 400/401 flip needs-reauth).
    """
    return status in (403, 429) or 500 <= status <= 599


@dataclass
class Usage:
    session_pct: float
    session_resets_at: datetime | None
    weekly_pct: float
    weekly_resets_at: datetime | None
    extra_usage_enabled: bool

    def to_dict(self) -> dict:
        return {
            "sessionPct": self.session_pct,
            "sessionResetsAt": self.session_resets_at.isoformat() if self.session_resets_at else None,
            "weeklyPct": self.weekly_pct,
            "weeklyResetsAt": self.weekly_resets_at.isoformat() if self.weekly_resets_at else None,
            "extraUsageEnabled": self.extra_usage_enabled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Usage":
        from datetime import datetime
        def _p(s):
            return datetime.fromisoformat(s) if s else None
        return cls(
            session_pct=d["sessionPct"],
            session_resets_at=_p(d["sessionResetsAt"]),
            weekly_pct=d["weeklyPct"],
            weekly_resets_at=_p(d["weeklyResetsAt"]),
            extra_usage_enabled=d["extraUsageEnabled"],
        )


def _retry_delay(http_headers, attempt: int, rng=random.random) -> float:
    """Seconds to wait before retry *attempt* (0-based).

    Honors a sane integer ``Retry-After`` header if present; otherwise uses
    exponential backoff with equal jitter so concurrent callers don't retry in
    lockstep and re-trigger the same rate limit.
    """
    ra = http_headers.get("Retry-After") if http_headers else None
    if ra is not None:
        try:
            secs = float(int(ra))
            if 0 <= secs <= _RETRY_AFTER_CAP:
                return secs
        except (TypeError, ValueError):
            pass  # HTTP-date form unsupported — fall through to computed backoff
    base = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** attempt))
    return base * (0.5 + 0.5 * rng())  # equal jitter: [base/2, base)


def http_request(method: str, url: str, headers: dict | None = None,
                 data: bytes | None = None, timeout: int = 15, *,
                 max_retries: int = _DEFAULT_MAX_RETRIES,
                 max_total_sec: float = _DEFAULT_MAX_TOTAL_SEC,
                 sleep=time.sleep, rng=random.random, clock=time.monotonic) -> tuple[int, bytes]:
    """Issue an HTTP request, retrying transient failures with backoff.

    Retries transient HTTP statuses (403 / 429 / 5xx) AND network errors (URLError /
    timeout). Retries are safe for our callers: such failures happen before the
    (single-use) refresh token is consumed server-side. Terminal statuses (4xx
    other than 403/429 — notably 400/401) and 2xx return immediately; a persistent
    network failure raises TransientError so callers leave the account status unchanged.

    ``max_total_sec`` bounds total wall-clock across all attempts so a stalling
    endpoint can't turn max_retries x socket-timeout into minutes of blocking.
    ``sleep`` / ``rng`` / ``clock`` are injectable for deterministic tests.
    """
    start = clock()
    # Single choke point for all outbound HTTP — stamp a real User-Agent here so
    # refresh, usage and any future endpoint clear the Cloudflare bot rule. A
    # caller-supplied UA wins (setdefault), e.g. for tests.
    hdrs = dict(headers or {})
    hdrs.setdefault("User-Agent", USER_AGENT)

    def _retry_sleep(headers, attempt: int) -> bool:
        # Sleep before the next attempt if the wall-clock budget allows. Returns
        # True if a retry should proceed, False if the budget is spent.
        remaining = max_total_sec - (clock() - start)
        if remaining <= 0:
            return False
        sleep(min(_retry_delay(headers, attempt, rng), remaining))
        return True

    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, method=method, headers=hdrs, data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read()
            if attempt < max_retries and _is_transient_status(status) and _retry_sleep(e.headers, attempt):
                continue
            return status, body
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < max_retries and _retry_sleep(None, attempt):
                continue
            raise TransientError(
                f"network error after {attempt + 1} attempt(s): {e}") from e


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


def fetch_usage(access_token: str, *, max_retries: int = _DEFAULT_MAX_RETRIES,
                max_total_sec: float = _DEFAULT_MAX_TOTAL_SEC) -> Usage:
    status, body = http_request(
        "GET", USAGE_URL,
        headers={"Authorization": f"Bearer {access_token}", "anthropic-beta": BETA_HEADER},
        max_retries=max_retries, max_total_sec=max_total_sec,
    )
    if status == 401:
        raise AuthError("usage endpoint returned 401")
    if status != 200:
        raise UsageError(f"usage endpoint returned {status}")
    return parse_usage(json.loads(body))


def refresh_access_token(refresh_token: str, prior: dict, *,
                         max_retries: int = _DEFAULT_MAX_RETRIES,
                         max_total_sec: float = _DEFAULT_MAX_TOTAL_SEC) -> dict:
    form = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    })
    status, body = http_request(
        "POST", TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=form.encode("utf-8"),
        max_retries=max_retries, max_total_sec=max_total_sec,
    )
    if _is_transient_status(status):
        # Rate-limit / server error: the refresh token is presumed valid. Surface
        # as transient so callers leave the account status untouched instead of
        # flipping it to needs-reauth.
        raise TransientError(f"token refresh rate-limited/unavailable: {status}")
    if status in (400, 401):
        # The ONLY statuses that prove the grant itself is rejected (400
        # invalid_grant / 401 unauthorized). These — and only these — flip the
        # account to needs-reauth.
        raise AuthError(f"token refresh failed with {status}")
    if status != 200:
        # Any other non-2xx (notably 403 — a Cloudflare edge block, `error code:
        # 1010`) is NOT proof the grant is dead. It's environmental and hits every
        # account identically, so treating it as a dead token mass-flips all
        # accounts to needs-reauth in one sweep. Surface as transient → status
        # left unchanged. (Sibling of the 429/5xx carve-out above.)
        raise TransientError(f"token refresh got unexpected status {status} (treated as transient)")
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
