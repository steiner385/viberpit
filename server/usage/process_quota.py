"""ProcessQuotaRefresher — read Copilot and Codex quota the way Claude quota is read: cold.

Anthropic usage is one REST call, so ``UsagePoller`` keeps it warm every sweep
whether or not a session is running. Copilot's premium-request quota is only
served by the copilot2api proxy's ``/usage``, and Codex's rate-limit buckets
only by a logged-in ``codex app-server``'s ``account/rateLimits/read`` — both
are processes the daemon owns, ref-counted by sessions and torn down by an idle
reaper. Left to that lifecycle, an account with no session open had no quota at
all: the accounts page showed "Usage unavailable" next to Claude accounts with
live bars, and the selector ranked it blind.

This refresher closes that gap with one policy for both providers:

- process running → read live, throttled by the ordinary REST cache TTL;
- process not running and the ledger reading older than
  ``process_quota_ttl_sec`` → start the process, read once, persist, and hand
  it straight back to the idle reaper (``release``), so a cold read costs one
  short-lived process per account per TTL and never keeps one alive;
- otherwise → serve the ledger.

It is the ONLY writer of these providers' ledger entries. ``ccswitch.accounts
.usage_for`` (CLI, selector probes, the poller) reads what it left and never
touches a process. Start failures share the ledger's usage-fetch cooldown with
the Anthropic path, so a missing binary or a dead authority is not respawned
every sweep. ``force=True`` (the manual refresh button) bypasses both the cache
and the cooldown.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any, Protocol

from ccswitch import account_registry, ledger
from ccswitch import copilot as _copilot
from ccswitch.accounts import list_accounts as _default_list_accounts
from ccswitch.config import load_config as _default_load_config
from ccswitch.quota import QuotaSnapshot

from server.aio import cancel_and_reap

log = logging.getLogger(__name__)


def persist_quota_snapshot(account: str, snapshot: QuotaSnapshot) -> None:
    """Persist one process-backed snapshot in the canonical ledger path.

    Blocking (file lock) — call through ``asyncio.to_thread`` from the loop.
    """
    with ledger.locked_ledger() as led:
        ledger.record_usage(led, account, snapshot, snapshot.fetched_at)


class QuotaSource(Protocol):
    """One provider's process-backed quota, behind its process manager."""

    provider: str

    def available(self) -> bool: ...
    def running(self, account: str) -> bool: ...
    async def start(self, account: str) -> None: ...
    async def read(self, account: str) -> QuotaSnapshot | None: ...
    def release(self, account: str) -> None: ...


class CopilotQuotaSource:
    """``/usage`` from the account's copilot2api proxy (``CopilotProxyManager``).

    The manager is resolved through a callable because app.py / ipc.serve install
    it on the orchestrator after construction.
    """

    provider = _copilot.PROVIDER

    def __init__(self, manager: Callable[[], Any]) -> None:
        self._manager = manager

    def available(self) -> bool:
        return self._manager() is not None

    def running(self, account: str) -> bool:
        mgr = self._manager()
        return mgr is not None and mgr.port_for(account) is not None

    async def start(self, account: str) -> None:
        await self._manager().ensure(account)

    async def read(self, account: str) -> QuotaSnapshot | None:
        # ``usage`` is a blocking urllib call; never run it on the loop.
        payload = await asyncio.to_thread(self._manager().usage, account)
        if payload is None:
            return None
        return _copilot.usage_from_proxy(payload, datetime.now(timezone.utc))

    def release(self, account: str) -> None:
        self._manager().release(account)


class CodexQuotaSource:
    """``account/rateLimits/read`` from the account's ``codex app-server``.

    ``CodexRunnerManager.ensure`` arms the idle reaper itself for a runner nobody
    attaches to, so ``release`` has nothing to do here.
    """

    provider = account_registry.PROVIDER_OPENAI

    def __init__(self, manager: Callable[[], Any], reader: Callable[[], Any]) -> None:
        self._manager = manager
        self._reader = reader

    def available(self) -> bool:
        mgr = self._manager()
        return mgr is not None and bool(mgr.available)

    def running(self, account: str) -> bool:
        mgr = self._manager()
        return mgr is not None and mgr.client_for(account) is not None

    async def start(self, account: str) -> None:
        await self._manager().ensure(account)

    async def read(self, account: str) -> QuotaSnapshot | None:
        client = self._manager().client_for(account)
        return await self._reader().read(account, client=client)

    def release(self, account: str) -> None:
        return None


class ProcessQuotaRefresher:
    """Keep process-backed quota fresh on the poller's cadence; see module doc.

    Args:
        sources: One ``QuotaSource`` per provider.
        sweep_interval: Seconds between background sweeps (the usage poller's).
    """

    def __init__(
        self,
        sources: Iterable[QuotaSource],
        *,
        sweep_interval: float = 60.0,
        jitter_frac: float = 0.1,
        rng: Callable[[], float] = random.random,
        list_accounts: Callable[[], list[dict]] = _default_list_accounts,
        load_config: Callable[[], Any] = _default_load_config,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._sources: dict[str, QuotaSource] = {s.provider: s for s in sources}
        self._sweep_interval = sweep_interval
        self._jitter_frac = jitter_frac
        self._rng = rng
        self._list_accounts = list_accounts
        self._load_config = load_config
        self._clock = clock
        # One in-flight refresh per account: the sweep, the accounts page and the
        # refresh button must not race three cold starts for one proxy.
        self._locks: dict[str, asyncio.Lock] = {}
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def source_for(self, account: str) -> QuotaSource | None:
        """The account's source, or None for an unregistered / REST-backed account."""
        try:
            provider = account_registry.provider_of(account)
        except account_registry.UnknownAccountError:
            return None
        return self._sources.get(provider)

    async def refresh(
        self, account: str, *, force: bool = False, persist: bool = True
    ) -> QuotaSnapshot | None:
        """Fresh-enough quota for *account*, starting its process only when needed.

        Returns None when the account is not process-backed, its provider is not
        available on this host, or nothing could be read; a degraded (non-
        available) snapshot is returned but never persisted, so a current outage
        is reported as such rather than masked by an older reading.
        """
        source = self.source_for(account)
        if source is None or not source.available():
            return None
        lock = self._locks.setdefault(account, asyncio.Lock())
        async with lock:
            return await self._refresh_locked(source, account, force=force, persist=persist)

    async def sweep(self) -> None:
        """Refresh every enabled process-backed account, each isolated."""
        names = [
            a["name"] for a in self._list_accounts()
            if a.get("enabled", True) and a.get("provider") in self._sources
        ]
        await asyncio.gather(*(self._refresh_quiet(n) for n in names))

    async def start(self) -> None:
        """Start the background sweep loop (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="process-quota-refresher")

    async def stop(self) -> None:
        if self._task is not None:
            await cancel_and_reap(self._task, phase="process-quota-refresher")
            self._task = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _refresh_locked(
        self, source: QuotaSource, account: str, *, force: bool, persist: bool
    ) -> QuotaSnapshot | None:
        cfg = self._load_config()
        running = source.running(account)
        if not force:
            # A live process is cheap to re-ask on the REST cadence; a cold one
            # costs a spawn, so its reading stays good for the process TTL.
            ttl = cfg.usage_cache_ttl_sec if running else cfg.process_quota_ttl_sec
            cached = await asyncio.to_thread(self._cached, account, float(ttl))
            if cached is not None:
                return cached
            if not running and await asyncio.to_thread(self._cooling_down, account):
                log.debug("process quota: %s in fetch-failure cooldown — skipping cold start",
                          account)
                return None
        started_cold = False
        if not running:
            try:
                await source.start(account)
            except Exception:  # noqa: BLE001 — one account must never fail the sweep
                log.warning("process quota: could not start the %s process for %s",
                            source.provider, account, exc_info=True)
                await asyncio.to_thread(self._note_failure, account)
                return None
            started_cold = True
        try:
            snapshot = await source.read(account)
        except Exception:  # noqa: BLE001 — see above
            log.warning("process quota: %s read failed for %s",
                        source.provider, account, exc_info=True)
            snapshot = None
        finally:
            if started_cold:
                source.release(account)
        if snapshot is None or not snapshot.is_available:
            await asyncio.to_thread(self._note_failure, account)
            return snapshot
        if persist:
            await asyncio.to_thread(persist_quota_snapshot, account, snapshot)
        return snapshot

    def _cached(self, account: str, ttl: float) -> QuotaSnapshot | None:
        with ledger.locked_ledger() as led:
            got = ledger.stale_usage(led, account, datetime.now(timezone.utc), max_age_sec=ttl)
        return None if got is None else got[0]

    def _cooling_down(self, account: str) -> bool:
        with ledger.locked_ledger() as led:
            return ledger.usage_fetch_cooldown_active(led, account, self._clock())

    def _note_failure(self, account: str) -> None:
        with ledger.locked_ledger() as led:
            ledger.note_usage_fetch_failure(led, account, self._clock())

    async def _refresh_quiet(self, account: str) -> None:
        try:
            await self.refresh(account)
        except Exception:  # noqa: BLE001 — a sweep must outlive any one account
            log.exception("process quota: refresh for %s raised", account)

    def _next_delay(self) -> float:
        return self._sweep_interval * (1.0 + self._jitter_frac * self._rng())

    async def _loop(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:  # pragma: no cover
                log.exception("process quota: error during sweep")
            await asyncio.sleep(self._next_delay())
