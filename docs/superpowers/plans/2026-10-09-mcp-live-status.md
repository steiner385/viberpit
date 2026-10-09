# MCP Live Status + Reauth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show whether each configured MCP server actually works, per account and host, and let the user test, reconnect and re-authenticate it from Settings or from inside a live session.

**Architecture:** Claude sessions poll the SDK's `get_mcp_status()` and emit a sanitized `McpStatus` event. The daemon upserts the result into a new `mcp_server_status` table, which becomes the single source for needs-auth. A no-prompt SDK probe fills gaps on demand, and a successful OAuth login refreshes `mcpOAuth` into account config dirs and then reconnects the affected live sessions. The web shows a status pill in Settings › MCP and an `MCP n/m` readout in the session header.

**Tech Stack:** Python 3.12 FastAPI + SQLAlchemy (async) + `claude-agent-sdk` (`ClaudeSDKClient.get_mcp_status` / `reconnect_mcp_server`); React + TS + React Query + vitest.

**Spec:** `docs/superpowers/specs/2026-10-09-mcp-live-status-design.md`. Read it before starting any task.

## Global Constraints

- Never copy the SDK status `config` field anywhere (event, DB, log, API): it carries headers and env secrets.
- `error` text: truncate to 500 chars, and replace every URL query string with `?<redacted>`.
- Filter out the internal app server (`server.sessions.tools` wire name, `viberpit.brand`) and any entry with `scope == "sdk"`.
- Poll and probe failures log at **DEBUG** (a WARNING auto-files an issue).
- Timestamps come from `server/db/clock.py:db_utc_now()`. Never use `func.now()`.
- Teardown of any background task uses `server/aio.py:cancel_and_reap()`.
- Kill switch `VIBERPIT_MCP_STATUS_ENABLED`, default `true`. When it is off there is no polling, persistence, probe or readout, and the API returns empty `status` lists.
- Migration `0078_mcp_server_status`, `down_revision = '0077_personal_quick_replies'`. Re-check the newest revision on `origin/master` before committing it.
- Settle loop: re-poll every 5 s while any server is `pending`, stopping after 30 s total. Probe: settle until no `pending` or 20 s pass. `POST /api/library/mcp/test` budget: 30 s.
- Probe concurrency: one per account (later callers await the in-flight result), and a global cap of 2 per host.
- Worst-status order for the Settings pill: `failed > needs-auth > pending > connected`. `disabled` renders neutral, and no rows renders "Not checked yet".
- New settings use the `VIBERPIT_` prefix (brand ratchet).
- Bash hook trap: never do a bare `cd web`; use `(cd web && …)`. Write commit messages to a file and pass `-F`.
- Tests: `env -u VIBERPIT_TAILNET_ALLOWLIST VIBERPIT_DEV=1`, with every `VIBERPIT_SESSIOND_*`, `VIBERPIT_DAEMON_ROLE` and `CCSWITCH_*` mirror unset. Use the main checkout's `.venv/bin/python -m pytest`, and `PYTHONPATH=$PWD/src` when `src/viberpit/` changes.

## Review Focus

1. **A server whose name contains `plugin:<p>:<s>`.** It must match the short name everywhere: Settings row, `resolve_login_key` and the session popover. Test it in Task 7 (`test_plugin_key_rows_match_short_name`).
2. **The SDK returns a status the type doesn't list** (a future CLI adds `"connecting"`). It must be stored and shown verbatim as neutral, never crash or vanish. Test it in Task 2 (`test_unknown_status_kept_verbatim`) and in Task 10's pill test.
3. **A probe while a login is in flight for the same account.** Both run `claude` under one cfg dir; the probe must not rewrite `.credentials.json`. Test it in Task 5 (`test_probe_never_composes_or_writes_credentials`).
4. **A session closes mid-settle-loop.** The poll task must be reaped without `CancelledError` leaking into `aclose`, and nothing is emitted after close. Test it in Task 3 (`test_close_during_settle_reaps_poll_task`).
5. **The mcpOAuth refresh races a CLI token rotation.** Rewriting `.credentials.json` must preserve the file's current `claudeAiOauth` byte-for-byte. Test it in Task 8 (`test_refresh_preserves_claude_oauth`).

---

### Task 1: Real-binary verification (Principle III gate)

No production code. This answers the assumptions the rest of the plan builds on. **If any answer contradicts the "Expected" column, STOP and report to the controller before Task 2.**

**Files:**
- Create: `docs/superpowers/spikes/2026-10-09-mcp-status-verification.md`
- Create (throwaway, not committed): `/tmp/mcp_spike/spike.py`

- [ ] **Step 1: Write the spike script**

```python
# /tmp/mcp_spike/spike.py — run with the deploy venv python, CLAUDE_CONFIG_DIR set to a composed account cfg dir
import asyncio, json, os, sys, time
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

async def main(cwd: str) -> None:
    opts = ClaudeAgentOptions(cwd=cwd, env={"CLAUDE_CONFIG_DIR": os.environ["CLAUDE_CONFIG_DIR"]})
    async with ClaudeSDKClient(options=opts) as c:
        t0 = time.monotonic()
        for _ in range(8):
            st = await c.get_mcp_status()
            rows = [{k: s.get(k) for k in ("name", "status", "error", "scope")} | {"tools": len(s.get("tools") or [])}
                    for s in st["mcpServers"]]
            print(round(time.monotonic() - t0, 1), json.dumps(rows))
            if not any(r["status"] == "pending" for r in rows):
                break
            await asyncio.sleep(2.5)
        if len(sys.argv) > 2:
            await c.reconnect_mcp_server(sys.argv[2])
            print("after reconnect", json.dumps([(s["name"], s["status"]) for s in (await c.get_mcp_status())["mcpServers"]]))

asyncio.run(main(sys.argv[1]))
```

- [ ] **Step 2: Run it and record the answers**

The account must be a real Anthropic account with at least one HTTP server lacking a token, and one deliberately broken stdio server added to user scope through Settings (remove it afterwards).

```bash
CLAUDE_CONFIG_DIR=$HOME/.claude-accounts/cfg/<acct> /home/tony/GitHub/viberpit-deploy/.venv-py314/bin/python -I /tmp/mcp_spike/spike.py /tmp 2>&1 | tee /tmp/mcp-spike.log
```

Record each question with its observed answer:

| # | Question | Expected |
|---|---|---|
| 1 | Status for an unauthenticated HTTP server | `needs-auth` |
| 2 | Status and `error` for a stdio server whose command doesn't exist | `failed`, non-empty error |
| 3 | Is any server `pending` at t≈0, and when does it settle? | some `pending`, settled < 20 s |
| 4 | Name format for a plugin-bundled server | `plugin:<plugin>:<server>` |
| 5 | `scope` value reported for the in-process SDK server, if one is mounted | `sdk` |
| 6 | No usage recorded: compare the account's `/api/usage` before and after the run, and check that no transcript `.jsonl` for this run appears under `projects/` | unchanged / none |
| 7 | Rerun with a 2nd arg naming a `needs-auth` server after completing its OAuth (Settings flow) under a DIFFERENT account, then copying `mcpOAuth` from `store/mcp.shared.json` into THIS cfg dir's `.credentials.json` while the client is connected: does it become `connected`? | `connected` |
| 8 | `grep -n mcpOAuth server/ipc/creds_rpc.py server/sessions/claude_authority.py` (none expected), and confirm rocky's account store is the sshfs mount of dobby's (`findmnt ~/.claude-accounts` on rocky), so worker cfg dirs ARE the shared files | none; sshfs |

- [ ] **Step 3: Write the findings doc and commit**

Write the table with the observed answers into `docs/superpowers/spikes/2026-10-09-mcp-status-verification.md`, plus the verbatim `/tmp/mcp-spike.log` excerpt (with no secrets).

```bash
git add docs/superpowers/spikes/2026-10-09-mcp-status-verification.md
git commit -F /tmp/msg-task1.txt   # "docs(spike): verify get_mcp_status / reconnect against CLI 2.1.295"
```

---

### Task 2: Status vocabulary, sanitizer, event, engine contract

**Files:**
- Create: `server/mcp_health/__init__.py` (empty), `server/mcp_health/status.py`
- Modify: `server/sessions/events.py` (add `McpStatus` after `ContextUsage`)
- Modify: `server/sessions/engine.py` (`AgentCapabilities.mcp_status`, Protocol methods)
- Modify: `server/sessions/sdk_contract.py` (add `get_mcp_status`, `reconnect_mcp_server` to `SDKClientProtocol`)
- Modify: `server/tests/fakes/fake_sdk.py` (`FakeSDKClient` support)
- Modify: `server/codex/driver.py` (`CodexAgentDriver` stubs)
- Test: `server/tests/test_mcp_health_status.py`

**Interfaces:**
- Produces:
  - `McpServerState(name: str, status: str, error: str | None, tool_count: int | None, scope: str | None)` (frozen dataclass) with `to_wire() -> dict` returning `{"name","status","error","toolCount","scope"}`
  - `sanitize_statuses(raw: object) -> list[McpServerState]`
  - `redact_error(text: str | None) -> str | None`
  - `ev.McpStatus(servers: list[McpServerState])`, wire `{"type": "mcpStatus", "servers": [...]}`
  - `AgentCapabilities.mcp_status: bool = False`
  - Protocol `async def mcp_status(self) -> list[McpServerState]` and `async def reconnect_mcp_server(self, name: str) -> None`
  - `FakeSDKClient(..., mcp_status: list[dict] | Callable[[], list[dict]] | None = None)` with `.reconnected: list[str]`

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_mcp_health_status.py
from server.mcp_health.status import McpServerState, redact_error, sanitize_statuses
from server.sessions import events as ev
from server.sessions.tools.server import SERVER_NAME


def test_sanitize_drops_config_and_counts_tools():
    raw = {"mcpServers": [{"name": "notion", "status": "connected", "scope": "user",
                           "config": {"type": "http", "headers": {"Authorization": "Bearer s3cret"}},
                           "tools": [{"name": "a"}, {"name": "b"}]}]}
    [s] = sanitize_statuses(raw)
    assert s == McpServerState("notion", "connected", None, 2, "user")
    assert "s3cret" not in repr(s) and "s3cret" not in str(s.to_wire())


def test_sanitize_filters_internal_and_sdk_scope():
    raw = {"mcpServers": [{"name": SERVER_NAME, "status": "connected"},
                          {"name": "inproc", "status": "connected", "scope": "sdk"},
                          {"name": "gh", "status": "failed", "error": "boom"}]}
    assert [s.name for s in sanitize_statuses(raw)] == ["gh"]


def test_unknown_status_kept_verbatim():
    [s] = sanitize_statuses({"mcpServers": [{"name": "x", "status": "connecting"}]})
    assert s.status == "connecting"


def test_redact_error_strips_query_and_truncates():
    out = redact_error("GET https://h/p?token=abc&x=1 failed " + "y" * 900)
    assert "abc" not in out and "?<redacted>" in out and len(out) <= 500


def test_sanitize_tolerates_garbage():
    assert sanitize_statuses(None) == [] and sanitize_statuses({"mcpServers": [1, {"status": "x"}]}) == []


def test_event_wire_shape():
    e = ev.McpStatus(servers=[McpServerState("n", "needs-auth", None, None, "user")])
    assert e.to_wire() == {"type": "mcpStatus", "servers": [
        {"name": "n", "status": "needs-auth", "error": None, "toolCount": None, "scope": "user"}]}
```

Confirm the import path of the internal server's wire name: `grep -n "SERVER_NAME" server/sessions/tools/server.py`. Use whatever constant it exports.

- [ ] **Step 2: Run it and verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_mcp_health_status.py -q`
Expected: FAIL, `ModuleNotFoundError: server.mcp_health`

- [ ] **Step 3: Implement**

```python
# server/mcp_health/status.py
"""Sanitized MCP server status — the one vocabulary every surface uses (spec 2026-10-09).

The SDK's McpServerStatus carries the server ``config`` (headers, env: secrets). Nothing
downstream ever sees it: this module is the only place a raw status dict is read."""
from __future__ import annotations

import re
from dataclasses import dataclass

from server.sessions.tools.server import SERVER_NAME

ERROR_MAX = 500
_QUERY = re.compile(r"(https?://[^\s?#]+)\?[^\s#]*")


@dataclass(frozen=True, slots=True)
class McpServerState:
    name: str
    status: str
    error: str | None
    tool_count: int | None
    scope: str | None

    def to_wire(self) -> dict:
        return {"name": self.name, "status": self.status, "error": self.error,
                "toolCount": self.tool_count, "scope": self.scope}


def redact_error(text: str | None) -> str | None:
    if not isinstance(text, str) or not text:
        return None
    return _QUERY.sub(r"\1?<redacted>", text)[:ERROR_MAX]


def sanitize_statuses(raw: object) -> list[McpServerState]:
    entries = raw.get("mcpServers") if isinstance(raw, dict) else None
    out: list[McpServerState] = []
    for s in entries if isinstance(entries, list) else []:
        if not isinstance(s, dict):
            continue
        name, status = s.get("name"), s.get("status")
        if not isinstance(name, str) or not isinstance(status, str):
            continue
        scope = s.get("scope") if isinstance(s.get("scope"), str) else None
        if name == SERVER_NAME or scope == "sdk":
            continue
        tools = s.get("tools")
        out.append(McpServerState(name=name, status=status, error=redact_error(s.get("error")),
                                  tool_count=len(tools) if isinstance(tools, list) else None,
                                  scope=scope))
    return out
```

In `server/sessions/events.py`, after `ContextUsage`:

```python
@dataclass
class McpStatus:
    """Sanitized per-server MCP connection status for one live session (spec 2026-10-09).
    Built only by server.mcp_health.status.sanitize_statuses — never carries server config."""
    servers: list  # list[McpServerState]
    def to_wire(self): return {"type": "mcpStatus", "servers": [s.to_wire() for s in self.servers]}
```

In `engine.py`, add the capability field with a comment (`#: The driver can report per-server MCP status (get_mcp_status) and reconnect one server.`) as `mcp_status: bool = False`. Add to the `AgentDriver` Protocol, next to `context_usage`:

```python
    async def mcp_status(self) -> list: ...            # list[McpServerState]
    async def reconnect_mcp_server(self, name: str) -> None: ...
```

In `sdk_contract.py`, next to `get_context_usage`:

```python
    async def get_mcp_status(self) -> dict: ...
    async def reconnect_mcp_server(self, server_name: str) -> None: ...
```

In `FakeSDKClient.__init__`, add a `mcp_status=None` kwarg stored as `self._mcp_status` and set `self.reconnected = []`. Then add:

```python
    async def get_mcp_status(self):
        v = self._mcp_status() if callable(self._mcp_status) else self._mcp_status
        if v is None:
            raise RuntimeError("mcp_status not scripted")   # mirrors an old CLI rejecting the control request
        return {"mcpServers": list(v)}
    async def reconnect_mcp_server(self, server_name):
        self.reconnected.append(server_name)
```

In `CodexAgentDriver` (capability stays False until sub-project 4):

```python
    async def mcp_status(self) -> list:
        return []

    async def reconnect_mcp_server(self, name: str) -> None:
        raise NotImplementedError("Codex MCP status is sub-project 4 of the MCP management spec")
```

Grep for every other class implementing the `AgentDriver` protocol (`grep -rn "async def context_usage" server/ --include=*.py`), and give each non-Claude one the same two stubs. That includes `RemoteDriver` in `server/sessions/remote.py`, which gets real RPC-backed versions in Task 6. Add stubs here so mypy stays green.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `.venv/bin/python -m pytest server/tests/test_mcp_health_status.py server/tests/test_engine*.py -q && .venv/bin/python -m mypy server/ --ignore-missing-imports`
Expected: PASS, and no mypy errors.

- [ ] **Step 5: Commit**

```bash
git add server/mcp_health server/sessions/events.py server/sessions/engine.py server/sessions/sdk_contract.py server/tests/fakes/fake_sdk.py server/codex/driver.py server/sessions/remote.py server/tests/test_mcp_health_status.py
git commit -F /tmp/msg-task2.txt   # "feat(mcp): sanitized MCP status vocabulary, McpStatus event, engine contract"
```

---

### Task 3: Claude driver polls status and reconnects

**Files:**
- Modify: `server/sessions/claude_driver.py` (capability, ctor, `start()`, turn-end block ~:1581, new methods after `_emit_context_usage`, `aclose()`)
- Modify: `server/sessions/engine.py` (`AgentDriverConfig.on_mcp_status`)
- Modify: `server/sessions/claude_driver.py:ClaudeAgentDriverFactory.create` (pass `on_mcp_status`)
- Modify: `server/config.py` (both settings classes) and `server/api/settings.py` (row): `mcp_status_enabled`
- Test: `server/tests/test_claude_driver_mcp_status.py`

**Interfaces:**
- Consumes: Task 2's `sanitize_statuses`, `ev.McpStatus`, `FakeSDKClient(mcp_status=…)`
- Produces:
  - `ClaudeAgentDriver.mcp_status() -> list[McpServerState]`, which returns the cached last snapshot when the stream has ended
  - `ClaudeAgentDriver.reconnect_mcp_server(name) -> None`, which raises `RuntimeError("session not live")` on a dead client
  - `AgentDriverConfig.on_mcp_status: Callable[[str, list[McpServerState]], Awaitable[None]] | None`
  - Settings `get_settings().mcp_status_enabled: bool`
  - Module constants `MCP_SETTLE_INTERVAL_S = 5.0` and `MCP_SETTLE_MAX_S = 30.0`

- [ ] **Step 1: Write the failing tests**

Model the driver construction on an existing driver test. Copy the `_driver(...)` helper pattern from `server/tests/test_claude_driver_context_usage.py`; if that file doesn't exist, use `grep -ln "context_usage=" server/tests/test_*driver*.py`.

```python
# server/tests/test_claude_driver_mcp_status.py
import asyncio
import pytest
from server.sessions import claude_driver as cd
from server.sessions import events as ev
from server.tests.fakes.fake_sdk import FakeSDKClient


def _collect(driver):
    q = driver.subscribe()      # use the subscription API the context-usage tests use
    return q


@pytest.mark.asyncio
async def test_start_emits_and_persists_status(make_driver):
    seen = []
    async def on_mcp(sid, states): seen.append((sid, [s.status for s in states]))
    client = FakeSDKClient([], mcp_status=[{"name": "gh", "status": "connected", "tools": [{}]}])
    d = make_driver(client, on_mcp_status=on_mcp)
    await d.start()
    await asyncio.sleep(0)               # let the poll task run once
    await d._mcp_poll_task               # settle loop exits immediately: nothing pending
    assert seen == [(d.session_id, ["connected"])]
    assert [s.name for s in await d.mcp_status()] == ["gh"]
    await d.aclose()


@pytest.mark.asyncio
async def test_settle_loop_repolls_while_pending(make_driver, monkeypatch):
    monkeypatch.setattr(cd, "MCP_SETTLE_INTERVAL_S", 0.01)
    answers = iter([[{"name": "a", "status": "pending"}], [{"name": "a", "status": "connected"}]])
    client = FakeSDKClient([], mcp_status=lambda: next(answers))
    d = make_driver(client)
    await d.start(); await d._mcp_poll_task
    assert [s.status for s in await d.mcp_status()] == ["connected"]
    await d.aclose()


@pytest.mark.asyncio
async def test_settle_loop_gives_up_after_cap(make_driver, monkeypatch):
    monkeypatch.setattr(cd, "MCP_SETTLE_INTERVAL_S", 0.01)
    monkeypatch.setattr(cd, "MCP_SETTLE_MAX_S", 0.05)
    client = FakeSDKClient([], mcp_status=[{"name": "a", "status": "pending"}])
    d = make_driver(client)
    await d.start(); await asyncio.wait_for(d._mcp_poll_task, 1.0)
    await d.aclose()


@pytest.mark.asyncio
async def test_unsupported_control_request_latches_and_logs_debug(make_driver, caplog):
    client = FakeSDKClient([])           # mcp_status=None → get_mcp_status raises
    d = make_driver(client)
    await d.start(); await d._mcp_poll_task
    assert d._mcp_unsupported is True
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR") and "mcp" in r.getMessage()]
    await d.aclose()


@pytest.mark.asyncio
async def test_turn_end_polls(make_driver):
    calls = []
    def answer(): calls.append(1); return [{"name": "a", "status": "connected"}]
    client = FakeSDKClient([], mcp_status=answer)
    d = make_driver(client)
    await d.start(); await d._mcp_poll_task
    n = len(calls)
    await d.send_prompt("hi")
    await d.wait_idle()                  # use whatever settle helper the context-usage tests use
    assert len(calls) == n + 1
    await d.aclose()


@pytest.mark.asyncio
async def test_reconnect_calls_sdk_then_repolls(make_driver):
    client = FakeSDKClient([], mcp_status=[{"name": "a", "status": "connected"}])
    d = make_driver(client)
    await d.start(); await d._mcp_poll_task
    await d.reconnect_mcp_server("a")
    assert client.reconnected == ["a"]
    await d.aclose()


@pytest.mark.asyncio
async def test_close_during_settle_reaps_poll_task(make_driver, monkeypatch):
    monkeypatch.setattr(cd, "MCP_SETTLE_INTERVAL_S", 10.0)
    client = FakeSDKClient([], mcp_status=[{"name": "a", "status": "pending"}])
    d = make_driver(client)
    await d.start(); await asyncio.sleep(0.01)
    await d.aclose()                     # must not raise or hang
    assert d._mcp_poll_task.done()


@pytest.mark.asyncio
async def test_kill_switch_disables_polling(make_driver, monkeypatch):
    monkeypatch.setenv("VIBERPIT_MCP_STATUS_ENABLED", "false")
    from server.config import get_settings; get_settings.cache_clear()
    client = FakeSDKClient([], mcp_status=[{"name": "a", "status": "connected"}])
    d = make_driver(client)
    await d.start()
    assert d._mcp_poll_task is None
    await d.aclose()
    get_settings.cache_clear()
```

`make_driver` is a fixture defined at the top of this test file. It builds a `ClaudeAgentDriver` with `sdk_factory=lambda *a, **k: client`, exactly as the existing context-usage driver tests do. Copy their construction verbatim and add `on_mcp_status`. Check how they clear settings: if `get_settings` is not `lru_cache`d, use the reset the other kill-switch tests use (`grep -rn "SESSION_TOOLS_ENABLED" server/tests | head`).

- [ ] **Step 2: Run them and verify they fail**

Run: `.venv/bin/python -m pytest server/tests/test_claude_driver_mcp_status.py -q`
Expected: FAIL, `AttributeError: '_mcp_poll_task'`

- [ ] **Step 3: Implement**

Settings. In `server/config.py`, add next to `session_tools_enabled` in BOTH classes:

```python
        # MCP live status (spec 2026-10-09): driver polling, persistence, probe, readout.
        mcp_status_enabled: bool = Field(default=True, alias="VIBERPIT_MCP_STATUS_ENABLED")
```

```python
        mcp_status_enabled: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("VIBERPIT_MCP_STATUS_ENABLED", "true").lower() in ("1", "true", "yes", "on")
        )
```

Then add a row to `server/api/settings.py`'s list: `("mcp_status_enabled", "VIBERPIT_MCP_STATUS_ENABLED", "Sessions report MCP server connection status; Settings › MCP can test servers")`.

`engine.py`, in `AgentDriverConfig`, next to `on_context_usage`:

```python
    #: Fired with the sanitized MCP status snapshot each time a Claude driver polls it.
    on_mcp_status: Callable[[str, list], Awaitable[None]] | None = None
```

Driver:
- set `mcp_status=True` in `capabilities`;
- add an `on_mcp_status=None` ctor kwarg;
- pass `on_mcp_status=config.on_mcp_status` in the factory.

In `__init__`:

```python
        self._on_mcp_status = on_mcp_status
        self._mcp_last: list = []                     # last sanitized snapshot
        self._mcp_unsupported = False                 # latched after the first control-request failure
        self._mcp_poll_task: asyncio.Task | None = None
```

At the end of `start()`, after `self._start_listener()`:

```python
        if get_settings().mcp_status_enabled:
            self._mcp_poll_task = asyncio.create_task(self._mcp_settle())
```

Import `get_settings` the way the module already does; grep for it first. Turn-end block, right after `await self._emit_context_usage()`, under the same `if not self._stream_ended:`:

```python
                await self._poll_mcp_status()
```

New methods, after `_emit_context_usage`:

```python
    async def mcp_status(self) -> list:
        """Fresh sanitized status when live; the last snapshot otherwise."""
        if self._client is None or self._stream_ended or self._mcp_unsupported:
            return list(self._mcp_last)
        await self._poll_mcp_status()
        return list(self._mcp_last)

    async def reconnect_mcp_server(self, name: str) -> None:
        if self._client is None or self._stream_ended:
            raise RuntimeError("session not live")
        await self._client.reconnect_mcp_server(name)
        await self._poll_mcp_status()

    async def _poll_mcp_status(self) -> list | None:
        """One get_mcp_status round-trip → emit + persist. Best-effort, DEBUG on failure."""
        if (self._mcp_unsupported or self._client is None or self._stream_ended
                or not get_settings().mcp_status_enabled):
            return None
        try:
            states = sanitize_statuses(await self._client.get_mcp_status())
        except Exception:
            self._mcp_unsupported = True
            log.debug("mcp_status pull failed session=%s", self.session_id, exc_info=True)
            return None
        self._mcp_last = states
        self._emit(ev.McpStatus(servers=states))
        if self._on_mcp_status is not None:
            try:
                await self._on_mcp_status(self.session_id, states)
            except Exception:
                log.debug("on_mcp_status persist failed session=%s", self.session_id, exc_info=True)
        return states

    async def _mcp_settle(self) -> None:
        """Poll once at start, then while anything is pending, capped at MCP_SETTLE_MAX_S."""
        deadline = asyncio.get_running_loop().time() + MCP_SETTLE_MAX_S
        states = await self._poll_mcp_status()
        while (states and any(s.status == "pending" for s in states)
               and asyncio.get_running_loop().time() < deadline and not self._closing):
            await asyncio.sleep(MCP_SETTLE_INTERVAL_S)
            states = await self._poll_mcp_status()
```

A transient failure, as opposed to unsupported, is also latched, which is deliberate: one DEBUG line and no retry storm. A session restart clears it.

Module constants near the top: `MCP_SETTLE_INTERVAL_S = 5.0` and `MCP_SETTLE_MAX_S = 30.0`. In `aclose()`, right after `self._cancel_suggest()`:

```python
            if self._mcp_poll_task is not None:
                await cancel_and_reap(self._mcp_poll_task, phase=f"driver.mcp-poll:{self.session_id}")
```

Confirm `cancel_and_reap` accepts a done task without raising: read `server/aio.py`.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `.venv/bin/python -m pytest server/tests/test_claude_driver_mcp_status.py server/tests/test_no_swallowed_outer_cancellation.py server/tests/test_claude_driver*.py -q -n 4`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/sessions/claude_driver.py server/sessions/engine.py server/config.py server/api/settings.py server/tests/test_claude_driver_mcp_status.py
git commit -F /tmp/msg-task3.txt   # "feat(sessions): Claude driver polls MCP status and reconnects servers"
```

---

### Task 4: `mcp_server_status` table, store, orchestrator persistence

**Files:**
- Modify: `server/db/models.py` (new model after `HostHeartbeat`)
- Create: `server/db/migrations/versions/0078_mcp_server_status.py`
- Create: `server/mcp_health/store.py`
- Modify: `server/sessions/orchestrator.py` (wire `on_mcp_status=self._persist_mcp_status` at ~:2514; add `_persist_mcp_status` next to `_persist_context_usage` ~:6992)
- Modify: `server/tests/test_postgres_compat.py` (`_REQUIRED_EXTRAS`)
- Test: `server/tests/test_mcp_health_store.py`

**Interfaces:**
- Consumes: `McpServerState`
- Produces:
  - Model `McpServerHealth` (table `mcp_server_status`)
  - `async def upsert_states(account: str, host: str, states: list[McpServerState], *, source: str, session_id: str | None) -> None`
  - `async def rows(server: str | None = None) -> list[dict]`. Each row dict is `{account, host, server, status, error, toolCount, scope, source, sessionId, observedAt (ISO str + "Z")}`.
  - `def short_name(server_key: str) -> str`, which strips `plugin:<p>:`.
  - `async def needs_auth_by_server() -> dict[str, list[str]]`, mapping short name to sorted accounts whose row is `needs-auth`.
  - `async def login_key_for(short: str) -> str`, the full key from rows, defaulting to `short`.

- [ ] **Step 1: Write the failing tests**

Use the canonical temp-DB fixture `isolated_db` (`grep -rn "def isolated_db" server/tests/conftest.py`).

```python
# server/tests/test_mcp_health_store.py
import pytest
from server.mcp_health import store
from server.mcp_health.status import McpServerState as S


@pytest.mark.asyncio
async def test_upsert_overwrites_same_key(isolated_db):
    await store.upsert_states("alice", "dobby", [S("gh", "needs-auth", None, None, "user")], source="session", session_id="s1")
    await store.upsert_states("alice", "dobby", [S("gh", "connected", None, 3, "user")], source="probe", session_id=None)
    [r] = await store.rows()
    assert (r["status"], r["toolCount"], r["source"], r["sessionId"]) == ("connected", 3, "probe", None)
    assert r["observedAt"].endswith("Z")


@pytest.mark.asyncio
async def test_hosts_are_distinct_keys(isolated_db):
    await store.upsert_states("alice", "dobby", [S("x", "connected", None, 1, "user")], source="probe", session_id=None)
    await store.upsert_states("alice", "rocky", [S("x", "failed", "ENOENT", None, "user")], source="probe", session_id=None)
    assert sorted(r["host"] for r in await store.rows("x")) == ["dobby", "rocky"]


@pytest.mark.asyncio
async def test_needs_auth_and_login_key_use_short_names(isolated_db):
    await store.upsert_states("bob", "dobby", [S("plugin:railway:railway", "needs-auth", None, None, "user")], source="session", session_id="s")
    await store.upsert_states("al", "dobby", [S("plugin:railway:railway", "needs-auth", None, None, "user")], source="session", session_id="s")
    assert await store.needs_auth_by_server() == {"railway": ["al", "bob"]}
    assert await store.login_key_for("railway") == "plugin:railway:railway"
    assert await store.login_key_for("custom") == "custom"
```

Add an orchestrator-level test in the same file. Build a `SessionOrchestrator` the way `server/tests/test_orchestrator_context_usage*.py` does (grep `_persist_context_usage` in `server/tests`), call `await orch._persist_mcp_status(sid, [S("gh","connected",None,1,"user")])` for a record whose account is `alice`, and assert one row with `host == self_host_name()` and `session_id == sid`.

- [ ] **Step 2: Run them and verify they fail**

Run: `.venv/bin/python -m pytest server/tests/test_mcp_health_store.py -q`
Expected: FAIL, `ImportError: store`

- [ ] **Step 3: Implement**

Model (`server/db/models.py`, after `HostHeartbeat`):

```python
class McpServerHealth(Base):
    """Latest observed MCP connection status per (account, host, server) — spec 2026-10-09.

    Written by session drivers (source='session') and the on-demand probe (source='probe').
    The single source of needs-auth for Settings › MCP and the OAuth account picker."""

    __tablename__ = "mcp_server_status"
    __table_args__ = (UniqueConstraint("account", "host", "server", name="uq_mcp_server_status_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account: Mapped[str] = mapped_column(String, nullable=False)
    host: Mapped[str] = mapped_column(String, nullable=False)
    server: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scope: Mapped[str | None] = mapped_column(String, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
```

Import `UniqueConstraint` if `models.py` doesn't already.

Migration `0078_mcp_server_status.py`, in the style of `0077`:

```python
"""MCP live status per (account, host, server); create_all installs the table at boot."""
import sqlalchemy as sa
from alembic import op

revision = '0078_mcp_server_status'
down_revision = '0077_personal_quick_replies'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'mcp_server_status',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('account', sa.String(), nullable=False),
        sa.Column('host', sa.String(), nullable=False),
        sa.Column('server', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('observed_at', sa.DateTime(), nullable=False),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('tool_count', sa.Integer(), nullable=True),
        sa.Column('scope', sa.String(), nullable=True),
        sa.Column('session_id', sa.String(), nullable=True),
        sa.UniqueConstraint('account', 'host', 'server', name='uq_mcp_server_status_key'),
    )


def downgrade() -> None:
    op.drop_table('mcp_server_status')
```

Store (`server/mcp_health/store.py`). Upsert as select-then-update-or-insert inside one `db_session()` so it is dialect-neutral; check whether the repo has a shared upsert helper first (`grep -rn "on_conflict_do_update\|def upsert" server/db server/*.py | head`) and use it if present.

```python
"""Persistence for MCP live status (spec 2026-10-09). Rows are the latest observation per key."""
from __future__ import annotations

from sqlalchemy import select

from server.db.clock import db_utc_now
from server.db.models import McpServerHealth
from server.db.session import db_session          # use the import the orchestrator uses
from server.mcp_health.status import McpServerState


def short_name(server_key: str) -> str:
    return server_key.rsplit(":", 1)[-1] if server_key.startswith("plugin:") else server_key


async def upsert_states(account: str, host: str, states: list[McpServerState], *,
                        source: str, session_id: str | None) -> None:
    if not states:
        return
    async with db_session() as db:
        now = await db_utc_now(db)               # match db_utc_now's real signature
        existing = {r.server: r for r in (await db.execute(select(McpServerHealth).where(
            McpServerHealth.account == account, McpServerHealth.host == host,
            McpServerHealth.server.in_([s.name for s in states])))).scalars()}
        for s in states:
            row = existing.get(s.name) or McpServerHealth(account=account, host=host, server=s.name)
            row.status, row.error, row.tool_count, row.scope = s.status, s.error, s.tool_count, s.scope
            row.source, row.session_id, row.observed_at = source, session_id, now
            db.add(row)


def _wire(r: McpServerHealth) -> dict:
    return {"account": r.account, "host": r.host, "server": r.server, "status": r.status,
            "error": r.error, "toolCount": r.tool_count, "scope": r.scope, "source": r.source,
            "sessionId": r.session_id, "observedAt": r.observed_at.isoformat() + "Z"}


async def rows(server: str | None = None) -> list[dict]:
    async with db_session() as db:
        q = select(McpServerHealth).order_by(McpServerHealth.server, McpServerHealth.account, McpServerHealth.host)
        out = [_wire(r) for r in (await db.execute(q)).scalars()]
    return [r for r in out if server is None or short_name(r["server"]) == short_name(server)]


async def needs_auth_by_server() -> dict[str, list[str]]:
    acc: dict[str, set[str]] = {}
    for r in await rows():
        if r["status"] == "needs-auth":
            acc.setdefault(short_name(r["server"]), set()).add(r["account"])
    return {k: sorted(v) for k, v in acc.items()}


async def login_key_for(short: str) -> str:
    for r in await rows(short):
        return r["server"]
    return short
```

Read `server/db/clock.py` first and call `db_utc_now` exactly as its docstring says (sync vs async, whether it takes a session).

Orchestrator: next to `_persist_context_usage`:

```python
    async def _persist_mcp_status(self, session_id: str, states: list) -> None:
        """Driver ``on_mcp_status`` callback: record the latest per-server status for this
        session's account on THIS host (spec 2026-10-09). Best-effort like context usage."""
        rec = self._records.get(session_id)
        if rec is None or not states:
            return
        await mcp_store.upsert_states(rec.account, self_host_name(), states,
                                      source="session", session_id=session_id)
```

Wire it with `on_mcp_status=self._persist_mcp_status,` next to `on_context_usage=` (~:2514). Import `from server.mcp_health import store as mcp_store`. Add `McpServerHealth` to `_REQUIRED_EXTRAS` in `test_postgres_compat.py`, following the existing entries' shape: supply `account`, `host`, `server`, `status`, `source`, `observed_at`.

- [ ] **Step 4: Run the tests and verify they pass, including Postgres**

Run: `.venv/bin/python -m pytest server/tests/test_mcp_health_store.py server/tests/test_schema_reconcile_coverage.py server/tests/test_postgres_compat.py -q`, then the Postgres suite per the `running-the-postgres-suite-locally` notes (scratch DB owned by `ccswitch_test`, `VIBERPIT_TEST_PG_URL=…`).
Expected: PASS on both dialects.

- [ ] **Step 5: Commit**

```bash
git add server/db/models.py server/db/migrations/versions/0078_mcp_server_status.py server/mcp_health/store.py server/sessions/orchestrator.py server/tests/test_postgres_compat.py server/tests/test_mcp_health_store.py
git commit -F /tmp/msg-task4.txt   # "feat(db): mcp_server_status table; sessions persist MCP status"
```

---

### Task 5: No-prompt probe + `probe_mcp` across the fleet

**Files:**
- Create: `server/mcp_health/probe.py`
- Modify: `server/sessions/orchestrator.py` (`async def probe_mcp(self, account=None) -> dict`)
- Modify: `server/sessions/remote.py` (`RemoteOrchestrator.probe_mcp`)
- Modify: `server/ipc/orchestrator_rpc.py` (`_SIMPLE += "probe_mcp"`)
- Modify: `server/sessions/fleet_orchestrator.py` (`_MERGE_ALL_HOSTS["probe_mcp"] = _merge_probe`)
- Modify: `server/sessions/facade.py` (pass-through)
- Test: `server/tests/test_mcp_health_probe.py`

**Interfaces:**
- Consumes: `sanitize_statuses`, `store.upsert_states`, `self_host_name()`
- Produces:
  - `async def probe_account(account: str, *, client_factory=None, settle_s: float = 20.0, interval_s: float = 2.0) -> list[McpServerState]`
  - `async def probe(accounts: list[str], **kw) -> dict`, returning `{"rows": [...store rows for this host...], "skipped": [{"account", "reason"}]}`
  - `orch.probe_mcp(account: str | None = None) -> dict` (same shape)
  - Fleet merge: concatenate `rows` and `skipped` across hosts

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_mcp_health_probe.py
import asyncio
import pytest
from server.mcp_health import probe
from server.tests.fakes.fake_sdk import FakeSDKClient


def _factory(answer, opened):
    def make(account, cfg_dir):
        opened.append(account)
        return FakeSDKClient([], mcp_status=answer, startup_frames=[])
    return make


@pytest.mark.asyncio
async def test_probe_settles_pending(monkeypatch, tmp_path):
    answers = iter([[{"name": "a", "status": "pending"}], [{"name": "a", "status": "connected"}]])
    monkeypatch.setattr(probe, "_cfg_dir_for", lambda a: tmp_path)
    states = await probe.probe_account("alice", client_factory=_factory(lambda: next(answers), []), interval_s=0.01)
    assert [s.status for s in states] == ["connected"]


@pytest.mark.asyncio
async def test_probe_timeout_records_pending_with_error(monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "_cfg_dir_for", lambda a: tmp_path)
    states = await probe.probe_account("alice", client_factory=_factory([{"name": "a", "status": "pending"}], []),
                                       settle_s=0.05, interval_s=0.01)
    assert states[0].status == "pending" and "timed out" in states[0].error


@pytest.mark.asyncio
async def test_concurrent_probes_of_one_account_share_one_client(monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "_cfg_dir_for", lambda a: tmp_path)
    opened = []
    f = _factory([{"name": "a", "status": "connected"}], opened)
    await asyncio.gather(probe.probe_account("alice", client_factory=f), probe.probe_account("alice", client_factory=f))
    assert opened == ["alice"]


@pytest.mark.asyncio
async def test_probe_never_composes_or_writes_credentials(monkeypatch, tmp_path):
    import viberpit.configdir as cfgmod
    monkeypatch.setattr(cfgmod, "compose", lambda *a, **k: pytest.fail("probe must not compose"))
    (tmp_path / ".credentials.json").write_text('{"claudeAiOauth": {"x": 1}}')
    monkeypatch.setattr(probe, "_cfg_dir_for", lambda a: tmp_path)
    await probe.probe_account("alice", client_factory=_factory([{"name": "a", "status": "connected"}], []))
    assert (tmp_path / ".credentials.json").read_text() == '{"claudeAiOauth": {"x": 1}}'


@pytest.mark.asyncio
async def test_probe_skips_accounts_without_cfg(monkeypatch, isolated_db):
    monkeypatch.setattr(probe, "_cfg_dir_for", lambda a: None)
    out = await probe.probe(["ghost"], client_factory=_factory([], []))
    assert out["skipped"] == [{"account": "ghost", "reason": "no config dir on this host"}]
```

Add a fleet merge test in `server/tests/test_fleet_orchestrator.py`, next to the existing `_MERGE_ALL_HOSTS` tests (~:553). It should assert that `_MERGE_ALL_HOSTS["probe_mcp"]([{"rows":[1],"skipped":[]},{"rows":[2],"skipped":[{"account":"b"}]}]) == {"rows":[1,2],"skipped":[{"account":"b"}]}`.

- [ ] **Step 2: Run them and verify they fail**

Run: `.venv/bin/python -m pytest server/tests/test_mcp_health_probe.py -q`
Expected: FAIL, `ImportError`

- [ ] **Step 3: Implement**

```python
# server/mcp_health/probe.py
"""On-demand MCP health probe (spec 2026-10-09 §3).

Opens a ClaudeSDKClient under an account's EXISTING cfg dir, never sends a prompt (control
requests start no turn — verified in docs/superpowers/spikes/2026-10-09-mcp-status-verification.md),
polls get_mcp_status until nothing is pending, and closes. It never composes or writes the
cfg dir: a concurrent OAuth login or live session may own it."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path

from server.hostname import self_host_name
from server.mcp_health import store
from server.mcp_health.status import McpServerState, sanitize_statuses

log = logging.getLogger(__name__)
_GLOBAL = asyncio.Semaphore(2)          # created lazily if the module is imported off-loop; see note
_inflight: dict[str, asyncio.Future] = {}


def _cfg_dir_for(account: str) -> Path | None:
    from viberpit import paths
    d = paths.account_cfg_dir(account)
    return d if (d / ".claude.json").is_file() else None


def _default_client(account: str, cfg_dir: Path):
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from server.sessions.helper_call import helper_cwd
    return ClaudeSDKClient(options=ClaudeAgentOptions(
        cwd=str(helper_cwd()), env={"CLAUDE_CONFIG_DIR": str(cfg_dir)},
        extra_args={"no-session-persistence": None}))


async def probe_account(account: str, *, client_factory=None, settle_s: float = 20.0,
                        interval_s: float = 2.0) -> list[McpServerState]:
    if account in _inflight:
        return await asyncio.shield(_inflight[account])
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _inflight[account] = fut
    try:
        states = await _run(account, client_factory or _default_client, settle_s, interval_s)
        fut.set_result(states)
        return states
    except BaseException as exc:
        fut.set_exception(exc)
        fut.exception()                  # mark retrieved: no "never retrieved" warning
        raise
    finally:
        _inflight.pop(account, None)


async def _run(account, factory, settle_s, interval_s) -> list[McpServerState]:
    cfg = _cfg_dir_for(account)
    if cfg is None:
        raise LookupError("no config dir on this host")
    async with _GLOBAL:
        client = factory(account, cfg)
        async with client:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + settle_s
            states = sanitize_statuses(await client.get_mcp_status())
            while any(s.status == "pending" for s in states) and loop.time() < deadline:
                await asyncio.sleep(interval_s)
                states = sanitize_statuses(await client.get_mcp_status())
    return [replace(s, error=s.error or f"timed out after {int(settle_s)}s") if s.status == "pending" else s
            for s in states]


async def probe(accounts: list[str], **kw) -> dict:
    host = self_host_name()
    skipped: list[dict] = []
    for account in accounts:
        try:
            states = await probe_account(account, **kw)
        except LookupError as exc:
            skipped.append({"account": account, "reason": str(exc)})
            continue
        except Exception as exc:
            log.debug("mcp probe failed account=%s", account, exc_info=True)
            skipped.append({"account": account, "reason": type(exc).__name__})
            continue
        await store.upsert_states(account, host, states, source="probe", session_id=None)
    return {"rows": [r for r in await store.rows() if r["host"] == host], "skipped": skipped}
```

Check that `helper_cwd` exists (`grep -n "def helper_cwd" server/sessions/helper_call.py`); the `helper-calls-load-cwd-claude-md` memory says to use it. Confirm `extra_args={"no-session-persistence": None}` matches the form `stateless_helper_options` uses. Module-level `asyncio.Semaphore(2)` is safe on Python ≥3.10, where it binds to the loop on first use, but the test suite runs many loops. If a "bound to a different event loop" error appears, build the semaphore lazily per running loop in a `WeakKeyDictionary[loop, Semaphore]`.

Orchestrator (`SessionOrchestrator`):

```python
    async def probe_mcp(self, account: str | None = None) -> dict:
        """Run the no-prompt MCP probe on THIS host (spec 2026-10-09 §3)."""
        if not get_settings().mcp_status_enabled:
            return {"rows": [], "skipped": []}
        names = [account] if account else accounts._anthropic_account_names()
        return await mcp_probe.probe(names)
```

Use the module path the orchestrator already uses for account names; grep `_anthropic_account_names` to see how it is imported elsewhere. Remote: `async def probe_mcp(self, account=None): return await self._client.call("probe_mcp", account=account)`. Add `"probe_mcp"` to `_SIMPLE`. Facade: `async def probe_mcp(self, *a, **k): return await _maybe_await(self._orch.probe_mcp(*a, **k))`. Fleet:

```python
def _merge_probe(parts: list[dict]) -> dict:
    """Concatenate per-host probe results (probe_mcp): every host probes its own cfg dirs."""
    return {"rows": [r for p in parts for r in (p or {}).get("rows", [])],
            "skipped": [s for p in parts for s in (p or {}).get("skipped", [])]}
```

Register it as `"probe_mcp": _merge_probe` in `_MERGE_ALL_HOSTS`. Read how `_MERGE_ALL_HOSTS` entries are dispatched (around `__getattr__` ~:905) to confirm that kwargs pass through.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `.venv/bin/python -m pytest server/tests/test_mcp_health_probe.py server/tests/test_fleet_orchestrator.py server/tests/test_orchestrator_rpc*.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/mcp_health/probe.py server/sessions/orchestrator.py server/sessions/remote.py server/ipc/orchestrator_rpc.py server/sessions/fleet_orchestrator.py server/sessions/facade.py server/tests/test_mcp_health_probe.py server/tests/test_fleet_orchestrator.py
git commit -F /tmp/msg-task5.txt   # "feat(mcp): no-prompt MCP health probe, fanned out to every host"
```

---

### Task 6: Session endpoints: live status + reconnect

**Files:**
- Modify: `server/sessions/orchestrator.py` (`mcp_status_for`, `mcp_reconnect`, `reconnect_mcp_everywhere`)
- Modify: `server/sessions/remote.py` (`RemoteOrchestrator` methods; `RemoteDriver` stubs from Task 2 become RPC-backed only if `RemoteDriver` is used for these calls, which it is not: orchestrator-level RPCs suffice, so leave the stubs)
- Modify: `server/ipc/orchestrator_rpc.py` (`_SIMPLE`)
- Modify: `server/sessions/fleet_orchestrator.py` (`_PER_SESSION` += `mcp_status_for`, `mcp_reconnect`; `_MERGE_ALL_HOSTS["reconnect_mcp_everywhere"] = _merge_sum_int`)
- Modify: `server/sessions/facade.py`; `server/sessions/session_actions.py` (`mcp_reconnect` action + audit)
- Modify: `server/api/sessions.py` (2 routes)
- Modify: the two sessions-facade test doubles (`grep -rn "class FakeOrch\|class _FakeSessions" server/tests`)
- Test: `server/tests/test_session_mcp_routes.py`

**Interfaces:**
- Consumes: driver `mcp_status()` / `reconnect_mcp_server()`
- Produces:
  - `orch.mcp_status_for(session_id) -> list[dict] | None` (`None` = not live)
  - `orch.mcp_reconnect(session_id, server) -> list[dict]`, which raises `LookupError` when not live and `ValueError(msg)` on an SDK error
  - `orch.reconnect_mcp_everywhere(server: str, statuses: list[str]) -> int`, which reconnects `server` in every live Claude driver on this host whose last snapshot shows it in `statuses`, and returns the count
  - `GET /api/sessions/{id}/mcp` → `{"servers": [...]}` or 404
  - `POST /api/sessions/{id}/mcp/{server}/reconnect` → `{"servers": [...]}`, 404, or 409 `{"detail":{"code":"MCP_RECONNECT_FAILED","message":...}}`

- [ ] **Step 1: Write the failing tests**

Follow `server/tests/test_session_interrupt*.py` (grep `"/interrupt"` in `server/tests`) for the TestClient + fake-orchestrator harness.

```python
# server/tests/test_session_mcp_routes.py  (sketch the harness exactly like the interrupt route tests)
def test_get_live_status(client, fake_orch):
    fake_orch.mcp = {"s1": [{"name": "gh", "status": "connected", "error": None, "toolCount": 2, "scope": "user"}]}
    r = client.get("/api/sessions/s1/mcp")
    assert r.status_code == 200 and r.json()["servers"][0]["name"] == "gh"

def test_get_not_live_404(client, fake_orch):
    assert client.get("/api/sessions/nope/mcp").status_code == 404

def test_reconnect_audits_and_returns_status(client, fake_orch, audit_rows):
    fake_orch.mcp = {"s1": [{"name": "gh", "status": "connected"}]}
    r = client.post("/api/sessions/s1/mcp/gh/reconnect")
    assert r.status_code == 200 and fake_orch.reconnected == [("s1", "gh")]
    assert any(a.type == "session.mcp_reconnect" for a in audit_rows())

def test_reconnect_sdk_error_409(client, fake_orch):
    fake_orch.reconnect_error = "Server not found: zz"
    r = client.post("/api/sessions/s1/mcp/zz/reconnect")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "MCP_RECONNECT_FAILED"
```

Also add an orchestrator unit test for `reconnect_mcp_everywhere`. Two fake drivers: one whose `_mcp_last` shows `gh: needs-auth`, one with `gh: connected`. With `statuses=["needs-auth","failed"]` it reconnects only the first and returns `1`. Extend the `_PER_SESSION` completeness test if it enumerates expected names.

- [ ] **Step 2: Run them and verify they fail.** Run: `.venv/bin/python -m pytest server/tests/test_session_mcp_routes.py -q`. Expected: 404 / AttributeError.

- [ ] **Step 3: Implement**

Orchestrator:

```python
    async def mcp_status_for(self, session_id: str) -> list[dict] | None:
        d = self._drivers.get(session_id)
        if d is None:
            return None
        return [s.to_wire() for s in await d.mcp_status()]

    async def mcp_reconnect(self, session_id: str, server: str) -> list[dict]:
        d = self._drivers.get(session_id)
        if d is None:
            raise LookupError(session_id)
        try:
            await d.reconnect_mcp_server(server)
        except NotImplementedError as exc:
            raise ValueError(str(exc)) from exc
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        return [s.to_wire() for s in await d.mcp_status()]

    async def reconnect_mcp_everywhere(self, server: str, statuses: list[str]) -> int:
        """Reconnect *server* in every live driver on this host whose last snapshot shows it
        in *statuses* (spec 2026-10-09 §4, post-login). Failures are logged at DEBUG and
        surface through the next McpStatus poll."""
        n = 0
        for sid, d in list(self._drivers.items()):
            last = getattr(d, "_mcp_last", None) or []
            if not any(short_name(s.name) == short_name(server) and s.status in statuses for s in last):
                continue
            target = next(s.name for s in last if short_name(s.name) == short_name(server))
            try:
                await d.reconnect_mcp_server(target)
                n += 1
            except Exception:
                log.debug("mcp reconnect failed session=%s server=%s", sid, server, exc_info=True)
        return n
```

Replace the `getattr(d, "_mcp_last")` private read with a public driver property `last_mcp_status` (add it in `claude_driver.py`, returning `list(self._mcp_last)`, and to the Codex driver as `[]`). Use `short_name` from `server.mcp_health.store`.

`session_actions.py`:

```python
async def mcp_reconnect(orch, db, *, session_id: str, server: str, identity: str, via: str | None = None) -> dict:
    """Reconnect one MCP server inside a live session (spec 2026-10-09 §4)."""
    try:
        servers = await orch.mcp_reconnect(session_id, server)
    except LookupError:
        raise ActionError("SESSION_NOT_LIVE", "This session is not running.", http=404)
    except ValueError as exc:
        raise ActionError("MCP_RECONNECT_FAILED", str(exc)[:300], http=409)
    await _audit(db, "session.mcp_reconnect", identity, {"sessionId": session_id, "server": server}, via)
    return {"servers": servers}
```

Check `ActionError`'s real constructor signature in `session_actions.py` and match it. Routes in `server/api/sessions.py`, next to `interrupt_session`:

```python
@router.get("/{session_id}/mcp")
async def session_mcp_status(session_id: str, request: Request,
                             _identity: str = Depends(current_identity)) -> dict:
    """Live MCP server status for a running session (spec 2026-10-09)."""
    servers = await _get_orchestrator(request).mcp_status_for(session_id)
    if servers is None:
        raise HTTPException(status_code=404, detail={"code": "SESSION_NOT_LIVE"})
    return {"servers": servers}


@router.post("/{session_id}/mcp/{server}/reconnect")
async def session_mcp_reconnect(session_id: str, server: str, request: Request,
                                identity: str = Depends(current_identity), db=Depends(get_db)):
    if not valid_mcp_name(server.rsplit(":", 1)[-1]):
        raise HTTPException(status_code=400, detail={"code": "INVALID_SERVER"})
    try:
        return await session_actions.mcp_reconnect(_get_orchestrator(request), db, session_id=session_id,
                                                   server=server, identity=identity,
                                                   via=getattr(request.state, "via", None))
    except ActionError as exc:
        return _action_error_response(exc)
```

Import `valid_mcp_name` from `server/library/names.py`. Then:
- **Remote:** `mcp_status_for`, `mcp_reconnect` and `reconnect_mcp_everywhere` are each a one-line `self._client.call(...)`. Remember that `LookupError`/`ValueError` must survive the RPC hop. Read how `stop_subagent`'s `None` and `ActionError` codes cross today, and mirror it: return `{"error": "not_live"}` / `{"error": "reconnect_failed", "message": …}` from the daemon side, and re-raise in `RemoteOrchestrator`.
- **`_SIMPLE`:** add all three names.
- **Fleet:** add `mcp_status_for` and `mcp_reconnect` to `_PER_SESSION`; `_MERGE_ALL_HOSTS["reconnect_mcp_everywhere"] = lambda parts: sum(p or 0 for p in parts)` (write it as a named `_merge_sum_int` function next to `_merge_sum_floats`).
- **Facade:** add pass-throughs for all three.
- **Both test doubles:** add the methods.

- [ ] **Step 4: Run the tests and verify they pass.** Run: `.venv/bin/python -m pytest server/tests/test_session_mcp_routes.py server/tests/test_fleet_orchestrator.py server/tests/test_session_actions*.py -q -n 4`. Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -F /tmp/msg-task6.txt`, message "feat(api): live session MCP status and per-server reconnect".

---

### Task 7: Settings read side uses the table; Test endpoint; cache reader removed

**Files:**
- Modify: `server/library/scan.py` (delete `_needs_auth_by_server`; `list_mcp_servers(status_rows: list[dict] | None = None)`)
- Modify: `server/api/library.py` (`get_mcp` awaits rows; new `POST /mcp/test`; `mcp_auth_start` accepts an optional `{account}` body and passes preferred accounts)
- Modify: `server/mcp_auth.py`:
  - `default_account_picker(server, preferred: list[str])`
  - `McpAuthManager.start(server, *, preferred: list[str] = (), login_key: str | None = None)`
  - `resolve_login_key` deleted
  - `PtyLoginSession` takes `login_key`
- Modify tests: `server/tests/test_library_scan.py`, `server/tests/test_mcp_auth_pty.py`, `server/tests/test_mcp_auth_routes.py`
- Test: `server/tests/test_library_mcp_status.py`

**Interfaces:**
- Consumes: `store.rows`, `store.needs_auth_by_server`, `store.login_key_for`, `orch.probe_mcp`
- Produces:
  - Every `GET /api/library/mcp` server entry has `status: list[row]` and NO `needsAuth` key.
  - `POST /api/library/mcp/test` takes body `{"account"?: str, "server"?: str}` and returns `{"rows": [...], "skipped": [...]}`, filtered to `server` (short-name match) when given.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_library_mcp_status.py
from server.library import scan

def test_list_attaches_rows_by_short_name(shared):   # reuse test_library_scan.py's `shared` fixture
    (shared / "claude.shared.json").write_text('{"mcpServers": {"notion": {"type": "http", "url": "https://x"}}}')
    rows = [{"server": "notion", "account": "a", "host": "dobby", "status": "needs-auth"},
            {"server": "plugin:p:other", "account": "a", "host": "dobby", "status": "connected"}]
    [s] = scan.list_mcp_servers(status_rows=rows)["servers"]
    assert s["status"] == [rows[0]] and "needsAuth" not in s

def test_plugin_key_rows_match_short_name(shared, plugin_with_mcp):   # fixture shape from test_list_mcp_servers_user_plugin_and_auth
    rows = [{"server": "plugin:railway:railway", "account": "a", "host": "dobby", "status": "needs-auth"}]
    s = next(x for x in scan.list_mcp_servers(status_rows=rows)["servers"] if x["name"] == "railway")
    assert s["status"] == rows

def test_cache_reader_is_gone():
    assert not hasattr(scan, "_needs_auth_by_server")
```

Route tests (TestClient, with `orch.probe_mcp` monkeypatched to return fixed rows):
- `POST /api/library/mcp/test {"server":"notion"}` returns only notion rows;
- the kill switch off makes `GET` return `status: []` for every server;
- `POST /mcp/notion/auth {"account":"bob"}` makes the manager receive `preferred=["bob"]`.

Update `test_mcp_auth_pty.py`'s picker tests to pass `preferred` instead of monkeypatching `_needs_auth_by_server`. Replace the `resolve_login_key` tests with `store.login_key_for` coverage, which Task 4 already has, and delete them.

- [ ] **Step 2: Run them and verify they fail.** Expected: `TypeError: unexpected keyword 'status_rows'`.

- [ ] **Step 3: Implement**

`scan.list_mcp_servers(status_rows=None)`: keep the merge logic, but replace both `"needsAuth": needs_auth.get(name, [])` with `"status": by_short.get(name, [])`, where

```python
    by_short: dict[str, list[dict]] = {}
    for r in status_rows or []:
        by_short.setdefault(short_name(r["server"]), []).append(r)
```

and import `short_name` from `server.mcp_health.store`. Delete `_needs_auth_by_server`. `server/api/library.py`:

```python
async def _status_rows() -> list[dict]:
    return await mcp_store.rows() if get_settings().mcp_status_enabled else []


@router.get("/mcp")
async def get_mcp(_identity: str = Depends(current_identity)) -> dict:
    return scan.list_mcp_servers(status_rows=await _status_rows())


class McpTestBody(BaseModel):
    account: str | None = None
    server: str | None = None


@router.post("/mcp/test")
async def mcp_test(body: McpTestBody, request: Request,
                   _identity: str = Depends(current_identity)) -> dict:
    """Run the no-prompt probe on every host (spec 2026-10-09 §3), ≤30 s."""
    if not get_settings().mcp_status_enabled:
        return {"rows": [], "skipped": []}
    orch = request.app.state.sessions          # capture lazily — FleetOrchestrator swaps in after router build
    try:
        out = await asyncio.wait_for(orch.probe_mcp(account=body.account), timeout=30.0)
    except TimeoutError:
        raise HTTPException(status_code=504, detail={"error": "mcp_probe_timeout"})
    if body.server:
        out["rows"] = [r for r in out["rows"] if short_name(r["server"]) == short_name(body.server)]
    return out
```

Check how other library routes reach the orchestrator; follow that accessor, not `app.state.sessions`, if one exists. `mcp_auth_start`:
- add an optional body `class McpAuthStartBody(BaseModel): account: str | None = None`, with `body: McpAuthStartBody | None = None`;
- the existing `servers = …` lookup uses `scan.list_mcp_servers(status_rows=await _status_rows())`;
- set `preferred = ([body.account] if body and body.account else []) + (await mcp_store.needs_auth_by_server()).get(name, [])`;
- call `_mcp_auth(request).start(name, preferred=preferred, login_key=await mcp_store.login_key_for(name))`.

`mcp_auth.py`:
- `default_account_picker(server, preferred)` iterates `list(preferred) + accounts._anthropic_account_names()`;
- the manager's `_pick` signature becomes `Callable[[str, list[str]], tuple[str, Path]]`;
- `start()` stores `login_key` and the factory receives it: `session_factory(server, account, cfg_dir, login_key)`;
- `PtyLoginSession.__init__(…, login_key)` uses it in place of `resolve_login_key(self._server)`;
- update `build_default_manager` accordingly.

Delete `resolve_login_key`. Keep `clear_needs_auth_cache`: it resets the CLI's own cache so the next CLI start re-tries, which is CLI behaviour (spec §2).

- [ ] **Step 4: Run the tests and verify they pass.** Run: `.venv/bin/python -m pytest server/tests/test_library*.py server/tests/test_mcp_auth*.py -q`. Expected: PASS. Then `grep -rn "_needs_auth_by_server\|resolve_login_key\|needsAuth" server src` should return nothing.

- [ ] **Step 5: Commit**: "feat(library): MCP status rows replace the needs-auth cache; add Test endpoint".

---

### Task 8: Reauth follow-through: refresh mcpOAuth, reconnect live sessions, re-probe

**Files:**
- Modify: `src/viberpit/creds.py` (`refresh_cfg_mcp_oauth(cfg_dir: Path) -> bool`)
- Modify: `server/mcp_auth.py` (`McpAuthManager(on_success: Callable[[str, list[str]], Awaitable[None]] | None = None)`, called after success)
- Modify: `server/app.py` (~:798, wire `on_success`)
- Create: `server/mcp_health/after_login.py`
- Test: `tests/test_creds_mcp_refresh.py` (core suite, run with `PYTHONPATH=$PWD/src`) and `server/tests/test_mcp_after_login.py`

**Interfaces:**
- Consumes: `orch.reconnect_mcp_everywhere`, `orch.probe_mcp`
- Produces:
  - `refresh_cfg_mcp_oauth(cfg_dir)`: rewrites ONLY the `mcpOAuth` key of `<cfg_dir>/.credentials.json` as `merge_keep_later(current_mcp, shared_mcp)`; returns True if it changed anything.
  - `async def after_login(orch, server: str, accounts_with_cfg: list[str]) -> dict` returns `{"refreshed": int, "reconnected": int}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_creds_mcp_refresh.py
import json
from viberpit import creds, paths

def test_refresh_preserves_claude_oauth(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "shared_mcp_path", lambda: tmp_path / "mcp.shared.json")
    (tmp_path / "mcp.shared.json").write_text(json.dumps({"notion|1": {"serverName": "notion", "accessToken": "new", "expiresAt": 2}}))
    cfg = tmp_path / "cfg"; cfg.mkdir()
    original = {"claudeAiOauth": {"accessToken": "live-rotated", "refreshToken": "r"}, "mcpOAuth": {}}
    (cfg / ".credentials.json").write_text(json.dumps(original))
    assert creds.refresh_cfg_mcp_oauth(cfg) is True
    out = json.loads((cfg / ".credentials.json").read_text())
    assert out["claudeAiOauth"] == original["claudeAiOauth"]
    assert out["mcpOAuth"]["notion|1"]["accessToken"] == "new"

def test_refresh_noop_when_up_to_date(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "shared_mcp_path", lambda: tmp_path / "mcp.shared.json")
    blob = {"k": {"serverName": "n", "accessToken": "t", "expiresAt": 5}}
    (tmp_path / "mcp.shared.json").write_text(json.dumps(blob))
    cfg = tmp_path / "cfg"; cfg.mkdir()
    (cfg / ".credentials.json").write_text(json.dumps({"mcpOAuth": blob}))
    assert creds.refresh_cfg_mcp_oauth(cfg) is False

def test_refresh_skips_missing_credentials_file(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "shared_mcp_path", lambda: tmp_path / "mcp.shared.json")
    (tmp_path / "mcp.shared.json").write_text("{}")
    assert creds.refresh_cfg_mcp_oauth(tmp_path) is False
```

```python
# server/tests/test_mcp_after_login.py
import pytest
from server.mcp_health import after_login

class _Orch:
    def __init__(self): self.calls = []
    async def reconnect_mcp_everywhere(self, server, statuses): self.calls.append(("rc", server, tuple(statuses))); return 2
    async def probe_mcp(self, account=None): self.calls.append(("probe", account)); return {"rows": [], "skipped": []}

@pytest.mark.asyncio
async def test_after_login_refreshes_then_reconnects_then_probes(monkeypatch, tmp_path):
    refreshed = []
    monkeypatch.setattr(after_login, "_refresh", lambda acct: refreshed.append(acct) or True)
    orch = _Orch()
    out = await after_login.after_login(orch, "notion", ["alice", "bob"])
    assert refreshed == ["alice", "bob"]
    assert orch.calls[0] == ("rc", "notion", ("needs-auth", "failed"))
    assert ("probe", None) in orch.calls
    assert out == {"refreshed": 2, "reconnected": 2}
```

Add one `McpAuthManager` test to `test_mcp_auth*.py`: after a scripted successful `submit_redirect`, `on_success` is awaited once with `(server, accounts)`. Also check that an `on_success` raising still leaves `state == "success"`, logged at DEBUG.

- [ ] **Step 2: Run them and verify they fail.** Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_creds_mcp_refresh.py server/tests/test_mcp_after_login.py -q`. Expected: AttributeError / ImportError.

- [ ] **Step 3: Implement**

`creds.py`:

```python
def refresh_cfg_mcp_oauth(cfg_dir: Path) -> bool:
    """Merge the shared mcpOAuth store into ONE cfg dir's .credentials.json in place.

    Touches only the ``mcpOAuth`` key: a live CLI may have just rotated ``claudeAiOauth``
    in this same file, and a full compose would clobber it (cf. #2695). Used after an MCP
    login so live sessions of OTHER accounts can reconnect with the new token (spec
    2026-10-09 §4). Returns True when the file changed."""
    path = cfg_dir / CREDENTIALS_NAME
    if not path.is_file():
        return False
    shared_path = paths.shared_mcp_path()
    shared = read_json(shared_path) if shared_path.exists() else {}
    current = read_json(path)
    merged = merge_keep_later(current.get("mcpOAuth"), shared)
    if merged == current.get("mcpOAuth"):
        return False
    current["mcpOAuth"] = merged
    write_json_secure(path, current)
    return True
```

Check whether `CREDENTIALS_NAME` lives in `creds.py` or `configdir.py`, and import accordingly without creating a cycle. Read `merge_keep_later`'s docstring to confirm it merges per key by later expiry.

`server/mcp_health/after_login.py`:

```python
"""Post-OAuth follow-through (spec 2026-10-09 §4): push the new token into every account
cfg dir, reconnect live sessions that were failing on this server, then re-probe."""
from __future__ import annotations

import asyncio
import logging

from viberpit import creds, paths

log = logging.getLogger(__name__)


def _refresh(account: str) -> bool:
    return creds.refresh_cfg_mcp_oauth(paths.account_cfg_dir(account))


async def after_login(orch, server: str, accounts: list[str]) -> dict:
    refreshed = 0
    for account in accounts:
        try:
            refreshed += bool(await asyncio.to_thread(_refresh, account))
        except Exception:
            log.debug("mcp after_login refresh failed account=%s", account, exc_info=True)
    reconnected = await orch.reconnect_mcp_everywhere(server, ["needs-auth", "failed"])
    try:
        await orch.probe_mcp(account=None)
    except Exception:
        log.debug("mcp after_login probe failed server=%s", server, exc_info=True)
    return {"refreshed": refreshed, "reconnected": reconnected}
```

`McpAuthManager`:
- add an `on_success=None` ctor kwarg;
- in `submit_redirect`, after the state flips to `success` and outside the lock, spawn `asyncio.create_task(self._fire_success(server))`. Keep a reference in `self._success_tasks: set[asyncio.Task]`, and remove it in a done-callback;
- `_fire_success` awaits `self._on_success(server, accounts._anthropic_account_names())` inside `try/except Exception: log.debug(...)`;
- `shutdown()` reaps those tasks with `cancel_and_reap`.

Running it in the background keeps the redirect response fast; the UI picks up results through the `libraryMcp` invalidation and `mcpStatus` events. In `server/app.py`, where the manager is built (~:798):

```python
    _mcp_mgr = _build_mcp_auth(on_success=lambda server, accts: after_login.after_login(
        app.state.sessions, server, accts))
```

`app.state.sessions` is resolved at call time, which is required: `FleetOrchestrator` replaces it later in the lifespan. `build_default_manager(on_success=None)` passes the callback through. The daemon-side manager (`server/ipc/serve.py:104`) keeps `on_success=None`, since only the edge serves the auth routes.

- [ ] **Step 4: Run the tests and verify they pass**: the core `tests/` file (with `PYTHONPATH=$PWD/src`; confirm with `python -c "import viberpit.creds as m; print(m.__file__)"`) plus `server/tests/test_mcp_auth*.py server/tests/test_mcp_after_login.py`. Expected: PASS.

- [ ] **Step 5: Commit**: "feat(mcp): after OAuth login, refresh tokens into account cfg dirs and reconnect live sessions".

---

### Task 9: Web data layer: types, hooks, event, reducer

**Files:**
- Modify: `web/src/api/types.ts` (`McpStatusRow`, `McpServerState`; `LibraryMcpServer.status` replaces `needsAuth`)
- Modify: `web/src/api/hooks.ts` (`useTestMcp`, `useSessionMcp`, `useReconnectSessionMcp`; `useStartMcpAuth` accepts `{name, account?}`)
- Modify: `web/src/session/events.ts` (`EvMcpStatus`, union, `SESSION_EVENT_TYPE_FLAGS`)
- Modify: `web/src/session/reducer.ts` (`mcpServers: McpServerState[] | null`, preserved across `replayReset` like `contextUsage`)
- Create: `web/src/features/settings/sections/mcpStatus.ts` (pure helpers)
- Test: `web/src/features/settings/sections/mcpStatus.test.ts`, plus a reducer test case in the existing reducer test file (`grep -ln "contextUsage" web/src/session/*.test.ts`)

**Interfaces:**
- Produces (TS):
  ```ts
  export interface McpServerState { name: string; status: string; error: string | null; toolCount: number | null; scope: string | null }
  export interface McpStatusRow { account: string; host: string; server: string; status: string; error: string | null; toolCount: number | null; scope: string | null; source: 'session' | 'probe'; sessionId: string | null; observedAt: string }
  // mcpStatus.ts
  export type McpTone = 'bad' | 'warn' | 'pending' | 'ok' | 'neutral' | 'unknown'
  export function worstStatus(statuses: string[]): string | null
  export function toneFor(status: string | null): McpTone
  export function shortName(server: string): string
  export function relativeAge(iso: string, now?: number): string   // reuse an existing relative-time util if one exists — grep web/src/lib first
  ```
  - `useTestMcp(): UseMutation<{rows, skipped}, ApiRequestError, {account?: string; server?: string}>` invalidates `QK.libraryMcp`
  - `useSessionMcp(sessionId, enabled)` queries `['session-mcp', id]`
  - `useReconnectSessionMcp(sessionId)` is a mutation on `{server}`

- [ ] **Step 1: Write the failing tests**

```ts
// web/src/features/settings/sections/mcpStatus.test.ts
import { describe, expect, it } from 'vitest'
import { shortName, toneFor, worstStatus } from './mcpStatus'

describe('worstStatus', () => {
  it('ranks failed > needs-auth > pending > connected', () => {
    expect(worstStatus(['connected', 'needs-auth', 'pending'])).toBe('needs-auth')
    expect(worstStatus(['connected', 'failed'])).toBe('failed')
  })
  it('returns null for no rows', () => expect(worstStatus([])).toBeNull())
  it('keeps unknown statuses but ranks them below known problems', () => {
    expect(worstStatus(['connecting'])).toBe('connecting')
    expect(worstStatus(['connecting', 'failed'])).toBe('failed')
  })
})

describe('toneFor', () => {
  it('maps the vocabulary', () => {
    expect(toneFor('failed')).toBe('bad'); expect(toneFor('needs-auth')).toBe('warn')
    expect(toneFor('pending')).toBe('pending'); expect(toneFor('connected')).toBe('ok')
    expect(toneFor('disabled')).toBe('neutral'); expect(toneFor('connecting')).toBe('neutral')
    expect(toneFor(null)).toBe('unknown')
  })
})

it('shortName strips plugin prefix', () => expect(shortName('plugin:railway:railway')).toBe('railway'))
```

Reducer test: dispatch `{type:'mcpStatus', servers:[{name:'gh',status:'failed',error:'x',toolCount:null,scope:'user'}]}`, assert `state.mcpServers[0].status === 'failed'`, then dispatch `replayReset` and assert `mcpServers` is preserved.

- [ ] **Step 2: Run them and verify they fail.** Run: `(cd web && npx vitest run src/features/settings/sections/mcpStatus.test.ts)`. Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```ts
// web/src/features/settings/sections/mcpStatus.ts
// MCP status vocabulary shared by Settings › MCP and the in-session readout (spec 2026-10-09).
const RANK: Record<string, number> = { failed: 4, 'needs-auth': 3, pending: 2, connected: 0, disabled: 0 }
export type McpTone = 'bad' | 'warn' | 'pending' | 'ok' | 'neutral' | 'unknown'

export function worstStatus(statuses: string[]): string | null {
  if (statuses.length === 0) return null
  // Unknown statuses rank 1: above healthy (worth showing), below every known problem.
  return statuses.reduce((a, b) => ((RANK[b] ?? 1) > (RANK[a] ?? 1) ? b : a))
}

export function toneFor(status: string | null): McpTone {
  if (status == null) return 'unknown'
  return ({ failed: 'bad', 'needs-auth': 'warn', pending: 'pending', connected: 'ok' } as const)[status as 'failed'] ?? 'neutral'
}

export function shortName(server: string): string {
  return server.startsWith('plugin:') ? server.slice(server.lastIndexOf(':') + 1) : server
}
```

The `worstStatus(['connected','disabled'])` tie returns the first element, `'connected'`, which is fine. Write the `toneFor` lookup as a plain `Record<string, McpTone>` if the cast trips eslint. For `relativeAge`, grep `web/src/lib` for an existing "3 m ago" formatter (`timeAgo`, `relativeTime`, `formatAgo`) and re-export it rather than writing one.

Types: replace `needsAuth: string[]` with `status: McpStatusRow[]` on `LibraryMcpServer`. Hooks follow the `useLibraryMcp` / `useStartMcpAuth` patterns:

```ts
export function useTestMcp() {
  const qc = useQueryClient()
  return useMutation<{ rows: McpStatusRow[]; skipped: { account: string; reason: string }[] }, ApiRequestError, { account?: string; server?: string }>({
    mutationFn: (body) => api.post('/library/mcp/test', body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.libraryMcp }),
  })
}

export function useSessionMcp(sessionId: string, enabled: boolean) {
  return useQuery<{ servers: McpServerState[] }>({
    queryKey: ['session-mcp', sessionId],
    queryFn: () => api.get(`/sessions/${encodeURIComponent(sessionId)}/mcp`),
    enabled, staleTime: 15_000, retry: false,
  })
}

export function useReconnectSessionMcp(sessionId: string) {
  const qc = useQueryClient()
  return useMutation<{ servers: McpServerState[] }, ApiRequestError, { server: string }>({
    mutationFn: ({ server }) => api.post(`/sessions/${encodeURIComponent(sessionId)}/mcp/${encodeURIComponent(server)}/reconnect`),
    onSuccess: (data) => qc.setQueryData(['session-mcp', sessionId], data),
  })
}
```

Change `useStartMcpAuth` to `mutationFn: ({ name, account }: { name: string; account?: string }) => api.post(..., account ? { account } : undefined)`, and update `McpAuthFlow` to call `startMcpAuth.mutate({ name: server, account: accounts[0] })`. Event: `export interface EvMcpStatus { type: 'mcpStatus'; servers: McpServerState[] }`. Add it to the union and set `mcpStatus: true` in the flags. Reducer: add the state field `mcpServers: McpServerState[] | null` (initial `null`) and

```ts
    case 'mcpStatus':
      // Live MCP connection status (spec 2026-10-09). Not part of the transcript, so —
      // like contextUsage — it survives replayReset.
      return { ...state, mcpServers: Array.isArray(ev.servers) ? ev.servers : [] }
```

Make sure the `replayReset` case carries `mcpServers` forward (read how it preserves `contextUsage`).

- [ ] **Step 4: Run the tests and verify they pass.** Run: `(cd web && npm run typecheck && npx vitest run src/features/settings/sections/mcpStatus.test.ts src/session)`. Expected: PASS; typecheck errors will remain only in `McpSection.tsx` (fixed in Task 10). If typecheck must be green per task, fold the minimal `s.status` substitution into this commit.

- [ ] **Step 5: Commit**: "feat(web): MCP status types, hooks, mcpStatus event and reducer".

---

### Task 10: Settings › MCP status UI

**Files:**
- Modify: `web/src/features/settings/sections/McpSection.tsx`
- Create: `web/src/features/settings/sections/McpStatusPill.tsx` (exported pill, reused in Task 11)
- Modify test: `web/src/features/settings/sections/McpSection.test.tsx` (fixtures use `status`, not `needsAuth`)
- Check: `grep -rn "needs auth" web/e2e` and update any matching spec.

**Interfaces:**
- Consumes: `worstStatus`, `toneFor`, `shortName`, `relativeAge`, `useTestMcp`, `McpAuthFlow`
- Produces: `<McpStatusPill status={string | null} />`. Labels: `Connected`, `Failed`, `Needs auth`, `Connecting…` (pending), `Disabled`, `Not checked yet` (null), and the verbatim status otherwise. Tones map to the existing tokens (`text-danger`/`bg-danger-wash`, `text-warning`/`bg-warning-wash`, `text-ink-muted`, `text-success`). Check the token names in `web/src/index.css` / `tailwind.config` before using them.

- [ ] **Step 1: Write the failing tests** (in `McpSection.test.tsx`, which already mocks `@/api/hooks`; add `useTestMcp` to its mock)

```tsx
it('shows the worst status pill and per-account detail', async () => {
  mockServers([{ name: 'notion', source: 'user', readonly: false, transport: 'http', config: {},
    status: [row({ account: 'a', status: 'connected' }), row({ account: 'b', status: 'needs-auth' })] }])
  render(<McpSection />)
  expect(screen.getByText('Needs auth')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: /details for notion/i }))
  expect(screen.getByText(/b · dobby/)).toBeInTheDocument()
})

it('shows Not checked yet without rows and runs Test', async () => {
  const test = mockTestMcp()
  mockServers([{ name: 'gh', source: 'user', readonly: false, transport: 'stdio', config: {}, status: [] }])
  render(<McpSection />)
  expect(screen.getByText('Not checked yet')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Test gh' }))
  expect(test.mutate).toHaveBeenCalledWith({ server: 'gh' }, expect.anything())
})

it('Test all probes without a server filter', async () => {
  const test = mockTestMcp()
  mockServers([])
  render(<McpSection />)
  await userEvent.click(screen.getByRole('button', { name: 'Test all' }))
  expect(test.mutate).toHaveBeenCalledWith({}, expect.anything())
})

it('offers Reauth only for http/sse rows that need auth', () => {
  mockServers([{ name: 'notion', source: 'user', readonly: false, transport: 'http', config: {},
    status: [row({ account: 'b', status: 'needs-auth' })] }])
  render(<McpSection />)
  expect(screen.getByRole('button', { name: /sign in/i })).toBeInTheDocument()   // McpAuthFlow's idle button label — check its real text
})

it('renders an unknown status verbatim as neutral', () => {
  mockServers([{ name: 'x', source: 'user', readonly: false, transport: 'http', config: {}, status: [row({ status: 'connecting' })] }])
  render(<McpSection />)
  expect(screen.getByText('connecting')).toBeInTheDocument()
})
```

Write the `row()`, `mockServers()` and `mockTestMcp()` helpers at the top of the test file, following its existing mock style.

- [ ] **Step 2: Run them and verify they fail.** Run: `(cd web && npx vitest run src/features/settings/sections/McpSection.test.tsx)`. Expected: FAIL.

- [ ] **Step 3: Implement**
- **Each row:** keep the existing card button, which opens the editor dialog, and append `<McpStatusPill status={worstStatus(s.status.map(r => r.status))} />` inside it.
- **Siblings below the card** (never nested interactive elements, per the existing comment):
  - a disclosure button `aria-label={`Details for ${s.name}`}` that toggles a `<ul>` of rows. Each row reads `{r.account} · {r.host}`, then the pill, then `r.error` (`text-xs text-ink-muted`, `break-words`), then `checked {relativeAge(r.observedAt)} · from {r.source}`;
  - a `Test` button with `aria-label={`Test ${s.name}`}` that calls `test.mutate({ server: s.name }, { onSettled })`, shows a spinner label `Testing…` while pending, and renders `skipped` reasons inline if any.
- **Reauth:** render `<McpAuthFlow server={s.name} accounts={needsAuthAccounts} />` when `needsAuthAccounts = [...new Set(s.status.filter(r => r.status === 'needs-auth').map(r => r.account))]` is non-empty and the transport is http/sse. Keep the existing stdio explanation for stdio rows.
- **Section header:** add a `Test all` button (`test.mutate({}, …)`) next to the existing hint text, and change the hint to "MCP servers are shared across accounts; changes apply to newly started sessions. Test checks each server without starting a session."
- **Layout:** check the layout at 375 px wide (phone). The row's pill and buttons must wrap, not overflow (`flex-wrap`).

- [ ] **Step 4: Run the tests and verify they pass**: `(cd web && npm run typecheck && npm run lint && npx vitest run src/features/settings)`. Then `grep -rn "needs auth\|needsAuth" web/src web/e2e` should return nothing, and you should run the settings e2e spec if one covers MCP (`grep -rln "mcp" web/e2e`).

- [ ] **Step 5: Commit**: "feat(web): Settings › MCP shows live status with Test and Reauth".

---

### Task 11: In-session MCP readout

**Files:**
- Create: `web/src/features/sessions/McpStatusReadout.tsx`
- Modify: `web/src/features/sessions/SessionMeta.tsx` (new optional `mcpSlot?: React.ReactNode` prop, rendered immediately after the ctx readout block ~:268-278)
- Modify: `web/src/features/sessions/SessionBody.tsx:94-112` (pass `mcpSlot`)
- Test: `web/src/features/sessions/McpStatusReadout.test.tsx`

**Interfaces:**
- Consumes: `rt.state.mcpServers` (Task 9), `useSessionMcp`, `useReconnectSessionMcp`, `McpStatusPill`, `toneFor`, `McpAuthFlow`
- Produces: `<McpStatusReadout sessionId={string} live={McpServerState[] | null} />`, which renders nothing when it has no servers. The label is `MCP {connected}/{total}`, warning-toned when any server is `failed`/`needs-auth`, muted otherwise. It opens a popover listing each server with its pill, error and `{toolCount} tools`, plus the actions:
  - **Reconnect**: shown when the status is not `connected`/`disabled`;
  - **Reauth**: shown when the status is `needs-auth`; it renders `McpAuthFlow` with the session's account;
  - **Manage →**: links to `settingsSectionPath('mcp')`.

- [ ] **Step 1: Write the failing tests**

```tsx
it('renders nothing with no servers', () => {
  mockSessionMcp({ servers: [] })
  const { container } = render(<McpStatusReadout sessionId="s1" live={null} />)
  expect(container).toBeEmptyDOMElement()
})

it('prefers live event data over the fetched snapshot and warns on problems', () => {
  mockSessionMcp({ servers: [st('a', 'connected')] })
  render(<McpStatusReadout sessionId="s1" live={[st('a', 'connected'), st('b', 'needs-auth')]} />)
  const btn = screen.getByRole('button', { name: /MCP 1\/2/ })
  expect(btn.className).toMatch(/warning/)
})

it('Reconnect calls the session endpoint for that server', async () => {
  const rc = mockReconnect()
  render(<McpStatusReadout sessionId="s1" live={[st('b', 'failed', 'ENOENT')]} />)
  await userEvent.click(screen.getByRole('button', { name: /MCP 0\/1/ }))
  expect(screen.getByText('ENOENT')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Reconnect b' }))
  expect(rc.mutate).toHaveBeenCalledWith({ server: 'b' })
})

it('links to Settings › MCP', async () => {
  render(<McpStatusReadout sessionId="s1" live={[st('a', 'connected')]} />, { wrapper: MemoryRouterWrapper })
  await userEvent.click(screen.getByRole('button', { name: /MCP 1\/1/ }))
  expect(screen.getByRole('link', { name: /manage/i })).toHaveAttribute('href', expect.stringContaining('/settings/mcp'))
})
```

Copy the popover primitive and test wrapper from an existing SessionMeta popover, such as the quota or account readout (`grep -n "Popover" web/src/features/sessions/SessionMeta.tsx`). Write the `st()` / `mockSessionMcp()` / `mockReconnect()` helpers at the top, following the mocking style of other `features/sessions` tests.

- [ ] **Step 2: Run them and verify they fail.** Run: `(cd web && npx vitest run src/features/sessions/McpStatusReadout.test.tsx)`.

- [ ] **Step 3: Implement**

`servers = live ?? query.data?.servers ?? []`; query enabled `live == null`. The session's account for Reauth comes from a prop: add `account: string` to the component and pass `rt` account in `SessionBody` (use whatever `SessionMeta` receives as `account`). In `SessionMeta.tsx`, add `mcpSlot` to `Props`, destructure it, and render `{mcpSlot}` right after the ctx readout element so it sits in the same readout row. `SessionBody.tsx`: `mcpSlot={<McpStatusReadout sessionId={rt.sessionId} account={…} live={rt.state.mcpServers} />}`. Gate on the capability: if the session runtime exposes engine capabilities, pass `live` only when `mcp_status` is supported; otherwise rely on the endpoint returning `[]` (Codex) so the readout renders nothing.

- [ ] **Step 4: Run the tests and verify they pass**: **full** `(cd web && npm run typecheck && npm run lint && npm run test 2>&1 | tee /tmp/web-test.log; echo EXIT ${PIPESTATUS[0]})`. About 47 files mock `@/api/hooks`, and `SessionBody` now uses new hooks, so add `useSessionMcp`/`useReconnectSessionMcp` to any manual mock that breaks. Then run `(cd web && npm run build)`.

- [ ] **Step 5: Commit**: "feat(web): in-session MCP status readout with reconnect and reauth".

---

### Task 12: Docs, full gates, PR

**Files:**
- Modify: `docs/product/features/` — add `mcp-status.md` (what it shows, Test, reauth follow-through, kill switch). Link it from the library/settings feature doc if one lists sections (`grep -rln "MCP servers" docs/product/features`).
- Modify: `docs/superpowers/specs/2026-10-09-mcp-live-status-design.md`: set Status to "implemented", and record any Task 1 finding that changed a detail.

- [ ] **Step 1: Run the four `lint-and-test` gates plus web.** Run each with `tee` to `/tmp/`, and run the server suite inside the test-slot acquire block from `.agents/instructions/core.md`:
  - `ruff check src/ server/`
  - `mypy server/ --ignore-missing-imports`
  - `PYTHONPATH=$PWD/src pytest tests/`
  - `pytest server/tests/ -n auto --dist loadfile --timeout=300`
  - the Postgres suite (`VIBERPIT_TEST_PG_URL`)
  - `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)`
  - `python scripts/brand_ratchet.py`
- [ ] **Step 2: Do a manual smoke test against the real app**, following the `screenshot-branch-against-live-loopback-api` notes. Open Settings › MCP, press Test all, confirm the pills; open a live session and confirm `MCP n/m` and Reconnect.
- [ ] **Step 3: Before opening the PR**, re-check for in-flight PRs touching these files (the GraphQL query in core.md) and that `0078` is still the next migration number on `origin/master`.
- [ ] **Step 4: Open the PR.** Push, run `gh pr create --body-file /tmp/pr-body.md`; the body includes the spec and plan paths and the Task 1 findings summary. Then `gh pr merge <#> --squash --auto`, and confirm the PR is not a draft.
