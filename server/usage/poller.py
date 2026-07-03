"""UsagePoller — async wrapper around ccswitch.accounts.usage_for.

Responsibilities:
- Deduplicate concurrent per-account fetches (one in-flight request per account).
- Fan changes out to subscriber queues.
- Run a background loop on a configurable interval.

Tests inject a fake ``usage_fn`` so no real network calls are made.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from ccswitch.accounts import list_accounts
from ccswitch.accounts import usage_for as _default_usage_fn
from ccswitch.oauth import Usage

log = logging.getLogger(__name__)


class UsagePoller:
    """Poll per-account usage, deduplicate in-flight fetches, and publish changes.

    Args:
        poll_interval: Seconds between background sweeps (default 60).
        usage_fn: Callable ``(account: str) -> Usage | None``.  Defaults to
            ``ccswitch.accounts.usage_for``; replace in tests with a fake.
    """

    def __init__(
        self,
        poll_interval: float = 60.0,
        usage_fn: Callable[[str], Usage | None] | None = None,
        *,
        jitter_frac: float = 0.1,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
        backoff_base: float | None = None,
        backoff_cap: float = 900.0,
    ) -> None:
        self._poll_interval = poll_interval
        self._usage_fn = usage_fn if usage_fn is not None else _default_usage_fn
        # Spread sweeps by up to jitter_frac of the interval so multiple pollers /
        # account refreshes don't resync into a lockstep burst that self-triggers 429s.
        self._jitter_frac = jitter_frac
        self._rng = rng

        # Per-account adaptive backoff. When a fetch fails (returns None — a 429
        # throttle, a needs-reauth account, or a live-session token the SDK owns),
        # the account enters an exponentially-growing cooldown during which get()
        # serves the cached value instead of re-fetching. This caps the load a
        # single failing account can put on Anthropic's API from BOTH the sweep
        # loop and on-demand dashboard reads — and stops a sustained 429 storm
        # from blocking the account's own recovery to 'ok'.
        self._clock = clock
        self._backoff_base = backoff_base if backoff_base is not None else poll_interval
        self._backoff_cap = backoff_cap
        self._fail_count: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}

        # Last-known usage per account
        self._cache: dict[str, Usage | None] = {}

        # Per-account lock so only one coroutine fetches a given account at a time
        self._fetch_locks: dict[str, asyncio.Lock] = {}

        # Subscriber queues — receive {"account": str, "usage": Usage | None}
        self._subscribers: list[asyncio.Queue[dict]] = []

        # Background task handle
        self._task: asyncio.Task[None] | None = None

        # Dedicated, bounded thread pool for the blocking usage_fn. Isolating these
        # off the process-wide default executor means a storm of slow/hanging upstream
        # usage calls (e.g. Anthropic's usage endpoint 429ing and stalling) can saturate
        # ONLY this pool — it can no longer starve the shared executor that the daemon's
        # control path (DB writes, /healthz-adjacent work) depends on, which is how a
        # usage stall could wedge the whole daemon (incident 2026-07-02).
        self._executor_workers = int(os.environ.get("CCSWITCH_USAGE_POLLER_WORKERS", "4"))
        self._executor: ThreadPoolExecutor | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get(self, account: str) -> Usage | None:
        """Return usage for *account*, fetching only if no in-flight request exists.

        Concurrent callers for the same account share a single in-flight request:
        the first acquires the lock and runs the network call; subsequent callers
        that arrive while the lock is held wait and then read the cache the first
        caller wrote — avoiding N duplicate fetches for the same account.
        """
        # Backoff gate: a recently-failed account is served from cache (no network)
        # until its cooldown elapses, so one throttled account can't be re-hammered
        # on every sweep and every dashboard load.
        if self._clock() < self._cooldown_until.get(account, 0.0):
            return self._cache.get(account)

        lock = self._fetch_locks.setdefault(account, asyncio.Lock())

        # Fast path: if the lock is already held another coroutine is fetching;
        # wait for it and read the result it wrote rather than issuing a duplicate request.
        if lock.locked():
            async with lock:
                # By the time we hold the lock the first caller has already written
                # the cache; return whatever is there (may be None on auth failure).
                return self._cache.get(account)

        async with lock:
            loop = asyncio.get_running_loop()
            try:
                result = await loop.run_in_executor(
                    self._ensure_executor(), self._usage_fn, account)
            except Exception:
                # The usage_fn contract is to return None on failure, but a
                # malformed credential slot or other unexpected fault can still
                # raise. Contain it here: a single account must degrade to "no
                # usage" (None) and grow its backoff, never propagate out of the
                # sweep. all() aggregates via get(), so one bad account propagating
                # would 500 the whole /rpc/usage RPC — which the remote-mode edge
                # maps to a blanket 409 on every route. See test_usage.py.
                log.exception("UsagePoller: usage fetch for %s raised; treating as None", account)
                result = None
            self._note_outcome(account, result)
            old = self._cache.get(account, _SENTINEL)
            self._cache[account] = result
            if old is _SENTINEL or old != result:
                self._publish(account, result)
            return result

    def reset_backoff(self, account: str) -> None:
        """Clear an account's failure cooldown so the next get() fetches immediately.

        Call after an event that makes a previously-failing account healthy again
        (e.g. a successful re-auth) so fresh usage is fetched without waiting out
        the remaining cooldown.
        """
        self._fail_count.pop(account, None)
        self._cooldown_until.pop(account, None)

    def cached(self, account: str) -> Usage | None:
        """Return the last-known usage for *account* without fetching, or ``None``.

        A read-only accessor over the poller's cache — used by callers (e.g. the
        title generator's account-`limited` guard) that want the most recent
        usage without triggering a network request.
        """
        return self._cache.get(account)

    async def all(self) -> dict[str, Usage | None]:
        """Fetch usage for every registered account and return the full mapping."""
        names = [a["name"] for a in list_accounts()]
        results = await asyncio.gather(*(self.get(n) for n in names))
        return dict(zip(names, results))

    def subscribe(self) -> "asyncio.Queue[dict]":
        """Return a new queue that receives ``{"account": str, "usage": Usage|None}`` events."""
        q: asyncio.Queue[dict] = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[dict]") -> None:
        """Remove a previously-subscribed queue."""
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def _ensure_executor(self) -> ThreadPoolExecutor:
        """Return the dedicated usage executor, creating it on first use.

        Lazy + recreatable so get() works before start() and after a stop()/start()
        cycle (stop() tears the pool down to release its threads at teardown).
        """
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self._executor_workers, thread_name_prefix="usage-poller")
        return self._executor

    async def start(self) -> None:
        """Start the background polling loop (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="usage-poller")

    async def stop(self) -> None:
        """Cancel the background loop and tear down the dedicated executor.

        ``cancel_futures=True`` drops queued (not-yet-started) fetches; ``wait=False``
        means we don't block teardown on an in-flight blocking usage_fn hung on a slow
        socket — the point is that a stalled usage fetch must never delay shutdown.
        """
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _next_delay(self) -> float:
        """Poll interval plus up to ``jitter_frac`` extra — never less than the interval."""
        return self._poll_interval * (1.0 + self._jitter_frac * self._rng())

    def _backoff_delay(self, fail_count: int) -> float:
        """Cooldown for the *fail_count*-th consecutive failure: exponential, capped.

        ``base * 2**(fail_count-1)`` clamped to ``backoff_cap``, plus up to
        ``jitter_frac`` extra so multiple throttled accounts don't resync.
        """
        raw = self._backoff_base * (2 ** (max(fail_count, 1) - 1))
        return min(raw, self._backoff_cap) * (1.0 + self._jitter_frac * self._rng())

    def _note_outcome(self, account: str, result: Usage | None) -> None:
        """Update an account's backoff state from a fetch result.

        None (throttle / auth failure / SDK-owned token) grows the cooldown; any
        real usage clears it.
        """
        if result is None:
            n = self._fail_count.get(account, 0) + 1
            self._fail_count[account] = n
            self._cooldown_until[account] = self._clock() + self._backoff_delay(n)
        else:
            self._fail_count.pop(account, None)
            self._cooldown_until.pop(account, None)

    async def _loop(self) -> None:
        """Poll all accounts every ``_poll_interval`` seconds (plus jitter)."""
        while True:
            try:
                await self.all()
            except Exception:  # pragma: no cover
                log.exception("UsagePoller: error during sweep")
            await asyncio.sleep(self._next_delay())

    def _publish(self, account: str, usage: Usage | None) -> None:
        event = {"account": account, "usage": usage}
        for q in list(self._subscribers):
            q.put_nowait(event)


# Sentinel object to distinguish "not yet cached" from "cached as None"
_SENTINEL: Any = object()
