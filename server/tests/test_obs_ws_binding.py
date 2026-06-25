"""Behavioral tests: session_id contextvar is bound while _forward_live runs.

Design:
- The trivial guard proves bind_session itself works (context manager round-trip).
- The behavioral test proves that _forward_live wraps its body in bind_session so
  that session_id_var.get() returns the passed session_id while events() is being
  consumed, and is None (reset) after the coroutine finishes.
"""
from __future__ import annotations

import asyncio

import pytest

from server.obs.context import bind_session, session_id_var


# ---------------------------------------------------------------------------
# Trivial guard: bind_session context manager semantics
# ---------------------------------------------------------------------------

def test_bind_session_is_used_for_scoping() -> None:
    """Guard: the context manager sets and resets the contextvar."""
    with bind_session("ws-sess"):
        assert session_id_var.get() == "ws-sess"
    assert session_id_var.get() is None


# ---------------------------------------------------------------------------
# Behavioral test: _forward_live binds session_id while iterating events
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forward_live_binds_session_id() -> None:
    """_forward_live must set session_id_var while consuming driver events.

    Approach: the fake driver's events() async-generator captures
    session_id_var.get() during iteration, which is the only reliable way
    to observe contextvars (they are read at call-time, not filter-time).
    After the coroutine completes the contextvar must be reset to None.
    """
    from server.bridge.ws import _forward_live

    observed: list[str | None] = []

    class FakeDriver:
        async def events(self):
            # Capture the contextvar value mid-stream.
            observed.append(session_id_var.get())
            yield _DummyEvent()

    class _DummyEvent:
        def to_wire(self) -> dict:
            return {"type": "dummy"}

    out_q: asyncio.Queue[dict] = asyncio.Queue()
    session_id = "test-session-abc"

    # Ensure it starts as None (no leak from prior tests).
    assert session_id_var.get() is None

    await _forward_live(FakeDriver(), out_q, session_id)

    # The contextvar must have been the session_id while events() ran.
    assert observed == [session_id], (
        f"Expected session_id_var.get() == {session_id!r} inside _forward_live, "
        f"got {observed!r}"
    )

    # After the coroutine finishes the contextvar must be reset.
    assert session_id_var.get() is None, (
        "session_id_var was not reset after _forward_live completed"
    )

    # The event was enqueued as expected.
    assert not out_q.empty()


@pytest.mark.asyncio
async def test_forward_live_resets_session_id_on_exception() -> None:
    """session_id_var must be reset even when the stream raises unexpectedly."""
    from server.bridge.ws import _forward_live

    observed_in_exception_path: list[str | None] = []

    class BoomDriver:
        async def events(self):
            observed_in_exception_path.append(session_id_var.get())
            raise RuntimeError("stream exploded")
            yield  # make it an async generator

    out_q: asyncio.Queue[dict] = asyncio.Queue()
    session_id = "boom-session-xyz"

    assert session_id_var.get() is None

    # _forward_live catches non-CancelledError exceptions and emits an error frame.
    await _forward_live(BoomDriver(), out_q, session_id)

    # Contextvar was bound while the exception propagated inside the generator.
    assert observed_in_exception_path == [session_id]

    # Must be reset after the coroutine exits (the context manager handles this).
    assert session_id_var.get() is None

    # An error wire frame was emitted.
    assert not out_q.empty()
    wire = out_q.get_nowait()
    assert wire.get("type") == "error"
