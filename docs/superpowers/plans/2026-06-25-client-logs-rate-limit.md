# Per-identity Rate Limiting for /api/client-logs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reusable per-identity token-bucket rate limiter and apply it to `POST /api/client-logs`, returning `429` + `Retry-After` when a single identity floods the endpoint.

**Architecture:** A pure, framework-free `TokenBucketRateLimiter` (injectable clock, O(1) per check) in a new `server/api/ratelimit.py`, plus a thin FastAPI dependency that chains off `current_identity` and raises `HTTPException(429, headers={"Retry-After": ...})`. The dependency is attached to the `ingest` route via `dependencies=[...]`.

**Tech Stack:** Python 3.12, FastAPI, pytest. Issue #275.

## Global Constraints

- Rate policy: capacity **60** tokens, refill **1.0 token/sec** (60 req/min/identity, burst 60). Hardcoded module constants; the class still takes `(capacity, refill_per_sec)`.
- `429` response MUST include a `Retry-After` header in whole seconds, **≥ 1** (`max(1, math.ceil(retry))`).
- `check()` never raises and never `await`s (atomic under single-process asyncio — no lock).
- Clock is injectable (`now=time.monotonic` default) so tests drive time without `sleep`.
- The existing 413 batch cap (>100 entries) and 4000-char truncation are unchanged.
- Backend gates: `ruff` + `mypy` clean on touched files; tests via the prebuilt venv with `PYTHONPATH=<worktree>/src`. Run from the worktree against `/home/tony/GitHub/cc-account-switcher/.venv/bin/python`.
- Test env: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src <venv>/python -m pytest ...`. Only add test imports the test uses (CI runs ruff whole-tree).

---

### Task 1: `TokenBucketRateLimiter` + FastAPI dependency

**Files:**
- Create: `server/api/ratelimit.py`
- Test: `server/tests/test_ratelimit.py`

**Interfaces:**
- Produces:
  - `class TokenBucketRateLimiter` with `__init__(self, capacity: float, refill_per_sec: float, *, now: Callable[[], float] = time.monotonic, max_keys: int = 4096)` and `check(self, key: str) -> float | None` (returns `None` if allowed, else positive seconds until a token is free).
  - module constants `CLIENT_LOGS_CAPACITY = 60.0`, `CLIENT_LOGS_REFILL_PER_SEC = 1.0`
  - module singleton `_client_logs_limiter = TokenBucketRateLimiter(CLIENT_LOGS_CAPACITY, CLIENT_LOGS_REFILL_PER_SEC)`
  - `def client_logs_rate_limit(identity: str = Depends(current_identity)) -> None` — raises `HTTPException(429, detail="rate limit exceeded", headers={"Retry-After": str(max(1, math.ceil(retry)))})` when limited.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_ratelimit.py
import math

from server.api.ratelimit import TokenBucketRateLimiter


class _FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_allows_up_to_capacity_then_blocks():
    clock = _FakeClock()
    rl = TokenBucketRateLimiter(capacity=3, refill_per_sec=1.0, now=clock)
    assert rl.check("a") is None
    assert rl.check("a") is None
    assert rl.check("a") is None
    blocked = rl.check("a")
    assert blocked is not None and blocked > 0


def test_refills_over_time():
    clock = _FakeClock()
    rl = TokenBucketRateLimiter(capacity=1, refill_per_sec=1.0, now=clock)
    assert rl.check("a") is None          # consume the only token
    assert rl.check("a") is not None      # empty now
    clock.advance(1.0)                    # one token refilled
    assert rl.check("a") is None


def test_retry_after_reflects_deficit():
    clock = _FakeClock()
    rl = TokenBucketRateLimiter(capacity=1, refill_per_sec=0.5, now=clock)
    assert rl.check("a") is None
    retry = rl.check("a")
    # need 1 token at 0.5/sec => ~2s
    assert retry is not None and math.isclose(retry, 2.0, rel_tol=0.05)


def test_distinct_keys_are_independent():
    clock = _FakeClock()
    rl = TokenBucketRateLimiter(capacity=1, refill_per_sec=1.0, now=clock)
    assert rl.check("a") is None
    assert rl.check("b") is None          # b unaffected by a
    assert rl.check("a") is not None      # a still exhausted


def test_does_not_exceed_capacity_on_long_idle():
    clock = _FakeClock()
    rl = TokenBucketRateLimiter(capacity=2, refill_per_sec=1.0, now=clock)
    assert rl.check("a") is None          # consume 1 from an existing bucket -> 1 left
    clock.advance(1000.0)                 # would refill 1000 tokens; must clamp to capacity 2
    assert rl.check("a") is None
    assert rl.check("a") is None
    assert rl.check("a") is not None      # only 2 available after idle, not 1001


def test_prune_drops_idle_full_buckets_over_max_keys():
    clock = _FakeClock()
    rl = TokenBucketRateLimiter(capacity=1, refill_per_sec=1.0, now=clock, max_keys=2)
    rl.check("a")                         # a now empty (active)
    clock.advance(10.0)                   # a refills to full (idle)
    rl.check("b")                         # b active/empty
    rl.check("c")                         # exceeds max_keys=2 -> prune full/idle buckets (a)
    assert "a" not in rl._buckets
    # active buckets survive
    assert "b" in rl._buckets and "c" in rl._buckets
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ratelimit.py -q`
Expected: FAIL — `ModuleNotFoundError: server.api.ratelimit`.

- [ ] **Step 3: Create `server/api/ratelimit.py`**

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ratelimit.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Lint**

Run: `/home/tony/GitHub/cc-account-switcher/.venv/bin/ruff check server/api/ratelimit.py server/tests/test_ratelimit.py && /home/tony/GitHub/cc-account-switcher/.venv/bin/mypy server/api/ratelimit.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add server/api/ratelimit.py server/tests/test_ratelimit.py
git commit -m "feat(ratelimit): reusable per-identity token-bucket limiter + FastAPI dep (#275)"
```

---

### Task 2: Apply the limiter to `POST /api/client-logs`

**Files:**
- Modify: `server/api/client_logs.py:43` (route decorator)
- Test: `server/tests/test_client_logs_api.py` (extend)

**Interfaces:**
- Consumes: `client_logs_rate_limit` from `server.api.ratelimit` (Task 1), and for tests, `_client_logs_limiter` to reset/inspect state.

- [ ] **Step 1: Write the failing tests** (append to `server/tests/test_client_logs_api.py`)

```python
def test_rate_limit_returns_429_with_retry_after(client):
    # Drain the bucket: capacity is 60, so the 61st request in a burst is limited.
    from server.api.ratelimit import _client_logs_limiter
    _client_logs_limiter._buckets.clear()  # isolate from other tests
    body = {"entries": [{"level": "error", "msg": "x"}]}
    last = None
    for _ in range(61):
        last = client.post("/api/client-logs", json=body)
    assert last.status_code == 429
    assert int(last.headers["Retry-After"]) >= 1
    _client_logs_limiter._buckets.clear()  # leave clean state for other tests


def test_within_limit_still_accepted(client):
    from server.api.ratelimit import _client_logs_limiter
    _client_logs_limiter._buckets.clear()
    r = client.post("/api/client-logs", json={"entries": [{"level": "error", "msg": "x"}]})
    assert r.status_code == 202
    _client_logs_limiter._buckets.clear()
```

NOTE: the existing `client` fixture overrides `current_identity` to a single fixed identity
(`"tester@example.com"`), so all requests share one bucket — perfect for exhausting it. Clearing
`_client_logs_limiter._buckets` at the start/end isolates these tests from the happy-path tests
that also POST (which would otherwise spend tokens). If the existing happy-path tests begin to
flake from shared limiter state, add `_client_logs_limiter._buckets.clear()` in the `client`
fixture setup — check whether that's needed and do it if so.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_client_logs_api.py -q`
Expected: `test_rate_limit_returns_429_with_retry_after` FAILS (currently every request returns 202; no 429).

- [ ] **Step 3: Apply the dependency to the route** — edit `server/api/client_logs.py`.

Add the import (after line 12, beside the other `server.api`/auth imports):
```python
from server.api.ratelimit import client_logs_rate_limit
```
Change the route decorator (line 43):
```python
@router.post("", status_code=202, dependencies=[Depends(client_logs_rate_limit)])
```
(`Depends` is already imported in `client_logs.py`.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_client_logs_api.py -q`
Expected: PASS (all existing tests + 2 new). If a pre-existing happy-path test now flakes due to shared limiter state, add the fixture-level `_buckets.clear()` noted in Step 1 and re-run.

- [ ] **Step 5: Lint**

Run: `/home/tony/GitHub/cc-account-switcher/.venv/bin/ruff check server/api/client_logs.py server/tests/test_client_logs_api.py && /home/tony/GitHub/cc-account-switcher/.venv/bin/mypy server/api/client_logs.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add server/api/client_logs.py server/tests/test_client_logs_api.py
git commit -m "feat(client-logs): rate-limit POST /api/client-logs per identity (#275)"
```

---

## Final verification (before PR)

- [ ] `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src <venv>/python -m pytest server/tests/test_ratelimit.py server/tests/test_client_logs_api.py -q` — all green.
- [ ] `<venv>/ruff check server/api/ratelimit.py server/api/client_logs.py server/tests/test_ratelimit.py server/tests/test_client_logs_api.py` clean; `<venv>/mypy server/api/ratelimit.py server/api/client_logs.py` no new errors.
- [ ] PR body includes `Fixes #275`.
