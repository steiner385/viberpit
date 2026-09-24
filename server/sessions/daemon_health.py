"""Issue #434: daemon lifecycle health for the dashboard.

The session daemon runs behind a watchdog (``scripts/ccswitch-health-check.sh``) that
restarts it on a hard wedge. Before this, a wedge → restart → orphaned-turn was invisible in
the UI and diagnosable only by log spelunking (`journalctl` for ``ALERT[daemon-wedged]``,
correlating PIDs, reading ``last_turn_at`` out of the DB). This module makes those restarts
visible by tracking them from the long-lived *edge* process (which outlives the daemon).

Two facts drive it:

- The daemon's epoch (from ``/info``) is ``<start_ms_hex>-<pid>-<uuid8>`` (see
  ``server.obs.context.PROC_EPOCH`` + ``server.ipc.daemon.create_daemon_app``). The first
  hex segment is the process start time in ms, so daemon uptime/start is derivable from the
  current epoch alone — no extra bookkeeping.
- A *change* in epoch between polls is a restart. Restart timestamps are kept in a bounded
  window (default 24h) so ``restartCount24h`` answers "is this daemon flapping?" (the 2026-07-02
  incident restarted 9× in a day). In-memory only: the edge outlives the daemon, and a deploy of
  the edge itself is a reasonable reset boundary for the count. ``lastRestartAt`` additionally
  falls back to the watchdog's ``$STATE_DIR/last-restart`` file, which survives an edge restart.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from server.aio import cancel_and_reap
# Mirror the watchdog's default (scripts/ccswitch-health-check.sh: STATE_DIR).
from server.hostpaths import HEALTH_STATE_DIR as _DEFAULT_HEALTH_STATE_DIR
from server.hostname import self_host_name
from server.obs.context import log_event

log = logging.getLogger("server.sessions.daemon_health")


@dataclass(frozen=True)
class Restart:
    """One observed epoch change, with the facts needed to judge and report it."""
    was: str
    now: str
    was_sha: str | None
    now_sha: str | None
    count: int          # restarts inside the window, this one included


def epoch_started_at_ms(epoch: str | None) -> int | None:
    """Parse the daemon start time (ms since the unix epoch) out of an epoch string.

    Epoch shape is ``<start_ms_hex>-<pid>[-<uuid8>]``; the leading hex segment is the value
    of ``int(time.time() * 1000)`` captured at process start. Returns None for anything that
    doesn't parse so callers degrade to "uptime unknown" rather than crash.
    """
    if not epoch:
        return None
    head = epoch.split("-", 1)[0]
    try:
        return int(head, 16)
    except ValueError:
        return None


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _watchdog_last_restart() -> float | None:
    """The unix timestamp in the watchdog's ``$STATE_DIR/last-restart`` file, or None.

    Written by ``scripts/ccswitch-health-check.sh`` on every wedge-restart. Persists across
    edge restarts, so it backstops ``lastRestartAt`` when the in-memory history is empty.
    Best-effort: a missing/garbage file just yields None.
    """
    state_dir = os.environ.get("VIBERPIT_HEALTH_STATE_DIR", _DEFAULT_HEALTH_STATE_DIR)
    path = os.path.join(state_dir, "last-restart")
    try:
        with open(path) as fh:
            return float(fh.read().strip())
    except (OSError, ValueError):
        return None


class DaemonHealthMonitor:
    """Poll the daemon's ``/info`` and expose a restart/uptime snapshot for the dashboard.

    ``info_fn`` is an async callable returning the ``/info`` dict (``{"epoch": ...}``). It is
    injected (rather than a ``DaemonClient``) so the monitor is trivially unit-testable and
    decoupled from transport. ``poll_once`` is best-effort: a daemon that's momentarily
    unreachable mid-restart must never propagate an error out of the poll loop.

    ``stale_after_s`` controls how long a cached epoch is trusted without a fresh successful
    probe before ``available`` flips to ``False``. Defaults to 3 × ``poll_interval_s`` (i.e.
    three consecutive missed polls trigger unavailability). This prevents the edge from serving
    a false-green ``available: true`` from a tombstoned cache when the daemon is dead (issue
    #766).
    """

    def __init__(
        self,
        info_fn: Callable[[], Awaitable[dict]] | None,
        *,
        window_s: float = 86_400.0,
        poll_interval_s: float = 15.0,
        stale_after_s: float | None = None,
        clock: Callable[[], float] = time.time,
        host_name: str | None = None,
    ) -> None:
        self._info_fn = info_fn
        self._window_s = window_s
        self._poll_interval_s = poll_interval_s
        self._stale_after_s = stale_after_s if stale_after_s is not None else 3 * poll_interval_s
        self._clock = clock
        # The monitor watches the daemon on THIS host (app.state.daemon_endpoint), so its
        # fleet identity is this process's own — the same name the deploy controller uses
        # for its `local_name`, which is what makes `restart_deliberate` able to match.
        self._host_name = host_name or self_host_name()
        self._epoch: str | None = None
        self._sha: str | None = None              # the daemon's commit, to explain a restart
        self._last_seen_at: float | None = None  # when /info last succeeded (issue #766)
        self._restarts: deque[float] = deque()   # unix timestamps of observed epoch changes
        self._task: asyncio.Task | None = None

    def record(self, epoch: str | None, *, now: float, sha: str | None = None) -> Restart | None:
        """Fold a freshly-observed epoch into the history; return the restart, if any.

        A change from a previously-seen epoch is a restart; the first epoch we ever see is
        not (the edge just connected). Unknown/None epochs are ignored.

        Deliberately does NOT log. Whether a restart is an incident or a deploy doing its
        job takes an `await` (`host_upgrade.restart_deliberate`), and forcing that into a
        synchronous fold would either block the loop or split the decision across two
        places. `poll_once` reports; this stays a pure, synchronously-testable fold.
        """
        if not epoch:
            return None
        restart = None
        if self._epoch is not None and epoch != self._epoch:
            # Prune BEFORE counting. `_prune` used to run only in `snapshot`, so the count
            # in the log included restarts that had already aged out of the window, and
            # `len(...) + 1` then added a restart that had just been appended — #1653's own
            # sample read "2 restart(s) in the last 24h" for the first restart of the day.
            self._prune(now)
            self._restarts.append(now)
            restart = Restart(was=self._epoch, now=epoch, was_sha=self._sha, now_sha=sha,
                              count=len(self._restarts))
        self._epoch = epoch
        # Keep the last sha we were told about: a daemon that stops reporting one must not
        # make the NEXT restart look like a deploy by comparing a real sha against None.
        if sha:
            self._sha = sha
        self._last_seen_at = now
        return restart

    async def _report(self, restart: Restart) -> None:
        """Log an observed restart at the severity its explanation earns.

        A restart is routine — every deploy performs one, and this host does 7-9 a day —
        so logging all of them at WARNING filed a stream of auto-errors about a daemon
        doing exactly what it was told (#1653, and #1614 before it). An UNEXPLAINED
        restart is still an incident and still warns: that is the wedge→watchdog event
        issue #434 built this monitor to surface.

        Structured rather than interpolated, because `server.obs.error_signature`
        fingerprints on the normalized message: with the epochs in the text, a record
        whose hex runs happened not to normalize would mint a fresh issue lineage per
        restart. Keyed on the event name, the family is one fingerprint and the volatile
        parts ride in fields (#2126).
        """
        from server.sessions import host_upgrade  # deferred: runners imports this package

        try:
            deliberate = await host_upgrade.restart_deliberate(
                self._host_name, was_sha=restart.was_sha, now_sha=restart.now_sha)
        except Exception as exc:  # noqa: BLE001 — a log level must never break the poll loop
            log.debug("daemon-health: restart_deliberate failed (%s)", exc)
            deliberate = False
        log_event(
            log, logging.INFO if deliberate else logging.WARNING, "daemon.restart_observed",
            host=self._host_name, wasEpoch=restart.was, epoch=restart.now,
            wasSha=restart.was_sha, sha=restart.now_sha,
            restartCount=restart.count, windowHours=round(self._window_s / 3600, 1),
            deliberate=deliberate)

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_s
        while self._restarts and self._restarts[0] < cutoff:
            self._restarts.popleft()

    def snapshot(self, now: float | None = None) -> dict:
        """A JSON-safe health snapshot for ``GET /api/daemon/health``."""
        if now is None:
            now = self._clock()
        self._prune(now)
        started_ms = epoch_started_at_ms(self._epoch)
        started_at = started_ms / 1000.0 if started_ms is not None else None
        last_restart = self._restarts[-1] if self._restarts else _watchdog_last_restart()
        # Issue #766: available is only True when we have a fresh successful probe within the
        # staleness window. A cached epoch from a dead daemon must not read as available=True.
        last_seen_ago = (now - self._last_seen_at) if self._last_seen_at is not None else None
        available = (
            self._epoch is not None
            and last_seen_ago is not None
            and last_seen_ago <= self._stale_after_s
        )
        return {
            "available": available,
            "epoch": self._epoch,
            "startedAt": _iso(started_at),
            "uptimeSec": (now - started_at) if started_at is not None else None,
            "restartCount24h": len(self._restarts),
            "lastRestartAt": _iso(last_restart),
            "lastSeenAgoSec": last_seen_ago,
        }

    async def poll_once(self) -> None:
        if self._info_fn is None:
            return
        try:
            info = await self._info_fn()
        except Exception as exc:  # noqa: BLE001 — /info unreachable mid-restart is expected
            log.debug("daemon-health poll: /info unavailable (%s)", exc)
            return
        restart = self.record(info.get("epoch"), now=self._clock(), sha=info.get("sha"))
        if restart is not None:
            await self._report(restart)

    async def _run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self._poll_interval_s)

    def start(self) -> None:
        """Launch the background poll loop (idempotent)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="daemon-health-monitor")

    async def stop(self) -> None:
        if self._task is not None:
            # Reached from the SIGTERM drain: an outer cancellation must unwind us,
            # not be mistaken for the poller's own (issue #1164).
            await cancel_and_reap(self._task, phase="daemon-health-monitor")
            self._task = None
