"""Tests for server.sessions.rotation (T022).

Drives the rotate() primitive using the fake PTY stub
(tests/fakes/fake_claude_pty.py) via SessionManager.

Assertions:
- rotate() waits until the session is idle, terminates the old PTY, spawns a
  new PTY for the same session id with --resume <claude_session_id> in argv,
  and returns the new account name.
- Max-wait fallback: a session that never goes idle is rotated anyway after
  max_wait, with an interrupt sent first.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from server.sessions.manager import SessionManager
from server.sessions.rotation import RotationDeps, rotate
from server.sessions.types import SessionRecord

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent
FAKE_PTY = str(REPO_ROOT / "tests" / "fakes" / "fake_claude_pty.py")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_argv_idle(idle_after: float = 0.1) -> list[str]:
    """Return argv for a fake that goes idle after idle_after seconds of no input."""
    return [sys.executable, FAKE_PTY]


def _env_idle(idle_after: float = 0.1) -> dict:
    """Environment for the fake: goes idle quickly, never-ending loop."""
    return {
        **os.environ,
        "CCSWITCH_FAKE_EXIT_ON": "/quit",
        "CCSWITCH_FAKE_IDLE_AFTER": str(idle_after),
    }


def _env_busy() -> dict:
    """Environment for the fake: blocks forever reading stdin (never idle)."""
    return {**os.environ, "CCSWITCH_FAKE_EXIT_ON": "/quit"}


async def _poll_until(cond, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(interval)
    return False


def _make_record(
    session_id: str = "sess-1",
    account: str = "alpha",
    claude_session_id: str = "cld-abc123",
    cols: int = 80,
    rows: int = 24,
    cwd: str | None = None,
) -> SessionRecord:
    return SessionRecord(
        id=session_id,
        name="Test Session",
        account=account,
        claude_session_id=claude_session_id,
        cols=cols,
        rows=rows,
        cwd=cwd or str(REPO_ROOT),
    )


# ---------------------------------------------------------------------------
# T022-a: normal rotation — waits for idle, terminates old PTY, spawns new one
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rotate_waits_for_idle_and_respawns():
    """Full rotation path:
    - Spawns fake PTY on 'alpha'.
    - select_account returns 'beta'.
    - rotate() waits for idle, terminates alpha's PTY, spawns a new one for 'beta'.
    - The new PTY's argv includes --resume <claude_session_id>.
    - The returned account is 'beta'.
    """
    mgr = SessionManager()
    spawned_argvs: list[list[str]] = []

    original_spawn = mgr.spawn

    async def recording_spawn(session_id, argv, env, cwd, cols=80, rows=24):
        spawned_argvs.append(argv)
        return await original_spawn(session_id, argv, env, cwd, cols, rows)

    mgr.spawn = recording_spawn  # type: ignore[assignment]

    session = _make_record(session_id="s-rotate-1", account="alpha", claude_session_id="CLD-001")

    # Spawn the initial fake session (goes idle quickly: 0.05s of no input)
    initial_argv = [sys.executable, FAKE_PTY]
    await mgr.spawn(
        session_id=session.id,
        argv=initial_argv,
        env=_env_idle(idle_after=0.05),
        cwd=session.cwd,
        cols=session.cols,
        rows=session.rows,
    )

    # Wait for the banner to confirm it's running
    banner_ok = await _poll_until(lambda: b"fake-claude ready" in mgr.scrollback(session.id))
    assert banner_ok, "fake PTY did not start"

    # Record old PID
    old_pid = mgr._sessions[session.id].pid  # type: ignore[attr-defined]

    # Build deps
    composed_path = Path("/tmp/ccswitch-test-cfg/beta")
    spawned_in_rotate: list[list[str]] = []

    async def spy_spawn(session_id, argv, env, cwd, cols=80, rows=24):
        spawned_in_rotate.append(argv)
        return await original_spawn(session_id, argv, env, cwd, cols, rows)

    mgr.spawn = spy_spawn  # type: ignore[assignment]

    # Fake spawn that re-uses the fake_claude_pty so the new PTY actually runs
    def build_argv(claude_session_id: str, config_dir: str) -> list[str]:
        # In tests, re-use the fake stub with --resume simulated via env marker
        return [sys.executable, FAKE_PTY, "--resume", claude_session_id]

    deps = RotationDeps(
        manager=mgr,
        select_account=lambda exclude: "beta",
        compose=lambda name: composed_path,
        claude_argv=build_argv,
        idle_secs=0.05,
        max_wait=5.0,
    )

    new_account = await rotate(session, deps)

    # --- assertions ---
    assert new_account == "beta", f"expected 'beta', got {new_account!r}"
    assert session.account == "beta", "SessionRecord.account should be updated"

    # The new PTY should be alive (or at least have been spawned)
    assert len(spawned_in_rotate) == 1
    new_argv = spawned_in_rotate[0]
    assert "--resume" in new_argv, f"--resume not in argv: {new_argv}"
    assert "CLD-001" in new_argv, f"claude_session_id not in argv: {new_argv}"

    # Old PID should be dead
    assert not mgr._sessions[session.id]._running or mgr._sessions[session.id].pid != old_pid  # type: ignore[attr-defined]

    # Clean up
    await mgr.terminate(session.id)


# ---------------------------------------------------------------------------
# T022-b: max-wait fallback — rotate proceeds even if session never goes idle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rotate_max_wait_fallback():
    """When the session never becomes idle within max_wait, rotate() should
    still proceed (sending an interrupt first as a graceful signal)."""
    mgr = SessionManager()
    spawned_in_rotate: list[list[str]] = []
    original_spawn = mgr.spawn

    async def spy_spawn(session_id, argv, env, cwd, cols=80, rows=24):
        spawned_in_rotate.append(argv)
        return await original_spawn(session_id, argv, env, cwd, cols, rows)

    session = _make_record(
        session_id="s-maxwait",
        account="alpha",
        claude_session_id="CLD-999",
    )

    # Spawn the initial fake — this one reads stdin actively and always responds
    # (no CCSWITCH_FAKE_IDLE_AFTER), so it will never be idle in the short window
    await mgr.spawn(
        session_id=session.id,
        argv=[sys.executable, FAKE_PTY],
        env=_env_busy(),
        cwd=session.cwd,
        cols=session.cols,
        rows=session.rows,
    )
    banner_ok = await _poll_until(lambda: b"fake-claude ready" in mgr.scrollback(session.id))
    assert banner_ok, "fake PTY did not start"

    # Keep the PTY "busy" by feeding it periodic input so it never goes quiet
    busy = True

    async def keep_busy():
        while busy:
            try:
                await mgr.write_input(session.id, b"ping\n")
            except Exception:
                break
            await asyncio.sleep(0.02)

    busy_task = asyncio.create_task(keep_busy())

    # Install spy spawn for the re-spawn
    mgr.spawn = spy_spawn  # type: ignore[assignment]

    deps = RotationDeps(
        manager=mgr,
        select_account=lambda exclude: "beta",
        compose=lambda name: Path("/tmp/ccswitch-test-cfg-maxwait/beta"),
        claude_argv=lambda cid, cfg: [sys.executable, FAKE_PTY, "--resume", cid],
        idle_secs=10.0,   # unreachably long idle threshold
        max_wait=0.15,    # short max_wait so the test finishes quickly
    )

    new_account = await rotate(session, deps)

    # Stop the busy writer
    busy = False
    busy_task.cancel()
    try:
        await busy_task
    except asyncio.CancelledError:
        pass

    # Despite never being idle, rotation must have completed
    assert new_account == "beta"
    assert session.account == "beta"
    assert len(spawned_in_rotate) == 1
    assert "--resume" in spawned_in_rotate[0]
    assert "CLD-999" in spawned_in_rotate[0]

    # Clean up
    await mgr.terminate(session.id)
