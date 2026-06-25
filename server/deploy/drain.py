from __future__ import annotations
import asyncio
import os
import time
from typing import Callable
from server.ipc.client import DaemonError, DaemonUnavailable


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


async def drain_and_wait(client, *, turn_cap_s: float | None = None,
                         condense_cap_s: float | None = None, poll_s: float = 2.0,
                         now: Callable[[], float] = time.monotonic,
                         sleep=asyncio.sleep) -> str:
    """Ask the daemon to drain, then wait until it's safe to restart.

    A session mid-compaction ('condensing') is uninterruptible and loses data if
    killed mid-write, so it is exempt from ``turn_cap_s`` and instead protected up
    to the longer ``condense_cap_s`` ceiling. Ordinary in-flight turns are still
    capped at ``turn_cap_s``. Returns 'ready' (safe), 'cap' (gave up — restart
    anyway), or 'unavailable' (daemon unreachable).
    """
    turn_cap_s = turn_cap_s if turn_cap_s is not None else _env_float("CCSWITCH_DRAIN_TURN_CAP_SEC", 180.0)
    condense_cap_s = condense_cap_s if condense_cap_s is not None else _env_float("CCSWITCH_DRAIN_CONDENSE_CAP_SEC", 570.0)
    try:
        await client.call("begin_drain")
    except (DaemonUnavailable, DaemonError):
        return "unavailable"
    start = now()
    while True:
        try:
            status = await client.call("drain_status")
        except (DaemonUnavailable, DaemonError):
            return "unavailable"
        if status.get("state") == "ready":
            return "ready"
        elapsed = now() - start
        ceiling = condense_cap_s if status.get("condensing", 0) > 0 else turn_cap_s
        if elapsed >= ceiling:
            return "cap"
        await sleep(poll_s)
