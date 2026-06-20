"""ReauthManager — one-at-a-time account re-authentication state machine.

States: idle | running | cancelling | success | error | timeout

Usage (production):
    manager = ReauthManager()          # uses default production runner
    state = await manager.start("primary", "me@example.com")
    # poll:
    state = manager.status("primary")
    # cancel:
    state = await manager.cancel("primary")
    # startup sweep (call once):
    manager.sweep_stale()
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ReauthBusy(Exception):
    """Another reauth is already running."""

    def __init__(self, holding_identity: str | None, started_at: float | None):
        self.holding_identity = holding_identity
        self.started_at = started_at
        super().__init__(f"reauth busy (identity={holding_identity!r})")


class ReauthNoKey(Exception):
    """No encryption key is configured — refuse to store plaintext credentials."""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class ReauthState:
    state: str = "idle"          # idle|running|cancelling|success|error|timeout
    message: str | None = None
    identity: str | None = None
    started_at: float | None = None
    # internal only — not serialised
    _task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    _launcher_pid: int | None = field(default=None, repr=False, compare=False)
    _temp_profile: str | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict:
        """Return a camelCase dict safe for JSON serialisation."""
        return {
            "state": self.state,
            "message": self.message,
            "identity": self.identity,
            "startedAt": self.started_at,
        }


# ---------------------------------------------------------------------------
# Production default runner
# ---------------------------------------------------------------------------

def _make_default_runner() -> Callable[[str], bool]:
    """Build the production runner closure.

    prepare_reauth_profile(chrome_profile)
      → env_extra {BROWSER: realpath(launcher), DISPLAY: reauth_display,
                   CCSWITCH_REAUTH_PROFILE: temp}
      → asyncio.to_thread(capture_login, name, env_extra=...)
      → finally: cleanup_reauth_profile(temp)
    """
    async def _production_runner(name: str) -> bool:
        from ccswitch import auth_login
        from server.config import get_settings

        settings = get_settings()
        chrome_profile = settings.chrome_profile
        reauth_display = settings.reauth_display

        # Locate the launcher script (shipped with the package)
        launcher = (
            Path(__file__).parent.parent / "scripts" / "ccswitch-reauth-browser"
        )
        launcher_real = os.path.realpath(str(launcher))

        temp = auth_login.prepare_reauth_profile(chrome_profile)
        try:
            env_extra = {
                "BROWSER": launcher_real,
                "DISPLAY": reauth_display,
                "CCSWITCH_REAUTH_PROFILE": temp,
            }
            return await asyncio.to_thread(
                auth_login.capture_login, name, env_extra=env_extra
            )
        finally:
            auth_login.cleanup_reauth_profile(temp)

    return _production_runner


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class ReauthManager:
    """One-at-a-time reauth state machine with injectable runner and timeout."""

    def __init__(
        self,
        *,
        runner: Callable[[str], bool] | None = None,
        key_available: Callable[[], bool] | None = None,
        timeout_s: float = 300.0,
    ) -> None:
        if runner is None:
            runner = _make_default_runner()
        if key_available is None:
            from ccswitch.crypto import key_available as _ka
            key_available = _ka

        self._runner = runner
        self._key_available = key_available
        self._timeout_s = timeout_s
        self._state = ReauthState()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, name: str, identity: str) -> ReauthState:
        """Start a reauth for *name*.

        Raises:
            ReauthNoKey:  no encryption key is configured (would store plaintext).
            ReauthBusy:   another reauth is already in progress.
        """
        if not self._key_available():
            raise ReauthNoKey("No encryption key configured — refusing to store plaintext credentials")

        async with self._lock:
            if self._state.state in ("running", "cancelling"):
                raise ReauthBusy(self._state.identity, self._state.started_at)

            self._state = ReauthState(
                state="running",
                identity=identity,
                started_at=time.time(),
            )
            task = asyncio.create_task(self._run(name))
            self._state._task = task

        return ReauthState(
            state=self._state.state,
            message=self._state.message,
            identity=self._state.identity,
            started_at=self._state.started_at,
        )

    def status(self, name: str) -> ReauthState:
        """Return a snapshot of the current state (name is for API compat)."""
        s = self._state
        return ReauthState(
            state=s.state,
            message=s.message,
            identity=s.identity,
            started_at=s.started_at,
        )

    async def cancel(self, name: str) -> ReauthState:
        """Cancel a running reauth.

        Sets state to 'cancelling', cancels the asyncio task, and kills any
        tracked launcher PID.
        """
        async with self._lock:
            if self._state.state not in ("running", "cancelling"):
                return self.status(name)

            self._state.state = "cancelling"
            task = self._state._task

        # Kill the launcher Chrome window if we know its PID
        self._kill_launcher()

        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

        async with self._lock:
            if self._state.state == "cancelling":
                self._state.state = "idle"
                self._state.message = "cancelled"

        return self.status(name)

    def sweep_stale(self) -> None:
        """Remove reauth-tmp directories older than 600 seconds.

        Called at startup to clean up leftover profiles from crashed runs.
        """
        from ccswitch.paths import reauth_tmp_dir

        tmp = reauth_tmp_dir()
        if not tmp.exists():
            return

        now = time.time()
        for child in tmp.iterdir():
            try:
                age = now - os.stat(child).st_mtime
                if age > 600:
                    import shutil
                    shutil.rmtree(child, ignore_errors=True)
                    log.info("sweep_stale: removed %s (age=%.0fs)", child, age)
            except OSError:
                pass

    async def shutdown(self) -> None:
        """Graceful shutdown — cancel any running reauth and wait."""
        await self.cancel("*")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run(self, name: str) -> None:
        """Execute the runner under a timeout and update state on completion."""
        try:
            result = await asyncio.wait_for(self._runner(name), timeout=self._timeout_s)
            async with self._lock:
                if self._state.state == "running":
                    self._state.state = "success" if result else "error"
                    self._state.message = None if result else "runner returned False"
        except asyncio.TimeoutError:
            async with self._lock:
                if self._state.state == "running":
                    self._state.state = "timeout"
                    self._state.message = f"timed out after {self._timeout_s}s"
            self._kill_launcher()
            log.warning("reauth timed out for %r after %.0fs", name, self._timeout_s)
        except asyncio.CancelledError:
            # cancel() handles the state transition
            raise
        except Exception as exc:
            async with self._lock:
                if self._state.state == "running":
                    self._state.state = "error"
                    self._state.message = str(exc)
            log.exception("reauth error for %r: %s", name, exc)

    def _kill_launcher(self) -> None:
        """Send SIGTERM to the Chrome launcher PID if known."""
        pid = self._state._launcher_pid
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
                log.info("sent SIGTERM to launcher PID %d", pid)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            self._state._launcher_pid = None

        # Also try reading launcher.pid from the temp profile dir
        temp = self._state._temp_profile
        if temp:
            pid_file = Path(temp) / "launcher.pid"
            try:
                pid_str = pid_file.read_text().strip()
                pid = int(pid_str)
                os.kill(pid, signal.SIGTERM)
                log.info("sent SIGTERM to launcher PID %d (from pid file)", pid)
            except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError, OSError):
                pass
