"""The daemon names the session whose process subtree is eating memory (#2739)."""
import logging
import os

from server.sessions.adopt import ProcInfo
from server.sessions.memory_watch import (
    MemoryWatch, ProcRow, SessionKey, memory_watch_from_env, scan_proc,
)

MB = 1048576
DAEMON = 100


class _Log:
    def __init__(self) -> None:
        self.records: list[tuple[int, str, dict]] = []

    def log(self, level, msg, *a, extra=None, **k) -> None:
        self.records.append((level, (extra or {}).get("event", msg), dict((extra or {}).get("fields") or {})))

    def debug(self, *a, **k) -> None:  # pragma: no cover
        pass


SESSIONS = [SessionKey("sess-A", "eng-A", "/wt/a"), SessionKey("sess-B", "eng-B", "/wt/b")]


def _rows(a_node_mb=9000):
    return [
        ProcRow(200, DAEMON, 300 * MB, "claude"),      # session A's CLI
        ProcRow(201, 200, a_node_mb * MB, "node"),     # its runaway child
        ProcRow(300, DAEMON, 200 * MB, "claude"),      # session B, healthy
        ProcRow(999, 1, 50000 * MB, "elsewhere"),      # not under the daemon
    ]


def _describe(pid):
    return {
        200: ProcInfo(200, ["claude", "--resume", "eng-A"], {}, "/wt/a", DAEMON),
        300: ProcInfo(300, ["claude"], {}, "/wt/b/sub", DAEMON),
    }.get(pid)


def _watch(rows, log, clock=lambda: 0.0, **kw):
    return MemoryWatch(lambda: SESSIONS, threshold_bytes=4096 * MB, root_pid=DAEMON,
                       scan=lambda: rows() if callable(rows) else rows,
                       describe=_describe, clock=clock, _log=log, **kw)


def test_names_the_session_and_the_biggest_process():
    log = _Log()
    assert _watch(_rows(), log).sample() == 1
    level, event, f = log.records[0]
    assert (level, event) == (logging.WARNING, "session.memory_high")
    assert f["session"] == "sess-A" and f["rootPid"] == 200
    assert f["rssMb"] == 9300 and f["top"][0] == {"pid": 201, "comm": "node", "rssMb": 9000}


def test_quiet_below_threshold_and_ignores_non_descendants():
    log = _Log()
    assert _watch(_rows(a_node_mb=100), log).sample() == 0
    assert log.records == []


def test_matches_by_cwd_when_there_is_no_resume_arg():
    log = _Log()
    rows = [ProcRow(300, DAEMON, 5000 * MB, "claude")]
    _watch(rows, log).sample()
    assert log.records[0][2]["session"] == "sess-B"


def test_unattributable_root_still_logs_with_null_session():
    log = _Log()
    rows = [ProcRow(400, DAEMON, 5000 * MB, "node")]
    _watch(rows, log).sample()
    assert log.records[0][2]["session"] is None and log.records[0][2]["rootPid"] == 400


def test_throttled_until_cooldown_or_regrowth():
    log, t = _Log(), [0.0]
    size = [5000]
    w = _watch(lambda: [ProcRow(200, DAEMON, size[0] * MB, "claude")], log, clock=lambda: t[0])
    assert w.sample() == 1
    t[0] = 60
    assert w.sample() == 0            # inside cooldown, no growth
    size[0] = 8000
    assert w.sample() == 1            # grew >= 1.5x
    t[0] = 1000
    assert w.sample() == 1            # cooldown elapsed


def test_scan_proc_sees_this_process_with_its_rss():
    rows = {r.pid: r for r in scan_proc()}
    me = rows[os.getpid()]
    assert me.ppid == os.getppid() and me.rss_bytes > 0


def test_env_kill_switch(monkeypatch):
    monkeypatch.setenv("VIBERPIT_SESSION_MEMORY_WATCH", "off")
    assert memory_watch_from_env(lambda: []) is None
    monkeypatch.delenv("VIBERPIT_SESSION_MEMORY_WATCH")
    assert memory_watch_from_env(lambda: []) is not None


# --- session subtrees are the preferred global-OOM victim (oom_score_adj) ---

class _Adj:
    """Fake /proc/<pid>/oom_score_adj: a dict, with a call log and failure knobs."""

    def __init__(self, values: dict[int, int], *, fail_write: frozenset[int] = frozenset()) -> None:
        self.values = dict(values)
        self.fail_write = set(fail_write)
        self.reads: list[int] = []
        self.writes: list[tuple[int, int]] = []

    def read(self, pid: int) -> int | None:
        self.reads.append(pid)
        return self.values.get(pid)

    def write(self, pid: int, value: int) -> bool:
        self.writes.append((pid, value))
        if pid in self.fail_write:
            return False
        self.values[pid] = value
        return True


def _adj_watch(rows, adj, log=None, **kw):
    return _watch(rows, log or _Log(), read_oom_adj=adj.read, write_oom_adj=adj.write, **kw)


def test_raises_every_subtree_member_below_target_even_under_threshold():
    adj = _Adj({200: 0, 201: 0, 300: 700, 999: 0, DAEMON: 0})
    _adj_watch(_rows(a_node_mb=100), adj, oom_score_adj=500).sample()
    assert sorted(adj.writes) == [(200, 500), (201, 500)]
    assert adj.values[300] == 700          # already above target: left alone
    assert adj.values[999] == 0            # not under the daemon
    assert adj.values[DAEMON] == 0         # never the daemon itself
    assert DAEMON not in adj.reads and 999 not in adj.reads


def test_marked_pids_are_cached_and_dead_pids_pruned():
    adj = _Adj({200: 0, 201: 0, 300: 0})
    rows = [_rows()]
    w = _adj_watch(lambda: rows[0], adj, oom_score_adj=500)
    w.sample()
    adj.reads.clear()
    w.sample()
    assert adj.reads == []                 # nothing re-read once marked
    rows[0] = [r for r in _rows() if r.pid != 201]
    w.sample()                             # 201 exits -> pruned from the cache
    rows[0] = _rows()                      # pid 201 reused by a new process
    adj.values[201] = 0
    w.sample()
    assert adj.reads == [201] and adj.values[201] == 500


def test_failed_write_is_not_retried_every_interval():
    adj = _Adj({200: 0, 201: 0, 300: 0}, fail_write=frozenset({201}))
    w = _adj_watch(_rows(), adj, oom_score_adj=500)
    w.sample()
    w.sample()
    assert adj.writes.count((201, 500)) == 1


def test_zero_target_disables_marking():
    adj = _Adj({200: 0, 201: 0, 300: 0})
    _adj_watch(_rows(), adj, oom_score_adj=0).sample()
    assert adj.reads == [] and adj.writes == []


def test_marking_failure_cannot_suppress_memory_high():
    def boom(pid):
        raise RuntimeError("proc exploded")
    log = _Log()
    w = _watch(_rows(), log, read_oom_adj=boom, write_oom_adj=lambda p, v: True, oom_score_adj=500)
    assert w.sample() == 1
    assert log.records[0][1] == "session.memory_high"


def test_real_proc_oom_adj_roundtrip_on_own_child():
    import subprocess
    from server.sessions.memory_watch import read_oom_score_adj, write_oom_score_adj
    proc = subprocess.Popen(["sleep", "30"])
    try:
        before = read_oom_score_adj(proc.pid)
        assert before is not None and before < 1000
        assert write_oom_score_adj(proc.pid, before + 1) is True
        assert read_oom_score_adj(proc.pid) == before + 1
    finally:
        proc.kill()
        proc.wait()
    assert read_oom_score_adj(proc.pid) is None          # gone -> None, never raises
    assert write_oom_score_adj(proc.pid, 500) is False


def test_env_target_parsing_and_clamp(monkeypatch):
    monkeypatch.delenv("VIBERPIT_SESSION_MEMORY_WATCH", raising=False)
    monkeypatch.delenv("VIBERPIT_SESSION_OOM_SCORE_ADJ", raising=False)

    def target():
        w = memory_watch_from_env(lambda: [])
        assert w is not None
        return w.oom_score_adj
    assert target() == 500
    monkeypatch.setenv("VIBERPIT_SESSION_OOM_SCORE_ADJ", "0")
    assert target() == 0
    monkeypatch.setenv("VIBERPIT_SESSION_OOM_SCORE_ADJ", "5000")
    assert target() == 1000
    monkeypatch.setenv("VIBERPIT_SESSION_OOM_SCORE_ADJ", "junk")
    assert target() == 500


# --- fast path: mark the daemon's direct children within ~2 s of spawn ---

def _fast_watch(adj, kids, **kw):
    calls: list[int] = []

    def children(pid):
        calls.append(pid)
        return kids() if callable(kids) else kids
    w = _watch([], _Log(), read_oom_adj=adj.read, write_oom_adj=adj.write,
               read_children=children, **kw)
    return w, calls


def test_mark_roots_raises_direct_children_and_caches():
    adj = _Adj({200: 0, 300: 800, DAEMON: 0})
    w, calls = _fast_watch(adj, [200, 300, DAEMON], oom_score_adj=500)
    w.mark_roots()
    assert calls == [DAEMON]                       # asks for the daemon's children
    assert adj.writes == [(200, 500)]              # 300 already higher; daemon never
    assert DAEMON not in adj.reads
    adj.reads.clear()
    w.mark_roots()
    assert adj.reads == []                         # shared _marked cache


def test_mark_roots_and_full_pass_share_the_cache():
    adj = _Adj({200: 0, 201: 0, 300: 0})
    w = _watch(_rows(), _Log(), read_oom_adj=adj.read, write_oom_adj=adj.write,
               read_children=lambda pid: [200, 300], oom_score_adj=500)
    w.mark_roots()
    adj.reads.clear()
    w.sample()
    assert adj.reads == [201]                      # roots already marked by the fast path


def test_mark_roots_noop_without_children_file_or_target():
    adj = _Adj({200: 0})
    w, _ = _fast_watch(adj, None, oom_score_adj=500)
    w.mark_roots()                                 # kernel without CONFIG_PROC_CHILDREN
    assert adj.reads == [] and adj.writes == []
    w, calls = _fast_watch(adj, [200], oom_score_adj=0)
    w.mark_roots()
    assert calls == [] and adj.writes == []


def test_read_proc_children_unions_every_thread():
    """A child spawned from a still-live worker thread is listed only in THAT
    thread's /proc/<pid>/task/<tid>/children — the reader must union all tasks."""
    import subprocess
    import threading
    from server.sessions.memory_watch import read_proc_children
    main = subprocess.Popen(["sleep", "30"])
    spawned: list[subprocess.Popen] = []
    release, ready = threading.Event(), threading.Event()

    def worker():
        spawned.append(subprocess.Popen(["sleep", "30"]))
        ready.set()
        release.wait(10)
    t = threading.Thread(target=worker)
    t.start()
    try:
        assert ready.wait(10)
        kids = read_proc_children(os.getpid())
        assert kids is not None and {main.pid, spawned[0].pid} <= set(kids)
    finally:
        release.set()
        t.join()
        for p in [main, *spawned]:
            p.kill()
            p.wait()
    assert read_proc_children(2 ** 22 + 1) is None  # no such pid → None


def test_run_marks_fresh_roots_on_the_fast_interval():
    import asyncio
    adj = _Adj({200: 0})
    w, _ = _fast_watch(adj, [200], oom_score_adj=500, mark_interval=0.01)
    w.interval = 3600.0                            # the full pass never fires

    async def go():
        task = asyncio.create_task(w.run())
        for _ in range(200):
            if adj.writes:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
    assert adj.writes == [(200, 500)]


def test_env_mark_interval(monkeypatch):
    monkeypatch.delenv("VIBERPIT_SESSION_MEMORY_WATCH", raising=False)
    monkeypatch.delenv("VIBERPIT_SESSION_OOM_MARK_INTERVAL_S", raising=False)
    w = memory_watch_from_env(lambda: [])
    assert w is not None and w.mark_interval == 2.0
    monkeypatch.setenv("VIBERPIT_SESSION_OOM_MARK_INTERVAL_S", "5")
    w = memory_watch_from_env(lambda: [])
    assert w is not None and w.mark_interval == 5.0
