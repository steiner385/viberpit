"""Per-session virtual X display management (Xvfb + x11vnc + managed browser)."""
from __future__ import annotations
import asyncio
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from server.sessions.adopt import _proc_lister, terminate_process_tree

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
    debug_port: int | None = None


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
                 vnc_idle_timeout_s: float = 1800.0,
                 spawn=asyncio.create_subprocess_exec, ready_check=None, run_dir: str = "/tmp",
                 ready_timeout_s: float = 2.0, ready_interval_s: float = 0.05) -> None:
        self._allocator = allocator
        self._resolution = resolution
        self._max_browsers = max_browsers
        self._vnc_idle_timeout_s = vnc_idle_timeout_s
        self._spawn = spawn
        self._run_dir = run_dir
        self._ready_check = ready_check or (lambda d: _default_ready_check(d, run_dir))
        self._ready_timeout_s = ready_timeout_s
        self._ready_interval_s = ready_interval_s
        self._specs: dict[str, DisplaySpec] = {}
        self._lock = asyncio.Lock()
        self._idle_tasks: set = set()
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

    async def stop_all(self) -> None:
        """Concurrently tear down every tracked display — the shutdown sweep (issue #206).

        Closing sessions sequentially summed each ~10s ``stop_display`` past systemd's
        ``TimeoutStopSec``, so the process was SIGKILLed before cleanup finished and
        per-session Xvfb/x11vnc/chrome leaked. Stopping all displays concurrently bounds
        total teardown to roughly a single ``stop_display``; ``return_exceptions`` ensures
        one session's failure can't abort the rest.
        """
        sids = list(self._specs.keys())
        if not sids:
            return
        await asyncio.gather(*(self.stop_display(sid) for sid in sids), return_exceptions=True)

    async def ensure_vnc(self, session_id: str) -> int | None:
        async with self._lock:
            spec = self._specs.get(session_id)
            if spec is None:
                return None
            if spec.x11vnc is None:
                spec.x11vnc = await self._spawn(
                    "x11vnc", "-display", f":{spec.lease.display}",
                    "-rfbport", str(spec.lease.rfbport), "-localhost", "-nopw",
                    "-auth", spec.xauthority, "-forever", "-shared", "-quiet",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
            return spec.lease.rfbport

    async def attach(self, session_id: str) -> int | None:
        spec = self._specs.get(session_id)
        if spec is None:
            return None
        port = await self.ensure_vnc(session_id)
        spec.vnc_clients += 1
        return port

    def detach(self, session_id: str) -> None:
        spec = self._specs.get(session_id)
        if spec is None:
            return
        spec.vnc_clients = max(0, spec.vnc_clients - 1)
        if spec.vnc_clients == 0:
            t = asyncio.create_task(self._idle_teardown(session_id))
            self._idle_tasks.add(t)
            t.add_done_callback(self._idle_tasks.discard)

    async def _idle_teardown(self, session_id: str, timeout: float | None = None) -> None:
        await asyncio.sleep(timeout if timeout is not None else self._vnc_idle_timeout_s)
        spec = self._specs.get(session_id)
        if spec and spec.vnc_clients == 0 and spec.x11vnc is not None:
            await _terminate(spec.x11vnc)
            spec.x11vnc = None

    async def launch_browser(
        self,
        session_id: str,
        url: str | None,
        *,
        debug_port: bool = False,
        profile_dir: str | None = None,
    ) -> bool:
        spec = self._specs.get(session_id)
        if spec is None or len(spec.browsers) >= self._max_browsers:
            return False
        argv = ["chromium",
                "--disable-dev-shm-usage", "--no-first-run", "--start-maximized"]
        if debug_port:
            port = spec.lease.rfbport + 1000
            argv.append(f"--remote-debugging-port={port}")
            argv.append("--remote-allow-origins=*")
            spec.debug_port = port
        if profile_dir is not None:
            argv.append(f"--user-data-dir={profile_dir}")
        if url:
            argv.append(url)
        env = dict(os.environ, DISPLAY=f":{spec.lease.display}", XAUTHORITY=spec.xauthority)
        try:
            proc = await self._spawn(*argv, env=env,
                                     stdout=asyncio.subprocess.DEVNULL,
                                     stderr=asyncio.subprocess.DEVNULL)
        except FileNotFoundError:
            log.exception("x11: chromium missing")
            return False
        spec.browsers.append(proc)
        return True

    def debug_port_for(self, session_id: str) -> int | None:
        spec = self._specs.get(session_id)
        return spec.debug_port if spec is not None else None

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

    def sweep_orphan_processes(
        self,
        *,
        lister=_proc_lister,
        killer=lambda pid: terminate_process_tree(pid, grace=0.5),
    ) -> int:
        """Reap leaked Xvfb/x11vnc/chromium from a prior run (issue #206, startup half).

        A process is an orphan when its X display falls inside this host's managed pool
        (``display_start … display_start+display_count``) yet maps to no live session.
        At process start ``_specs`` is empty, so every in-range display is reaped —
        exactly what a fresh boot needs after a SIGKILLed shutdown left displays behind.
        The managed-range gate keeps the sweep from ever touching a user's own ``:0``
        browser. Counterpart to ``stop_all`` (clean shutdown) and ``sweep_stale`` (which
        only unlinks stale lock/socket *files*, never the processes holding them).

        ``lister``/``killer`` are injected so the sweep is unit-testable without /proc.
        Returns the number of processes actually signalled.
        """
        managed = range(self._display_start, self._display_start + self._display_count)
        live = {s.lease.display for s in self._specs.values()}
        killed = 0
        for proc in lister():
            cmd = getattr(proc, "cmdline", None) or []
            if not cmd:
                continue
            exe = os.path.basename(cmd[0])
            if not (exe == "Xvfb" or exe == "x11vnc" or exe.startswith("chromium")):
                continue
            display = self._display_of(proc)
            if display is None or display not in managed or display in live:
                continue
            try:
                if killer(proc.pid):
                    killed += 1
            except Exception:
                log.exception("x11: orphan reap failed pid=%s", getattr(proc, "pid", "?"))
        return killed

    @staticmethod
    def _display_of(proc) -> int | None:
        """Display number a process is bound to, parsed from argv (Xvfb/x11vnc) or the
        ``DISPLAY`` env (chromium). Returns the int N from a ``:N`` / ``:N.screen`` token."""
        cmd = getattr(proc, "cmdline", None) or []
        exe = os.path.basename(cmd[0]) if cmd else ""
        if exe == "Xvfb":
            for tok in cmd[1:]:
                n = X11Manager._parse_display(tok)
                if n is not None:
                    return n
        elif exe == "x11vnc":
            for i, tok in enumerate(cmd):
                if tok == "-display" and i + 1 < len(cmd):
                    return X11Manager._parse_display(cmd[i + 1])
        return X11Manager._parse_display(getattr(proc, "environ", {}).get("DISPLAY", ""))

    @staticmethod
    def _parse_display(token: str) -> int | None:
        if not token or not token.startswith(":"):
            return None
        try:
            return int(token[1:].split(".", 1)[0])
        except ValueError:
            return None

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


import shutil  # noqa: E402 — kept here so the module stays importable without shutil at top-level scope


def build_x11_manager(settings) -> "X11Manager | None":
    """Factory: return None when disabled or when binaries are missing (fail-loud warning)."""
    if not getattr(settings, "enable_xvfb", False):
        return None
    missing = [b for b in ("Xvfb", "x11vnc", "xauth") if shutil.which(b) is None]
    if missing:
        log.warning(
            "x11: ENABLED but missing binaries %s — feature disabled. "
            "Install: apt install xvfb x11vnc; playwright install --with-deps chromium",
            missing,
        )
        return None
    alloc = DisplayAllocator(settings.display_start, settings.display_count, settings.vnc_port_base)
    return X11Manager(
        alloc,
        resolution=settings.xvfb_resolution,
        max_browsers=settings.max_browsers,
        vnc_idle_timeout_s=settings.vnc_idle_timeout_s,
    )
