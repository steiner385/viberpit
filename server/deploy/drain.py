from __future__ import annotations
import asyncio
import time
from typing import Callable
from server.ipc.client import DaemonError, DaemonUnavailable


async def drain_and_wait(client, *, cap_s: float = 180.0, poll_s: float = 2.0,
                         now: Callable[[], float] = time.monotonic,
                         sleep=asyncio.sleep) -> str:
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
        if now() - start >= cap_s:
            return "cap"
        await sleep(poll_s)
