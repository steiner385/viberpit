"""Per-account GitHub Copilot translating-proxy supervision.

Modeled 1:1 on server/sessions/x11manager.py: one supervised child process per copilot
account, resolved via shutil.which/vendored path, health-gated before use, liveness-graced,
idle ref-count torn down, and orphan-reaped on startup. One proxy serves all of an account's
live sessions.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets as _secrets
import shutil
from dataclasses import dataclass

from ccswitch import copilot as _copilot
from server.sessions.adopt import _proc_lister, terminate_process_tree

log = logging.getLogger("server.copilot_proxy")

PROXY_EXE = "copilot2api"


def resolve_copilot_proxy() -> str | None:
    """Vendored-path override first, then PATH. None (feature-disabled) when absent."""
    override = os.environ.get("CCSWITCH_COPILOT_PROXY_BIN")
    if override and os.path.exists(override):
        return override
    return shutil.which(PROXY_EXE)


class ProxyPortExhausted(RuntimeError):
    pass


class ProxyPortAllocator:
    """Asyncio-locked pool of loopback ports (mirrors DisplayAllocator)."""

    def __init__(self, start: int, count: int) -> None:
        self._free: list[int] = list(range(start, start + count))
        self._used: set[int] = set()
        self._lock = asyncio.Lock()

    async def alloc(self) -> int:
        async with self._lock:
            if not self._free:
                raise ProxyPortExhausted("copilot proxy port pool exhausted")
            p = self._free.pop(0)
            self._used.add(p)
            return p

    async def release(self, port: int) -> None:
        async with self._lock:
            if port in self._used:
                self._used.discard(port)
                self._free.append(port)
                self._free.sort()


class ProxyStartError(RuntimeError):
    pass


def _default_ready_check(port: int) -> bool:
    """True once GET /usage returns 200 (proxy has bound + authenticated)."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/usage", timeout=1.0) as r:
            return r.status == 200
    except Exception:
        return False


def _default_usage_fetch(port: int) -> dict:
    """Fetch /usage JSON from the proxy port."""
    import json
    import urllib.request
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/usage", timeout=2.0) as r:
        return json.loads(r.read())


@dataclass
class _ProxySpec:
    account: str
    port: int
    proc: object
    sessions: int = 0


async def _terminate(proc, timeout: float = 5.0) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill(); await proc.wait()
    except ProcessLookupError:
        pass
    except Exception:
        log.exception("copilot proxy terminate failed")


class CopilotProxyManager:
    def __init__(self, allocator, *, token_dir_for,
                 spawn=asyncio.create_subprocess_exec, ready_check=None,
                 secret: str | None = None, ready_timeout_s: float = 10.0,
                 ready_interval_s: float = 0.05, liveness_s: float = 0.0,
                 idle_grace_s: float = 300.0, usage_fetch=None) -> None:
        self._alloc = allocator
        self._token_dir_for = token_dir_for
        self._spawn = spawn
        self._ready_check = ready_check or _default_ready_check
        self._secret = secret or _secrets.token_hex(16)
        self._ready_timeout_s = ready_timeout_s
        self._ready_interval_s = ready_interval_s
        self._liveness_s = liveness_s
        self._idle_grace_s = idle_grace_s
        self._usage_fetch = usage_fetch or _default_usage_fetch
        self._specs: dict[str, _ProxySpec] = {}
        self._lock = asyncio.Lock()
        self._idle_tasks: set[asyncio.Task] = set()

    def port_for(self, account: str) -> int | None:
        s = self._specs.get(account)
        return s.port if s else None

    def env_for(self, account: str) -> dict[str, str]:
        s = self._specs.get(account)
        return _copilot.launch_env(s.port, self._secret) if s else {}

    async def ensure(self, account: str) -> int:
        async with self._lock:
            if account in self._specs:
                return self._specs[account].port
            binary = resolve_copilot_proxy()
            if binary is None:
                raise ProxyStartError("copilot2api binary not found")
            port = await self._alloc.alloc()
            try:
                proc = await self._spawn(
                    binary, "-port", str(port), "-token-dir", str(self._token_dir_for(account)),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            except FileNotFoundError as e:
                await self._alloc.release(port)
                raise ProxyStartError(f"spawn failed: {e}") from e
            if not await self._await_alive(proc) or not await self._await_ready(port):
                await _terminate(proc)
                await self._alloc.release(port)
                raise ProxyStartError(f"copilot proxy for {account} never became ready")
            self._specs[account] = _ProxySpec(account=account, port=port, proc=proc)
            return port

    async def _await_alive(self, proc) -> bool:
        if self._liveness_s <= 0:
            return True
        await asyncio.sleep(self._liveness_s)
        return getattr(proc, "returncode", None) is None

    async def _await_ready(self, port: int) -> bool:
        waited = 0.0
        while waited <= self._ready_timeout_s:
            if await asyncio.to_thread(self._ready_check, port):
                return True
            await asyncio.sleep(self._ready_interval_s)
            waited += self._ready_interval_s
        return await asyncio.to_thread(self._ready_check, port)

    def attach(self, account: str) -> None:
        """Increment the session ref-count for the account's proxy."""
        spec = self._specs.get(account)
        if spec is None:
            return
        spec.sessions += 1

    def detach(self, account: str) -> None:
        """Decrement the ref-count; schedule idle teardown when it hits 0."""
        spec = self._specs.get(account)
        if spec is None:
            return
        spec.sessions = max(0, spec.sessions - 1)
        if spec.sessions == 0:
            t = asyncio.create_task(self._idle_teardown(account))
            self._idle_tasks.add(t)
            t.add_done_callback(self._idle_tasks.discard)

    async def _idle_teardown(self, account: str, timeout: float | None = None) -> None:
        await asyncio.sleep(timeout if timeout is not None else self._idle_grace_s)
        spec = self._specs.get(account)
        if spec and spec.sessions == 0:
            await self.stop(account)

    async def stop(self, account: str) -> None:
        """Tear down a single account's proxy, release its port."""
        async with self._lock:
            spec = self._specs.pop(account, None)
        if spec is None:
            return
        await _terminate(spec.proc)
        await self._alloc.release(spec.port)

    async def stop_all(self) -> None:
        """Concurrently tear down every tracked proxy — the shutdown sweep.

        Stopping all proxies concurrently bounds total teardown to roughly a single
        stop(); return_exceptions ensures one account's failure can't abort the rest.
        """
        accounts = list(self._specs.keys())
        if not accounts:
            return
        await asyncio.gather(*(self.stop(acct) for acct in accounts), return_exceptions=True)

    def usage(self, account: str) -> dict | None:
        """Return usage payload from the proxy, or None if unknown/unreachable."""
        spec = self._specs.get(account)
        if spec is None:
            return None
        try:
            return self._usage_fetch(spec.port)
        except Exception:
            return None

    def sweep_orphans(
        self,
        *,
        lister=_proc_lister,
        killer=lambda pid: terminate_process_tree(pid, grace=0.5),
    ) -> int:
        """Reap stray copilot2api processes from a prior run that map to no live spec.

        A process is an orphan when its exe basename == PROXY_EXE yet it maps to no
        live spec. At process start _specs is empty, so every copilot2api process is
        reaped — exactly what a fresh boot needs after a SIGKILLed shutdown.

        lister/killer are injected so the sweep is unit-testable without /proc.
        Returns the number of processes actually signalled.
        """
        live_ports = {s.port for s in self._specs.values()}
        killed = 0
        for proc in lister():
            cmd = getattr(proc, "cmdline", None) or []
            if not cmd:
                continue
            exe = os.path.basename(cmd[0])
            if exe != PROXY_EXE:
                continue
            # Check if this proc's port matches a live spec: parse -port from cmdline
            try:
                port_idx = cmd.index("-port")
                port = int(cmd[port_idx + 1])
            except (ValueError, IndexError):
                port = None
            if port is not None and port in live_ports:
                continue
            try:
                if killer(proc.pid):
                    killed += 1
            except Exception:
                log.exception("copilot: orphan reap failed pid=%s", getattr(proc, "pid", "?"))
        return killed


def build_copilot_proxy_manager(settings) -> "CopilotProxyManager | None":
    """Factory: returns None (with a warning) when copilot is disabled or binary absent."""
    if not getattr(settings, "enable_copilot", False):
        return None
    if resolve_copilot_proxy() is None:
        log.warning(
            "copilot: ENABLED but %s binary not found — feature disabled. "
            "Set CCSWITCH_COPILOT_PROXY_BIN or install the pinned release.",
            PROXY_EXE,
        )
        return None
    from ccswitch import paths
    return CopilotProxyManager(
        ProxyPortAllocator(settings.copilot_port_base, settings.copilot_port_count),
        token_dir_for=paths.copilot_token_dir,
        idle_grace_s=settings.copilot_idle_grace_s,
    )
