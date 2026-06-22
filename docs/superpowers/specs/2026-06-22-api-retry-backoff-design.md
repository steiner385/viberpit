# Auto-retry with backoff for Anthropic API requests

**Date:** 2026-06-22
**Status:** Approved (design) — extends PR #65

## Context

All outbound Anthropic API calls funnel through one function: `oauth.http_request`
(used by `fetch_usage` and `refresh_access_token`; the web server reaches the
network only through them). PR #65 (`fix(oauth,poller)`) already added retry with
exponential backoff + equal jitter and `Retry-After` honoring for transient HTTP
statuses (429 / 5xx), with injectable `sleep`/`rng` for tests and `max_retries=3`.

This change **extends** that single implementation (rather than adding a parallel
one — constitution Principle VI) to close the remaining gaps so retry is truly
"across the board."

## Gaps closed

1. **Network errors / timeouts were uncaught.** `http_request` caught `HTTPError`
   but let `urllib.error.URLError` / `TimeoutError` propagate as raw exceptions
   that crash callers. Now they are caught and retried with the same backoff. On
   exhaustion `http_request` raises `TransientError` (a network blip means the
   token is still valid → callers leave account status unchanged, never flip to
   needs-reauth). `accounts.usage_for` now also catches `TransientError` at its
   `fetch_usage` sites that previously only caught `UsageError`.

2. **No wall-clock cap.** `max_retries` alone doesn't bound latency: each attempt
   can burn the full per-attempt socket timeout (15s), so on a *stalling* endpoint
   `max_retries × timeout` could block ~45–75s. A new `max_total_sec` (default 15s)
   caps total wall-clock: before each retry, `http_request` checks the elapsed
   budget (via an injectable `clock`) and caps the sleep to the remaining budget;
   once spent it stops (returns the last status, or raises `TransientError`).
   Fast-failing endpoints still get all `max_retries`; a stalling one gives up at
   ~15s.

3. **Knobs not configurable.** Retry knobs were module constants. Added
   `Config.retry_max_retries` (3) and `Config.retry_max_total_sec` (15.0), tunable
   via `config.json`. `accounts` threads them through `fetch_usage` /
   `refresh_access_token` → `http_request`. A drift-guard test asserts the Config
   defaults equal `oauth._DEFAULT_MAX_RETRIES` / `_DEFAULT_MAX_TOTAL_SEC`.

## Idempotency

Retrying `refresh_access_token` is safe because we only retry 429 / 5xx / network
errors — failures that occur before the single-use refresh token is consumed
server-side. Terminal 200 / 401 / 400 are never retried, so a rotated-but-lost
token cannot be double-spent.

## Testing (TDD)

Extends `TestHttpRequestRetry`: network-error retried-then-success, timeout
retried, network-error exhaustion → `TransientError`, wall-clock budget stops
retries early (mocked `clock`), Config↔oauth default parity. Plus
`accounts.usage_for` treats a persistent `TransientError` from `fetch_usage` as
status-unchanged. `sleep`/`rng`/`clock` are injected so tests are instant and
deterministic.

## Out of scope (YAGNI)

Async/httpx retry (no separate async network path), circuit breakers, retrying
terminal auth, exposing backoff base/cap as Config (kept as internal constants).
