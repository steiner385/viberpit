"""Wait-for-idle rotation primitive.

Given a live session, this module:
1. Waits until the session is idle (no PTY output for ``idle_secs``), bounded by
   ``max_wait``.  If ``max_wait`` elapses it sends SIGINT as a graceful interrupt
   then proceeds anyway (fallback per R2).
2. Picks the next account (excluding the current one) via ``select_account``.
3. Terminates the current PTY.
4. Composes the new account's CLAUDE_CONFIG_DIR.
5. Spawns a new PTY for the SAME session id (rebridges WebSocket subscribers).
6. Nudges a resize to force a terminal repaint.
7. Returns the new account name.

No REST/WS/orchestrator wiring is done here — this is a pure async primitive.
"""
from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from server.sessions.manager import SessionManager
from server.sessions.types import SessionRecord


@dataclass
class RotationDeps:
    """Injectable dependencies for ``rotate()``.

    Attributes:
        manager: The ``SessionManager`` instance that owns the current PTY.
        select_account: Callable ``(exclude: frozenset[str]) -> str`` —
            returns the next account name, excluding the current one.
            Should raise ``AllAccountsBusy`` if no account is available.
        compose: Callable ``(account_name: str) -> Path`` — writes the
            per-account ``CLAUDE_CONFIG_DIR`` and returns the path.
        claude_argv: Callable ``(claude_session_id: str, config_dir: str) -> list[str]``
            — builds the argv to re-spawn claude with ``--resume``.
        idle_secs: How long without PTY output counts as "idle" (default 2.0 s).
        max_wait: Hard bound on idle-wait before forcing rotation (default 60.0 s).
    """

    manager: SessionManager
    select_account: Callable[[frozenset[str]], str]
    compose: Callable[[str], Path]
    claude_argv: Callable[[str, str], list[str]]
    idle_secs: float = 2.0
    max_wait: float = 60.0


async def rotate(session: SessionRecord, deps: RotationDeps) -> str:
    """Rotate *session* to a new account.

    Returns the name of the account that was selected.
    """
    mgr = deps.manager

    # ------------------------------------------------------------------
    # Step 1 — wait for idle, bounded by max_wait
    # ------------------------------------------------------------------
    waited = await _wait_idle(session.id, mgr, deps.idle_secs, deps.max_wait)
    if not waited:
        # Fallback: max_wait elapsed — send a graceful interrupt so claude
        # can finish its current output flush before we kill the process.
        _try_interrupt(mgr, session.id)
        # Brief grace period for the interrupt to take effect
        await asyncio.sleep(0.2)

    # ------------------------------------------------------------------
    # Step 2 — pick the next account (exclude the current one)
    # ------------------------------------------------------------------
    new_account = deps.select_account(frozenset({session.account}))

    # ------------------------------------------------------------------
    # Step 3 — terminate the current PTY
    # ------------------------------------------------------------------
    await mgr.terminate(session.id)

    # ------------------------------------------------------------------
    # Step 4 — compose the new account's CLAUDE_CONFIG_DIR
    # ------------------------------------------------------------------
    new_cfg_dir = deps.compose(new_account)

    # ------------------------------------------------------------------
    # Step 5 — spawn a new PTY for the SAME session id (rebridge)
    # ------------------------------------------------------------------
    argv = deps.claude_argv(session.claude_session_id, str(new_cfg_dir))
    env = {**_base_env(), "CLAUDE_CONFIG_DIR": str(new_cfg_dir)}
    await mgr.spawn(
        session_id=session.id,
        argv=argv,
        env=env,
        cwd=session.cwd,
        cols=session.cols,
        rows=session.rows,
    )

    # ------------------------------------------------------------------
    # Step 6 — nudge resize to force repaint on the client terminal
    # ------------------------------------------------------------------
    await mgr.resize(session.id, session.cols, session.rows)

    # ------------------------------------------------------------------
    # Step 7 — update the in-memory record and return the new account
    # ------------------------------------------------------------------
    session.account = new_account
    return new_account


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _wait_idle(
    session_id: str,
    mgr: SessionManager,
    idle_secs: float,
    max_wait: float,
    poll: float = 0.05,
) -> bool:
    """Poll until the session is idle.  Return True if idle was detected,
    False if max_wait was exhausted first."""
    deadline = asyncio.get_event_loop().time() + max_wait
    while asyncio.get_event_loop().time() < deadline:
        if mgr.is_idle(session_id, idle_secs):
            return True
        await asyncio.sleep(poll)
    return False


def _try_interrupt(mgr: SessionManager, session_id: str) -> None:
    """Send SIGINT to the session process (best-effort)."""
    try:
        # Access the internal pid; the manager exposes it via the handle
        session = mgr._sessions.get(session_id)  # type: ignore[attr-defined]
        if session is not None and session._running:
            os.kill(session.pid, signal.SIGINT)
    except (ProcessLookupError, OSError):
        pass


def _base_env() -> dict[str, str]:
    """Return os.environ as a plain dict (the child needs a full env)."""
    return dict(os.environ)
