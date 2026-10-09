"""Per-session memory attribution for the daemon cgroup (#2739).

``OOMPolicy=continue`` (#2690) keeps one OOM-killed tool from stopping the whole
daemon, but the kernel's global OOM line only names a bare ``node`` pid inside
``viberpit-daemon.service`` — by the time anyone reads it the process is gone and
nothing says which session spawned it (the 10 GB ``node`` of 2026-10-07 was never
attributable). This sampler closes that gap *before* the kill: every interval it
sums resident memory per direct child of the daemon (each live ``claude`` CLI, plus
its MCP servers, test runners and dev servers beneath it) and, when a subtree
crosses the threshold, logs ``session.memory_high`` naming the session and the
biggest processes in the subtree.

Level is WARNING on purpose — it files an auto-error issue, which is the point of a
runaway child — so it is throttled per root: one record per ``cooldown`` unless the
subtree has grown by ``regrow_factor`` since the last one.

The same pass also makes session subtrees the preferred victim of a *host-wide* OOM:
every member of every direct-child subtree whose ``oom_score_adj`` is below
``VIBERPIT_SESSION_OOM_SCORE_ADJ`` (default 500, ``0`` disables, clamped to the
kernel's 0..1000) is raised to it — every subtree, every sample, independent of the
memory threshold. Without it a session's runaway ``pytest -n``/``dotnet build``/
``node`` sits at 0 while Chrome renderers (300) and desktop apps (200) score higher,
so the kernel kills the desktop instead (dobby, 2026-10-09). Raising is always
permitted for own-uid processes; children inherit the value at fork, so tools spawned
after their CLI root is marked are covered with no further writes. Marked pids are
cached (pruned when they leave the tree) so each interval re-reads only new pids; the
daemon's own pid is never touched, and a marking failure never suppresses the
``session.memory_high`` emission.

Because inheritance happens at fork, the CLI root must be marked *early* — a tool it
spawns inside the 30 s window before the full pass keeps 0. So :meth:`MemoryWatch.run`
also runs a cheap fast path every ``VIBERPIT_SESSION_OOM_MARK_INTERVAL_S`` (default
2 s) that marks only the daemon's *direct* children, read from
``/proc/<daemon>/task/*/children``. That file is per-thread (a child forked from a
worker thread is listed only under that thread), so every task is unioned. The SDK
never hands the driver the CLI pid and the vendor transport is not ours to patch
(constitution I), so polling our own children is the hook. Kernels without
``CONFIG_PROC_CHILDREN`` simply skip the fast path; the full pass still covers them.

The scan, the session index, the children list and the ``oom_score_adj``
reader/writer are injected so the policy is unit-tested without a real ``/proc``;
only :func:`scan_proc`, :func:`read_proc_children`, :func:`read_oom_score_adj` and
:func:`write_oom_score_adj` touch the filesystem.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from server.config import env_float
from server.obs.context import emit_event
from server.sessions.adopt import ProcInfo, _read_proc

log = logging.getLogger(__name__)

__all__ = [
    "ProcRow", "SessionKey", "MemoryWatch", "scan_proc", "memory_watch_from_env",
    "read_oom_score_adj", "write_oom_score_adj", "read_proc_children",
]

_ENABLE_ENV = "VIBERPIT_SESSION_MEMORY_WATCH"
_THRESHOLD_ENV = "VIBERPIT_SESSION_MEMORY_WARN_MB"
_INTERVAL_ENV = "VIBERPIT_SESSION_MEMORY_INTERVAL_S"
DEFAULT_THRESHOLD_MB = 4096.0
DEFAULT_INTERVAL_S = 30.0
_OOM_ADJ_ENV = "VIBERPIT_SESSION_OOM_SCORE_ADJ"
DEFAULT_SESSION_OOM_SCORE_ADJ = 500
_OOM_MARK_INTERVAL_ENV = "VIBERPIT_SESSION_OOM_MARK_INTERVAL_S"
DEFAULT_OOM_MARK_INTERVAL_S = 2.0
_OOM_ADJ_MAX = 1000  # kernel ceiling; negatives need CAP_SYS_RESOURCE, 0 disables
_TOP_N = 3


@dataclass(frozen=True)
class ProcRow:
    pid: int
    ppid: int
    rss_bytes: int
    comm: str


@dataclass(frozen=True)
class SessionKey:
    """What the orchestrator knows about a live session, for matching a process."""
    session_id: str
    engine_session_id: str
    cwd: str


def scan_proc() -> list[ProcRow]:
    """Every process's pid, parent, resident bytes and name, from ``/proc/*/stat``."""
    page = os.sysconf("SC_PAGE_SIZE")
    rows: list[ProcRow] = []
    try:
        names = [n for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        return rows
    for name in names:
        try:
            with open(f"/proc/{name}/stat", encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
            # comm (field 2) is parenthesised and may contain spaces or ')': split
            # around the LAST ')' (same rule as adopt._read_ppid).
            head, _, tail = raw.rpartition(")")
            comm = head.partition("(")[2]
            fields = tail.split()
            # fields[0] = state, [1] = ppid, … [21] = rss (pages)
            rows.append(ProcRow(int(name), int(fields[1]), int(fields[21]) * page, comm))
        except (OSError, ValueError, IndexError):
            continue  # raced an exit, or unreadable — skip, never fatal
    return rows


def read_oom_score_adj(pid: int) -> int | None:
    """``/proc/<pid>/oom_score_adj``, or ``None`` if the process is gone/unreadable."""
    try:
        with open(f"/proc/{pid}/oom_score_adj", encoding="ascii") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def write_oom_score_adj(pid: int, value: int) -> bool:
    """Set ``/proc/<pid>/oom_score_adj``; ``False`` if gone or not permitted (EACCES)."""
    try:
        with open(f"/proc/{pid}/oom_score_adj", "w", encoding="ascii") as fh:
            fh.write(str(value))
        return True
    except OSError:
        return False


def read_proc_children(pid: int) -> list[int] | None:
    """Direct children of *pid*, unioned over ``/proc/<pid>/task/*/children``.

    ``None`` when no children file is readable (pid gone, or a kernel without
    ``CONFIG_PROC_CHILDREN``) so the caller can tell "unsupported" from "no children".
    """
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return None
    kids: set[int] = set()
    readable = False
    for tid in tids:
        try:
            with open(f"/proc/{pid}/task/{tid}/children", encoding="ascii") as fh:
                raw = fh.read()
        except OSError:
            continue  # thread exited mid-walk, or the file does not exist
        readable = True
        kids.update(int(c) for c in raw.split() if c.isdigit())
    return sorted(kids) if readable else None


def _subtree(root: int, children: dict[int, list[ProcRow]], by_pid: dict[int, ProcRow]) -> list[ProcRow]:
    out: list[ProcRow] = []
    stack = [root]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        row = by_pid.get(pid)
        if row is not None:
            out.append(row)
        stack.extend(c.pid for c in children.get(pid, ()))
    return out


def _resume_arg(cmdline: list[str]) -> str | None:
    for i, part in enumerate(cmdline):
        if part == "--resume":
            return cmdline[i + 1] if i + 1 < len(cmdline) else None
        if part.startswith("--resume="):
            return part.split("=", 1)[1]
    return None


def match_session(info: ProcInfo | None, sessions: list[SessionKey]) -> str | None:
    """The session a daemon child belongs to: ``--resume`` id first, then cwd."""
    if info is None:
        return None
    resume = _resume_arg(info.cmdline)
    if resume:
        for s in sessions:
            if s.engine_session_id and s.engine_session_id == resume:
                return s.session_id
    if info.cwd:
        # Longest cwd wins so a nested worktree beats its parent checkout.
        best = None
        for s in sessions:
            if s.cwd and (info.cwd == s.cwd or info.cwd.startswith(s.cwd.rstrip("/") + "/")):
                if best is None or len(s.cwd) > len(best.cwd):
                    best = s
        if best is not None:
            return best.session_id
    return None


class MemoryWatch:
    def __init__(
        self,
        sessions: Callable[[], list[SessionKey]],
        *,
        threshold_bytes: int,
        interval: float = DEFAULT_INTERVAL_S,
        cooldown: float = 600.0,
        regrow_factor: float = 1.5,
        root_pid: int | None = None,
        scan: Callable[[], list[ProcRow]] = scan_proc,
        describe: Callable[[int], ProcInfo | None] = _read_proc,
        clock: Callable[[], float] = time.monotonic,
        oom_score_adj: int = 0,
        read_oom_adj: Callable[[int], int | None] = read_oom_score_adj,
        write_oom_adj: Callable[[int, int], bool] = write_oom_score_adj,
        mark_interval: float = DEFAULT_OOM_MARK_INTERVAL_S,
        read_children: Callable[[int], list[int] | None] = read_proc_children,
        _log: logging.Logger | None = None,
    ) -> None:
        self._sessions = sessions
        self.threshold_bytes = threshold_bytes
        self.interval = interval
        self.cooldown = cooldown
        self.regrow_factor = regrow_factor
        self._root = root_pid if root_pid is not None else os.getpid()
        self._scan = scan
        self._describe = describe
        self._clock = clock
        self._log = _log if _log is not None else log
        self._last: dict[int, tuple[float, int]] = {}  # root pid -> (when, bytes logged)
        # 0 (the constructor default) disables marking; memory_watch_from_env passes
        # the env target, so only the daemon's sampler ever writes to real /proc.
        self.oom_score_adj = max(0, min(_OOM_ADJ_MAX, int(oom_score_adj)))
        self._read_oom_adj = read_oom_adj
        self._write_oom_adj = write_oom_adj
        self.mark_interval = mark_interval
        self._read_children = read_children
        # pids already at/above target or attempted once (a failed write is not
        # retried every interval); pruned to the live subtree members each sample.
        # Shared by the full pass and the fast path, which run in different
        # to_thread workers — hence the lock.
        self._marked: set[int] = set()
        self._marked_lock = threading.Lock()

    def _mark_pid(self, pid: int) -> None:
        """Raise one pid's ``oom_score_adj`` to the target (never lower it). Lock held."""
        if pid == self._root or pid in self._marked:
            return
        current = self._read_oom_adj(pid)
        if current is not None and current < self.oom_score_adj:
            self._write_oom_adj(pid, self.oom_score_adj)
        self._marked.add(pid)

    def _mark(self, members: list[ProcRow], seen: set[int]) -> None:
        with self._marked_lock:
            for m in members:
                if m.pid != self._root:
                    seen.add(m.pid)
                self._mark_pid(m.pid)

    def mark_roots(self) -> bool:
        """Fast path: mark only the daemon's direct children (fresh CLI roots).

        Returns ``False`` when the children list is unavailable, so :meth:`run` can
        stop polling on a kernel without ``CONFIG_PROC_CHILDREN``.
        """
        if self.oom_score_adj <= 0:
            return False
        kids = self._read_children(self._root)
        if kids is None:
            return False
        with self._marked_lock:
            for pid in kids:
                self._mark_pid(pid)
        return True

    def sample(self) -> int:
        """One pass; returns how many ``session.memory_high`` records were emitted."""
        rows = self._scan()
        by_pid = {r.pid: r for r in rows}
        children: dict[int, list[ProcRow]] = {}
        for r in rows:
            children.setdefault(r.ppid, []).append(r)
        now = self._clock()
        emitted = 0
        live = set()
        marking = self.oom_score_adj > 0
        members_seen: set[int] = set()
        for child in children.get(self._root, ()):
            members = _subtree(child.pid, children, by_pid)
            if marking:
                try:
                    self._mark(members, members_seen)
                except Exception:  # never let marking cost the memory_high record
                    marking = False
                    self._log.debug("session oom_score_adj marking failed", exc_info=True)
            total = sum(m.rss_bytes for m in members)
            live.add(child.pid)
            if total < self.threshold_bytes:
                self._last.pop(child.pid, None)
                continue
            prev = self._last.get(child.pid)
            if prev is not None and now - prev[0] < self.cooldown \
                    and total < prev[1] * self.regrow_factor:
                continue
            self._last[child.pid] = (now, total)
            top = sorted(members, key=lambda m: m.rss_bytes, reverse=True)[:_TOP_N]
            info = self._describe(child.pid)
            emit_event(self._log, logging.WARNING, "session.memory_high", {
                "session": match_session(info, self._sessions()),
                "rootPid": child.pid,
                "rootComm": child.comm,
                "cwd": info.cwd if info else None,
                "rssMb": round(total / 1048576),
                "thresholdMb": round(self.threshold_bytes / 1048576),
                "processes": len(members),
                "top": [{"pid": m.pid, "comm": m.comm, "rssMb": round(m.rss_bytes / 1048576)}
                        for m in top],
            })
            emitted += 1
        for gone in set(self._last) - live:
            del self._last[gone]
        if marking:
            with self._marked_lock:
                self._marked &= members_seen
        return emitted

    async def run(self) -> None:
        """The full sample loop, plus the fast root-marking loop when marking is on."""
        if self.oom_score_adj > 0:
            await asyncio.gather(self._sample_loop(), self._mark_loop())
        else:
            await self._sample_loop()

    async def _sample_loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await asyncio.to_thread(self.sample)
            except Exception:  # a diagnostic must never take the daemon down
                self._log.debug("session memory sample failed", exc_info=True)

    async def _mark_loop(self) -> None:
        while True:
            try:
                if not await asyncio.to_thread(self.mark_roots):
                    self._log.info("session oom fast path off: no /proc children list; "
                                   "the %.0fs full pass still marks", self.interval)
                    return
            except Exception:  # never fatal; the full pass is the catch-all
                self._log.debug("session oom root marking failed", exc_info=True)
            await asyncio.sleep(self.mark_interval)


def memory_watch_from_env(sessions: Callable[[], list[SessionKey]]) -> MemoryWatch | None:
    """The production sampler, or ``None`` when ``VIBERPIT_SESSION_MEMORY_WATCH`` is off."""
    if os.environ.get(_ENABLE_ENV, "").strip().lower() in {"0", "false", "no", "off"}:
        return None
    return MemoryWatch(
        sessions,
        threshold_bytes=int(env_float(_THRESHOLD_ENV, DEFAULT_THRESHOLD_MB, minimum=1.0) * 1048576),
        interval=env_float(_INTERVAL_ENV, DEFAULT_INTERVAL_S, minimum=1.0),
        # Over-range values clamp to 1000 in the constructor; junk/negative -> default.
        oom_score_adj=int(env_float(_OOM_ADJ_ENV, DEFAULT_SESSION_OOM_SCORE_ADJ, minimum=0.0)),
        mark_interval=env_float(_OOM_MARK_INTERVAL_ENV, DEFAULT_OOM_MARK_INTERVAL_S, minimum=0.5),
    )
