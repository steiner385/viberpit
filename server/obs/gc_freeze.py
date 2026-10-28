"""Freeze the long-lived startup heap out of the cyclic GC (#3150 cause C).

A gen-2 collection walks every tracked object. On a host under memory pressure
most of the edge heap sits in swap, so each full collection faults it back in:
``gc_s`` ≈ ``lag_s`` with 10k-23k major faults stalled the loop 8-20 s (dobby,
2026-10-23..27). Nearly all of that heap is built at startup — routers, models,
the import graph — and lives for the whole process, so walking it is pure cost.

``gc.freeze()`` moves every currently tracked object to the permanent
generation, which collections skip. It runs once, after a delay long enough for
startup and the background warm-ups (STT model, model catalog) to finish, and
after a ``gc.collect()`` so garbage cycles alive at that moment are reclaimed
rather than frozen forever.

``VIBERPIT_GC_FREEZE_DELAY_S`` sets the delay; ``0`` disables the freeze.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import time

from server.config import env_float

log = logging.getLogger("server.obs.gc_freeze")

DEFAULT_GC_FREEZE_DELAY_S = 120.0

_frozen = False


def freeze_startup_heap() -> bool:
    """Collect, then freeze the surviving heap. Once per process; False on repeat."""
    global _frozen
    if _frozen:
        return False
    _frozen = True
    t0 = time.monotonic()
    gc.collect()
    gc.freeze()
    log.info("gc.startup_heap_frozen", extra={
        "frozen_objects": gc.get_freeze_count(),
        "duration_s": round(time.monotonic() - t0, 3),
    })
    return True


async def freeze_after_startup(delay_s: float | None = None) -> None:
    """Sleep ``delay_s`` (default: the env knob), then ``freeze_startup_heap()``."""
    if delay_s is None:
        delay_s = env_float("VIBERPIT_GC_FREEZE_DELAY_S", DEFAULT_GC_FREEZE_DELAY_S,
                            minimum=0.0)
    if delay_s <= 0:
        return
    await asyncio.sleep(delay_s)
    freeze_startup_heap()
