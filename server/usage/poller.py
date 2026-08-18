"""UsagePoller — async wrapper around ccswitch.accounts.usage_for.

Responsibilities:
- Deduplicate concurrent per-account fetches (one in-flight request per account).
- Fan changes out to subscriber queues.
- Run a background loop on a configurable interval.
- Count consecutive failures per account (rotation-monitor input).

NOT a responsibility: rate-limit backoff. That is owned by usage_for's ledger
cooldown (usageFetchFails/usageFetchCooldownUntil), shared by every caller in
every process — get() always delegates, so a recovery that lands in the ledger
via any path (re-auth, CLI, selection probe) is visible immediately.

Tests inject a fake ``usage_fn`` so no real network calls are made.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
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
    ) -> None:
        self._poll_interval = poll_interval
        self._usage_fn = usage_fn if usage_fn is not None else _default_usage_fn
        # Spread sweeps by up to jitter_frac of the interval so multiple pollers /
        # account refreshes don't resync into a lockstep burst that self-triggers 429s.
        self._jitter_frac = jitter_frac
        self._rng = rng

        # Consecutive-failure counter per account, read by the orchestrator's
        # rotation monitor to spot persistently dead accounts. The fetch-failure
        # BACKOFF itself lives in usage_for's ledger cooldown (shared by every
        # caller in every process) — the poller must NOT gate fetches with its
        # own in-memory cooldown on top: doing so masked recoveries, serving a
        # stale cached None after a re-auth had already refreshed the ledger
        # (kindash, 2026-07-09).
        self._fail_count: dict[str, int] = {}

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
                # usage" (None), never propagate out of the
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

    def cached(self, account: str) -> Usage | None:
        """Return the last-known usage for *account* without fetching, or ``None``.

        A read-only accessor over the poller's cache — used by callers (e.g. the
        title generator's account-`limited` guard) that want the most recent
        usage without triggering a network request.
        """
        return self._cache.get(account)

    def consecutive_failures(self, account: str) -> int:
        """Return the number of consecutive fetch failures for *account*.

        A non-zero value means the last N polls all returned None (network
        error, auth failure, dead Copilot proxy, etc.).  The orchestrator uses
        this to distinguish a transient hiccup from a persistently unreachable
        account that warrants rotating sessions away.
        """
        return self._fail_count.get(account, 0)

    async def all(self) -> dict[str, Usage | None]:
        """Fetch usage for every registered account and return the full mapping."""
        names = [a["name"] for a in list_accounts() if a.get("enabled", True)]
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

    def _note_outcome(self, account: str, result: Usage | None) -> None:
        """Track consecutive failures per account (rotation-monitor input only).

        The fetch-rate backoff itself is usage_for's ledger cooldown; nothing
        here may gate future fetches.
        """
        if result is None:
            self._fail_count[account] = self._fail_count.get(account, 0) + 1
        else:
            self._fail_count.pop(account, None)

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
