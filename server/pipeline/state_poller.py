"""Background populator for the pipeline dashboard frame.

The dashboard frame (``server.api.pipeline._build_state``) is expensive: it
gathers PR population, merge trains, the merge queue, per-repo deploy blocks,
workflow-impact cards, and a 7-day ETA simulation. The SSE feed used to rebuild
it *inside every connection's generator loop*, so N open browser tabs meant N
full rebuilds every cycle.

``PipelineStatePoller`` computes the frame **once** per interval into a shared
in-memory cache. The ``/api/pipeline/events`` fan-out and ``/api/pipeline/snapshot``
read that cache instead of recomputing, so build cost is constant regardless of
how many tabs are connected, and a freshly opened tab paints from a warm frame.

Lifecycle mirrors ``PrPopulationPoller`` / ``DeployWatcher`` (start/stop/run).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Awaitable, Callable

from server.aio import cancel_and_reap

log = logging.getLogger(__name__)

# Match the historic SSE cadence so per-tab freshness is unchanged.
_POLL_INTERVAL_S = 10.0

Builder = Callable[[], Awaitable[dict]]


class PipelineStatePoller:
    """Computes the dashboard frame on an interval into a shared cache.

    ``builder`` is an async, no-arg callable returning one frame dict — inject
    ``lambda: _build_state(orch)`` so the single source of truth stays in
    ``server.api.pipeline`` and test-time monkeypatching of ``_build_state`` is
    honoured (resolve it at call time, don't capture the function object).
    """

    def __init__(
        self,
        builder: Builder,
        *,
        interval_s: float = _POLL_INTERVAL_S,
    ) -> None:
        self._builder = builder
        self._interval_s = interval_s
        self._state: dict | None = None
        self._generation = 0
        self._task: asyncio.Task | None = None
        self._cond = asyncio.Condition()

    # ------------------------------------------------------------------ #
    # Reads                                                               #
    # ------------------------------------------------------------------ #

    @property
    def generation(self) -> int:
        """Monotonic counter, bumped once per published frame (0 = none yet)."""
        return self._generation

    def snapshot(self) -> dict | None:
        """Latest cached frame, or ``None`` before the first successful build."""
        return self._state

    async def wait_for_frame(
        self, after_generation: int, timeout: float
    ) -> tuple[dict | None, int]:
        """Block until a frame newer than ``after_generation`` is published.

        Returns ``(state, generation)``. On timeout it returns the current frame
        (possibly unchanged) so callers can emit a heartbeat rather than hang —
        this preserves the ~10 s cadence the frontend's staleness watchdog expects.
        """
        async with self._cond:
            if self._generation <= after_generation:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self._cond.wait_for(
                            lambda: self._generation > after_generation
                        ),
                        timeout,
                    )
            return self._state, self._generation

    # ------------------------------------------------------------------ #
    # Population                                                          #
    # ------------------------------------------------------------------ #

    async def build_now(self) -> dict:
        """Build one frame immediately, publish it, and return it.

        Cold-start fallback for the first reader after boot, before the loop has
        produced anything. May raise if the builder raises — callers decide how
        to degrade.
        """
        state = await self._builder()
        await self._publish(state)
        return state

    async def poll_once(self) -> None:
        """One build → publish. Never raises (the loop must not die)."""
        try:
            state = await self._builder()
        except Exception:
            log.exception("pipeline state build failed")  # carry-forward last frame
            return
        await self._publish(state)

    async def _publish(self, state: dict) -> None:
        async with self._cond:
            self._state = state
            self._generation += 1
            self._cond.notify_all()

    # ------------------------------------------------------------------ #
    # Lifecycle (mirrors PrPopulationPoller / DeployWatcher)              #
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self._interval_s)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="pipeline-state-poller")

    async def stop(self) -> None:
        if self._task is not None:
            await cancel_and_reap(self._task, phase="pipeline-state-poller")
            self._task = None
