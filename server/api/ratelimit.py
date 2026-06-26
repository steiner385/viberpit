"""Reusable per-identity token-bucket rate limiting for the API layer.

A pure, framework-free limiter (TokenBucketRateLimiter) plus a thin FastAPI
dependency. First used by POST /api/client-logs (issue #275) to stop a single
tailnet client flooding the JSON log sink; reusable by other routers.
"""
from __future__ import annotations

import math
import time
from typing import Callable

from fastapi import Depends, HTTPException

from server.auth.middleware import current_identity

# Policy for the client-logs endpoint: 60 req/min/identity, burst 60 (~5x the
# normal client's ~12 req/min steady-state). Hardcoded; the class stays tunable.
CLIENT_LOGS_CAPACITY = 60.0
CLIENT_LOGS_REFILL_PER_SEC = 1.0


class TokenBucketRateLimiter:
    """Per-key token bucket. O(1) per check; one (tokens, last_refill) pair per key.

    check(key) consumes one token, lazily refilling by elapsed wall time (clamped
    to capacity). Returns None when allowed, else the positive number of seconds
    until a token frees up (for Retry-After). Never raises; never awaits, so under
    single-process asyncio it is atomic relative to other coroutines (no lock).
    """

    def __init__(
        self,
        capacity: float,
        refill_per_sec: float,
        *,
        now: Callable[[], float] = time.monotonic,
        max_keys: int = 4096,
    ) -> None:
        self._capacity = float(capacity)
        self._refill = float(refill_per_sec)
        self._now = now
        self._max_keys = max_keys
        self._buckets: dict[str, tuple[float, float]] = {}

    def check(self, key: str) -> float | None:
        now = self._now()
        tokens, last = self._buckets.get(key, (self._capacity, now))
        # Refill for elapsed time, clamped to capacity.
        tokens = min(self._capacity, tokens + (now - last) * self._refill)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            result: float | None = None
        else:
            # Exhausted: store the (refilled) state and report time to one token.
            self._buckets[key] = (tokens, now)
            result = (1.0 - tokens) / self._refill if self._refill > 0 else float("inf")
        # Bound memory regardless of outcome (cheap: only scans when over the cap).
        if len(self._buckets) > self._max_keys:
            self._prune(now)
        return result

    def _prune(self, now: float) -> None:
        """Drop idle buckets (refilled back to full capacity) to bound memory.

        Only full buckets are removed — they are indistinguishable from a fresh
        bucket, so dropping them changes nothing for the owner. Active (partially
        drained) buckets are kept.
        """
        for k in [
            k for k, (tok, last) in self._buckets.items()
            if min(self._capacity, tok + (now - last) * self._refill) >= self._capacity
        ]:
            del self._buckets[k]


_client_logs_limiter = TokenBucketRateLimiter(
    CLIENT_LOGS_CAPACITY, CLIENT_LOGS_REFILL_PER_SEC
)


def client_logs_rate_limit(identity: str = Depends(current_identity)) -> None:
    """FastAPI dependency: 429 + Retry-After when this identity exceeds the limit."""
    retry = _client_logs_limiter.check(identity)
    if retry is not None:
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={"Retry-After": str(max(1, math.ceil(retry)))},
        )
