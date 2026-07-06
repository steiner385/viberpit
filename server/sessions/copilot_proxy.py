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
import shutil

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


import secrets as _secrets
from dataclasses import dataclass

from ccswitch import copilot as _copilot


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
                 ready_interval_s: float = 0.05, liveness_s: float = 0.0) -> None:
        self._alloc = allocator
        self._token_dir_for = token_dir_for
        self._spawn = spawn
        self._ready_check = ready_check or _default_ready_check
        self._secret = secret or _secrets.token_hex(16)
        self._ready_timeout_s = ready_timeout_s
        self._ready_interval_s = ready_interval_s
        self._liveness_s = liveness_s
        self._specs: dict[str, _ProxySpec] = {}
        self._lock = asyncio.Lock()

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
