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
