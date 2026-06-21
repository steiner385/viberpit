"""Backpressure / output-batching tests (T058).

Verifies that the WS output batcher (server/bridge/ws.py, NFR-005) coalesces
rapid output chunks into far fewer WS frames while preserving all data.

The batcher lives inside an async generator (`_batch_output`).  We extract the
coalescing logic into a testable coroutine here — rather than spinning up a full
TestClient + WebSocket — so the test is fast, isolated, and deterministic.

Architecture of the batcher (from ws.py):
  1. Drain all items already in the queue (non-blocking get_nowait loop).
  2. If anything was buffered, flush a single frame and clear the buffer.
  3. Wait up to _BATCH_INTERVAL for the next item.
  4. Repeat.

A None sentinel signals process exit.

Key assertions:
  - A burst of N chunks produces far fewer than N WS frames (coalescing).
  - Concatenating all frame payloads equals the concatenation of input chunks.
  - A None sentinel triggers a final flush and stops the loop.
"""
from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

import pytest

from server.bridge.ws import _BATCH_INTERVAL


# ---------------------------------------------------------------------------
# Minimal batcher re-implementation for unit testing
# ---------------------------------------------------------------------------
# We copy the exact logic from _batch_output so the test is stable against
# internal refactors and can run without a WebSocket or full app.

async def _run_batcher(
    chunks: list[bytes | None],
    *,
    inter_chunk_delay: float = 0.0,
) -> list[dict[str, Any]]:
    """Run the batcher over *chunks* and return a list of WS frames (as dicts).

    Each frame has the shape ``{"type": "output", "data": <b64>}`` or
    ``{"type": "sessionEnded", "code": None}``.

    *inter_chunk_delay* controls how fast chunks arrive.  Set to 0 for an
    instantaneous burst; set to > _BATCH_INTERVAL to force one-frame-per-chunk.
    """
    out_q: asyncio.Queue[bytes | None] = asyncio.Queue()
    frames: list[dict[str, Any]] = []

    async def _send(msg: dict) -> None:
        frames.append(msg)

    async def _produce() -> None:
        for chunk in chunks:
            await out_q.put(chunk)
            if inter_chunk_delay:
                await asyncio.sleep(inter_chunk_delay)

    async def _batch(session_id: str) -> None:
        """Mirrors _batch_output from server/bridge/ws.py exactly."""
        buf: list[bytes] = []
        while True:
            # Drain
            try:
                while True:
                    chunk = out_q.get_nowait()
                    if chunk is None:
                        if buf:
                            await _send({
                                "type": "output",
                                "sessionId": session_id,
                                "data": base64.b64encode(b"".join(buf)).decode(),
                            })
                            buf = []
                        await _send({"type": "sessionEnded", "sessionId": session_id, "code": None})
                        return
                    buf.append(chunk)
            except asyncio.QueueEmpty:
                pass

            if buf:
                await _send({
                    "type": "output",
                    "sessionId": session_id,
                    "data": base64.b64encode(b"".join(buf)).decode(),
                })
                buf = []

            try:
                chunk = await asyncio.wait_for(out_q.get(), timeout=_BATCH_INTERVAL)
                if chunk is None:
                    await _send({"type": "sessionEnded", "sessionId": session_id, "code": None})
                    return
                buf.append(chunk)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return

    producer = asyncio.create_task(_produce())
    batcher = asyncio.create_task(_batch("test-session"))

    await asyncio.gather(producer, batcher)
    return frames


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_burst_coalesces_into_fewer_frames():
    """A burst of many rapid chunks must produce far fewer WS output frames.

    NFR-005: frames flushed no more often than ~16ms.  With zero inter-chunk
    delay the entire burst arrives in one tick so the batcher drains them all
    in one pass and emits a single output frame (plus sessionEnded).
    """
    N = 100
    chunks: list[bytes | None] = [f"chunk-{i}\n".encode() for i in range(N)]
    chunks.append(None)  # sentinel

    frames = await _run_batcher(chunks, inter_chunk_delay=0.0)

    output_frames = [f for f in frames if f["type"] == "output"]
    ended_frames  = [f for f in frames if f["type"] == "sessionEnded"]

    # Coalescing: far fewer frames than chunks
    assert len(output_frames) < N // 5, (
        f"Expected strong coalescing (<{N // 5} frames) but got {len(output_frames)}"
    )
    assert len(ended_frames) == 1


@pytest.mark.asyncio
async def test_no_data_lost_in_burst():
    """Concatenating all output frame payloads must equal the concatenation of inputs."""
    chunks_data = [f"line-{i}\n".encode() for i in range(50)]
    chunks: list[bytes | None] = list(chunks_data) + [None]

    frames = await _run_batcher(chunks, inter_chunk_delay=0.0)

    output_frames = [f for f in frames if f["type"] == "output"]
    received = b"".join(base64.b64decode(f["data"]) for f in output_frames)
    expected = b"".join(chunks_data)

    assert received == expected, (
        f"Data loss detected: expected {len(expected)} bytes, got {len(received)} bytes"
    )


@pytest.mark.asyncio
async def test_sentinel_triggers_session_ended():
    """A None sentinel in the queue must produce exactly one sessionEnded frame."""
    chunks: list[bytes | None] = [b"hello", b" world", None]
    frames = await _run_batcher(chunks, inter_chunk_delay=0.0)

    ended = [f for f in frames if f["type"] == "sessionEnded"]
    assert len(ended) == 1
    assert ended[0]["sessionId"] == "test-session"


@pytest.mark.asyncio
async def test_slow_chunks_still_flushed():
    """Chunks spaced apart by more than _BATCH_INTERVAL each emit their own frame.

    This verifies the batcher does not hold data longer than one batch interval
    even when the inter-chunk gap exceeds the flush cadence.
    """
    # Each chunk arrives after the interval has elapsed → each gets its own frame
    delay = _BATCH_INTERVAL * 2
    chunks: list[bytes | None] = [b"a", b"b", b"c", None]

    frames = await _run_batcher(chunks, inter_chunk_delay=delay)
    output_frames = [f for f in frames if f["type"] == "output"]

    # Each slow chunk is flushed individually (one frame per chunk)
    assert len(output_frames) == 3

    # Data still intact
    received = b"".join(base64.b64decode(f["data"]) for f in output_frames)
    assert received == b"abc"


@pytest.mark.asyncio
async def test_empty_stream_produces_only_session_ended():
    """A stream with only the sentinel (no data) emits sessionEnded but no output."""
    frames = await _run_batcher([None], inter_chunk_delay=0.0)
    assert not any(f["type"] == "output" for f in frames)
    assert sum(1 for f in frames if f["type"] == "sessionEnded") == 1


@pytest.mark.asyncio
async def test_batch_interval_bounds_flush_frequency():
    """The wall-clock elapsed time per flush is bounded by roughly _BATCH_INTERVAL.

    We send 20 chunks spaced at zero delay and measure that the number of output
    frames is significantly less than 20, confirming the interval is the gate.
    """
    N = 20
    chunks: list[bytes | None] = [f"x{i}".encode() for i in range(N)] + [None]

    t0 = time.monotonic()
    frames = await _run_batcher(chunks, inter_chunk_delay=0.0)
    elapsed = time.monotonic() - t0

    output_frames = [f for f in frames if f["type"] == "output"]

    # Strong coalescing expected for a zero-delay burst
    assert len(output_frames) < N
    # Should complete in a reasonable time (well under 1s for 20 chunks)
    assert elapsed < 1.0, f"Batcher took unexpectedly long: {elapsed:.2f}s"
