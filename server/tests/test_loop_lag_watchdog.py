"""The watchdog must notice when the event loop stops being scheduled."""
import asyncio
import contextlib
import logging
import time

import pytest

from server.obs.loop_lag import LoopLagWatchdog


class _CapturingLog:
    """Stands in for a Logger, recording the structured events it is asked to emit."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, level, msg, *args, extra=None, **kwargs) -> None:
        extra = extra or {}
        self.events.append((extra.get("event", msg), dict(extra.get("fields") or {})))

    @property
    def fields(self) -> list[dict]:
        return [f for _event, f in self.events]


@pytest.mark.asyncio
async def test_reports_nothing_when_the_loop_is_healthy():
    log = _CapturingLog()
    wd = LoopLagWatchdog(interval=0.05, warn_after=0.5, _log=log)
    task = asyncio.create_task(wd.run())
    await asyncio.sleep(0.3)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert log.events == []
    assert wd.peak_lag_s < 0.5


@pytest.mark.asyncio
@pytest.mark.slow
async def test_warns_when_the_loop_is_blocked():
    log = _CapturingLog()
    wd = LoopLagWatchdog(interval=0.05, warn_after=0.2, _log=log)
    task = asyncio.create_task(wd.run())
    await asyncio.sleep(0.05)

    time.sleep(0.6)          # block the loop exactly as a sync sqlite read would
    await asyncio.sleep(0.1)  # let the watchdog observe the overshoot
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert log.events, "a 600ms block must be reported"
    assert wd.peak_lag_s >= 0.2


class _FakeClock:
    """Deterministic monotonic stand-in so throttle windows need no real sleeps."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_repeated_stalls_log_once_per_window():
    """A saturated loop overshoots every tick; it must not emit a line every tick."""
    log = _CapturingLog()
    clock = _FakeClock()
    wd = LoopLagWatchdog(interval=1.0, warn_after=0.5, warn_every=60.0,
                         _log=log, _clock=clock)

    for _ in range(30):
        clock.advance(1.0)
        wd._note_lag(0.9)

    assert len(log.events) == 1, f"expected one line, got {len(log.events)}"
    # peak_lag_s must stay accurate even for the suppressed ticks.
    assert wd.peak_lag_s == pytest.approx(0.9)


def test_suppressed_stalls_are_reported_in_the_next_warning():
    """Nothing is lost: the next line that gets through carries the swallowed count."""
    log = _CapturingLog()
    clock = _FakeClock()
    wd = LoopLagWatchdog(interval=1.0, warn_after=0.5, warn_every=10.0,
                         _log=log, _clock=clock)

    wd._note_lag(0.6)                # first stall — logs immediately
    for lag in (0.7, 2.5, 0.8):      # inside the quiet window — suppressed
        clock.advance(1.0)
        wd._note_lag(lag)
    clock.advance(30.0)              # window elapsed
    wd._note_lag(0.55)

    assert len(log.events) == 2
    event, fields = log.events[1]
    assert event == "loop_lag.stall"
    assert fields["suppressed"] == 3, f"suppressed count missing from {fields!r}"
    assert fields["suppressed_peak_s"] == pytest.approx(2.5), (
        f"suppressed peak missing from {fields!r}")
    # The worst lag overall is the suppressed 2.5s, not the logged 0.6s.
    assert wd.peak_lag_s == pytest.approx(2.5)


def test_a_lag_below_the_threshold_never_arms_the_throttle():
    """A quiet loop must not consume the first-warning slot with sub-threshold ticks."""
    log = _CapturingLog()
    clock = _FakeClock()
    wd = LoopLagWatchdog(interval=1.0, warn_after=0.5, warn_every=60.0,
                         _log=log, _clock=clock)

    for _ in range(5):
        clock.advance(1.0)
        wd._note_lag(0.01)
    assert log.events == []

    wd._note_lag(0.9)
    assert len(log.events) == 1
    assert log.fields[0]["suppressed"] == 0


@pytest.mark.asyncio
@pytest.mark.slow
async def test_two_real_stalls_produce_one_line():
    """End-to-end through run(): two blocks inside one window → a single warning."""
    log = _CapturingLog()
    # PSI stubbed absent: on a loaded CI runner the real /proc/pressure can
    # attribute one block to the host (INFO host_stall) and the other not
    # (WARNING stall) — two kinds, two throttles, two lines. This test is about
    # the throttle, not attribution.
    wd = LoopLagWatchdog(interval=0.05, warn_after=0.2, warn_every=60.0, _log=log,
                         _psi=lambda: None)
    task = asyncio.create_task(wd.run())
    await asyncio.sleep(0.05)

    for _ in range(2):
        time.sleep(0.4)       # block the loop exactly as a sync sqlite read would
        await asyncio.sleep(0.1)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert len(log.events) == 1, f"expected one line, got {log.events!r}"
    assert wd.peak_lag_s >= 0.2


def test_stall_records_are_one_condition_to_the_error_sink():
    """Eight measured lags, one filed condition (#1848 and its seven siblings).

    The watchdog's throttle only paces how often it reports; what the error sink
    counts as *one* condition is decided by the record's shape. When the lag was
    interpolated into the message it reached the fingerprint, so a week of one
    stall opened eight auto-error issues. Asserted through the real logging path
    because the bug lived in the handoff, not in either side alone.
    """
    from server.obs.error_signature import signature_of

    captured: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    log = logging.getLogger("server.obs.loop_lag")
    handler = _Sink()
    log.addHandler(handler)
    try:
        for lag in (0.78, 4.22, 1.12, 5.03, 5.21, 3.04, 3.63, 3.13):
            LoopLagWatchdog(interval=1.0, warn_after=0.5,
                            _clock=_FakeClock())._note_lag(lag)
    finally:
        log.removeHandler(handler)

    assert len(captured) == 8, f"expected one record per stall, got {len(captured)}"
    sigs = [signature_of(r, source="daemon", host="rocky") for r in captured]
    assert sigs[0].event == "loop_lag.stall"
    assert len({s.fingerprint for s in sigs}) == 1, "one stall split into many issues"
    # The measurement is still reported — it moved from the identity to the title.
    assert "lag_s" in sigs[0].sample_message


# --- routine overshoot is INFO, a real stall is WARNING (#1514) --------------
# WARNING is the auto-error filing decision. The daemon overshoots by ~1s many
# times a day under normal load; filing each as an issue (#1514 and ~30 siblings)
# produced tickets with no stack to act on. Only a stall past ``alert_after`` —
# by default the stack-dump threshold, so the filed warning has a dump beside it
# — is a WARNING. Routine overshoot is still reported, at INFO.

class _LevelLog(_CapturingLog):
    def __init__(self) -> None:
        super().__init__()
        self.levels: list[int] = []

    def log(self, level, msg, *args, extra=None, **kwargs) -> None:
        self.levels.append(level)
        super().log(level, msg, *args, extra=extra, **kwargs)


def test_routine_overshoot_logs_at_info_not_warning():
    log = _LevelLog()
    wd = LoopLagWatchdog(interval=1.0, warn_after=0.5, alert_after=5.0,
                         _log=log, _clock=_FakeClock())
    wd._note_lag(1.02)
    assert log.levels == [logging.INFO]
    assert log.events[0][0] == "loop_lag.stall"


def test_a_stall_past_alert_after_is_a_warning():
    log = _LevelLog()
    wd = LoopLagWatchdog(interval=1.0, warn_after=0.5, alert_after=5.0,
                         _log=log, _clock=_FakeClock())
    wd._note_lag(5.2)
    assert log.levels == [logging.WARNING]


def test_routine_overshoot_does_not_swallow_a_following_real_stall():
    """An INFO line must not spend the WARNING's throttle window."""
    log = _LevelLog()
    clock = _FakeClock()
    wd = LoopLagWatchdog(interval=1.0, warn_after=0.5, alert_after=5.0,
                         warn_every=60.0, _log=log, _clock=clock)
    wd._note_lag(1.0)
    clock.advance(2.0)
    wd._note_lag(0.9)            # suppressed INFO
    clock.advance(2.0)
    wd._note_lag(30.0)           # inside the INFO window, but a real stall
    assert log.levels == [logging.INFO, logging.WARNING]
    # The warning reports only its own class; routine ticks don't inflate it.
    assert log.fields[1]["suppressed"] == 0


def test_without_alert_after_every_reported_stall_is_a_warning():
    log = _LevelLog()
    wd = LoopLagWatchdog(interval=1.0, warn_after=0.5, _log=log, _clock=_FakeClock())
    wd._note_lag(0.9)
    assert log.levels == [logging.WARNING]


# --- a host-wide freeze is not the process's fault (#2866) --------------------
# When the host thrashes memory, edge and daemon stall at the same instants and
# no stack dump names a culprit. The kernel's PSI ``full`` counters measure that
# freeze directly; a stall they explain is INFO, one they don't stays a WARNING.

class _FakePsi:
    """Cumulative full-stall seconds per ``<scope>_<resource>``, advanced by the test."""

    def __init__(self, cgroup: bool = True) -> None:
        self.totals: dict[str, float] | None = {"host_memory": 100.0, "host_io": 50.0}
        if cgroup:
            self.totals |= {"cgroup_memory": 20.0, "cgroup_io": 10.0}
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return None if self.totals is None else dict(self.totals)

    def stall(self, memory: float = 0.0, io: float = 0.0, *,
              cgroup_memory: float = 0.0, cgroup_io: float = 0.0) -> None:
        assert self.totals is not None
        self.totals["host_memory"] += memory
        self.totals["host_io"] += io
        if "cgroup_memory" in self.totals:
            self.totals["cgroup_memory"] += cgroup_memory
            self.totals["cgroup_io"] += cgroup_io


def _alerting_watchdog(log, psi):
    return LoopLagWatchdog(interval=1.0, warn_after=0.5, alert_after=5.0,
                           _log=log, _clock=_FakeClock(), _psi=psi)


def test_a_stall_the_host_pressure_explains_is_info():
    log = _LevelLog()
    psi = _FakePsi()
    wd = _alerting_watchdog(log, psi)
    before = psi()
    psi.stall(memory=5.0, io=4.1)
    wd._note_lag(6.88, before, psi())

    assert log.levels == [logging.INFO]
    event, fields = log.events[0]
    assert event == "loop_lag.host_stall"
    assert fields["host_memory_full_s"] == pytest.approx(5.0)
    assert fields["host_io_full_s"] == pytest.approx(4.1)
    assert fields["lag_s"] == pytest.approx(6.88)


def test_a_stall_the_host_pressure_does_not_explain_stays_a_warning():
    log = _LevelLog()
    psi = _FakePsi()
    wd = _alerting_watchdog(log, psi)
    before = psi()
    psi.stall(memory=0.4, io=0.2)       # a little reclaim, nowhere near the lag
    wd._note_lag(6.0, before, psi())

    assert log.levels == [logging.WARNING]
    event, fields = log.events[0]
    assert event == "loop_lag.stall"
    # #2882: the WARNING shows how much of the lag pressure did explain.
    assert fields["host_memory_full_s"] == pytest.approx(0.4)
    assert fields["host_io_full_s"] == pytest.approx(0.2)
    assert fields["cgroup_memory_full_s"] == pytest.approx(0.0)
    assert fields["cgroup_io_full_s"] == pytest.approx(0.0)


def test_psi_fields_on_a_warning_do_not_split_its_fingerprint():
    """#2882 put pressure fields on the WARNING; they must stay out of identity."""
    from server.obs.error_signature import signature_of

    captured: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    log = logging.getLogger("server.obs.loop_lag")
    handler = _Sink()
    log.addHandler(handler)
    try:
        LoopLagWatchdog(interval=1.0, alert_after=5.0, _clock=_FakeClock(),
                        _psi=_FakePsi(cgroup=False))._note_lag(6.0, None)
        psi = _FakePsi()
        before = psi()
        psi.stall(memory=0.4, cgroup_memory=1.3)
        LoopLagWatchdog(interval=1.0, alert_after=5.0, _clock=_FakeClock(),
                        _psi=psi)._note_lag(7.2, before, psi())
    finally:
        log.removeHandler(handler)

    assert [r.levelno for r in captured] == [logging.WARNING, logging.WARNING]
    sigs = [signature_of(r, source="edge", host="dobby") for r in captured]
    assert sigs[0].fingerprint == sigs[1].fingerprint


# --- a partly thrashing host: this unit froze, the host did not (#2882) -------
# Host ``full`` needs every task on the host stalled. When only the edge faults
# on its own swapped-out pages, the daemon keeps running and host ``full`` stays
# low; the edge's own cgroup ``full`` is what measures that freeze.

def test_a_stall_the_cgroup_pressure_explains_is_info():
    log = _LevelLog()
    psi = _FakePsi()
    wd = _alerting_watchdog(log, psi)
    before = psi()
    psi.stall(memory=0.3, cgroup_memory=6.1, cgroup_io=2.0)
    wd._note_lag(6.94, before, psi())

    assert log.levels == [logging.INFO]
    event, fields = log.events[0]
    assert event == "loop_lag.host_stall"
    assert fields["cgroup_memory_full_s"] == pytest.approx(6.1)
    assert fields["cgroup_io_full_s"] == pytest.approx(2.0)
    assert fields["host_memory_full_s"] == pytest.approx(0.3)


def test_without_a_cgroup_counter_the_host_alone_decides():
    log = _LevelLog()
    psi = _FakePsi(cgroup=False)
    wd = _alerting_watchdog(log, psi)
    before = psi()
    psi.stall(memory=0.4)
    wd._note_lag(6.0, before, psi())
    before = psi()
    psi.stall(memory=5.0)
    wd._note_lag(6.0, before, psi())

    assert log.levels == [logging.WARNING, logging.INFO]
    assert not any(k.startswith("cgroup_") for _, f in log.events for k in f)


def test_unavailable_psi_keeps_a_stall_a_warning():
    log = _LevelLog()
    psi = _FakePsi()
    psi.totals = None                    # kernel without PSI
    wd = _alerting_watchdog(log, psi)
    wd._note_lag(6.0, psi(), psi())
    assert log.levels == [logging.WARNING]


def test_a_failing_psi_reader_keeps_a_stall_a_warning():
    def _boom():
        raise PermissionError("no procfs")

    log = _LevelLog()
    wd = _alerting_watchdog(log, _boom)
    assert wd._read_psi() is None
    wd._note_lag(6.0, {"host_memory": 1.0, "host_io": 1.0}, wd._read_psi())
    assert log.levels == [logging.WARNING]


def test_a_host_stall_does_not_spend_the_warning_window():
    log = _LevelLog()
    psi = _FakePsi()
    wd = _alerting_watchdog(log, psi)
    before = psi()
    psi.stall(memory=9.0)
    wd._note_lag(9.5, before, psi())            # host-explained
    now = psi()
    wd._note_lag(9.5, now, now)          # same window, host quiet → a real stall
    assert log.levels == [logging.INFO, logging.WARNING]


@pytest.mark.asyncio
@pytest.mark.slow
async def test_run_snapshots_psi_around_each_tick():
    """End-to-end through run(): the snapshot is taken before the sleep."""
    log = _LevelLog()
    psi = _FakePsi()
    wd = LoopLagWatchdog(interval=0.05, warn_after=0.2, alert_after=0.2,
                         _log=log, _psi=psi)
    task = asyncio.create_task(wd.run())
    # The snapshot is read off-loop (#3130); resume mid-sleep of a tick whose
    # "before" read has landed, so the stall falls inside that tick's window.
    while psi.calls < 1:
        await asyncio.sleep(0.001)
    await asyncio.sleep(0.02)

    psi.stall(memory=1.0)                # the host froze for longer than the block
    time.sleep(0.4)
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert [e for e, _ in log.events] == ["loop_lag.host_stall"]
    assert log.levels == [logging.INFO]


def _psi_tree(tmp_path, cgroup_line="0::/system.slice/viberpit.service\n"):
    proc, cg = tmp_path / "proc", tmp_path / "cgroup"
    (proc / "pressure").mkdir(parents=True)
    (proc / "self").mkdir()
    (proc / "self" / "cgroup").write_text(cgroup_line)
    (proc / "pressure" / "memory").write_text(
        "some avg10=0.00 avg60=0.13 avg300=0.24 total=566117271\n"
        "full avg10=0.00 avg60=0.06 avg300=0.12 total=422753042\n")
    (proc / "pressure" / "io").write_text(
        "some avg10=1.00 avg60=0.00 avg300=0.00 total=9000000\n"
        "full avg10=0.50 avg60=0.00 avg300=0.00 total=2500000\n")
    unit = cg / "system.slice" / "viberpit.service"
    unit.mkdir(parents=True)
    (unit / "memory.pressure").write_text(
        "some avg10=0.00 avg60=0.00 avg300=0.00 total=8000000\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=7250000\n")
    (unit / "io.pressure").write_text(
        "some avg10=0.00 avg60=0.00 avg300=0.00 total=4000000\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=1000000\n")
    return proc, cg, unit


def test_psi_reader_reads_host_and_own_cgroup_full_totals(tmp_path):
    from server.obs.loop_lag import PsiReader

    proc, cg, _ = _psi_tree(tmp_path)
    assert PsiReader(str(proc), str(cg))() == {
        "host_memory": pytest.approx(422.753042), "host_io": pytest.approx(2.5),
        "cgroup_memory": pytest.approx(7.25), "cgroup_io": pytest.approx(1.0)}


def test_psi_reader_falls_back_to_host_only_without_cgroup_files(tmp_path):
    from server.obs.loop_lag import PsiReader

    proc, cg, unit = _psi_tree(tmp_path)
    (unit / "memory.pressure").unlink()
    (unit / "io.pressure").unlink()
    assert set(PsiReader(str(proc), str(cg))()) == {"host_memory", "host_io"}

    # cgroup v1 only: no unified ``0::`` line, so no unit counters at all.
    proc, cg, _ = _psi_tree(tmp_path / "v1", cgroup_line="4:memory:/system.slice/x\n")
    assert set(PsiReader(str(proc), str(cg))()) == {"host_memory", "host_io"}


def test_psi_reader_drops_only_the_unreadable_files(tmp_path):
    from server.obs.loop_lag import PsiReader

    proc, cg, unit = _psi_tree(tmp_path)
    (proc / "pressure" / "io").unlink()
    (unit / "io.pressure").write_text("some avg10=0.00 total=1\n")   # no full line
    assert set(PsiReader(str(proc), str(cg))()) == {"host_memory", "cgroup_memory"}

    for p in (proc / "pressure" / "memory", unit / "memory.pressure"):
        p.unlink()
    assert PsiReader(str(proc), str(cg))() is None


# --- the watchdog must not measure its own PSI reads (#3130) ------------------
# On a swapping host a procfs/kernfs read can block for seconds. Read on the loop
# inside the measured window, it stalled the loop *and* was filed as an
# unexplained stall — and a "before" read that blocked already had the stall in
# its totals, so the delta subtracted it out. The reads run off-loop, and their
# own duration is not lag.

class _SlowPsi(_FakePsi):
    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay

    def __call__(self):
        time.sleep(self.delay)
        return super().__call__()


@pytest.mark.asyncio
@pytest.mark.slow
async def test_a_slow_psi_read_is_not_a_stall():
    log = _LevelLog()
    psi = _SlowPsi(0.3)
    wd = LoopLagWatchdog(interval=0.05, warn_after=0.2, _log=log, _psi=psi)
    task = asyncio.create_task(wd.run())
    loop_ticks = 0
    for _ in range(40):                 # the loop itself stays schedulable
        await asyncio.sleep(0.025)
        loop_ticks += 1
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert psi.calls >= 2, "the watchdog must keep ticking through slow reads"
    assert log.events == [], f"the watchdog filed its own PSI read: {log.events!r}"
    assert wd.peak_lag_s < 0.2
    assert loop_ticks == 40


@pytest.mark.asyncio
@pytest.mark.slow
async def test_a_wedged_psi_read_times_out_and_is_not_requeued():
    import threading

    release = threading.Event()
    calls = []

    def _wedged():
        calls.append(1)
        release.wait(5.0)
        return {"host_memory": 1.0}

    log = _LevelLog()
    wd = LoopLagWatchdog(interval=0.05, warn_after=0.2, psi_timeout=0.05,
                         _log=log, _psi=_wedged)
    task = asyncio.create_task(wd.run())
    await asyncio.sleep(0.5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    release.set()

    assert len(calls) == 1, "a read still stuck in the kernel must not be queued again"
    assert log.events == [], f"a timed-out read was filed as a stall: {log.events!r}"


# --- a WARNING says what the process was doing (#3130) ------------------------
# utime ≈ lag: the loop ran our own code; majflt high: faulting heap back from
# swap (zram swap-in is not memstall, so PSI misses it); nivcsw high with utime
# ≈ 0: descheduled; gc_s: a collector pause.

def test_a_warning_carries_the_ticks_resource_usage():
    log = _LevelLog()
    wd = _alerting_watchdog(log, _FakePsi())
    usage = {"utime_s": 5.91, "stime_s": 0.12, "majflt": 3, "nivcsw": 40, "gc_s": 0.0}
    wd._note_lag(6.0, None, None, usage)

    assert log.levels == [logging.WARNING]
    _, fields = log.events[0]
    assert {k: fields[k] for k in usage} == usage


def test_routine_overshoot_does_not_carry_usage():
    log = _LevelLog()
    wd = _alerting_watchdog(log, _FakePsi())
    wd._note_lag(1.0, None, None, {"utime_s": 1.0, "stime_s": 0.0, "majflt": 0,
                                   "nivcsw": 0, "gc_s": 0.0})
    assert "utime_s" not in log.events[0][1]


def test_usage_fields_do_not_split_the_fingerprint():
    from server.obs.error_signature import signature_of

    captured: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    log = logging.getLogger("server.obs.loop_lag")
    handler = _Sink()
    log.addHandler(handler)
    try:
        for utime, majflt in ((5.9, 0), (0.1, 41234)):
            LoopLagWatchdog(interval=1.0, alert_after=5.0, _clock=_FakeClock(),
                            _psi=lambda: None)._note_lag(
                6.0, None, None,
                {"utime_s": utime, "stime_s": 0.0, "majflt": majflt, "nivcsw": 2, "gc_s": 0.0})
    finally:
        log.removeHandler(handler)

    sigs = [signature_of(r, source="edge", host="dobby") for r in captured]
    assert len(sigs) == 2 and sigs[0].fingerprint == sigs[1].fingerprint


def test_usage_since_diffs_cumulative_counters():
    from server.obs.loop_lag import usage_since

    before = {"utime_s": 1.0, "stime_s": 0.5, "majflt": 10, "nivcsw": 7, "gc_s": 0.25}
    after = {"utime_s": 4.256, "stime_s": 0.5, "majflt": 910, "nivcsw": 9, "gc_s": 1.0}
    assert usage_since(before, after) == {
        "utime_s": 3.26, "stime_s": 0.0, "majflt": 900, "nivcsw": 2, "gc_s": 0.75}


def test_gc_timer_accumulates_collection_time():
    import gc

    from server.obs.loop_lag import GcTimer

    timer = GcTimer()
    timer.install()
    try:
        assert timer.total_s == 0.0
        gc.collect()
        assert timer.total_s > 0.0
    finally:
        timer.remove()
    assert timer._callback not in gc.callbacks
    snapshot = timer.total_s
    gc.collect()
    assert timer.total_s == snapshot, "a removed timer must stop counting"


@pytest.mark.asyncio
@pytest.mark.slow
async def test_run_attaches_usage_to_a_real_stall():
    log = _LevelLog()
    wd = LoopLagWatchdog(interval=0.05, warn_after=0.2, _log=log, _psi=lambda: None)
    task = asyncio.create_task(wd.run())
    await asyncio.sleep(0.07)
    deadline = time.monotonic() + 0.4
    while time.monotonic() < deadline:   # on-CPU block, like a large json.loads
        pass
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert [e for e, _ in log.events] == ["loop_lag.stall"]
    fields = log.events[0][1]
    assert fields["utime_s"] + fields["stime_s"] >= 0.2, fields
    assert {"majflt", "nivcsw", "gc_s"} <= fields.keys()
