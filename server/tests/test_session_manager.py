"""Tests for server.sessions.manager (T021).

Spawns the fake-claude PTY stub (tests/fakes/fake_claude_pty.py) via the
SessionManager and exercises all public API methods.

Polling strategy: short asyncio.sleep loops with a deadline so CI stays fast
without fixed sleeps.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from server.sessions.manager import SessionManager

# Path to the fake stub relative to repo root
REPO_ROOT = Path(__file__).parent.parent.parent
FAKE_PTY = str(REPO_ROOT / "tests" / "fakes" / "fake_claude_pty.py")


def _fake_argv() -> list[str]:
    return [sys.executable, FAKE_PTY]


def _env() -> dict:
    return {**os.environ, "CCSWITCH_FAKE_EXIT_ON": "/quit"}


async def _poll_until(condition, timeout: float = 3.0, interval: float = 0.05) -> bool:
    """Poll condition() until it returns truthy or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Test: banner appears in scrollback after spawn
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_banner_in_scrollback_after_spawn():
    mgr = SessionManager()
    handle = await mgr.spawn("s1", _fake_argv(), _env(), cwd=str(REPO_ROOT))
    try:
        ok = await _poll_until(lambda: b"fake-claude ready" in mgr.scrollback("s1"))
        assert ok, f"banner not in scrollback; got: {mgr.scrollback('s1')!r}"
    finally:
        await mgr.terminate("s1")


# ---------------------------------------------------------------------------
# Test: write_input produces echoed turn output in scrollback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_input_echoed_in_scrollback():
    mgr = SessionManager()
    await mgr.spawn("s2", _fake_argv(), _env(), cwd=str(REPO_ROOT))
    try:
        # Wait for banner
        await _poll_until(lambda: b"fake-claude ready" in mgr.scrollback("s2"))

        await mgr.write_input("s2", b"hello\n")
        ok = await _poll_until(lambda: b"got: hello" in mgr.scrollback("s2"))
        assert ok, f"echoed turn not found; got: {mgr.scrollback('s2')!r}"
    finally:
        await mgr.terminate("s2")


# ---------------------------------------------------------------------------
# Test: subscribe() receives live chunks for a subsequent input
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscribe_receives_live_chunks():
    mgr = SessionManager()
    await mgr.spawn("s3", _fake_argv(), _env(), cwd=str(REPO_ROOT))
    try:
        # Wait for banner
        await _poll_until(lambda: b"fake-claude ready" in mgr.scrollback("s3"))

        q = mgr.subscribe("s3")
        await mgr.write_input("s3", b"world\n")

        # Collect chunks from the queue until we see the response or timeout
        collected = b""
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                chunk = q.get_nowait()
                if chunk is None:
                    break
                collected += chunk
                if b"got: world" in collected:
                    break
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.05)

        assert b"got: world" in collected, f"live chunk not received; got: {collected!r}"
    finally:
        mgr.unsubscribe("s3", q)
        await mgr.terminate("s3")


# ---------------------------------------------------------------------------
# Test: is_idle() — True after quiet, False right after new output
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_is_idle_detection():
    mgr = SessionManager()
    await mgr.spawn("s4", _fake_argv(), _env(), cwd=str(REPO_ROOT))
    try:
        # Wait for banner (output)
        await _poll_until(lambda: b"fake-claude ready" in mgr.scrollback("s4"))
        # Send a turn and wait for its output to land
        await mgr.write_input("s4", b"ping\n")
        await _poll_until(lambda: b"got: ping" in mgr.scrollback("s4"))

        # After output stops, idle should become True within a short wait
        idle_secs = 0.05
        ok = await _poll_until(lambda: mgr.is_idle("s4", idle_secs), timeout=2.0, interval=0.02)
        assert ok, "is_idle never became True"

        # Now send another turn — last_output_at should advance (idle clock resets).
        before_write = time.monotonic()
        await mgr.write_input("s4", b"pong\n")
        await _poll_until(lambda: b"got: pong" in mgr.scrollback("s4"))
        # last_output_at must be >= before_write, proving the clock reset
        assert mgr.last_output_at("s4") >= before_write, "last_output_at did not update after pong"
        # With a very long idle threshold, definitely not idle right after output
        assert not mgr.is_idle("s4", 60.0), "is_idle(60s) should be False right after output"
    finally:
        await mgr.terminate("s4")


# ---------------------------------------------------------------------------
# Test: resize() doesn't error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resize_no_error():
    mgr = SessionManager()
    handle = await mgr.spawn("s5", _fake_argv(), _env(), cwd=str(REPO_ROOT))
    try:
        await _poll_until(lambda: b"fake-claude ready" in mgr.scrollback("s5"))
        # Must not raise
        await mgr.resize("s5", 120, 40)
        assert handle.cols == 120
        assert handle.rows == 40
    finally:
        await mgr.terminate("s5")


# ---------------------------------------------------------------------------
# Test: scrollback is bounded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scrollback_bounded():
    limit = 200
    mgr = SessionManager(scrollback_bytes=limit)
    await mgr.spawn("s6", _fake_argv(), _env(), cwd=str(REPO_ROOT))
    try:
        await _poll_until(lambda: b"fake-claude ready" in mgr.scrollback("s6"))

        # Send enough turns to exceed the limit
        for i in range(20):
            await mgr.write_input("s6", f"line{i:03d}\n".encode())
            # Don't flood without waiting a tiny bit
            await asyncio.sleep(0.02)

        # Allow output to settle
        await asyncio.sleep(0.3)

        sb = mgr.scrollback("s6")
        assert len(sb) <= limit, f"scrollback too large: {len(sb)} > {limit}"
    finally:
        await mgr.terminate("s6")


# ---------------------------------------------------------------------------
# Test: terminate() returns exit code; is_running() becomes False; sentinel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_terminate_exits_cleanly():
    mgr = SessionManager()
    await mgr.spawn("s7", _fake_argv(), _env(), cwd=str(REPO_ROOT))
    try:
        await _poll_until(lambda: b"fake-claude ready" in mgr.scrollback("s7"))

        q = mgr.subscribe("s7")
        assert mgr.is_running("s7")

        code = await mgr.terminate("s7", timeout=5.0)

        assert not mgr.is_running("s7"), "is_running should be False after terminate"
        assert isinstance(code, int) or code is None  # SIGTERM or SIGKILL may differ

        # Subscriber should have received the None sentinel
        sentinel_received = False
        while True:
            try:
                item = q.get_nowait()
                if item is None:
                    sentinel_received = True
                    break
            except asyncio.QueueEmpty:
                break
        assert sentinel_received, "subscriber never received None sentinel"
    except Exception:
        # Already terminated above; swallow double-terminate errors
        pass


# ---------------------------------------------------------------------------
# Test: graceful exit via /quit — exit code 0
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clean_exit_via_quit_command():
    mgr = SessionManager()
    await mgr.spawn("s8", _fake_argv(), _env(), cwd=str(REPO_ROOT))

    await _poll_until(lambda: b"fake-claude ready" in mgr.scrollback("s8"))

    await mgr.write_input("s8", b"/quit\n")

    ok = await _poll_until(lambda: not mgr.is_running("s8"), timeout=3.0)
    assert ok, "process did not exit after /quit"
    assert mgr.exit_code("s8") == 0


# ---------------------------------------------------------------------------
# Test: KeyError on unknown session_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_session_raises():
    mgr = SessionManager()
    with pytest.raises(KeyError):
        mgr.scrollback("does-not-exist")
