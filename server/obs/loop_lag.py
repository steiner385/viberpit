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
"""
import asyncio
import logging
import sys
import threading
import time
import traceback

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


class LoopLagWatchdog:
    def __init__(
        self,
        interval: float = 1.0,
        warn_after: float = 0.5,
        warn_every: float = 60.0,
        dump_after: float | None = None,
        dump_path: str | None = None,
        _log=None,
        _clock=None,
        _tracer=None,
    ) -> None:
        self.interval = interval
        self.warn_after = warn_after
        self.warn_every = warn_every
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
        # Throttle state: when we last emitted, and what we swallowed since.
        self._last_warn_at: float | None = None
        self._suppressed = 0
        self._suppressed_peak = 0.0

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

    def _note_lag(self, lag: float) -> None:
        """Record one measured overshoot and warn about it, rate-limited.

        Split out of ``run`` so the throttle is testable without real sleeps.
        """
        if lag > self.peak_lag_s:
            self.peak_lag_s = lag
        if lag < self.warn_after:
            return

        now = self._clock()
        if self._last_warn_at is not None and (now - self._last_warn_at) < self.warn_every:
            # Inside the quiet window: count it, remember the worst, stay silent.
            self._suppressed += 1
            if lag > self._suppressed_peak:
                self._suppressed_peak = lag
            return

        if self._suppressed:
            self._log.warning(
                "loop_lag: event loop blocked for %.2fs (interval=%.2fs); "
                "%d further stall(s) suppressed in the last %.0fs, worst %.2fs",
                lag, self.interval, self._suppressed,
                now - (self._last_warn_at or now), self._suppressed_peak,
            )
        else:
            self._log.warning(
                "loop_lag: event loop blocked for %.2fs (interval=%.2fs)",
                lag, self.interval,
            )
        self._last_warn_at = now
        self._suppressed = 0
        self._suppressed_peak = 0.0

    async def run(self) -> None:
        self._arm_tracer()
        try:
            while True:
                started = self._clock()
                await asyncio.sleep(self.interval)
                lag = (self._clock() - started) - self.interval
                self._note_lag(lag)
                # Re-arm the deadman switch: this tick proves the loop is alive,
                # so push the dump deadline out. If the *next* sleep is swallowed
                # by a block longer than ``dump_after``, the tracer thread fires
                # and captures the culprit's stack — which this coroutine,
                # unschedulable during the block, never could.
                self._arm_tracer()
        finally:
            self._disarm_tracer()
