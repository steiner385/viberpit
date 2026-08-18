"""Shared GitHub-API priority gate.

``PrStatusPoller`` (PR-status refresh) and ``PrPopulationPoller`` (ETA
enrichment) draw from the SAME GitHub App rate-limit budget. PR-status is
user-facing and higher priority; ETA enrichment is best-effort background
data. Under a rate-limit squeeze the two starve each other, and — because ETA
is fetched as one batched query for every open PR — a single throttled call
wipes ETA off the whole board at once (issue #671: 32 ``eta_fetch_failed``).

This gate lets the low-priority ETA path YIELD to in-flight PR-status
refreshes: pr_poller wraps its network calls in ``high_priority()``, and the
ETA path awaits ``wait_until_idle()`` (bounded) before spending budget. It is
NOT a rate limiter or a lock — high-priority work never blocks, and the
low-priority waiter always proceeds once the (bounded) timeout elapses, so a
stuck refresh can never permanently starve ETA.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import AsyncIterator


class PriorityGate:
    """Cooperative two-tier gate: high-priority holders make ``busy`` true;
    low-priority callers can wait for it to clear.

    The gate is deliberately loop-agnostic at construction (``asyncio.Event``
    binds to the running loop lazily on first await), so a single instance may
    be created outside an event loop and shared by pollers.
    """

    def __init__(self) -> None:
        self._high = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def busy(self) -> bool:
        """True while at least one high-priority section is in flight."""
        return self._high > 0

    @contextlib.asynccontextmanager
    async def high_priority(self) -> AsyncIterator[None]:
        """Mark a high-priority (PR-status) section. Never blocks; increments the
        in-flight count so concurrent low-priority waiters yield."""
        self._high += 1
        self._idle.clear()
        try:
            yield
        finally:
            self._high -= 1
            if self._high <= 0:
                self._high = 0
                self._idle.set()

    async def wait_until_idle(self, timeout: float | None = None) -> bool:
        """Block until no high-priority section is in flight, or ``timeout``
        seconds elapse. Returns True if the gate went idle, False on timeout.

        A ``timeout`` bound is what keeps this a *yield*, not a lock: the ETA
        path defers to PR-status refreshes but always proceeds eventually, so a
        wedged refresh cannot permanently suppress ETA enrichment.
        """
        if self._idle.is_set():
            return True
        try:
            if timeout is None:
                await self._idle.wait()
            else:
                await asyncio.wait_for(self._idle.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False
