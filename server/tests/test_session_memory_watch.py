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
