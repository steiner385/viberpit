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

Host ``full`` misses a *partly* thrashing host (#2882): the edge froze 7s faulting
on its own swapped-out pages while the daemon kept logging, so host-wide ``full``
stayed low. ``PsiReader`` therefore also reads this process's own cgroup
``memory.pressure`` / ``io.pressure`` ``full`` counters (``cgroup_*_full_s``) and
the largest of all the deltas is what must cover the lag. Every delta rides on
the WARNING too, so an unexplained stall shows how much pressure *did* explain.

The PSI reads must not sit inside the window they measure (#3130). On a swapping
host a procfs/kernfs ``read()`` can block for seconds; done synchronously on the
loop, that stalled the loop, was counted as lag, and — when the "before" read was
the slow one — already had the stall in its totals, so the delta subtracted it
out. Half of dobby's stack dumps showed the loop thread inside
``_psi_full_total_s``. So reads run on a dedicated one-thread executor with a
timeout, a read still wedged in the kernel is never queued behind, and the read's
own duration is taken off the measured lag. A loop block *during* a read still
counts: the await's overshoot beyond the read is lag.

PSI cannot see everything that freezes the loop — synchronous zram swap-in is not
memstall, and the loop running our own code is not pressure at all. So a WARNING
also carries the tick's ``getrusage(RUSAGE_SELF)`` deltas and GC time
(``utime_s``, ``stime_s``, ``majflt``, ``nivcsw``, ``gc_s``): utime ≈ lag is the
loop on CPU in our code, high ``majflt`` is faulting heap back from swap, high
``nivcsw`` with utime ≈ 0 is descheduling, and ``gc_s`` is a collector pause.
"""
import asyncio
import concurrent.futures
import gc
import logging
import os
import resource
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


def _psi_full_total_s(path: str) -> float | None:
    """Cumulative ``full`` stall seconds from one PSI file, or None if unreadable."""
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("full "):
            for part in line.split():
                if part.startswith("total="):
                    try:
                        return int(part[len("total="):]) / 1e6
                    except ValueError:
                        return None
    return None


def _own_cgroup_dir(proc_root: str, cgroup_root: str) -> str | None:
    """This process's cgroup v2 directory (``0::/system.slice/x.service``), or None."""
    try:
        with open(f"{proc_root}/self/cgroup") as fh:
            text = fh.read()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            return os.path.join(cgroup_root, line[3:].strip().lstrip("/"))
    return None  # cgroup v1 only: no unified hierarchy, no per-unit PSI


class PsiReader:
    """Cumulative PSI ``full`` stall seconds, keyed ``<scope>_<resource>``, or None.

    Two scopes. ``host`` (``/proc/pressure/*``) is time during which *every*
    non-idle task on the host was stalled — the whole-host freeze of #2866.
    ``cgroup`` (``<own cgroup>/*.pressure``) is time during which every task in
    *this unit* was stalled (#2882): a partly thrashing host keeps host ``full``
    low while the edge faults on its own swapped-out pages, which only the unit's
    counter sees. For the daemon the unit also holds the claude children, so its
    ``full`` stays conservative — one faulting child does not trip it.

    The cgroup is resolved once, at construction. Each file is read on its own:
    a missing one drops only its key (cgroup v1, no PSI, a hidden procfs), and a
    reader with nothing readable returns None so the watchdog keeps every stall
    the process's own.
    """

    def __init__(self, proc_root: str = "/proc", cgroup_root: str = "/sys/fs/cgroup") -> None:
        self._paths = {f"host_{r}": f"{proc_root}/pressure/{r}" for r in _PSI_RESOURCES}
        cgroup_dir = _own_cgroup_dir(proc_root, cgroup_root)
        if cgroup_dir is not None:
            self._paths.update(
                {f"cgroup_{r}": f"{cgroup_dir}/{r}.pressure" for r in _PSI_RESOURCES})

    def __call__(self) -> dict[str, float] | None:
        totals = {}
        for key, path in self._paths.items():
            total = _psi_full_total_s(path)
            if total is not None:
                totals[key] = total
        return totals or None


def _rusage_totals() -> dict[str, float]:
    """Cumulative CPU time, major faults and involuntary switches of this process."""
    r = resource.getrusage(resource.RUSAGE_SELF)
    return {"utime_s": r.ru_utime, "stime_s": r.ru_stime,
            "majflt": r.ru_majflt, "nivcsw": r.ru_nivcsw}


class GcTimer:
    """Cumulative seconds spent in the cyclic garbage collector, via ``gc.callbacks``."""

    def __init__(self) -> None:
        self.total_s = 0.0
        self._started: float | None = None

    def _callback(self, phase: str, _info: dict) -> None:
        if phase == "start":
            self._started = time.perf_counter()
        elif self._started is not None:
            self.total_s += time.perf_counter() - self._started
            self._started = None

    def install(self) -> None:
        if self._callback not in gc.callbacks:
            gc.callbacks.append(self._callback)

    def remove(self) -> None:
        if self._callback in gc.callbacks:
            gc.callbacks.remove(self._callback)


def usage_since(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    """Per-tick deltas of cumulative usage counters; seconds rounded, counts exact."""
    out: dict[str, float] = {}
    for k in before.keys() & after.keys():
        delta = after[k] - before[k]
        out[k] = round(delta, 2) if k.endswith("_s") else int(delta)
    return out


# A PSI read is microseconds when the kernel is healthy; one slower than this is
# abandoned for the tick (the tick goes unattributed) rather than waited on.
PSI_READ_TIMEOUT_S = 1.0

# Share of a stall that full stall time (host-wide or this unit's) must cover for
# the stall to be attributed to memory/io pressure rather than to this process's
# own code (#2866, #2882).
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
        psi_timeout: float = PSI_READ_TIMEOUT_S,
        _tracer=None,
        _psi=None,
        _usage=None,
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
        # PSI reader (#2866, #2882); returns cumulative full-stall seconds per
        # ``<scope>_<resource>``, or None when nothing exposes PSI.
        self._psi = _psi if _psi is not None else PsiReader()
        # Off-loop PSI reads (#3130): one dedicated worker, so a read wedged in
        # the kernel can never starve the shared default executor.
        self.psi_timeout = psi_timeout
        self._psi_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._psi_inflight: asyncio.Future | None = None
        # Cumulative usage counters diffed per tick for the WARNING (#3130).
        self._gc = GcTimer()
        self._usage = _usage if _usage is not None else self._usage_totals
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

    def _timed_read_psi(self) -> tuple[dict[str, float] | None, float]:
        """One PSI snapshot and how long the read took; runs on the PSI worker."""
        t0 = self._clock()
        totals = self._read_psi()
        return totals, self._clock() - t0

    async def _read_psi_off_loop(self) -> tuple[dict[str, float] | None, float]:
        """A PSI snapshot read off the loop, and the seconds not to count as lag.

        A slow kernfs read can then neither stall the loop nor be measured as
        a stall. A read that outlives ``psi_timeout`` is abandoned for this tick
        (its whole wait is excused), and while it is still stuck no new read is
        queued behind it.
        """
        if self._psi_inflight is not None and not self._psi_inflight.done():
            return None, 0.0
        if self._psi_executor is None:
            self._psi_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="loop-lag-psi")
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self._psi_executor, self._timed_read_psi)
        self._psi_inflight = fut
        try:
            return await asyncio.wait_for(asyncio.shield(fut), self.psi_timeout)
        except asyncio.TimeoutError:
            return None, self.psi_timeout

    def _shutdown_psi_executor(self) -> None:
        executor, self._psi_executor = self._psi_executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def _usage_totals(self) -> dict[str, float]:
        return {**_rusage_totals(), "gc_s": self._gc.total_s}

    def _is_alert(self, lag: float) -> bool:
        """Whether ``lag`` is reported at the WARNING tier (pressure-attributed)."""
        return lag >= self.warn_after and (self.alert_after is None or lag >= self.alert_after)

    @staticmethod
    def _pressure_since(
        psi_before: dict[str, float] | None, psi_after: dict[str, float] | None,
    ) -> dict[str, float] | None:
        """Full-stall seconds per ``<scope>_<resource>`` between two snapshots, or None."""
        if psi_before is None or psi_after is None:
            return None
        return {
            r: max(0.0, psi_after[r] - psi_before[r])
            for r in psi_before.keys() & psi_after.keys()
        }

    def _note_lag(
        self,
        lag: float,
        psi_before: dict[str, float] | None = None,
        psi_after: dict[str, float] | None = None,
        usage: dict[str, float] | None = None,
    ) -> None:
        """Record one measured overshoot and warn about it, rate-limited.

        Split out of ``run`` so the throttle is testable without real sleeps.
        ``psi_before``/``psi_after`` are the PSI snapshots around the tick; with
        both, a stall that host or cgroup pressure explains is not a WARNING.
        ``usage`` is the tick's resource-usage delta, attached at that tier.
        """
        if lag > self.peak_lag_s:
            self.peak_lag_s = lag
        if lag < self.warn_after:
            return

        event = "loop_lag.stall"
        extra: dict[str, float] = {}
        if not self._is_alert(lag):
            kind, level = "routine", logging.INFO
        else:
            kind, level = "alert", logging.WARNING
            pressure = self._pressure_since(psi_before, psi_after)
            if pressure:
                # Attached to the WARNING too, so an unexplained stall still shows
                # how much of it pressure did explain (#2882).
                extra = {f"{k}_full_s": round(s, 2) for k, s in sorted(pressure.items())}
                # Memory and io full-stall overlap during thrash (reclaim *is* io),
                # and the cgroup's stall is inside the host's — so the largest
                # single counter, never a sum, is pressure's share.
                if max(pressure.values()) >= HOST_STALL_SHARE * lag:
                    kind, level, event = "host", logging.INFO, "loop_lag.host_stall"
            if usage:
                extra.update(usage)
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
        self._gc.install()
        try:
            while True:
                started = self._clock()
                usage_before = self._usage()
                psi_before, excused = await self._read_psi_off_loop()
                await asyncio.sleep(self.interval)
                # The read's own time is the kernel's, not the loop's (#3130);
                # whatever the await overshot beyond it is still lag.
                lag = (self._clock() - started) - self.interval - excused
                usage = usage_since(usage_before, self._usage())
                psi_after = None
                if psi_before is not None and self._is_alert(lag):
                    # Read after the window closes, so it is never measured.
                    psi_after, _ = await self._read_psi_off_loop()
                self._note_lag(lag, psi_before, psi_after, usage)
                # Re-arm the deadman switch: this tick proves the loop is alive,
                # so push the dump deadline out. If the *next* sleep is swallowed
                # by a block longer than ``dump_after``, the tracer thread fires
                # and captures the culprit's stack — which this coroutine,
                # unschedulable during the block, never could.
                self._arm_tracer()
        finally:
            self._disarm_tracer()
            self._gc.remove()
            self._shutdown_psi_executor()


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
