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

The scan and the session index are injected so the policy is unit-tested without a
real ``/proc``; only :func:`scan_proc` touches the filesystem.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from server.config import env_float
from server.obs.context import emit_event
from server.sessions.adopt import ProcInfo, _read_proc

log = logging.getLogger(__name__)

__all__ = ["ProcRow", "SessionKey", "MemoryWatch", "scan_proc", "memory_watch_from_env"]

_ENABLE_ENV = "VIBERPIT_SESSION_MEMORY_WATCH"
_THRESHOLD_ENV = "VIBERPIT_SESSION_MEMORY_WARN_MB"
_INTERVAL_ENV = "VIBERPIT_SESSION_MEMORY_INTERVAL_S"
DEFAULT_THRESHOLD_MB = 4096.0
DEFAULT_INTERVAL_S = 30.0
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
        for child in children.get(self._root, ()):
            members = _subtree(child.pid, children, by_pid)
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
        return emitted

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await asyncio.to_thread(self.sample)
            except Exception:  # a diagnostic must never take the daemon down
                self._log.debug("session memory sample failed", exc_info=True)


def memory_watch_from_env(sessions: Callable[[], list[SessionKey]]) -> MemoryWatch | None:
    """The production sampler, or ``None`` when ``VIBERPIT_SESSION_MEMORY_WATCH`` is off."""
    if os.environ.get(_ENABLE_ENV, "").strip().lower() in {"0", "false", "no", "off"}:
        return None
    return MemoryWatch(
        sessions,
        threshold_bytes=int(env_float(_THRESHOLD_ENV, DEFAULT_THRESHOLD_MB, minimum=1.0) * 1048576),
        interval=env_float(_INTERVAL_ENV, DEFAULT_INTERVAL_S, minimum=1.0),
    )
