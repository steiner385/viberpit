# Per-identity rate limiting for `/api/client-logs` — design

**Date:** 2026-06-25
**Issue:** #275
**Status:** Approved (brainstorming → spec)

## Problem

`POST /api/client-logs` enforces batch-size (≤100 entries → 413) and per-message length
(4000 chars) caps, but **not** a per-identity rate limit. The comprehensive-error-logging
design (`2026-06-24-comprehensive-error-logging-design.md` §4) specified per-identity rate
limiting so a buggy/malicious tailnet client can't flood the JSON log sink. No rate-limiting
infrastructure exists in the API layer yet, so it was deferred to this issue.

## Decisions (from brainstorming)

- **Algorithm:** token bucket — O(1), one `(tokens, last_refill)` pair per identity, allows
  bursts. Cheaper and more standard than a sliding-window log.
- **Rate:** capacity **60**, refill **1 token/sec** → 60 requests/min sustained per identity,
  burst up to 60. ~5× the normal client's ~12 req/min steady-state (200-entry ring, ~5s flush),
  so a legitimate client (even reconnecting) is never throttled; a genuine flood is.
- **Config:** hardcoded module-level constants. The limiter **class** still takes
  `(capacity, refill_per_sec)` so it is reusable and tunable per-endpoint and in tests, but no
  Settings/env wiring (YAGNI for a v1 internal endpoint).
- **Response:** `429 Too Many Requests` with a `Retry-After` header = whole seconds until the
  next token is available (≥1).
- **Placement:** new `server/api/ratelimit.py` (API-layer HTTP concern — produces 429s and a
  FastAPI dependency). Reusable by other routers.

## Architecture

### `server/api/ratelimit.py`

A pure, framework-free limiter plus a thin FastAPI adapter.

```
class TokenBucketRateLimiter:
    def __init__(self, capacity: float, refill_per_sec: float, *, now=time.monotonic): ...
    def check(self, key: str) -> float | None:
        """Consume one token for `key`. Return None if allowed; else the number of
        seconds (>0) until a token frees up (for Retry-After). Never raises."""
```

- State: `self._buckets: dict[str, tuple[float, float]]` = `key -> (tokens, last_refill)`.
- On `check(key)`: lazily refill based on elapsed `now() - last_refill` (clamped to capacity),
  then if `tokens >= 1` consume one and return `None`, else return
  `ceil_seconds_until_one_token = (1 - tokens) / refill_per_sec`.
- Injectable `now` (monotonic by default) so tests drive time deterministically without sleeps.
- Memory: one entry per distinct identity. Bounded by the tailnet (small). A light prune keeps
  it safe under churn: when the map exceeds `_MAX_KEYS` (e.g. 4096), drop entries that have
  refilled back to full capacity (idle). This is a guard, not a hot path.

FastAPI adapter (module-level, applied to the endpoint):

```
_client_logs_limiter = TokenBucketRateLimiter(capacity=60, refill_per_sec=1.0)

def client_logs_rate_limit(identity: str = Depends(current_identity)) -> None:
    retry = _client_logs_limiter.check(identity)
    if retry is not None:
        raise HTTPException(
            status_code=429, detail="rate limit exceeded",
            headers={"Retry-After": str(max(1, math.ceil(retry)))},
        )
```

Chaining off `current_identity` keeps auth-first ordering and gives the per-identity key.

### `server/api/client_logs.py`

Apply the dependency to the route so it runs before the body is processed:

```
@router.post("", status_code=202, dependencies=[Depends(client_logs_rate_limit)])
async def ingest(batch: ClientLogBatch, identity: str = Depends(current_identity)) -> dict:
    ...
```

The existing 413 batch cap and 4000-char truncation are unchanged.

## Concurrency

Single-process asyncio. `check()` performs no `await`, so it runs atomically relative to other
coroutines — concurrent requests for the same identity are serialized by the event loop and
cannot double-spend a token. No lock required. (If the app ever goes multi-process, this becomes
per-process — acceptable for a flood guard; documented as a known limit.)

## Error handling

`check()` never raises. The adapter raises only the intended `HTTPException(429, …)`. A malformed
clock or missing bucket simply creates a fresh full bucket.

## Testing

**`server/tests/test_ratelimit.py`** (pure limiter, injected fake clock):
- Allows up to `capacity` consecutive calls, then blocks (returns a positive retry).
- Returns `None` while tokens remain; a positive float when exhausted.
- Refills over time: after advancing the fake clock by `1/refill_per_sec`, one more call is allowed.
- `Retry-After` math: when exhausted, retry ≈ `(1 - tokens)/refill_per_sec`, and the dependency
  rounds up to ≥1 second.
- Distinct keys have independent buckets.
- Prune: exceeding `_MAX_KEYS` drops idle (full) buckets without affecting active ones.

**`server/tests/test_client_logs_api.py`** (extend existing):
- Exhausting the limit returns `429` with a `Retry-After` header (integer ≥1).
- Two different identities are limited independently (override `current_identity` per request, or
  drive the limiter directly) — a flood from one doesn't 429 the other.
- A request within the limit still returns `202` (existing happy-path unbroken).

Gates: `pytest server/tests/test_ratelimit.py server/tests/test_client_logs_api.py`, `ruff`, `mypy`.

## Out of scope / deferred

- Settings/env configurability (YAGNI; class is already parameterized).
- Distributed/multi-process limiting (single daemon today).
- Applying the limiter to other endpoints (it's reusable; no current need — do it when one arises).
- A global (all-identities) limit — per-identity is what the issue asks for.
