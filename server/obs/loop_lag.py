"""Event-loop lag watchdog.

Sleeps for a fixed interval and measures how much longer than that it actually
took. The overshoot is time the loop spent unable to schedule us — i.e. blocked
in synchronous work. RC1 (blocking sqlite3 on the loop) was invisible until an
external py-spy dump; this makes it self-reporting.

Warnings are rate-limited (``warn_every``). A saturated loop overshoots on every
tick, so an unthrottled watchdog would emit ~1 line/s (~86k lines/day) — and each
``log.warning`` is itself synchronous work on the already-stalled loop, so the
watchdog would amplify the problem it exists to report. A signal that noisy gets
tuned out. Nothing is lost: suppressed stalls are counted (with their worst lag)
and reported in the next warning that does get through, and ``peak_lag_s`` is
updated on every tick regardless of throttling.

The warning is a *structured event*, not an interpolated sentence, because pacing
and identity are different problems and this record had only the first one solved.
``server.obs.error_signature`` fingerprints a plain record on its normalized
message, and normalization masks only the shapes it recognizes — so the measured
lag leaked into the identity and every distinct value opened its own auto-error
issue: #1848, #1850, #1705, #1770, #1771, #1774, #1789 and #1797 are one stall
filed eight times. Keyed on ``loop_lag.stall``, the family is one fingerprint and
the measurements ride in ``fields``, which feed the issue's title but not its
identity. Same reasoning as ``server.obs.logdedupe.log_event_throttled``; the
throttle here predates it and stays, since it also has to bound the log-write cost
on an already-stalled loop.

Measuring the lag is only half the job: the coroutine that measures it is itself
unschedulable *during* a block, so by the time it wakes the culprit's frame is
gone (issue #762 — the daemon blocked for up to 130s and logged nothing about
where). ``ThreadStallTracer`` closes that gap: a plain ``threading.Thread`` runs
outside the event loop, so it stays schedulable while the loop is blocked and can
snapshot the stack of the thread stuck *inside* the blocking call. The watchdog
drives it as a deadman switch — re-armed on every healthy tick, it expires only
when the loop stops scheduling coroutines, which is exactly a stall.

The tracer used to be ``faulthandler.dump_traceback_later`` (issue #792): that
walks every thread's frame stack from a C timer thread that fires *without* the
GIL, so it read frames while other threads mutated them and segfaulted the daemon
under load — five clean journal-days turned into 5–7 crashes/day the moment it
shipped. The pure-Python tracer snapshots ``sys._current_frames()`` and formats
with ``traceback.format_stack()``; both run under the GIL, so no frame is ever
read mid-mutation. See ``ThreadStallTracer`` for the tradeoff this costs.

Level is a separate decision from reporting (#1514). WARNING is what the error
sink files as an issue, and the daemon overshoots by ~1s many times a day under
ordinary load; filing those produced tickets with no stack to act on. So a stall
is reported from ``warn_after`` but is a WARNING only from ``alert_after`` —
by default the stack-dump threshold, so every filed stall has a dump beside it.
The two levels throttle independently, so a routine INFO line can never spend
the window a real stall needs.

A WARNING also has to be the *process's* fault (#2866). When the host thrashes
memory, every process freezes at once — edge and daemon logged 5–29s stalls at
the same instants, and no stack dump named a culprit because the tracer thread
froze with the loop. So the watchdog snapshots the kernel's cumulative PSI
``full`` stall counters (``/proc/pressure/{memory,io}``) at every tick, and when
a lag crosses ``alert_after`` it diffs them over that window. If host-wide stall
time covers at least ``HOST_STALL_SHARE`` of the lag, the stall is reported as
``loop_lag.host_stall`` at INFO with the measured ``host_*_full_s``; a stall the
host's pressure does not explain stays a WARNING. PSI absent → WARNING, as before.
"""
import asyncio
import logging
import os
import sys
import threading
import time
import traceback

from server.config import env_float
from server.obs.context import emit_event

_default_log = logging.getLogger("server.obs.loop_lag")


class ThreadStallTracer:
    """Deadman switch that dumps every thread's stack from a plain Python thread.

    ``arm(timeout)`` sets a deadline ``timeout`` seconds out; the watchdog re-arms
    it on every healthy tick, so while the loop keeps scheduling coroutines the
    deadline is pushed forward and the dump never fires. When the loop blocks
    synchronously past ``timeout`` without a re-arm, the tracer's own thread —
    which runs outside the event loop and so stays schedulable — wakes, writes one
    dump of all thread stacks, and stays quiet until re-armed (``repeat=False``
    semantics). It names the culprit line that the async watchdog can only measure
    after the fact.

    The dump goes to ``file`` (default ``sys.stderr``, i.e. the journal) and is
    built from ``sys._current_frames()`` + ``traceback.format_stack()``. Both
    execute under the GIL, so a frame is never read while the thread owning it
    mutates it — the read is safe by construction. This is the whole point of the
    rewrite (issue #792): ``faulthandler.dump_traceback_later`` walked those same
    frames from a *GIL-free* C thread and segfaulted the daemon under load.

    KNOWN LIMITATION — deliberately narrower than faulthandler. A Python thread
    runs only when it can acquire the GIL. If the loop thread blocks in native
    code while **holding** the GIL, this watchdog thread cannot be scheduled and
    no dump is produced — faulthandler's C thread had no such dependency. Almost
    every stall in this daemon is Python-level or releases the GIL on I/O (sync
    ``sqlite3``, blocking sockets), so the real cases are covered; a GIL-holding C
    extension stall goes uncaptured, by design, in exchange for not crashing the
    process. ``cancel()`` disarms and stops the thread (it is only called at
    watchdog shutdown); a later ``arm`` transparently restarts it.
    """

    def __init__(self, file=None, *, _clock=None) -> None:
        self._file = file if file is not None else sys.stderr
        self._clock = _clock if _clock is not None else time.monotonic
        self._cond = threading.Condition()
        self._deadline: float | None = None
        self._timeout = 0.0
        self._fired = False
        self._stop = False
        self._thread: threading.Thread | None = None

    def arm(self, timeout: float) -> None:
        with self._cond:
            self._timeout = timeout
            self._deadline = self._clock() + timeout
            self._fired = False
            self._stop = False
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._watch, name="loop-lag-stall-tracer", daemon=True)
                self._thread.start()
            self._cond.notify_all()

    def cancel(self) -> None:
        with self._cond:
            self._deadline = None
            self._stop = True
            self._cond.notify_all()
            thread, self._thread = self._thread, None
        # Join outside the lock so the waking thread can re-acquire it and exit.
        # A dump-in-progress finishes first, so the bound is a dump write, not a
        # stall; keep it short so shutdown never hangs on a wedged tracer.
        if thread is not None:
            thread.join(timeout=2.0)

    def _watch(self) -> None:
        while True:
            with self._cond:
                # Idle until armed, or after a dump until re-armed.
                while not self._stop and (self._deadline is None or self._fired):
                    self._cond.wait()
                if self._stop:
                    return
                deadline = self._deadline
                if deadline is None:  # disarmed between wake and re-check
                    continue
                remaining = deadline - self._clock()
                if remaining > 0:
                    # A healthy re-arm pushed the deadline out; sleep to the new
                    # one. arm()/cancel() notify to wake us early to recompute.
                    self._cond.wait(timeout=remaining)
                    continue
                self._fired = True
            self._dump()  # outside the lock — arm()/cancel() must not block on it

    def _dump(self) -> None:
        frames = sys._current_frames()
        parts = [
            f"\nloop_lag stall dump: no healthy loop tick for >{self._timeout:.1f}s "
            f"— stacks of {len(frames)} thread(s):\n"
        ]
        for thread_id, frame in frames.items():
            parts.append(f"\nThread 0x{thread_id:x}:\n")
            parts.extend(traceback.format_stack(frame))
        try:
            self._file.write("".join(parts))
            self._file.flush()
        except Exception:  # pragma: no cover - a diagnostic dump must never crash the daemon
            pass


def _build_stall_tracer(dump_path: str | None):
    """Real tracer targeting ``dump_path`` (append) or ``sys.stderr`` (journal).

    The file handle lives for the process lifetime — a daemon-scoped watchdog is
    never torn down before exit, so we deliberately never close it.
    """
    if dump_path:
        # Line-buffered append keeps the path readable between dumps.
        handle = open(dump_path, "a", buffering=1)
        return ThreadStallTracer(file=handle)
    return ThreadStallTracer(file=sys.stderr)


_PSI_RESOURCES = ("memory", "io")


def read_host_psi() -> dict[str, float] | None:
    """Cumulative host-wide PSI ``full`` stall seconds per resource, or None.

    ``full`` is time during which *every* non-idle task was stalled on that
    resource — the whole-host freeze a loop-lag watchdog cannot see past. Cheap
    procfs reads; a kernel without PSI (or a container hiding it) returns None
    and the watchdog falls back to treating every stall as the process's own.
    """
    totals: dict[str, float] = {}
    for resource in _PSI_RESOURCES:
        try:
            with open(f"/proc/pressure/{resource}") as fh:
                text = fh.read()
        except OSError:
            return None
        for line in text.splitlines():
            if line.startswith("full "):
                for part in line.split():
                    if part.startswith("total="):
                        totals[resource] = int(part[len("total="):]) / 1e6
                break
        if resource not in totals:
            return None
    return totals


# Share of a stall that host-wide full stall time must cover for the stall to be
# attributed to the host rather than to this process (#2866).
HOST_STALL_SHARE = 0.5


class _Throttle:
    """One level's quiet window, and what it swallowed since its last line."""

    def __init__(self) -> None:
        self.last_at: float | None = None
        self.suppressed = 0
        self.suppressed_peak = 0.0


class LoopLagWatchdog:
    def __init__(
        self,
        interval: float = 1.0,
        warn_after: float = 0.5,
        warn_every: float = 60.0,
        alert_after: float | None = None,
        dump_after: float | None = None,
        dump_path: str | None = None,
        _log=None,
        _clock=None,
        _tracer=None,
        _psi=None,
    ) -> None:
        self.interval = interval
        self.warn_after = warn_after
        self.warn_every = warn_every
        # Stalls in [warn_after, alert_after) log at INFO; None keeps every
        # reported stall a WARNING.
        self.alert_after = alert_after
        self.peak_lag_s = 0.0
        self._log = _log if _log is not None else _default_log
        self._clock = _clock if _clock is not None else time.monotonic
        # Stack-dump-on-stall (issue #762, made segfault-safe in #792). Disabled
        # unless a positive threshold is given, so existing call sites and tests
        # are unaffected. A tracer may be injected for tests; otherwise a real
        # ThreadStallTracer is built lazily only when the feature is actually on.
        self.dump_after = dump_after if (dump_after and dump_after > 0) else None
        if self.dump_after is not None:
            self._tracer = _tracer if _tracer is not None else _build_stall_tracer(dump_path)
        else:
            self._tracer = None
        # Host PSI reader (#2866); returns cumulative full-stall seconds per
        # resource, or None when the host does not expose PSI.
        self._psi = _psi if _psi is not None else read_host_psi
        # Routine overshoot, host-explained stalls and real stalls throttle
        # independently: neither INFO class may spend the window a WARNING needs.
        self._throttles = {"routine": _Throttle(), "host": _Throttle(), "alert": _Throttle()}

    def _arm_tracer(self) -> None:
        """(Re)arm the deadman switch for the next interval, if enabled."""
        if self.dump_after is not None and self._tracer is not None:
            self._tracer.arm(self.dump_after)

    def _disarm_tracer(self) -> None:
        if self._tracer is not None:
            try:
                self._tracer.cancel()
            except Exception:  # pragma: no cover - disarm must never mask shutdown
                pass

    def _read_psi(self) -> dict[str, float] | None:
        try:
            return self._psi()
        except Exception:  # a diagnostic read must never kill the watchdog
            return None

    def _host_stall(self, psi_before: dict[str, float] | None) -> dict[str, float] | None:
        """Host-wide full-stall seconds per resource since ``psi_before``, or None."""
        if psi_before is None:
            return None
        psi_after = self._read_psi()
        if psi_after is None:
            return None
        return {
            r: max(0.0, psi_after[r] - psi_before[r])
            for r in psi_before.keys() & psi_after.keys()
        }

    def _note_lag(self, lag: float, psi_before: dict[str, float] | None = None) -> None:
        """Record one measured overshoot and warn about it, rate-limited.

        Split out of ``run`` so the throttle is testable without real sleeps.
        ``psi_before`` is the host PSI snapshot taken at the start of the tick;
        with it, a stall the host's own pressure explains is not a WARNING.
        """
        if lag > self.peak_lag_s:
            self.peak_lag_s = lag
        if lag < self.warn_after:
            return

        event = "loop_lag.stall"
        extra: dict[str, float] = {}
        if self.alert_after is not None and lag < self.alert_after:
            kind, level = "routine", logging.INFO
        else:
            kind, level = "alert", logging.WARNING
            host = self._host_stall(psi_before)
            # Memory and io full-stall overlap during thrash (reclaim *is* io), so
            # the larger of the two — not the sum — is the host's share.
            if host and max(host.values()) >= HOST_STALL_SHARE * lag:
                kind, level, event = "host", logging.INFO, "loop_lag.host_stall"
                extra = {f"host_{r}_full_s": round(s, 2) for r, s in sorted(host.items())}
        t = self._throttles[kind]
        now = self._clock()
        if t.last_at is not None and (now - t.last_at) < self.warn_every:
            # Inside the quiet window: count it, remember the worst, stay silent.
            t.suppressed += 1
            if lag > t.suppressed_peak:
                t.suppressed_peak = lag
            return

        # One field shape every time — ``suppressed`` is 0 rather than absent when
        # nothing was swallowed — so a reader (and the issue title) never has to
        # tell two record variants apart to answer "how bad, how often".
        emit_event(
            self._log,
            level,
            event,
            {
                "lag_s": round(lag, 2),
                "interval_s": round(self.interval, 2),
                "suppressed": t.suppressed,
                "suppressed_peak_s": round(t.suppressed_peak, 2),
                "since_last_warning_s": round(now - (t.last_at or now), 1),
                **extra,
            },
        )
        t.last_at = now
        t.suppressed = 0
        t.suppressed_peak = 0.0

    async def run(self) -> None:
        self._arm_tracer()
        try:
            while True:
                started = self._clock()
                psi_before = self._read_psi()
                await asyncio.sleep(self.interval)
                lag = (self._clock() - started) - self.interval
                self._note_lag(lag, psi_before)
                # Re-arm the deadman switch: this tick proves the loop is alive,
                # so push the dump deadline out. If the *next* sleep is swallowed
                # by a block longer than ``dump_after``, the tracer thread fires
                # and captures the culprit's stack — which this coroutine,
                # unschedulable during the block, never could.
                self._arm_tracer()
        finally:
            self._disarm_tracer()


# On by default (#1913). Opt-in meant it never ran: dobby logged stalls of up to
# 38s for weeks with no stack naming the culprit, because nobody set the knob.
# 5s sits well above routine overshoot (sub-second to ~1s under normal load), so
# only a real block produces a dump — one per stall, since the tracer stays quiet
# until the next healthy tick re-arms it.
DEFAULT_STALL_DUMP_S = 5.0


def watchdog_from_env(**kwargs) -> LoopLagWatchdog:
    """The production watchdog, as both the daemon and the edge build it.

    ``VIBERPIT_LOOP_STALL_DUMP_S`` sets the stall-dump threshold (``0`` turns the
    dump off); ``VIBERPIT_LOOP_STALL_DUMP_PATH`` sends it to a file instead of the
    journal. ``VIBERPIT_LOOP_LAG_WARN_S`` is the lag at which a stall becomes a
    WARNING (an auto-error issue); shorter overshoot logs at INFO. It defaults to
    the dump threshold so a filed stall comes with a stack (#1514). One factory,
    so the two processes cannot drift on the defaults.
    """
    return LoopLagWatchdog(
        alert_after=env_float("VIBERPIT_LOOP_LAG_WARN_S", DEFAULT_STALL_DUMP_S, minimum=0.0),
        dump_after=env_float("VIBERPIT_LOOP_STALL_DUMP_S", DEFAULT_STALL_DUMP_S, minimum=0.0),
        dump_path=os.environ.get("VIBERPIT_LOOP_STALL_DUMP_PATH") or None,
        **kwargs,
    )
