# MCP Auth Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Fix auth" action to the MCP settings page that authenticates an MCP server once via `claude mcp login <server> --no-browser` (headless paste-back OAuth) and clears the stale "needs auth" state across all accounts.

**Architecture:** A new `server/mcp_auth.py` module holds `McpAuthManager` — a one-at-a-time state machine (mirroring `server/reauth.py`) that owns a live PTY subprocess running `claude mcp login <server> --no-browser`. The subprocess prints an authorization URL (surfaced to the UI), then blocks on stdin for the user-pasted redirect URL. On success, the newly-written `mcpOAuth` blob is merged into the shared MCP store (`paths.shared_mcp_path()`, which every account's `.credentials.json` is composed from) and the per-account `mcp-needs-auth-cache.json` entries for that server are cleared across all accounts. New routes live under `/api/library/mcp/{name}/auth`; the frontend mirrors the existing reauth polling/paste pattern.

**Tech Stack:** Python 3 / FastAPI / asyncio / `pty` + `os` for the PTY child · React + TypeScript + React Query + Tailwind/shadcn · pytest / vitest.

**Spec:** This plan is self-contained (approved Approach A from the brainstorming session — no separate design doc). Key discovery: `claude mcp login <name> --no-browser` "Print the authorization URL instead of opening a browser (for SSH/headless sessions — paste the redirect URL back when prompted)".

## Global Constraints

- **Never fork/patch the claude binary** (Constitution I). We only *invoke* `claude mcp login`/`logout`.
- **Strict per-process isolation via `CLAUDE_CONFIG_DIR`** (Constitution II). The login subprocess MUST set `CLAUDE_CONFIG_DIR` to the chosen account's composed cfg dir and inherit nothing account-specific from the ambient env.
- **Verify against the real binary, not assumptions** (Constitution III). Task 1 is a mandatory probe of the real CLI I/O contract; downstream parsing is written against the captured output.
- **No parallel implementations** (Constitution VI). MCP auth is a genuinely distinct domain object from account reauth (server-scoped, shared-store merge, cache clear), so it gets its own manager — but it reuses `ccswitch.paths`, `ccswitch.creds`, `ccswitch.configdir`, `server.library.scan.read_json_safe`, and the frontend polling pattern rather than re-deriving them.
- **claude binary resolution**: use `shlex.split(os.environ.get("CCSWITCH_CLAUDE_BIN", cfg.claude_bin))` — the exact one-liner already used in `src/ccswitch/launcher.py:16` and `runner.py:19`.
- **Tests gate = four steps**: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`. Web: `vitest run`, `tsc --noEmit`, `eslint` (run in `web/`).
- **New server code goes under `server/`** (not `src/ccswitch/`) so the shared-venv shadowing caveat (deploy worktree `.pth`) does not silently test production code. Pure path/store logic imports `ccswitch.paths` for locations but lives in `server/mcp_auth.py`.
- **Credentials never logged.** Redirect URLs, tokens, and `.credentials.json` contents must never be written to logs or audit detail. Audit records server name + outcome only.

---

## Known Facts (exact symbols this plan calls)

- `ccswitch.paths.cfg_root() -> Path` — dir holding per-account cfg dirs (`~/.claude-accounts/cfg`).
- `ccswitch.paths.account_cfg_dir(name) -> Path` — `cfg_root()/name`; also where `mcp-needs-auth-cache.json` lives.
- `ccswitch.paths.shared_mcp_path() -> Path` — the shared MCP OAuth store.
- `ccswitch.creds.read_json(path) -> dict`, `write_json_secure(path, dict)` (atomic, 0600), `split_credentials(full) -> (claude, mcp)`.
- `ccswitch.configdir.compose(name) -> Path` — composes/writes the account cfg dir (`.claude.json` with shared `mcpServers`, `.credentials.json`); returns the cfg dir path.
- `ccswitch.accounts._anthropic_account_names() -> list[str]` — authenticated Anthropic accounts.
- `server.library.scan.read_json_safe(path) -> dict | None`, `scan._needs_auth_by_server() -> {server: [accounts]}`, `scan.list_mcp_servers()`.
- `server.library.scan` cache key format: bare `server` (user scope) or `plugin:<plugin>:<server>`.
- Reauth attachment to copy: `server/app.py:597` (`app.state.reauth = ...`), `server/ipc/serve.py:84` (daemon), `server/ipc/reauth_rpc.py:12` (`register_reauth(app, reauth)`).
- Reauth routes to mirror: `server/api/accounts.py:662` (start), `:710` (status, `scoped=True`), `:729` (cancel), `:761` (submit code).
- Frontend patterns to mirror: `web/src/api/hooks.ts:442` (`useReauthStatus`, `refetchInterval` 1500 ms, stop on terminal), `:435` (`useSubmitReauthCode`); types at `web/src/api/types.ts:3`; UI at `web/src/components/AccountsManager.tsx:601` (awaiting-code URL+paste block); MCP UI at `web/src/components/library/McpSection.tsx`.

---

## Task 1: Probe the real `claude mcp login --no-browser` I/O contract

**Files:**
- Create: `docs/superpowers/plans/notes/mcp-login-io.md` (captured evidence — committed so downstream tasks cite it)

**Interfaces:**
- Produces: the exact text shape of (a) the authorization-URL line and (b) the redirect-paste prompt, used to write the parser in Task 4.

- [ ] **Step 1: Pick a real HTTP needs-auth server + an account that has it.**

Run:
```bash
python -c "from server.library import scan; import json; print(json.dumps(scan._needs_auth_by_server(), indent=2))"
```
Choose a server whose transport is `http`/`sse` (e.g. `notion` or `railway`) and note one account from its list (e.g. `tonystein85_gmail.com`).

- [ ] **Step 2: Compose that account's cfg dir and capture the URL-emit phase in a PTY.**

Run (replace `SERVER` and `ACCT`):
```bash
python - <<'PY'
import os, pty, select, time, shlex
from ccswitch import configdir, paths
ACCT, SERVER = "ACCT", "SERVER"
cfg = str(configdir.compose(ACCT))
argv = shlex.split(os.environ.get("CCSWITCH_CLAUDE_BIN", "claude")) + ["mcp", "login", SERVER, "--no-browser"]
env = {**os.environ, "CLAUDE_CONFIG_DIR": cfg}
pid, fd = pty.fork()
if pid == 0:
    os.execvpe(argv[0], argv, env); os._exit(127)
buf = b""; deadline = time.time() + 30
while time.time() < deadline:
    r, _, _ = select.select([fd], [], [], 1.0)
    if fd in r:
        try: chunk = os.read(fd, 4096)
        except OSError: break
        if not chunk: break
        buf += chunk
        if b"http" in buf.lower() and (b"paste" in buf.lower() or b"redirect" in buf.lower()):
            break
print("=== RAW OUTPUT (URL phase) ===")
print(buf.decode(errors="replace"))
import signal; os.kill(pid, signal.SIGTERM)
PY
```
**Do NOT complete the OAuth** (no side effects occur until a redirect URL is submitted). SIGTERM aborts cleanly.

- [ ] **Step 3: Record findings.**

Write `docs/superpowers/plans/notes/mcp-login-io.md` capturing verbatim: the line that carries the `https://…` authorize URL, the exact prompt text that requests the pasted redirect URL, and (if observed) how a stdio server differs (it may error immediately — note the message shape). Redact the URL's query params (they contain PKCE state) — keep only the structural shape.

- [ ] **Step 4: Commit.**

```bash
git add docs/superpowers/plans/notes/mcp-login-io.md
git commit -m "docs(mcp-auth): capture claude mcp login --no-browser I/O contract"
```

**Note:** If the probe reveals the prompt/URL shape differs from the Task 4 assumptions (first `https://` token = URL; process blocks after emitting it), update Task 4's `_extract_url` regex and the "awaiting" heuristic accordingly before implementing.

---

## Task 2: `McpAuthState` dataclass + serialization

**Files:**
- Create: `server/mcp_auth.py`
- Test: `server/tests/test_mcp_auth_state.py`

**Interfaces:**
- Produces: `McpAuthState` with fields `state: str`, `server: str | None`, `message: str | None`, `auth_url: str | None`, `accounts: list[str]`, `started_at: float | None`; `.to_dict()` (camelCase) and `.from_dict()`. States: `idle | starting | awaiting_redirect | exchanging | success | error | cancelled`.

- [ ] **Step 1: Write the failing test.**

```python
# server/tests/test_mcp_auth_state.py
from server.mcp_auth import McpAuthState

def test_to_dict_camelcases_and_roundtrips():
    s = McpAuthState(state="awaiting_redirect", server="notion",
                     auth_url="https://x/y", accounts=["a", "b"], started_at=123.0)
    d = s.to_dict()
    assert d == {"state": "awaiting_redirect", "server": "notion", "message": None,
                 "authUrl": "https://x/y", "accounts": ["a", "b"], "startedAt": 123.0}
    assert McpAuthState.from_dict(d).server == "notion"

def test_defaults_are_idle():
    assert McpAuthState().to_dict()["state"] == "idle"
    assert McpAuthState().to_dict()["accounts"] == []
```

- [ ] **Step 2: Run to verify it fails.**

Run: `pytest server/tests/test_mcp_auth_state.py -v`
Expected: FAIL (`ModuleNotFoundError: server.mcp_auth`).

- [ ] **Step 3: Implement `McpAuthState` in `server/mcp_auth.py`.**

```python
"""McpAuthManager — one-at-a-time MCP-server auth via `claude mcp login`.

Mirrors server/reauth.py's state-machine shape. Owns a live PTY subprocess
that prints an authorize URL (surfaced to the UI) then blocks on stdin for the
user-pasted redirect URL. On success, merges the new mcpOAuth into the shared
store and clears stale per-account needs-auth caches.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

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
```

- [ ] **Step 4: Run to verify it passes.**

Run: `pytest server/tests/test_mcp_auth_state.py -v`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add server/mcp_auth.py server/tests/test_mcp_auth_state.py
git commit -m "feat(mcp-auth): add McpAuthState dataclass"
```

---

## Task 3: Shared-store merge + cross-account cache-clear helpers

**Files:**
- Modify: `server/mcp_auth.py`
- Test: `server/tests/test_mcp_auth_helpers.py`

**Interfaces:**
- Produces:
  - `merge_mcp_oauth_from_cfg(cfg_dir: Path) -> int` — read `<cfg_dir>/.credentials.json`, extract `mcpOAuth`, merge into `paths.shared_mcp_path()` (last-writer-wins per key). Returns number of server keys now present. No-op returning 0 if absent/empty.
  - `clear_needs_auth_cache(server: str) -> list[str]` — iterate `paths.cfg_root()` account dirs, remove any key in each `mcp-needs-auth-cache.json` whose bare name (`key.rsplit(":",1)[-1]` when `key.startswith("plugin:")`, else `key`) equals `server`; write back atomically. Returns the account names that had an entry cleared.

- [ ] **Step 1: Write the failing tests.**

```python
# server/tests/test_mcp_auth_helpers.py
import json
from pathlib import Path
import pytest
from server import mcp_auth

@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    cfg_root = tmp_path / "cfg"; cfg_root.mkdir()
    shared = tmp_path / "store" / "mcp.shared.json"
    monkeypatch.setattr(mcp_auth.paths, "cfg_root", lambda: cfg_root)
    monkeypatch.setattr(mcp_auth.paths, "shared_mcp_path", lambda: shared)
    return cfg_root, shared

def test_merge_adds_new_server_keys(fake_root):
    cfg_root, shared = fake_root
    acct = cfg_root / "alice"; acct.mkdir()
    (acct / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"x": 1}, "mcpOAuth": {"notion": {"tok": "A"}}}))
    n = mcp_auth.merge_mcp_oauth_from_cfg(acct)
    assert n == 1
    assert json.loads(shared.read_text())["notion"] == {"tok": "A"}

def test_merge_preserves_other_servers(fake_root):
    cfg_root, shared = fake_root
    shared.parent.mkdir(parents=True)
    shared.write_text(json.dumps({"linear": {"tok": "L"}}))
    acct = cfg_root / "alice"; acct.mkdir()
    (acct / ".credentials.json").write_text(json.dumps({"mcpOAuth": {"notion": {"tok": "A"}}}))
    mcp_auth.merge_mcp_oauth_from_cfg(acct)
    data = json.loads(shared.read_text())
    assert set(data) == {"linear", "notion"}

def test_merge_missing_file_is_noop(fake_root):
    cfg_root, _ = fake_root
    acct = cfg_root / "empty"; acct.mkdir()
    assert mcp_auth.merge_mcp_oauth_from_cfg(acct) == 0

def test_clear_removes_bare_and_plugin_keys_across_accounts(fake_root):
    cfg_root, _ = fake_root
    a = cfg_root / "alice"; a.mkdir()
    b = cfg_root / "bob"; b.mkdir()
    (a / "mcp-needs-auth-cache.json").write_text(json.dumps({"notion": {"t": 1}, "linear": {"t": 2}}))
    (b / "mcp-needs-auth-cache.json").write_text(json.dumps({"plugin:notion:notion": {"t": 3}}))
    cleared = mcp_auth.clear_needs_auth_cache("notion")
    assert set(cleared) == {"alice", "bob"}
    assert json.loads((a / "mcp-needs-auth-cache.json").read_text()) == {"linear": {"t": 2}}
    assert json.loads((b / "mcp-needs-auth-cache.json").read_text()) == {}

def test_clear_no_match_returns_empty(fake_root):
    cfg_root, _ = fake_root
    a = cfg_root / "alice"; a.mkdir()
    (a / "mcp-needs-auth-cache.json").write_text(json.dumps({"linear": {"t": 2}}))
    assert mcp_auth.clear_needs_auth_cache("notion") == []
```

- [ ] **Step 2: Run to verify it fails.**

Run: `pytest server/tests/test_mcp_auth_helpers.py -v`
Expected: FAIL (`AttributeError: module 'server.mcp_auth' has no attribute 'merge_mcp_oauth_from_cfg'`).

- [ ] **Step 3: Implement the helpers in `server/mcp_auth.py`.**

Add imports and functions:
```python
from pathlib import Path

from ccswitch import paths
from ccswitch.creds import read_json, write_json_secure
from server.library.scan import read_json_safe


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
```

- [ ] **Step 4: Run to verify it passes.**

Run: `pytest server/tests/test_mcp_auth_helpers.py -v`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add server/mcp_auth.py server/tests/test_mcp_auth_helpers.py
git commit -m "feat(mcp-auth): shared-store merge + cross-account cache-clear helpers"
```

---

## Task 4: `McpAuthManager` state machine (injectable session, no real subprocess)

**Files:**
- Modify: `server/mcp_auth.py`
- Test: `server/tests/test_mcp_auth_manager.py`

**Interfaces:**
- Consumes: `McpAuthState`, `merge_mcp_oauth_from_cfg`, `clear_needs_auth_cache` (Tasks 2–3).
- Produces:
  - `class McpAuthBusy(Exception)`.
  - `LoginSession` protocol: `account: str`, `cfg_dir: Path`, `async read_auth_url(timeout: float) -> str`, `async complete(redirect_url: str, timeout: float) -> None` (raises `McpAuthError` on failure), `terminate() -> None`.
  - `class McpAuthError(Exception)`.
  - `class McpAuthManager` with injectable `session_factory: Callable[[str, str, Path], LoginSession]`, `account_picker: Callable[[str], tuple[str, Path]]`, `clock: Callable[[], float]`, `url_timeout_s`, `complete_timeout_s`. Methods: `async start(server) -> McpAuthState`, `status(server) -> McpAuthState`, `async submit_redirect(server, url) -> McpAuthState`, `async cancel(server) -> McpAuthState`, `async shutdown()`.

- [ ] **Step 1: Write the failing tests (fake session + fake picker).**

```python
# server/tests/test_mcp_auth_manager.py
import asyncio
from pathlib import Path
import pytest
from server import mcp_auth
from server.mcp_auth import McpAuthManager, McpAuthError

class FakeSession:
    def __init__(self, account, cfg_dir, *, url="https://auth/x?state=s", fail=None):
        self.account, self.cfg_dir = account, cfg_dir
        self._url, self._fail = url, fail
        self.terminated = False
        self.submitted = None
    async def read_auth_url(self, timeout):
        return self._url
    async def complete(self, redirect_url, timeout):
        self.submitted = redirect_url
        if self._fail:
            raise McpAuthError(self._fail)
    def terminate(self):
        self.terminated = True

def make_manager(monkeypatch, tmp_path, *, fail=None, calls=None):
    cfg = tmp_path / "cfg-alice"; cfg.mkdir()
    sess = FakeSession("alice", cfg, fail=fail)
    monkeypatch.setattr(mcp_auth, "merge_mcp_oauth_from_cfg", lambda d: (calls.append(("merge", d)) or 1) if calls is not None else 1)
    monkeypatch.setattr(mcp_auth, "clear_needs_auth_cache", lambda s: (calls.append(("clear", s)) or ["alice"]) if calls is not None else ["alice"])
    mgr = McpAuthManager(
        session_factory=lambda server, acct, d: sess,
        account_picker=lambda server: ("alice", cfg),
    )
    return mgr, sess

@pytest.mark.asyncio
async def test_start_surfaces_auth_url(monkeypatch, tmp_path):
    mgr, sess = make_manager(monkeypatch, tmp_path)
    st = await mgr.start("notion")
    # start returns quickly as 'starting'; wait for URL read.
    for _ in range(50):
        if mgr.status("notion").state == "awaiting_redirect":
            break
        await asyncio.sleep(0.01)
    s = mgr.status("notion")
    assert s.state == "awaiting_redirect"
    assert s.auth_url == "https://auth/x?state=s"
    assert s.server == "notion"

@pytest.mark.asyncio
async def test_submit_redirect_success_merges_and_clears(monkeypatch, tmp_path):
    calls = []
    mgr, sess = make_manager(monkeypatch, tmp_path, calls=calls)
    await mgr.start("notion")
    for _ in range(50):
        if mgr.status("notion").state == "awaiting_redirect": break
        await asyncio.sleep(0.01)
    st = await mgr.submit_redirect("notion", "http://localhost:1/callback?code=abc")
    assert st.state == "success"
    assert sess.submitted == "http://localhost:1/callback?code=abc"
    assert ("merge", sess.cfg_dir) in calls
    assert ("clear", "notion") in calls
    assert st.accounts == ["alice"]

@pytest.mark.asyncio
async def test_submit_redirect_failure_sets_error(monkeypatch, tmp_path):
    mgr, sess = make_manager(monkeypatch, tmp_path, fail="server rejected code")
    await mgr.start("notion")
    for _ in range(50):
        if mgr.status("notion").state == "awaiting_redirect": break
        await asyncio.sleep(0.01)
    st = await mgr.submit_redirect("notion", "http://localhost/x")
    assert st.state == "error"
    assert "rejected" in (st.message or "")

@pytest.mark.asyncio
async def test_second_start_while_running_raises_busy(monkeypatch, tmp_path):
    mgr, sess = make_manager(monkeypatch, tmp_path)
    await mgr.start("notion")
    with pytest.raises(mcp_auth.McpAuthBusy):
        await mgr.start("railway")

@pytest.mark.asyncio
async def test_cancel_terminates_and_idles(monkeypatch, tmp_path):
    mgr, sess = make_manager(monkeypatch, tmp_path)
    await mgr.start("notion")
    st = await mgr.cancel("notion")
    assert st.state in ("cancelled", "idle")
    assert sess.terminated is True

@pytest.mark.asyncio
async def test_status_scoped_to_active_server(monkeypatch, tmp_path):
    mgr, sess = make_manager(monkeypatch, tmp_path)
    await mgr.start("notion")
    assert mgr.status("railway").state == "idle"  # different server → fresh idle

@pytest.mark.asyncio
async def test_read_url_error_surfaces_cli_message(monkeypatch, tmp_path):
    # stdio graceful-degradation: read_auth_url raises McpAuthError with the
    # CLI's verbatim message; the manager lands in error carrying that message.
    cfg = tmp_path / "cfg-alice"; cfg.mkdir()
    class ErrSession:
        account, cfg_dir = "alice", cfg
        async def read_auth_url(self, timeout):
            raise mcp_auth.McpAuthError('"aws-mcp" doesn\'t support OAuth login')
        async def complete(self, u, t): ...
        def terminate(self): pass
    mgr = McpAuthManager(session_factory=lambda s, a, d: ErrSession(),
                         account_picker=lambda s: ("alice", cfg))
    await mgr.start("aws-mcp")
    for _ in range(50):
        if mgr.status("aws-mcp").state in mcp_auth.TERMINAL: break
        await asyncio.sleep(0.01)
    s = mgr.status("aws-mcp")
    assert s.state == "error"
    assert "doesn't support OAuth login" in (s.message or "")
```

- [ ] **Step 2: Run to verify it fails.**

Run: `pytest server/tests/test_mcp_auth_manager.py -v`
Expected: FAIL (missing `McpAuthManager`/`McpAuthError`/`McpAuthBusy`).

- [ ] **Step 3: Implement the manager in `server/mcp_auth.py`.**

```python
import asyncio
import time
from typing import Awaitable, Callable, Protocol


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
        self._task: asyncio.Task | None = None
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
```

- [ ] **Step 4: Run to verify it passes.**

Run: `pytest server/tests/test_mcp_auth_manager.py -v`
Expected: PASS. Also run `mypy server/ --ignore-missing-imports` and fix any typing gaps.

- [ ] **Step 5: Commit.**

```bash
git add server/mcp_auth.py server/tests/test_mcp_auth_manager.py
git commit -m "feat(mcp-auth): McpAuthManager state machine with injectable session"
```

---

## Task 5: Production PTY `LoginSession` + default account picker

**Files:**
- Modify: `server/mcp_auth.py`
- Test: `server/tests/test_mcp_auth_pty.py`

**Confirmed CLI contract (from Task 1 + follow-up probe — build to THIS, not the help text):**
- MUST run under a **PTY** (`pty.fork`). With a plain pipe stdin, `claude` self-terminates ("stdin isn't a terminal").
- `claude mcp login <FULL_KEY> --no-browser` prints `Visit this URL to authorize:` then the authorize URL (OSC-8 hyperlink: the plain URL is the display text, wrapped in `\x1b]8;;…\x07` escapes), then `Or paste the redirect URL here:` and blocks (a loopback listener also comes up, but we use the paste path).
- `<FULL_KEY>` is the raw key stored in `mcp-needs-auth-cache.json` — `plugin:<plugin>:<server>` for plugin servers, bare `<server>` for user servers. The short name `scan` returns does NOT work ("No MCP server named").
- Completion: write the pasted `http://localhost:<port>/callback?code=…&state=…` + `\n` to the PTY; `claude` parses code/state from the string (port irrelevant) and exchanges, then exits 0.
- stdio servers exit 1 immediately: `"<key>" doesn't support OAuth login — it's only available for HTTP and SSE servers.` (no URL) — surface verbatim.

**Interfaces:**
- Consumes: `LoginSession` protocol (Task 4), findings from Task 1.
- Produces:
  - `resolve_login_key(server: str) -> str` — scan `paths.cfg_root()` account dirs' `mcp-needs-auth-cache.json`; return the raw key whose bare name (`_bare`, Task 3) equals `server`; fall back to `server` if none found (user-scope).
  - `class PtyLoginSession` implementing `LoginSession` — forks `claude mcp login <resolve_login_key(server)> --no-browser` under a PTY with `CLAUDE_CONFIG_DIR=<cfg_dir>`. `read_auth_url` reads until the first `https://…` token; if the process exits BEFORE any URL, raises `McpAuthError` carrying the captured output (stdio case). `complete` writes `redirect_url + "\n"` and reads to EOF, raising `McpAuthError` on nonzero exit or error text.
  - `default_account_picker(server) -> tuple[str, Path]` — choose an account (prefer one from `scan._needs_auth_by_server().get(server)`, else first `accounts._anthropic_account_names()`), `configdir.compose(name)`, return `(name, cfg_dir)`; raise `McpAuthError` if no account.
  - `_extract_url(text) -> str | None` (module function, unit-tested) — regex excludes control chars so OSC-8 escape bytes aren't captured.
  - `build_default_manager() -> McpAuthManager` wiring the two together.

- [ ] **Step 1: Write the failing test (URL extraction + picker, no real fork).**

```python
# server/tests/test_mcp_auth_pty.py
import pytest
from pathlib import Path
from server import mcp_auth
from server.mcp_auth import _extract_url, default_account_picker, McpAuthError

def test_extract_url_grabs_first_https_token():
    txt = "Open the following URL to authorize:\n  https://mcp.notion.com/authorize?state=abc \nPaste the redirect URL:"
    assert _extract_url(txt) == "https://mcp.notion.com/authorize?state=abc"

def test_extract_url_strips_osc8_escape_bytes():
    # Real output wraps the URL in an OSC-8 hyperlink; the plain URL is the
    # display text but escape bytes (\x1b ... \x07) surround it.
    txt = "Visit this URL to authorize:\r\n  \x1b]8;;https://backboard.railway.com/oauth/auth?state=s\x07https://backboard.railway.com/oauth/auth?state=s\x1b]8;;\x07\r\n"
    assert _extract_url(txt) == "https://backboard.railway.com/oauth/auth?state=s"

def test_extract_url_none_when_absent():
    assert _extract_url("connecting...\n") is None

def test_resolve_login_key_returns_full_plugin_key(monkeypatch, tmp_path):
    cfg_root = tmp_path / "cfg"; cfg_root.mkdir()
    (cfg_root / "alice").mkdir()
    import json
    (cfg_root / "alice" / "mcp-needs-auth-cache.json").write_text(
        json.dumps({"plugin:railway:railway": {"t": 1}}))
    monkeypatch.setattr(mcp_auth.paths, "cfg_root", lambda: cfg_root)
    assert mcp_auth.resolve_login_key("railway") == "plugin:railway:railway"

def test_resolve_login_key_falls_back_to_short_name(monkeypatch, tmp_path):
    cfg_root = tmp_path / "cfg"; cfg_root.mkdir()
    monkeypatch.setattr(mcp_auth.paths, "cfg_root", lambda: cfg_root)
    assert mcp_auth.resolve_login_key("customserver") == "customserver"

def test_picker_prefers_needs_auth_account(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_auth.scan, "_needs_auth_by_server", lambda: {"notion": ["bob", "alice"]})
    monkeypatch.setattr(mcp_auth.configdir, "compose", lambda n: tmp_path / n)
    name, cfg = default_account_picker("notion")
    assert name == "bob"  # first in the needs-auth list
    assert cfg == tmp_path / "bob"

def test_picker_raises_when_no_account(monkeypatch):
    monkeypatch.setattr(mcp_auth.scan, "_needs_auth_by_server", lambda: {})
    monkeypatch.setattr(mcp_auth.accounts, "_anthropic_account_names", lambda: [])
    with pytest.raises(McpAuthError):
        default_account_picker("notion")
```

- [ ] **Step 2: Run to verify it fails.**

Run: `pytest server/tests/test_mcp_auth_pty.py -v`
Expected: FAIL (missing `_extract_url` / `default_account_picker`).

- [ ] **Step 3: Implement PTY session + picker in `server/mcp_auth.py`.**

Add imports (`os`, `pty`, `re`, `select`, `shlex`, `signal`) and:
```python
from ccswitch import accounts, configdir
from ccswitch.config import load_config
from server.library import scan

# Exclude whitespace, quotes, angle brackets AND control chars (\x00-\x20) so
# the OSC-8 escape bytes (\x1b ... \x07) that wrap the real URL aren't captured.
_URL_RE = re.compile(r"https://[^\s\"'<>\x00-\x20]+")


def _extract_url(text: str) -> str | None:
    m = _URL_RE.search(text)
    return m.group(0) if m else None


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
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.cfg_dir)}
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.execvpe(argv[0], argv, env)
            except Exception:
                os._exit(127)
        self._pid, self._fd = pid, fd

    async def _read_chunk(self, timeout: float) -> bytes:
        assert self._fd is not None
        loop = asyncio.get_running_loop()
        def _blocking() -> bytes:
            r, _, _ = select.select([self._fd], [], [], timeout)
            if not r:
                return b""
            try:
                return os.read(self._fd, 4096)
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
            self._exit_code = 0
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
```

`_clean` strips ANSI/OSC escape sequences and CRs so surfaced messages are readable. Add it near `_extract_url`:
```python
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\]8;;[^\x07]*\x07")

def _clean(raw: bytes) -> str:
    return _ANSI_RE.sub("", raw.decode(errors="replace")).replace("\r", "").strip()
```

- [ ] **Step 4: Run to verify it passes.**

Run: `pytest server/tests/test_mcp_auth_pty.py -v` then `mypy server/ --ignore-missing-imports`.
Expected: PASS / clean.

- [ ] **Step 5: Commit.**

```bash
git add server/mcp_auth.py server/tests/test_mcp_auth_pty.py
git commit -m "feat(mcp-auth): PTY login session + default account picker"
```

---

## Task 6: API routes + app wiring

**Files:**
- Modify: `server/api/library.py` (add auth routes)
- Modify: `server/app.py` (attach `app.state.mcp_auth`, shutdown)
- Modify: `server/ipc/serve.py` (attach on the daemon app for parity)
- Test: `server/tests/test_mcp_auth_routes.py`

**Interfaces:**
- Consumes: `McpAuthManager`, `McpAuthBusy`, `build_default_manager`.
- Produces (all under existing `/api/library` prefix):
  - `POST /mcp/{name}/auth` → 202, body `McpAuthState.to_dict()`; 409 on `McpAuthBusy`.
  - `GET /mcp/{name}/auth` → `McpAuthState.to_dict()` (scoped).
  - `POST /mcp/{name}/auth/redirect` body `{redirectUrl: str}` → `McpAuthState.to_dict()`.
  - `POST /mcp/{name}/auth/cancel` → `McpAuthState.to_dict()`.
- `request.app.state.mcp_auth` holds the manager.

- [ ] **Step 1: Write the failing test (fake manager on app.state).**

```python
# server/tests/test_mcp_auth_routes.py
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.api import library
from server.mcp_auth import McpAuthState, McpAuthBusy

class FakeMgr:
    def __init__(self): self.calls = []
    async def start(self, server):
        if server == "busy": raise McpAuthBusy("other")
        self.calls.append(("start", server)); return McpAuthState(state="starting", server=server)
    def status(self, server): return McpAuthState(state="awaiting_redirect", server=server, auth_url="https://x")
    async def submit_redirect(self, server, url):
        self.calls.append(("redirect", server, url)); return McpAuthState(state="success", server=server, accounts=["a"])
    async def cancel(self, server):
        self.calls.append(("cancel", server)); return McpAuthState(state="cancelled", server=server)

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(library, "valid_mcp_name", lambda n: True)
    app = FastAPI(); app.include_router(library.router)
    app.state.mcp_auth = FakeMgr()
    # bypass auth dependency
    from server.auth.middleware import current_identity
    app.dependency_overrides[current_identity] = lambda: "tester"
    return TestClient(app)

def test_start_returns_202(client):
    r = client.post("/api/library/mcp/notion/auth")
    assert r.status_code == 202
    assert r.json()["server"] == "notion"

def test_start_busy_returns_409(client):
    assert client.post("/api/library/mcp/busy/auth").status_code == 409

def test_status_scoped(client):
    r = client.get("/api/library/mcp/notion/auth")
    assert r.json()["authUrl"] == "https://x"

def test_redirect_submits(client):
    r = client.post("/api/library/mcp/notion/auth/redirect", json={"redirectUrl": "http://localhost/cb?code=1"})
    assert r.json()["state"] == "success"
    assert r.json()["accounts"] == ["a"]

def test_cancel(client):
    assert client.post("/api/library/mcp/notion/auth/cancel").json()["state"] == "cancelled"
```

- [ ] **Step 2: Run to verify it fails.**

Run: `pytest server/tests/test_mcp_auth_routes.py -v`
Expected: FAIL (routes 404).

- [ ] **Step 3: Add routes to `server/api/library.py`.**

Add near the other `/mcp` routes:
```python
from fastapi import Request

from server.mcp_auth import McpAuthBusy


class RedirectBody(BaseModel):
    redirectUrl: str


def _mcp_auth(request: Request):
    mgr = getattr(request.app.state, "mcp_auth", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail={"error": "mcp_auth_unavailable"})
    return mgr


@router.post("/mcp/{name}/auth", status_code=202)
async def mcp_auth_start(name: str, request: Request,
                         identity: str = Depends(current_identity), db=Depends(get_db)) -> Any:
    if not valid_mcp_name(name):
        raise _bad("invalid server name")
    try:
        state = await _mcp_auth(request).start(name)
    except McpAuthBusy as exc:
        raise HTTPException(status_code=409, detail={"error": "mcp_auth_busy", "server": exc.server})
    await record_audit(db=db, type="library_mcp_auth_started", identity=identity, detail={"name": name})
    return state.to_dict()


@router.get("/mcp/{name}/auth")
async def mcp_auth_status(name: str, request: Request,
                          _identity: str = Depends(current_identity)) -> Any:
    if not valid_mcp_name(name):
        raise _bad("invalid server name")
    return _mcp_auth(request).status(name).to_dict()


@router.post("/mcp/{name}/auth/redirect")
async def mcp_auth_redirect(name: str, body: RedirectBody, request: Request,
                            _identity: str = Depends(current_identity)) -> Any:
    if not valid_mcp_name(name):
        raise _bad("invalid server name")
    state = await _mcp_auth(request).submit_redirect(name, body.redirectUrl)
    return state.to_dict()


@router.post("/mcp/{name}/auth/cancel")
async def mcp_auth_cancel(name: str, request: Request,
                          _identity: str = Depends(current_identity)) -> Any:
    if not valid_mcp_name(name):
        raise _bad("invalid server name")
    return (await _mcp_auth(request).cancel(name)).to_dict()
```

- [ ] **Step 4: Wire the manager onto both apps.**

In `server/app.py`, next to the `app.state.reauth = ...` block (~line 597):
```python
from server.mcp_auth import build_default_manager as _build_mcp_auth
app.state.mcp_auth = _build_mcp_auth()
```
And in the shutdown phase (near the reauth shutdown ~line 406):
```python
await bounded_wait(app.state.mcp_auth.shutdown(),
                   timeout=_SHUTDOWN_PHASE_TIMEOUT_S, phase="shutdown:mcp_auth")
```
In `server/ipc/serve.py`, next to the daemon `app.state.reauth = ReauthManager(...)` (~line 84):
```python
from server.mcp_auth import build_default_manager as _build_mcp_auth
app.state.mcp_auth = _build_mcp_auth()
```
> The PTY subprocess + cfg dirs live on whichever process owns the filesystem (the daemon/master and the single-host edge). Attaching in both `app.py` and `serve.py` matches how `reauth` is attached. Remote-mode edge→daemon proxying is out of scope (single-host dobby deployment); if remote mode ever needs it, mirror `server/ipc/reauth_rpc.py`. Add a `# NOTE:` comment to that effect at the `app.py` wiring site.

- [ ] **Step 5: Run tests + gate.**

Run: `pytest server/tests/test_mcp_auth_routes.py -v`, then the full `pytest server/tests/`, `ruff check server/`, `mypy server/ --ignore-missing-imports`.
Expected: PASS / clean.

- [ ] **Step 6: Commit.**

```bash
git add server/api/library.py server/app.py server/ipc/serve.py server/tests/test_mcp_auth_routes.py
git commit -m "feat(mcp-auth): API routes + manager wiring on app.state"
```

---

## Task 7: Frontend types + React Query hooks

**Files:**
- Modify: `web/src/api/types.ts`
- Modify: `web/src/api/hooks.ts`
- Test: `web/src/api/__tests__/mcpAuth.test.ts` (or the repo's colocated test convention — match existing hook tests)

**Interfaces:**
- Consumes: routes from Task 6.
- Produces:
  - `McpAuthState` TS interface: `{ state: 'idle'|'starting'|'awaiting_redirect'|'exchanging'|'success'|'error'|'cancelled'; server?: string|null; message?: string|null; authUrl?: string|null; accounts: string[]; startedAt?: number|null }`.
  - `useStartMcpAuth()` → `POST /library/mcp/{name}/auth`.
  - `useMcpAuthStatus(name, enabled)` → `GET`, `refetchInterval` 1500 ms, stop on terminal (`success`/`error`/`cancelled`).
  - `useSubmitMcpRedirect()` → `POST …/auth/redirect` `{ redirectUrl }`.
  - `useCancelMcpAuth()` → `POST …/auth/cancel`.
  - On `success`, invalidate `QK.libraryMcp`.

- [ ] **Step 1: Write the failing test.**

```typescript
// mirror the existing hook test setup (QueryClientProvider + mocked api)
import { describe, it, expect, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { useMcpAuthStatus } from '../hooks'
import * as apimod from '../client'

it('stops polling on terminal state', async () => {
  vi.spyOn(apimod.api, 'get').mockResolvedValue({ state: 'success', accounts: [] })
  const { result } = renderHook(() => useMcpAuthStatus('notion', true), { wrapper })
  await waitFor(() => expect(result.current.data?.state).toBe('success'))
})
```
(Use the same `wrapper`/mock harness the existing `useReauthStatus` test uses; if none exists, add a minimal QueryClient wrapper.)

- [ ] **Step 2: Run to verify it fails.**

Run (in `web/`): `vitest run src/api`
Expected: FAIL (`useMcpAuthStatus` undefined).

- [ ] **Step 3: Add the type and hooks.**

In `types.ts`:
```typescript
export interface McpAuthState {
  state: 'idle' | 'starting' | 'awaiting_redirect' | 'exchanging' | 'success' | 'error' | 'cancelled'
  server?: string | null
  message?: string | null
  authUrl?: string | null
  accounts: string[]
  startedAt?: number | null
}
```
In `hooks.ts` (mirror `useReauthStatus`/`useSubmitReauthCode`):
```typescript
export function useStartMcpAuth() {
  return useMutation<McpAuthState, ApiRequestError, string>({
    mutationFn: (name) => api.post<McpAuthState>(`/library/mcp/${encodeURIComponent(name)}/auth`),
  })
}

export function useMcpAuthStatus(name: string, enabled: boolean) {
  return useQuery<McpAuthState>({
    queryKey: ['mcp-auth-status', name],
    queryFn: () => api.get<McpAuthState>(`/library/mcp/${encodeURIComponent(name)}/auth`),
    enabled,
    refetchInterval: (query) => {
      const s = query.state.data?.state
      return s === 'success' || s === 'error' || s === 'cancelled' ? false : 1500
    },
    refetchOnWindowFocus: false,
    staleTime: 0,
  })
}

export function useSubmitMcpRedirect() {
  const qc = useQueryClient()
  return useMutation<McpAuthState, ApiRequestError, { name: string; redirectUrl: string }>({
    mutationFn: ({ name, redirectUrl }) =>
      api.post<McpAuthState>(`/library/mcp/${encodeURIComponent(name)}/auth/redirect`, { redirectUrl }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.libraryMcp }),
  })
}

export function useCancelMcpAuth() {
  return useMutation<McpAuthState, ApiRequestError, string>({
    mutationFn: (name) => api.post<McpAuthState>(`/library/mcp/${encodeURIComponent(name)}/auth/cancel`),
  })
}
```

- [ ] **Step 4: Run to verify it passes + typecheck.**

Run (in `web/`): `vitest run src/api`, `tsc --noEmit`.
Expected: PASS / clean.

- [ ] **Step 5: Commit.**

```bash
git add web/src/api/types.ts web/src/api/hooks.ts web/src/api/__tests__/mcpAuth.test.ts
git commit -m "feat(mcp-auth): frontend types + React Query hooks"
```

---

## Task 8: Frontend UI — "Fix auth" button + inline flow in McpSection

**Files:**
- Modify: `web/src/components/library/McpSection.tsx`
- Create: `web/src/components/library/McpAuthFlow.tsx`
- Test: `web/src/components/library/__tests__/McpAuthFlow.test.tsx`

**Interfaces:**
- Consumes: hooks from Task 7; `LibraryMcpServer.needsAuth` (already present).
- Produces: `<McpAuthFlow server={name} accounts={needsAuth} />` — renders a "Fix auth" button when idle; on start, polls status; when `awaiting_redirect`, shows the auth URL (open + copy) and a redirect-URL paste box with Submit; shows exchanging spinner, then success ("Authorized — cleared for N accounts") or an error with Retry; Cancel while active.

- [ ] **Step 1: Write the failing component test.**

```tsx
// McpAuthFlow.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { McpAuthFlow } from '../McpAuthFlow'
// mock the hooks to return awaiting_redirect with an authUrl, assert URL + paste box render
it('shows the auth URL and a paste box when awaiting redirect', () => {
  // ...mock useMcpAuthStatus → { state: 'awaiting_redirect', authUrl: 'https://x', accounts: [] }
  render(<McpAuthFlow server="notion" accounts={['a']} />, { wrapper })
  expect(screen.getByText(/https:\/\/x/)).toBeInTheDocument()
  expect(screen.getByPlaceholderText(/redirect url/i)).toBeInTheDocument()
})
```

- [ ] **Step 2: Run to verify it fails.**

Run (in `web/`): `vitest run src/components/library`
Expected: FAIL (`McpAuthFlow` missing).

- [ ] **Step 3: Implement `McpAuthFlow.tsx`** (mirror `AccountsManager.tsx:601` awaiting-code block: link + copy + input + submit; states from `useMcpAuthStatus`; start disabled while active; success shows `Authorized — cleared for {accounts.length} account(s)`; error shows `message` + Retry; Cancel button while `starting`/`awaiting_redirect`/`exchanging`). Copy tone: for a stdio server that errors, surface `message` verbatim (the "degrade gracefully" behavior).

- [ ] **Step 4: Wire it into `McpSection.tsx`.**

Render `<McpAuthFlow>` inside each server card. Replace the static orange `needs auth: …` badge so that, when `needsAuth.length > 0`, the badge sits next to a **Fix auth** button that expands the flow; keep the badge text (accounts list) as the affordance label. Do not duplicate the badge — route through the single existing rendering spot.

- [ ] **Step 5: Run tests + typecheck + lint.**

Run (in `web/`): `vitest run`, `tsc --noEmit`, `eslint src/components/library`.
Expected: PASS / clean.

- [ ] **Step 6: Commit.**

```bash
git add web/src/components/library/McpSection.tsx web/src/components/library/McpAuthFlow.tsx web/src/components/library/__tests__/McpAuthFlow.test.tsx
git commit -m "feat(mcp-auth): Fix auth button + inline paste-back flow in MCP settings"
```

---

## Task 9: End-to-end manual verification + polish

**Files:**
- Modify: `CLAUDE.md` (add a short "MCP auth fix" operational note if warranted)

**Interfaces:** none (verification task).

- [ ] **Step 1: Full backend gate.**

Run: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`.
Expected: all green.

- [ ] **Step 2: Full web gate.**

Run (in `web/`): `vitest run`, `tsc --noEmit`, `eslint`.
Expected: all green.

- [ ] **Step 3: Manual smoke against a real HTTP server (use `/run` or the running instance).**

On the Settings → MCP page, click **Fix auth** on an HTTP server showing "needs auth" (e.g. `notion`). Confirm: the auth URL appears → open it on a device, authorize, copy the `localhost` redirect URL → paste + Submit → state goes `exchanging` → `success` "cleared for N accounts". Reload; confirm the "needs auth" badge for that server is gone for all listed accounts (verify `mcp-needs-auth-cache.json` no longer contains the key in each account dir, and `paths.shared_mcp_path()` now has the server key).

- [ ] **Step 4: Manual smoke of graceful degradation (stdio server).**

Click **Fix auth** on a stdio server (e.g. `aws-mcp`). Confirm the CLI's message is surfaced verbatim and the UI lands in `error` with a Retry, not a hang.

- [ ] **Step 5: Commit any polish; open PR.**

Follow the completion workflow: push `feat/mcp-auth-fix`, open a non-draft PR with the Claude Code trailer, then `gh pr merge <#> --squash --auto`.

---

## Self-Review

**Spec coverage** (Approach A requirements → task):
- Headless paste-back auth via `claude mcp login --no-browser` → Tasks 1, 5.
- One-at-a-time state machine mirroring reauth → Task 4.
- Auth once → merge shared store → clear all accounts' stale caches → Task 3 (helpers), Task 4 (wired into success).
- Attempt all servers, degrade gracefully (stdio errors surfaced) → Task 5 `complete()` failure path, Task 8 error copy, Task 9 Step 4.
- API + wiring → Task 6. Frontend hooks + UI → Tasks 7–8.
- Cross-account badge clears in UI → Task 7 (`invalidateQueries(QK.libraryMcp)`), Task 8 success copy, Task 9 Step 3.

**Placeholder scan:** Task 1 (probe) and the two "adjust to match captured output" notes are deliberate verify-against-reality steps, not placeholders — each has concrete commands. Task 8 Step 3 references the exact template block (`AccountsManager.tsx:601`) rather than restating 60 lines of JSX; acceptable since it's a mirror with the shape fully specified in the interface block.

**Type consistency:** `McpAuthState` fields/`to_dict` camelCase (`authUrl`, `startedAt`) match the TS interface (Task 7). Manager method names (`start`/`status`/`submit_redirect`/`cancel`/`shutdown`) match the route callers (Task 6) and the fake in tests. `merge_mcp_oauth_from_cfg`/`clear_needs_auth_cache` names match across Tasks 3, 4, and their monkeypatches. Route paths (`/mcp/{name}/auth`, `…/auth/redirect`, `…/auth/cancel`) match the hooks in Task 7.
