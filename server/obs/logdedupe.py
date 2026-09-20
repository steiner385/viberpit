"""Window-based suppression for a log record that repeats on a polling loop.

The daemon's sweeps re-enter the same failure paths every tick, so a condition
that lasts a day becomes thousands of identical WARNING records. That is not a
cosmetic problem: :mod:`server.obs.errsink` captures WARNING+ and files one
auto-error issue per fingerprint, so a single stuck session produced #1702 with
2455 occurrences — a real signal buried under its own repetitions.

The policy here is deliberately a *window*, not a one-shot latch:

* **One-shot** ("warn once per process") loses the heartbeat. A condition that
  clears and returns a week later never says so again, and the journal cannot
  answer "is this still happening?" — the question every operator asks first.
* **Every-Nth** keeps a heartbeat but paces it by traffic rather than by time,
  so a hot loop still floods and a slow one goes quiet for hours.
* **Windowed** is the one that matches how the records are read: the first
  occurrence reports promptly, repeats inside ``window_s`` are demoted to DEBUG
  (still in the journal at debug level, out of the error sink), and the next
  occurrence after the window re-reports. A day-long stall becomes a handful of
  records that each mean "still broken", which is exactly the sink's unit.

Keys are caller-supplied and must be *bounded in cardinality by construction* —
a session id is fine, a timestamp is not. The store is capped and evicts oldest-
first regardless, so a caller that gets this wrong degrades to more logging
rather than to unbounded memory.

Nothing here reads the environment or the clock module: ``clock`` is injected so
tests state elapsed time instead of sleeping.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Final

from server.obs.context import emit_event

#: Repeats inside this many seconds are demoted, capping any one key at four
#: records a day.
#:
#: Chosen against the two incidents rather than by feel, because the loops that
#: re-enter these paths run at very different rates and a window picked for the
#: fast one is a no-op for the slow one:
#:
#:   #1702  2455 records / 52.8h  =  46.5/hour  (the stuck-session sweep)
#:   #1852    66 records / 169.6h =   0.39/hour (the titling/summary sweeps)
#:
#: A 30-minute window caps at 48/day — a 23x cut for #1702 and *nothing at all*
#: for #1852, which never exceeded 9.3/day to begin with. Six hours is the
#: shortest window that meaningfully paces both: 1117/day -> 4 and 9.3/day -> 4.
#:
#: Raising this further trades away the heartbeat, which is the property that
#: makes a window better than a latch; lowering it stops helping the slow loops.
#: Retune against measured rates, not intuition.
DEFAULT_WINDOW_S: Final = 21600.0

#: Distinct keys retained. Well above the live-session count on any one host, so
#: eviction is the safety net for a mis-keyed caller, never the normal path.
DEFAULT_CAPACITY: Final = 2048


class LogThrottle:
    """Answers "has *key* been reported recently?" for one family of records.

    One instance per record family, not one per process: sharing an instance
    across unrelated call sites would let a busy one evict a quiet one's key.

    Deliberately unlocked. Callers are on the daemon's event loop or in a worker
    thread, and under the GIL the worst a race can do is report one extra record
    or suppress one that was due — neither outcome is worth making every log call
    contend on a lock.
    """

    __slots__ = ("_window_s", "_capacity", "_clock", "_seen")

    def __init__(
        self,
        *,
        window_s: float = DEFAULT_WINDOW_S,
        capacity: int = DEFAULT_CAPACITY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_s = window_s
        self._capacity = max(1, capacity)
        self._clock = clock
        self._seen: OrderedDict[str, float] = OrderedDict()

    def should_log(self, key: str) -> bool:
        """Whether *key* may report at full level now, recording that it did.

        True on the first sighting and once per ``window_s`` after it. Calling
        this is what starts the window, so it must be called exactly once per
        occurrence — see :func:`log_throttled` for the usual wrapper.
        """
        now = self._clock()
        last = self._seen.get(key)
        if last is not None and (now - last) < self._window_s:
            # Not refreshed: the window runs from the last *reported* occurrence,
            # so a continuous failure still re-reports on schedule instead of
            # being held down forever by its own repeats.
            self._seen.move_to_end(key)
            return False
        self._seen[key] = now
        self._seen.move_to_end(key)
        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return True

    def clear(self) -> None:
        """Drop every window. For tests that assert on first-sighting behaviour."""
        self._seen.clear()

    def forget(self, key: str) -> None:
        """Drop *key*'s window so its next occurrence reports immediately.

        For callers that know the condition ended (a session was repaired or
        archived): the recovery is worth hearing about promptly if it breaks
        again, and a key that can never recur should not hold a slot.
        """
        self._seen.pop(key, None)


def log_throttled(
    log: logging.Logger,
    throttle: LogThrottle,
    key: str,
    level: int,
    msg: str,
    *args: object,
    exc_info: bool | BaseException | None = None,
) -> None:
    """Emit *msg* at *level*, or at DEBUG if *key* reported recently.

    Suppressed repeats are still written (at DEBUG) rather than dropped: the
    goal is to keep one stuck loop out of the error sink, not to hide it from
    someone who turned debug logging on to watch it.
    """
    if throttle.should_log(key):
        log.log(level, msg, *args, exc_info=exc_info)
        return
    log.debug(msg, *args, exc_info=exc_info)


def log_event_throttled(
    log: logging.Logger,
    throttle: LogThrottle,
    key: str,
    level: int,
    event: str,
    *,
    exc_info: bool | BaseException | None = None,
    **fields: Any,
) -> None:
    """Emit structured *event* at *level*, or at DEBUG if *key* reported recently.

    Prefer this over :func:`log_throttled` for any record that interpolates a
    *name* — a slug, a repo, a path. Throttling and structuring fix two halves
    of one problem and neither alone is enough:

    * Throttling paces how *often* a standing condition reports.
    * Structuring decides how many *distinct conditions* the error sink thinks
      it saw. :func:`server.obs.error_signature.signature_of` fingerprints a
      plain record on its normalized message, and normalization only masks the
      shapes it recognizes — uuids, quoted strings, hex runs, ints. A slug has
      none of those, so ``"... src=%s -> %s/%s"`` mints a fresh fingerprint, and
      therefore a separately filed issue, for every edge it reports. #1531/#1875
      (``src=credential-store-encryption``) and #1533/#1877
      (``src=config-dir-isolation``) are one call site split into two issue
      lineages by nothing but a slug; #1874/#1635/#1902/#1903 are the same story
      for the sibling pm-sync records. Keyed on ``event``, the family collapses
      to one fingerprint and the volatile parts ride in ``fields``, which feed
      the issue's title but not its identity.

    *key* still selects the throttle window and should stay as specific as the
    condition: while one broken edge is mid-window, a *newly* broken edge is
    news and must report promptly. Identity wants to be coarse and pacing wants
    to be fine, which is why they are separate arguments rather than one.
    """
    emit_event(
        log,
        level if throttle.should_log(key) else logging.DEBUG,
        event,
        fields,
        exc_info=exc_info,
    )
