"""McpAuthManager — one-at-a-time MCP-server auth via `claude mcp login`.

Mirrors server/reauth.py's state-machine shape. Owns a live PTY subprocess
that prints an authorize URL (surfaced to the UI) then blocks on stdin for the
user-pasted redirect URL. On success, merges the new mcpOAuth into the shared
store and clears stale per-account needs-auth caches.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pty
import re
import select
import shlex
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from ccswitch import accounts, configdir, paths
from ccswitch.config import load_config
from ccswitch.creds import write_json_secure
from server.library import scan
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
            async with self._lock:
                self._terminate_session()
                if self._state.server == server and self._state.state == "starting":
                    self._state.state = "error"
                    self._state.message = str(exc)
        except Exception as exc:  # timeout / EOF / spawn failure
            log.warning("mcp auth: read_auth_url failed for %r: %s", server, exc)
            async with self._lock:
                self._terminate_session()
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
                if self._state.server == server and self._state.state == "exchanging":
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
            if self._state.state not in TERMINAL:
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


# ---------------------------------------------------------------------------
# URL extraction helpers
# ---------------------------------------------------------------------------

# Exclude whitespace, quotes, angle brackets AND control chars (\x00-\x20) so
# the OSC-8 escape bytes (\x1b ... \x07) that wrap the real URL aren't captured.
_URL_RE = re.compile(r"https://[^\s\"'<>\x00-\x20]+")


def _extract_url(text: str) -> str | None:
    m = _URL_RE.search(text)
    return m.group(0) if m else None


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\]8;;[^\x07]*\x07")


def _clean(raw: bytes) -> str:
    return _ANSI_RE.sub("", raw.decode(errors="replace")).replace("\r", "").strip()


# ---------------------------------------------------------------------------
# PTY session + account picker
# ---------------------------------------------------------------------------

def _claude_argv() -> list[str]:
    cfg = load_config()
    return shlex.split(os.environ.get("CCSWITCH_CLAUDE_BIN", cfg.claude_bin))


def resolve_login_key(server: str) -> str:
    """Map a short server name to the FULL key `claude mcp login` expects.

    The authoritative source is the raw key in each account's
    mcp-needs-auth-cache.json (e.g. `plugin:railway:railway`). Falls back to the
    short name for user-scope servers not present in any cache.
    """
    root = paths.cfg_root()
    if root.is_dir():
        for acct_dir in sorted(root.iterdir()):
            cache = read_json_safe(acct_dir / "mcp-needs-auth-cache.json")
            if not isinstance(cache, dict):
                continue
            for key in cache:
                if _bare(key) == server:
                    return key
    return server


def default_account_picker(server: str) -> tuple[str, Path]:
    flagged = scan._needs_auth_by_server().get(server) or []
    candidates = list(flagged) + accounts._anthropic_account_names()
    for name in candidates:
        return name, configdir.compose(name)
    raise McpAuthError("No authenticated account is available to authorize with.")


class PtyLoginSession:
    """Runs `claude mcp login <full-key> --no-browser` under a PTY (required —
    a pipe stdin makes claude self-terminate as non-interactive)."""

    def __init__(self, server: str, account: str, cfg_dir: Path) -> None:
        self.account = account
        self.cfg_dir = cfg_dir
        self._server = server
        self._pid: int | None = None
        self._fd: int | None = None
        self._buf = b""
        self._exit_code: int | None = None  # cached on first reap (never waitpid twice)

    def _spawn(self) -> None:
        key = resolve_login_key(self._server)
        argv = _claude_argv() + ["mcp", "login", key, "--no-browser"]
        from server.sessions.account_auth import helper_env
        # writes_back: `claude mcp login` stores the server's OAuth token in the config dir's
        # .credentials.json. It must land in the real account dir — the host-local scratch copy
        # one-shot LLM helpers get (#1005) would strand the token on this host.
        env = helper_env(str(self.cfg_dir), writes_back=True)
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.execvpe(argv[0], argv, env)
            except Exception:
                os._exit(127)
        self._pid, self._fd = pid, fd

    async def _read_chunk(self, timeout: float) -> bytes:
        assert self._fd is not None
        fd = self._fd
        loop = asyncio.get_running_loop()

        def _blocking() -> bytes:
            r, _, _ = select.select([fd], [], [], timeout)
            if not r:
                return b""
            try:
                return os.read(fd, 4096)
            except OSError:
                return b""

        return await loop.run_in_executor(None, _blocking)

    async def read_auth_url(self, timeout: float) -> str:
        if self._pid is None:
            self._spawn()
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            chunk = await self._read_chunk(1.0)
            if chunk:
                self._buf += chunk
                url = _extract_url(self._buf.decode(errors="replace"))
                if url:
                    return url
            elif self._reap() is not None:
                # Process exited before emitting any URL — stdio servers do this
                # ("doesn't support OAuth login"). Surface the message verbatim.
                msg = _clean(self._buf) or "Authentication is not supported for this server."
                raise McpAuthError(msg)
        raise McpAuthError("Timed out waiting for the authorization URL.")

    async def complete(self, redirect_url: str, timeout: float) -> None:
        assert self._fd is not None
        os.write(self._fd, (redirect_url.strip() + "\n").encode())
        deadline = asyncio.get_running_loop().time() + timeout
        tail = b""
        while asyncio.get_running_loop().time() < deadline:
            chunk = await self._read_chunk(1.0)
            if chunk:
                tail += chunk
            elif (code := self._reap()) is not None:
                low = _clean(tail).lower()
                if code != 0 or "error" in low or "failed" in low or "invalid" in low:
                    raise McpAuthError(_clean(tail) or "MCP server rejected the authorization.")
                return
        raise McpAuthError("Timed out completing authorization.")

    def _reap(self) -> int | None:
        """Return the exit code once, caching it. None while still running.

        Reaps at most once — a second os.waitpid would raise ChildProcessError
        and lose the code (the bug the pre-flight scan flagged)."""
        if self._exit_code is not None:
            return self._exit_code
        if self._pid is None:
            return None
        try:
            pid, status = os.waitpid(self._pid, os.WNOHANG)
        except ChildProcessError:
            self._exit_code = -1  # reaped externally; real code unknown → treat as failure
            return self._exit_code
        if pid == 0:
            return None  # still running
        self._exit_code = os.waitstatus_to_exitcode(status)
        return self._exit_code

    def terminate(self) -> None:
        if self._pid is not None and self._exit_code is None:
            try:
                os.kill(self._pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            # Bounded wait for graceful exit, then SIGKILL, then reap — never leave a zombie.
            for _ in range(10):
                if self._reap() is not None:
                    break
                time.sleep(0.02)
            else:
                try:
                    os.kill(self._pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                self._reap()
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


def build_default_manager() -> McpAuthManager:
    return McpAuthManager(
        session_factory=lambda server, account, cfg_dir: PtyLoginSession(server, account, cfg_dir),
        account_picker=default_account_picker,
    )
