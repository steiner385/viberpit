"""McpAuthManager — one-at-a-time MCP-server auth via `claude mcp login`.

Mirrors server/reauth.py's state-machine shape. Owns a live PTY subprocess
that prints an authorize URL (surfaced to the UI) then blocks on stdin for the
user-pasted redirect URL. On success, merges the new mcpOAuth into the shared
store and clears stale per-account needs-auth caches.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from ccswitch import paths
from ccswitch.creds import write_json_secure
from server.library.scan import read_json_safe

log = logging.getLogger(__name__)

# Terminal states — polling stops here.
TERMINAL = ("success", "error", "cancelled")


@dataclass
class McpAuthState:
    state: str = "idle"          # idle|starting|awaiting_redirect|exchanging|success|error|cancelled
    server: str | None = None
    message: str | None = None
    auth_url: str | None = None
    accounts: list[str] = field(default_factory=list)  # accounts whose badge this run clears
    started_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "server": self.server,
            "message": self.message,
            "authUrl": self.auth_url,
            "accounts": list(self.accounts),
            "startedAt": self.started_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "McpAuthState":
        return McpAuthState(
            state=d.get("state", "idle"),
            server=d.get("server"),
            message=d.get("message"),
            auth_url=d.get("authUrl"),
            accounts=list(d.get("accounts", [])),
            started_at=d.get("startedAt"),
        )


def merge_mcp_oauth_from_cfg(cfg_dir: Path) -> int:
    """Merge <cfg_dir>/.credentials.json's mcpOAuth into the shared MCP store."""
    cred = read_json_safe(cfg_dir / ".credentials.json") or {}
    mcp = cred.get("mcpOAuth")
    if not isinstance(mcp, dict) or not mcp:
        return 0
    shared_path = paths.shared_mcp_path()
    existing = read_json_safe(shared_path) or {}
    existing.update(mcp)
    write_json_secure(shared_path, existing)
    return len(existing)


def _bare(key: str) -> str:
    return key.rsplit(":", 1)[-1] if key.startswith("plugin:") else key


def clear_needs_auth_cache(server: str) -> list[str]:
    """Drop `server`'s entry from every account's mcp-needs-auth-cache.json."""
    cleared: list[str] = []
    root = paths.cfg_root()
    if not root.is_dir():
        return cleared
    for acct_dir in sorted(root.iterdir()):
        cache_path = acct_dir / "mcp-needs-auth-cache.json"
        cache = read_json_safe(cache_path)
        if not isinstance(cache, dict):
            continue
        keys = [k for k in cache if _bare(k) == server]
        if not keys:
            continue
        for k in keys:
            cache.pop(k, None)
        write_json_secure(cache_path, cache)
        cleared.append(acct_dir.name)
    return cleared


class McpAuthError(Exception):
    """The `claude mcp login` exchange failed (bad redirect, server refusal, EOF)."""


class McpAuthBusy(Exception):
    def __init__(self, server: str | None):
        self.server = server
        super().__init__(f"mcp auth busy (server={server!r})")


class LoginSession(Protocol):
    account: str
    cfg_dir: Path

    async def read_auth_url(self, timeout: float) -> str: ...
    async def complete(self, redirect_url: str, timeout: float) -> None: ...
    def terminate(self) -> None: ...


class McpAuthManager:
    """One-at-a-time MCP-server auth state machine."""

    def __init__(
        self,
        *,
        session_factory: Callable[[str, str, Path], LoginSession],
        account_picker: Callable[[str], tuple[str, Path]],
        clock: Callable[[], float] = time.time,
        url_timeout_s: float = 60.0,
        complete_timeout_s: float = 120.0,
    ) -> None:
        self._factory = session_factory
        self._pick = account_picker
        self._clock = clock
        self._url_timeout = url_timeout_s
        self._complete_timeout = complete_timeout_s
        self._state = McpAuthState()
        self._session: LoginSession | None = None
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._lock = asyncio.Lock()

    def status(self, server: str) -> McpAuthState:
        s = self._state
        if s.server is not None and s.server != server:
            return McpAuthState()  # scope: other servers see a fresh idle
        return McpAuthState(state=s.state, server=s.server, message=s.message,
                            auth_url=s.auth_url, accounts=list(s.accounts),
                            started_at=s.started_at)

    async def start(self, server: str) -> McpAuthState:
        async with self._lock:
            if self._state.state in ("starting", "awaiting_redirect", "exchanging"):
                raise McpAuthBusy(self._state.server)
            account, cfg_dir = self._pick(server)
            self._session = self._factory(server, account, cfg_dir)
            self._state = McpAuthState(state="starting", server=server,
                                       accounts=[account], started_at=self._clock())
            self._task = asyncio.create_task(self._read_url(server))
        return self.status(server)

    async def _read_url(self, server: str) -> None:
        assert self._session is not None
        try:
            url = await self._session.read_auth_url(self._url_timeout)
            async with self._lock:
                if self._state.state == "starting" and self._state.server == server:
                    self._state.state = "awaiting_redirect"
                    self._state.auth_url = url
        except McpAuthError as exc:
            # Surface the CLI's own message (e.g. stdio "doesn't support OAuth
            # login") verbatim — this is the graceful-degradation path.
            log.warning("mcp auth: read_auth_url rejected for %r: %s", server, exc)
            self._terminate_session()
            async with self._lock:
                if self._state.server == server and self._state.state == "starting":
                    self._state.state = "error"
                    self._state.message = str(exc)
        except Exception as exc:  # timeout / EOF / spawn failure
            log.warning("mcp auth: read_auth_url failed for %r: %s", server, exc)
            self._terminate_session()
            async with self._lock:
                if self._state.server == server and self._state.state == "starting":
                    self._state.state = "error"
                    self._state.message = "Could not obtain an authorization URL."

    async def submit_redirect(self, server: str, redirect_url: str) -> McpAuthState:
        async with self._lock:
            if self._state.state != "awaiting_redirect" or self._state.server != server:
                return self.status(server)
            self._state.state = "exchanging"
            session = self._session
        assert session is not None
        try:
            await session.complete(redirect_url, self._complete_timeout)
            n_merged = merge_mcp_oauth_from_cfg(session.cfg_dir)
            cleared = clear_needs_auth_cache(server)
            async with self._lock:
                self._state.state = "success"
                self._state.accounts = cleared or self._state.accounts
                self._state.message = None
            log.info("mcp auth: %s ok (shared_keys=%d, cleared=%d accounts)",
                     server, n_merged, len(cleared))
        except McpAuthError as exc:
            await self._fail(server, str(exc))
        except Exception as exc:
            log.warning("mcp auth: unexpected failure for %r: %s", server, exc)
            await self._fail(server, "Authentication failed — try again.")
        finally:
            self._terminate_session()
        return self.status(server)

    async def cancel(self, server: str) -> McpAuthState:
        async with self._lock:
            if self._state.server != server:
                return self.status(server)
            task = self._task
        self._terminate_session()
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        async with self._lock:
            self._state = McpAuthState(state="cancelled", server=server,
                                       message="cancelled")
        return self.status(server)

    async def shutdown(self) -> None:
        if self._state.server:
            await self.cancel(self._state.server)

    async def _fail(self, server: str, msg: str) -> None:
        async with self._lock:
            if self._state.server == server:
                self._state.state = "error"
                self._state.message = msg

    def _terminate_session(self) -> None:
        s = self._session
        self._session = None
        if s is not None:
            try:
                s.terminate()
            except Exception:
                pass
