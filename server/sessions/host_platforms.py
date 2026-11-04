"""What host platforms a runner advertises, and whether the edge may dispatch to it.

Mirrors :mod:`server.sessions.host_engines`: one module owns the ``/info`` key, the
daemon-side build, the edge-side parse and the single dispatch verdict, so they
cannot disagree.

``linux`` is always implied. A runner that hosts a Windows VM lists ``windows`` in
``VIBERPIT_HOST_PLATFORMS``; it is only *advertised* while the VM answers an ssh
probe, so work tagged ``env: windows`` is never sent to a runner whose VM is down.
An absent ``platforms`` key (a daemon predating this change) means Linux only.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Final

__all__ = [
    "PLATFORMS_INFO_KEY",
    "KNOWN_PLATFORMS",
    "PlatformProbe",
    "configured_platforms",
    "build_platforms_info",
    "parse_platforms_info",
    "platform_dispatchable",
    "ssh_vm_check",
]

log = logging.getLogger(__name__)

#: The ``/info`` key. Named once so the daemon writer and the edge reader cannot drift.
PLATFORMS_INFO_KEY: Final = "platforms"

KNOWN_PLATFORMS: Final[frozenset[str]] = frozenset({"linux", "windows", "macos"})

_LINUX: Final = frozenset({"linux"})
_CHECK_TIMEOUT_S: Final = 10.0
_warned_unknown: set[str] = set()


def configured_platforms(raw: str | None) -> frozenset[str]:
    """Parse ``VIBERPIT_HOST_PLATFORMS`` (comma list). Always includes ``linux``."""
    out = set(_LINUX)
    for part in (raw or "").split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name in KNOWN_PLATFORMS:
            out.add(name)
        elif name not in _warned_unknown:
            _warned_unknown.add(name)
            log.warning("ignoring unknown host platform %r in VIBERPIT_HOST_PLATFORMS", name)
    return frozenset(out)


def build_platforms_info(advertised: frozenset[str]) -> list[str]:
    """The ``/info.platforms`` payload (sorted, so it is stable across polls)."""
    return sorted(advertised)


def parse_platforms_info(value: object) -> frozenset[str]:
    """Read ``/info.platforms``. Absent, malformed or empty means Linux only."""
    if not isinstance(value, list) or not value:
        return _LINUX
    return _LINUX | {v for v in value if isinstance(v, str) and v in KNOWN_PLATFORMS}


def platform_dispatchable(platforms: frozenset[str], platform: str | None) -> bool:
    """May work requiring *platform* (``None`` = no requirement) go to this runner?"""
    return platform is None or platform in platforms


async def ssh_vm_check(host: str) -> bool:
    """True when the Windows VM at ssh alias *host* accepts a non-interactive login."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "exit",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        return False
    try:
        return await asyncio.wait_for(proc.wait(), _CHECK_TIMEOUT_S) == 0
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False
    except Exception:
        return False


class PlatformProbe:
    """Filters configured platforms down to those that are actually reachable.

    Only ``windows`` has a reachability check (the VM); ``linux`` is the runner
    itself and ``macos`` passes through unchecked. Verdicts are cached for
    *ttl_s*; a lock lets one caller refresh a stale verdict while others wait for
    it, so ``/info`` never fans out into concurrent ssh processes.
    """

    def __init__(
        self,
        *,
        check: Callable[[str], Awaitable[bool]],
        ttl_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._check = check
        self._ttl_s = ttl_s
        self._clock = clock
        self._lock = asyncio.Lock()
        self._verdict: bool | None = None
        self._checked_at = 0.0

    def _fresh(self) -> bool:
        return self._verdict is not None and self._clock() - self._checked_at < self._ttl_s

    async def advertised(self, configured: frozenset[str]) -> frozenset[str]:
        out = set(configured) | _LINUX
        if "windows" in out:
            if not self._fresh():
                async with self._lock:
                    if not self._fresh():
                        try:
                            self._verdict = bool(await self._check("windows"))
                        except Exception:
                            self._verdict = False
                        self._checked_at = self._clock()
            if not self._verdict:
                out.discard("windows")
        return frozenset(out)
