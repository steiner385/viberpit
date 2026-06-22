"""Per-session virtual X display management (Xvfb + x11vnc + managed browser)."""
from __future__ import annotations
import asyncio
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from server.sessions.adopt import terminate_process_tree

log = logging.getLogger("server.x11")


class PoolExhausted(RuntimeError):
    """Raised when no display number is available."""


@dataclass(frozen=True)
class DisplayLease:
    display: int
    rfbport: int


class DisplayAllocator:
    """Asyncio-locked pool of (display, rfbport) pairs."""

    def __init__(self, start: int, count: int, port_base: int) -> None:
        self._free: list[int] = list(range(start, start + count))
        self._start = start
        self._port_base = port_base
        self._used: set[int] = set()
        self._lock = asyncio.Lock()

    async def alloc(self) -> DisplayLease:
        async with self._lock:
            if not self._free:
                raise PoolExhausted("display pool exhausted")
            display = self._free.pop(0)
            self._used.add(display)
            return DisplayLease(display=display, rfbport=self._port_base + (display - self._start))

    async def release(self, lease: DisplayLease) -> None:
        async with self._lock:
            if lease.display in self._used:
                self._used.discard(lease.display)
                self._free.append(lease.display)

    def in_use(self) -> int:
        return len(self._used)


def _default_ready_check(display: int, run_dir: str = "/tmp") -> bool:
    return Path(run_dir, ".X11-unix", f"X{display}").exists()


@dataclass
class DisplaySpec:
    session_id: str
    lease: DisplayLease
    xauthority: str
    xvfb: object | None = None
    x11vnc: object | None = None
    browsers: list = field(default_factory=list)   # managed Chromium procs
    vnc_clients: int = 0


async def _terminate(proc, timeout: float = 5.0) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
    except ProcessLookupError:
        pass
    except Exception:
        log.exception("x11: process terminate failed")


class X11Manager:
    def __init__(self, allocator: DisplayAllocator, *, resolution: str, max_browsers: int,
                 spawn=asyncio.create_subprocess_exec, ready_check=None, run_dir: str = "/tmp",
                 ready_timeout_s: float = 2.0, ready_interval_s: float = 0.05) -> None:
        self._allocator = allocator
        self._resolution = resolution
        self._max_browsers = max_browsers
        self._spawn = spawn
        self._run_dir = run_dir
        self._ready_check = ready_check or (lambda d: _default_ready_check(d, run_dir))
        self._ready_timeout_s = ready_timeout_s
        self._ready_interval_s = ready_interval_s
        self._specs: dict[str, DisplaySpec] = {}
        self._lock = asyncio.Lock()
        self._display_start = allocator._start
        self._display_count = len(allocator._free) + allocator.in_use()

    def display_for(self, session_id: str) -> DisplaySpec | None:
        return self._specs.get(session_id)

    def env_for(self, session_id: str) -> dict[str, str]:
        spec = self._specs.get(session_id)
        if not spec:
            return {}
        return {"DISPLAY": f":{spec.lease.display}", "XAUTHORITY": spec.xauthority}

    async def start_display(self, session_id: str) -> DisplaySpec | None:
        async with self._lock:
            if session_id in self._specs:
                return self._specs[session_id]
            try:
                lease = await self._allocator.alloc()
            except PoolExhausted:
                log.warning("x11: pool exhausted, session %s gets no display", session_id)
                return None
            xauth = os.path.join(self._run_dir, f"ccswitch-{session_id}.Xauthority")
            try:
                cookie = secrets.token_hex(16)
                # MIT-MAGIC-COOKIE-1; xauth add tolerates a fresh file
                addp = await self._spawn(
                    "xauth", "-f", xauth, "add", f":{lease.display}", ".", cookie,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await addp.wait()
                xvfb = await self._spawn(
                    "Xvfb", f":{lease.display}", "-screen", "0", self._resolution,
                    "-auth", xauth, "-nolisten", "tcp",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
            except FileNotFoundError:
                log.exception("x11: Xvfb/xauth binary missing")
                await self._allocator.release(lease)
                try:
                    os.unlink(xauth)
                except OSError:
                    pass
                return None
            if not await self._await_ready(lease.display):
                log.warning("x11: display :%d never became ready", lease.display)
                await _terminate(xvfb)
                await self._allocator.release(lease)
                return None
            spec = DisplaySpec(session_id=session_id, lease=lease, xauthority=xauth, xvfb=xvfb)
            self._specs[session_id] = spec
            return spec

    async def _await_ready(self, display: int) -> bool:
        deadline = self._ready_timeout_s
        waited = 0.0
        while waited <= deadline:
            if self._ready_check(display):
                return True
            await asyncio.sleep(self._ready_interval_s)
            waited += self._ready_interval_s
        return self._ready_check(display)

    async def stop_display(self, session_id: str) -> None:
        async with self._lock:
            spec = self._specs.pop(session_id, None)
        if spec is None:
            return
        for b in list(spec.browsers):
            try:
                pid = getattr(b, "pid", None)
                if pid:
                    await asyncio.to_thread(terminate_process_tree, pid)
            except Exception:
                log.exception("x11: browser tree kill failed")
        await _terminate(spec.x11vnc)
        await _terminate(spec.xvfb)
        for p in (spec.xauthority,
                  os.path.join(self._run_dir, f".X{spec.lease.display}-lock"),
                  os.path.join(self._run_dir, ".X11-unix", f"X{spec.lease.display}")):
            try:
                os.unlink(p)
            except OSError:
                pass
        await self._allocator.release(spec.lease)

    def metrics(self) -> dict:
        specs = list(self._specs.values())
        return {
            "displays_allocated": len(specs),
            "xvfb_count": sum(1 for s in specs if s.xvfb),
            "x11vnc_count": sum(1 for s in specs if s.x11vnc),
            "browser_count": sum(len(s.browsers) for s in specs),
            "vnc_clients_attached": sum(s.vnc_clients for s in specs),
            "pool_capacity": self._display_count,
        }

    def sweep_stale(self) -> int:
        removed = 0
        for n in range(self._display_start, self._display_start + self._display_count):
            if n in {s.lease.display for s in self._specs.values()}:
                continue
            lock = os.path.join(self._run_dir, f".X{n}-lock")
            if os.path.exists(lock):
                if not self._lock_owner_alive(lock):
                    for p in (lock, os.path.join(self._run_dir, ".X11-unix", f"X{n}")):
                        try:
                            os.unlink(p)
                        except OSError:
                            pass
                    removed += 1
        return removed

    @staticmethod
    def _lock_owner_alive(lock_path: str) -> bool:
        try:
            pid = int(Path(lock_path).read_text().strip())
        except (OSError, ValueError):
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
