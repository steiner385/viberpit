"""CopilotAuthManager — GitHub device-flow auth for the vendored copilot proxy.

States: pending | awaiting_authorization | authorized | failed

The manager spawns `copilot2api -token-dir <dir>` (login mode), reads its stdout
line-by-line, and scrapes the device-flow verification URL + user code. When
`credentials.json` appears in the token directory the state advances to
`authorized`. Modelled on server/reauth.py's ReauthManager/ReauthState pattern.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pure scraping helper — no async, fully unit-testable
# ---------------------------------------------------------------------------

_URI_RE = re.compile(r"https://github\.com/login/device")
_CODE_RE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b")


def _parse_device_prompt(line: str) -> tuple[str | None, str | None]:
    """Return *(verification_uri, user_code)* if *line* contains both; else *(None, None)*."""
    uri_match = _URI_RE.search(line)
    code_match = _CODE_RE.search(line)
    if uri_match and code_match:
        return uri_match.group(0), code_match.group(1)
    return None, None


# ---------------------------------------------------------------------------
# State dataclass
# ---------------------------------------------------------------------------

@dataclass
class CopilotAuthState:
    state: str = "pending"          # pending|awaiting_authorization|authorized|failed
    user_code: str | None = None
    verification_uri: str | None = None
    error: str | None = None
    # internal — not serialised
    _task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    _proc: Any = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "userCode": self.user_code,
            "verificationUri": self.verification_uri,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class CopilotAuthManager:
    """Per-account device-flow auth state machine with injectable subprocess."""

    def __init__(
        self,
        *,
        spawn: Callable = asyncio.create_subprocess_exec,
        token_dir_for: Callable[[str], str | Path],
    ) -> None:
        self._spawn = spawn
        self._token_dir_for = token_dir_for
        self._states: dict[str, CopilotAuthState] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, account: str) -> dict:
        """Spawn the proxy login process for *account* and begin scraping stdout."""
        from server.sessions.copilot_proxy import resolve_copilot_proxy

        binary = resolve_copilot_proxy()
        if binary is None:
            state = CopilotAuthState(state="failed", error="copilot2api binary not found")
            async with self._lock:
                self._states[account] = state
            return state.to_dict()

        token_dir = str(self._token_dir_for(account))
        Path(token_dir).mkdir(parents=True, mode=0o700, exist_ok=True)

        async with self._lock:
            # Cancel any existing in-flight login for this account
            existing = self._states.get(account)
            if existing is not None:
                await self._cancel_state(existing)
            state = CopilotAuthState(state="pending")
            self._states[account] = state

        try:
            proc = await self._spawn(
                binary, "-token-dir", token_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError) as exc:
            async with self._lock:
                state = self._states.get(account, CopilotAuthState())
                state.state = "failed"
                state.error = str(exc)
            return state.to_dict()

        async with self._lock:
            state = self._states[account]
            state._proc = proc
            task = asyncio.create_task(self._reader(account, proc, token_dir))
            state._task = task

        return self._snapshot(account)

    def status(self, account: str) -> dict:
        """Return current state dict for *account*."""
        return self._snapshot(account)

    async def cancel(self, account: str) -> dict:
        """Terminate the login process and reset state to pending."""
        async with self._lock:
            state = self._states.get(account)
            if state is None:
                return CopilotAuthState().to_dict()
            await self._cancel_state(state)
            self._states[account] = CopilotAuthState(state="pending")
        return self._snapshot(account)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _snapshot(self, account: str) -> dict:
        state = self._states.get(account, CopilotAuthState())
        return state.to_dict()

    async def _cancel_state(self, state: CopilotAuthState) -> None:
        """Terminate proc and cancel background task (must be called under the lock)."""
        proc = state._proc
        if proc is not None:
            try:
                proc.terminate()
            except (ProcessLookupError, OSError):
                pass
        task = state._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

    async def _reader(self, account: str, proc: Any, token_dir: str) -> None:
        """Background task: read stdout, scrape device prompt, poll for credentials."""
        creds_path = Path(token_dir) / "credentials.json"

        try:
            async for line_bytes in proc.stdout:
                line = line_bytes.decode(errors="replace").rstrip()
                uri, code = _parse_device_prompt(line)
                if uri and code:
                    async with self._lock:
                        state = self._states.get(account)
                        if state is not None and state.state == "pending":
                            state.state = "awaiting_authorization"
                            state.verification_uri = uri
                            state.user_code = code
                    log.info(
                        "copilot_auth: device prompt scraped account=%r code=%r",
                        account, code,
                    )

                # Poll for credentials after each line (cheap stat, file appears quickly)
                if creds_path.exists():
                    async with self._lock:
                        state = self._states.get(account)
                        if state is not None and state.state in ("pending", "awaiting_authorization"):
                            state.state = "authorized"
                    log.info("copilot_auth: credentials found account=%r", account)
                    return

            # Process exited — check one final time for credentials
            if creds_path.exists():
                async with self._lock:
                    state = self._states.get(account)
                    if state is not None and state.state in ("pending", "awaiting_authorization"):
                        state.state = "authorized"
            else:
                async with self._lock:
                    state = self._states.get(account)
                    if state is not None and state.state in ("pending", "awaiting_authorization"):
                        state.state = "failed"
                        state.error = "process exited without producing credentials"

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("copilot_auth: reader error account=%r", account)
            async with self._lock:
                state = self._states.get(account)
                if state is not None and state.state not in ("authorized", "failed"):
                    state.state = "failed"
                    state.error = str(exc)
