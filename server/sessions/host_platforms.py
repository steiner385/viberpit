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

from viberpit.engines import AgentEngine

from server.aio import cancel_and_reap

__all__ = [
    "PLATFORMS_INFO_KEY",
    "KNOWN_PLATFORMS",
    "PlatformDispatchRefused",
    "PlatformProbe",
    "configured_platforms",
    "build_platforms_info",
    "parse_platforms_info",
    "platform_dispatchable",
    "platform_launch_engine",
    "platform_routing_enabled",
    "require_platform_routing",
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


class PlatformDispatchRefused(RuntimeError):
    """A platform-gated launch that must not happen, raised before anything spawns.

    Gated work runs only where the platform is (anti-goal: an ``env: windows``
    session on a host that cannot reach the VM), so every path that cannot
    guarantee that refuses instead of falling back to a local or Codex spawn."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def platform_routing_enabled() -> bool:
    """Can gated work be placed on a platform host at all? Only the routed automation
    path (``VIBERPIT_AUTOMATION_HOST_ROUTING_ENABLED``) reaches the edge's router, so
    with it off no non-Linux platform is reachable. THE one read of that answer for
    the queue (``fleet_platform_state``) and the spawn (``require_platform_routing``),
    so a gated issue the spawn would refuse is never offered by the queue."""
    from server.config import get_settings
    return bool(get_settings().automation_host_routing_enabled)


def require_platform_routing(platform: str) -> None:
    """``PlatformDispatchRefused`` when a launch gated to *platform* has no routed path."""
    if not platform_routing_enabled():
        raise PlatformDispatchRefused(
            f"{platform} work needs automation host routing "
            "(VIBERPIT_AUTOMATION_HOST_ROUTING_ENABLED), which is off; "
            "it is never spawned on this host instead")


def platform_launch_engine(platform: str, engine: AgentEngine) -> AgentEngine:
    """The engine a launch gated to *platform* runs on, or ``PlatformDispatchRefused``.

    Only the routed path can place a session on a platform host, so with automation
    host routing off there is nowhere safe to spawn (``require_platform_routing``).
    And platform work is Claude-only (design call 5): Codex's workspace-write sandbox
    has no egress to reach the VM, so a Codex-pinned origin is refused rather than
    given a sandbox exception."""
    require_platform_routing(platform)
    if engine is AgentEngine.CODEX:
        raise PlatformDispatchRefused(
            f"{platform} work runs on Claude only, and this origin's engine is codex")
    return AgentEngine.CLAUDE_CODE


async def ssh_vm_check(host: str) -> bool:
    """True when the Windows VM at ssh alias *host* accepts a non-interactive login."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "exit",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError) as exc:
        log.debug("windows vm check: cannot start ssh: %s", exc)
        return False
    try:
        return await asyncio.wait_for(proc.wait(), _CHECK_TIMEOUT_S) == 0
    except asyncio.TimeoutError:
        log.debug("windows vm check: ssh to %s timed out", host)
        return False
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


class PlatformProbe:
    """Filters configured platforms down to those that are actually reachable.

    Only ``windows`` has a reachability check (the VM); ``linux`` is the runner
    itself and ``macos`` passes through unchecked.

    :meth:`advertised` NEVER awaits the check: it answers from the cached verdict
    at once and, when that verdict is stale or absent, starts one background
    refresh. Until a first verdict lands ``windows`` is not advertised, so work is
    never routed to a VM nobody has confirmed. The ``/info`` poll therefore cannot
    be stalled by a down or hung VM.
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
        self._refresh: asyncio.Task[None] | None = None
        self._verdict: bool | None = None
        self._checked_at = 0.0

    def _fresh(self) -> bool:
        return self._verdict is not None and self._clock() - self._checked_at < self._ttl_s

    async def _run_check(self) -> None:
        try:
            verdict = bool(await self._check("windows"))
        except Exception:
            log.debug("windows vm check raised", exc_info=True)
            verdict = False
        self._verdict = verdict
        self._checked_at = self._clock()

    async def advertised(self, configured: frozenset[str]) -> frozenset[str]:
        out = set(configured) | _LINUX
        if "windows" in out:
            if not self._fresh() and (self._refresh is None or self._refresh.done()):
                self._refresh = asyncio.create_task(
                    self._run_check(), name="host-platforms-vm-check")
            if not self._verdict:
                out.discard("windows")
        return frozenset(out)

    async def aclose(self) -> None:
        """Stop an in-flight refresh (daemon shutdown)."""
        await cancel_and_reap(self._refresh, phase="host-platforms.vm-check")
