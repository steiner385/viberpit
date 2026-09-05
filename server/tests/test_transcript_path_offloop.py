"""#826: resolve_transcript_path must not block the event loop.

The transcript roots on the production host are sshfs mounts backed by another
machine; a stalled mount turns _find_transcript's stat/glob into a multi-second
(observed: 20s) block of the whole daemon loop. The path-resolution work therefore
has to run off the loop via asyncio.to_thread — bounded replay ceilings can't help,
because asyncio.wait_for cancels a coroutine but cannot interrupt synchronous work
already executing on the loop.

Mirrors test_orchestrator_history_offloads_loop.py: assert the loop keeps TICKING
during a deliberately-slow filesystem read, not merely that the return value is right
(a result-only test is exactly what let earlier "offloaded" regressions ship).
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from server.sessions.orchestrator import SessionOrchestrator

BLOCK_S = 0.4


async def _ticker(stop: asyncio.Event, ticks: list[float]) -> None:
    while not stop.is_set():
        ticks.append(time.monotonic())
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_resolve_transcript_path_keeps_the_event_loop_responsive():
    """The synchronous _find_transcript (glob/stat over sshfs) is pushed to a worker
    thread, so even a wedged mount can't starve the loop."""
    orch = object.__new__(SessionOrchestrator)  # skip heavy __init__
    orch._drivers = {"s1": SimpleNamespace(config_dir="/cfg", engine_session_id="csid")}
    orch._records = {"s1": SimpleNamespace(cwd=None)}

    def _slow_find(config_dir, csid, cwd=None):
        time.sleep(BLOCK_S)  # stand-in for a stalled sshfs stat/glob
        return "/found/transcript.jsonl"

    orch._find_transcript = _slow_find  # type: ignore[method-assign]

    stop = asyncio.Event()
    ticks: list[float] = []
    ticker = asyncio.create_task(_ticker(stop, ticks))

    started = time.monotonic()
    path = await orch.resolve_transcript_path("s1")
    elapsed = time.monotonic() - started

    stop.set()
    await ticker

    assert path == "/found/transcript.jsonl"
    assert elapsed >= BLOCK_S, "the stub must actually block, else the test proves nothing"
    # Offloaded: ~elapsed/0.01 ticks. Run inline on the loop: the ticker freezes, ~1 tick.
    assert len(ticks) > (elapsed / 0.02), (
        f"event loop was starved: only {len(ticks)} ticks in {elapsed:.2f}s"
    )
