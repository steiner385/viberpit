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
import random
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
    ) -> None:
        self._poll_interval = poll_interval
        self._usage_fn = usage_fn if usage_fn is not None else _default_usage_fn
        # Spread sweeps by up to jitter_frac of the interval so multiple pollers /
        # account refreshes don't resync into a lockstep burst that self-triggers 429s.
        self._jitter_frac = jitter_frac
        self._rng = rng

        # Last-known usage per account
        self._cache: dict[str, Usage | None] = {}

        # Per-account lock so only one coroutine fetches a given account at a time
        self._fetch_locks: dict[str, asyncio.Lock] = {}

        # Subscriber queues — receive {"account": str, "usage": Usage | None}
        self._subscribers: list[asyncio.Queue[dict]] = []

        # Background task handle
        self._task: asyncio.Task[None] | None = None

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
            result = await loop.run_in_executor(None, self._usage_fn, account)
            old = self._cache.get(account, _SENTINEL)
            self._cache[account] = result
            if old is _SENTINEL or old != result:
                self._publish(account, result)
            return result

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

    async def start(self) -> None:
        """Start the background polling loop (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="usage-poller")

    async def stop(self) -> None:
        """Cancel the background loop and wait for it to finish."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _next_delay(self) -> float:
        """Poll interval plus up to ``jitter_frac`` extra — never less than the interval."""
        return self._poll_interval * (1.0 + self._jitter_frac * self._rng())

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
