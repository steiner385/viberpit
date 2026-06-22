"""Per-session virtual X display management (Xvfb + x11vnc + managed browser)."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass


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
