"""Async PTY session manager.

Each session owns a child process running in a pseudo-terminal.  The master fd
is drained by an asyncio reader callback that:
  - appends output to a bounded scrollback ring (bytes deque)
  - fans live chunks out to subscriber queues
  - captures EOF / exit and cleans up

Public interface matches the spec in T033 exactly so downstream WS/rotation
tasks can plug in without changes.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import signal
import struct
import termios
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SessionHandle:
    id: str
    pid: int
    cols: int
    rows: int


@dataclass
class _Session:
    """Internal state per spawned session."""
    handle: SessionHandle
    master_fd: int
    pid: int

    # Scrollback ring — stored as a list of chunks; trimmed on each append
    _scrollback: bytearray = field(default_factory=bytearray)
    _scrollback_limit: int = 256_000

    # Live subscribers (WS bridge will consume these)
    _subscribers: list[asyncio.Queue] = field(default_factory=list)

    # Timing
    _last_output_at: float = field(default_factory=time.monotonic)

    # Exit state
    _running: bool = True
    _exit_code: int | None = None

    def append_output(self, data: bytes) -> None:
        self._last_output_at = time.monotonic()
        self._scrollback.extend(data)
        # Trim to limit (keep the tail)
        if len(self._scrollback) > self._scrollback_limit:
            trim = len(self._scrollback) - self._scrollback_limit
            del self._scrollback[:trim]
        for q in list(self._subscribers):
            q.put_nowait(data)

    def mark_exited(self, code: int | None) -> None:
        self._running = False
        self._exit_code = code
        for q in list(self._subscribers):
            q.put_nowait(None)  # sentinel


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class SessionManager:
    def __init__(self, scrollback_bytes: int = 256_000) -> None:
        self._scrollback_bytes = scrollback_bytes
        self._sessions: dict[str, _Session] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def spawn(
        self,
        session_id: str,
        argv: list[str],
        env: dict,
        cwd: str,
        cols: int = 80,
        rows: int = 24,
    ) -> SessionHandle:
        """Fork argv in a PTY; wire async reader; return a SessionHandle."""
        master_fd, slave_fd = os.openpty()

        # Set initial window size on the slave before fork
        _set_winsize(slave_fd, cols, rows)

        pid = os.fork()
        if pid == 0:
            # ---- child ----
            try:
                os.close(master_fd)
                # Become session leader so slave becomes the controlling tty
                os.setsid()
                # Make slave the controlling terminal
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                # Wire stdio to slave
                for fd in (0, 1, 2):
                    os.dup2(slave_fd, fd)
                if slave_fd > 2:
                    os.close(slave_fd)
                os.chdir(cwd)
                os.execvpe(argv[0], argv, env)
            finally:
                os._exit(1)

        # ---- parent ----
        os.close(slave_fd)

        handle = SessionHandle(id=session_id, pid=pid, cols=cols, rows=rows)
        session = _Session(
            handle=handle,
            master_fd=master_fd,
            pid=pid,
            _scrollback_limit=self._scrollback_bytes,
        )
        self._sessions[session_id] = session

        # Register async reader on the master fd
        loop = asyncio.get_running_loop()
        loop.add_reader(master_fd, self._on_master_readable, session_id)

        return handle

    async def write_input(self, session_id: str, data: bytes) -> None:
        """Write raw bytes into the session's PTY master."""
        session = self._get(session_id)
        # os.write on a fd is non-blocking for small writes; wrap in executor
        # for safety on large payloads.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, os.write, session.master_fd, data)

    async def resize(self, session_id: str, cols: int, rows: int) -> None:
        """Send TIOCSWINSZ to the master fd and update the handle."""
        session = self._get(session_id)
        _set_winsize(session.master_fd, cols, rows)
        session.handle.cols = cols
        session.handle.rows = rows

    def scrollback(self, session_id: str) -> bytes:
        """Return the current bounded scrollback ring contents."""
        return bytes(self._get(session_id)._scrollback)

    def last_output_at(self, session_id: str) -> float:
        """Monotonic timestamp of the last PTY output byte."""
        return self._get(session_id)._last_output_at

    def is_idle(self, session_id: str, idle_secs: float) -> bool:
        """True if no output has been received for at least idle_secs."""
        return (time.monotonic() - self.last_output_at(session_id)) >= idle_secs

    def subscribe(self, session_id: str) -> "asyncio.Queue[bytes | None]":
        """Return a queue receiving live output chunks; None sentinel on exit."""
        session = self._get(session_id)
        q: asyncio.Queue[bytes | None] = asyncio.Queue()
        session._subscribers.append(q)
        return q

    def unsubscribe(self, session_id: str, q: "asyncio.Queue") -> None:
        """Remove a subscriber queue."""
        session = self._get(session_id)
        try:
            session._subscribers.remove(q)
        except ValueError:
            pass

    async def terminate(self, session_id: str, timeout: float = 10.0) -> int | None:
        """SIGTERM → await exit up to timeout → SIGKILL; return exit code."""
        session = self._get(session_id)
        if not session._running:
            return session._exit_code

        try:
            os.kill(session.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

        deadline = time.monotonic() + timeout
        while session._running and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

        if session._running:
            try:
                os.kill(session.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # Brief wait for SIGKILL to take effect
            for _ in range(20):
                if not session._running:
                    break
                await asyncio.sleep(0.05)

        return session._exit_code

    def is_running(self, session_id: str) -> bool:
        return self._get(session_id)._running

    def exit_code(self, session_id: str) -> int | None:
        return self._get(session_id)._exit_code

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get(self, session_id: str) -> _Session:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise KeyError(f"No session with id {session_id!r}") from None

    def _on_master_readable(self, session_id: str) -> None:
        """Called by the event loop when the master fd has data (or EOF)."""
        session = self._sessions.get(session_id)
        if session is None:
            return

        try:
            data = os.read(session.master_fd, 4096)
        except OSError:
            # EIO on Linux when the child's slave side is closed (EOF)
            data = b""

        if data:
            session.append_output(data)
        else:
            # EOF — child has exited; clean up
            loop = asyncio.get_event_loop()
            loop.remove_reader(session.master_fd)
            try:
                os.close(session.master_fd)
            except OSError:
                pass

            # Reap the child
            try:
                _, status = os.waitpid(session.pid, 0)
                code = os.waitstatus_to_exitcode(status)
            except ChildProcessError:
                code = None

            session.mark_exited(code)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_winsize(fd: int, cols: int, rows: int) -> None:
    """Apply TIOCSWINSZ to fd."""
    # struct winsize: rows, cols, xpixel, ypixel (all unsigned short)
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass
