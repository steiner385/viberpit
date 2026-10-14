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

The loop is demand-driven (#831): it rebuilds only while a reader has asked for a
frame within ``_IDLE_AFTER_S``. Only the Pipeline page reads it, yet it used to
rebuild every 10 s for the edge's whole lifetime -- five daemon RPCs per build
(``all_prs``, ``all_merge_trains``, ``latest_queue_entries``, ``deploy_config_list``,
``all_repos_stage_models``), ~30 RPC/min with nobody looking. A frame older than
that window is reported as absent, so the endpoints' cold-start path rebuilds it
inline rather than serving stale data.

Lifecycle mirrors ``PrPopulationPoller`` / ``DeployWatcher`` (start/stop/run).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Awaitable, Callable

from server.aio import cancel_and_reap
from server.ipc.client import DaemonUnavailable
from server.obs.logdedupe import LogThrottle, log_throttled

log = logging.getLogger(__name__)

# Match the historic SSE cadence so per-tab freshness is unchanged.
_POLL_INTERVAL_S = 10.0

# No reader for this long -> the loop parks until the next one. The SSE feed reads
# at least every _EVENTS_INTERVAL_S (10 s) per connection, so an open page never
# lapses; this also bounds how old a served frame can be after a lapse.
_IDLE_AFTER_S = 60.0

Builder = Callable[[], Awaitable[dict]]

# A session-daemon restart/deploy window makes every build in it hit
# DaemonUnavailable — an expected transient, not a pipeline bug. Logged via
# log.exception it filed #1656 (hundreds of ERROR records, each with a full httpx
# traceback). One key: the poller is a singleton and the condition is host-wide.
# Only the exception *class* is logged: raw httpx text varies per failure shape
# and would split one condition into several auto-error fingerprints.
#
# #2361: the throttled WARNING is itself the auto-error filing decision (WARNING+
# is what server.obs.errsink files), so the *first* occurrence of every restart
# window still filed one issue. A restart/deploy window is a few ticks and is not
# worth reporting at all — demoted to INFO. A genuine outage is still worth one
# WARNING, so consecutive failures are tracked and escalate exactly once past the
# threshold below, reset on the next successful publish.
_DAEMON_UNAVAILABLE_LOG = LogThrottle()

# 30 consecutive failed 10 s ticks ~= 5 minutes: long enough that a normal daemon
# restart/deploy window (observed well under a minute) never trips it, short
# enough that a genuine outage still reports promptly.
_DAEMON_UNAVAILABLE_ESCALATE_AFTER = 30


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
        idle_after_s: float = _IDLE_AFTER_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._builder = builder
        self._interval_s = interval_s
        self._idle_after_s = idle_after_s
        self._clock = clock
        self._state: dict | None = None
        self._frame_at: float | None = None
        self._last_demand_at: float | None = None
        self._demand = asyncio.Event()
        self._generation = 0
        self._task: asyncio.Task | None = None
        self._cond = asyncio.Condition()
        # Consecutive DaemonUnavailable failures and whether this outage already
        # escalated to a WARNING — both reset the moment a build succeeds.
        self._daemon_failure_streak = 0
        self._daemon_escalated = False

    # ------------------------------------------------------------------ #
    # Reads                                                               #
    # ------------------------------------------------------------------ #

    @property
    def generation(self) -> int:
        """Monotonic counter, bumped once per published frame (0 = none yet)."""
        return self._generation

    def note_demand(self) -> None:
        """Record that a reader wants frames; wakes a parked loop."""
        self._last_demand_at = self._clock()
        self._demand.set()

    def _wanted(self) -> bool:
        return (self._last_demand_at is not None
                and self._clock() - self._last_demand_at <= self._idle_after_s)

    def _frame_is_stale(self) -> bool:
        return self._frame_at is None or self._clock() - self._frame_at > self._idle_after_s

    def snapshot(self) -> dict | None:
        """Latest cached frame, or ``None`` before the first successful build or once
        the frame is older than the idle window (the loop parked; rebuild inline)."""
        self.note_demand()
        return None if self._frame_is_stale() else self._state

    async def wait_for_frame(
        self, after_generation: int, timeout: float
    ) -> tuple[dict | None, int]:
        """Block until a frame newer than ``after_generation`` is published.

        Returns ``(state, generation)``. On timeout it returns the current frame
        (possibly unchanged) so callers can emit a heartbeat rather than hang —
        this preserves the ~10 s cadence the frontend's staleness watchdog expects.
        """
        self.note_demand()
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
        except DaemonUnavailable as exc:
            self._daemon_failure_streak += 1
            if (self._daemon_failure_streak >= _DAEMON_UNAVAILABLE_ESCALATE_AFTER
                    and not self._daemon_escalated):
                # Fires once per outage, not once per tick: a persistent outage is
                # a genuine signal and is worth its own auto-error fingerprint.
                self._daemon_escalated = True
                log.warning(
                    "pipeline state build still skipped after %d consecutive polls: "
                    "session daemon unavailable (%s)",
                    self._daemon_failure_streak, type(exc).__name__)
            log_throttled(log, _DAEMON_UNAVAILABLE_LOG, "daemon", logging.INFO,
                          "pipeline state build skipped: session daemon unavailable "
                          "(%s)", type(exc).__name__)  # carry-forward last frame
            return
        except Exception:
            log.exception("pipeline state build failed")  # carry-forward last frame
            return
        self._daemon_failure_streak = 0
        self._daemon_escalated = False
        await self._publish(state)

    async def _publish(self, state: dict) -> None:
        async with self._cond:
            self._state = state
            self._frame_at = self._clock()
            self._generation += 1
            self._cond.notify_all()

    # ------------------------------------------------------------------ #
    # Lifecycle (mirrors PrPopulationPoller / DeployWatcher)              #
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        while True:
            if not self._wanted():
                self._demand.clear()
                await self._demand.wait()
                if self._frame_is_stale():
                    # The waking reader found no usable frame and is building one
                    # inline (cold path); don't race it with a second build.
                    await asyncio.sleep(self._interval_s)
                continue
            await self.poll_once()
            await asyncio.sleep(self._interval_s)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="pipeline-state-poller")

    async def stop(self) -> None:
        if self._task is not None:
            await cancel_and_reap(self._task, phase="pipeline-state-poller")
            self._task = None
