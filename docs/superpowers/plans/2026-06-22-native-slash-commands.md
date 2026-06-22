# Native Claude Code Slash Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the ccswitch web app a native Claude Code slash-command experience — `/`-triggered discovery/autocomplete plus real execution of session-control, client-only, and custom/skill commands.

**Architecture:** Server-authoritative dispatcher. A per-session `CommandCatalog` (built from the SDK's `get_server_info()`) is the single source of truth for *what* commands exist; a driver-owned `CommandDispatcher` is the authority for *how* each executes, routing by a server-assigned `kind` (`client` | `control` | `passthrough`). The frontend is a thin palette + result renderer over three new WebSocket message types.

**Tech Stack:** Python 3.12 · FastAPI · `claude-agent-sdk` · pytest/anyio · React 18 + TypeScript + Vite · Tailwind · vitest + Testing Library.

## Global Constraints

- **Constitution III — verify against the real binary/API, not assumptions.** Command behaviors that depend on the SDK (custom-command expansion, `/compact`) are pinned by a real-SDK spike BEFORE building on them (Task 1).
- **Constitution VI — single source of truth / no dead code.** Discovery comes ONLY from `get_server_info()`; never hand-scan `.claude/commands/`. Command `kind` is assigned server-side; the client never re-derives command semantics.
- **Constitution V — TDD.** Every task writes a failing test first.
- **Filter, don't fake.** Any discovered command that can't actually execute (interactive-only, or `/compact` if the spike shows it no-ops) is excluded from the catalog so the palette never offers a silent no-op.
- **No arg DSL (YAGNI).** Args are a single opaque string passed through verbatim.
- **DB columns added nullable** (per CLAUDE.md). This feature adds NO new DB columns — catalog is in-memory, rebuilt on attach.
- **Server tests** run from the main checkout's prebuilt env; auth-sensitive suites use `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest`. **Web tests** run in `web/`: `vitest run`, `tsc --noEmit`, `eslint`.
- **Commit trailer:** end every commit message with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

**Server (new)**
- `server/sessions/commands/__init__.py` — package marker.
- `server/sessions/commands/catalog.py` — `Command` dataclass, `classify()`, `build_catalog()`.
- `server/sessions/commands/dispatcher.py` — `dispatch()` routing.
- `server/tests/test_command_catalog.py`, `test_command_dispatcher.py`, `test_command_driver.py`, `test_ws_commands.py`.
- `server/tests/integration/test_sdk_commands_spike.py` — real-SDK spike (skipped unless opted in).
- `docs/superpowers/spikes/2026-06-22-slash-commands-verdicts.md` — spike findings (Task 1 output).

**Server (modify)**
- `server/sessions/sdk_contract.py` — add `set_model`, `get_server_info`, `get_context_usage` to the Protocol.
- `server/tests/fakes/fake_sdk.py` — implement the three new methods + constructor args.
- `server/sessions/driver.py` — add `set_model()`, `reconnect()`, `server_info()`, `context_usage()`.
- `server/sessions/orchestrator.py` — add `command_catalog()` and `run_command()` delegators.
- `server/bridge/ws.py` — emit `commands` on attach; handle inbound `command`.

**Frontend (new)**
- `web/src/components/session/CommandPalette.tsx` — `/`-triggered autocomplete.
- `web/src/components/session/CommandResultOverlay.tsx` — `/help` + `/context` overlays.
- `web/src/components/session/__tests__/CommandPalette.test.tsx`.

**Frontend (modify)**
- `web/src/session/events.ts` — `Command` type; `notice` block is reducer-only (not a wire event).
- `web/src/ws/client.ts` — `commands`/`commandResult` inbound; `command()` outbound; event-map entries.
- `web/src/session/reducer.ts` — `notice` block kind; `_notice` + `_clear` local actions.
- `web/src/components/session/Turn.tsx` — render the `notice` block.
- `web/src/session/useSessionRuntime.ts` — hold catalog + command-result UI state; `onCommand`; submit routing.
- `web/src/components/session/Composer.tsx` — detect `/`, mount `CommandPalette`, route command vs prompt.
- `web/src/components/session/SessionView.tsx` — render `CommandResultOverlay`.

---

## Task 1: Verification spike — pin SDK command behavior

**Files:**
- Create: `server/tests/integration/test_sdk_commands_spike.py`
- Create: `docs/superpowers/spikes/2026-06-22-slash-commands-verdicts.md`

**Interfaces:**
- Produces: documented verdicts that GATE Task 2's `EXCLUDED`/`classify` and Task 4's `/compact` decision: (a) the shape of `get_server_info()["commands"]`, (b) whether `query("/<custom>")` expands a project `.claude/commands/*.md`, (c) whether `query("/compact")` triggers compaction.

This task produces knowledge, not shippable code. The integration test is skipped by default (needs a real account + network); it is the reproducible probe behind the verdict doc. Mirrors the existing `docs/superpowers/spikes/2026-06-20-phase0-verdicts.md` precedent.

- [ ] **Step 1: Write the spike probe (skipped unless opted in)**

```python
# server/tests/integration/test_sdk_commands_spike.py
"""Real-SDK spike: pin slash-command behavior before building the dispatcher.

Opt in with CCSWITCH_SPIKE=1 and a usable account config dir in CCSWITCH_SPIKE_CONFIG_DIR.
These hit the real `claude` engine and spend a little quota; they are NOT part of CI.
"""
import os
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("CCSWITCH_SPIKE") != "1",
    reason="real-SDK spike; set CCSWITCH_SPIKE=1 to run",
)


@pytest.mark.anyio
async def test_server_info_commands_shape():
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    cfg = os.environ["CCSWITCH_SPIKE_CONFIG_DIR"]
    env = dict(os.environ, CLAUDE_CONFIG_DIR=cfg)
    async with ClaudeSDKClient(options=ClaudeAgentOptions(env=env, cwd=os.getcwd())) as c:
        info = await c.get_server_info()
        assert info is not None, "get_server_info returned None — not in streaming mode?"
        cmds = info.get("commands")
        print("COMMANDS SAMPLE:", (cmds or [])[:5])
        assert isinstance(cmds, list) and cmds, "no commands enumerated"


@pytest.mark.anyio
async def test_custom_command_expands_via_query(tmp_path):
    # Create a throwaway .claude/commands/spikeecho.md in cwd and confirm /spikeecho
    # produces an assistant turn (i.e. the template was expanded, not echoed verbatim).
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage
    cwd = tmp_path
    cmds_dir = cwd / ".claude" / "commands"
    cmds_dir.mkdir(parents=True)
    (cmds_dir / "spikeecho.md").write_text("Reply with exactly the word: PONG")
    env = dict(os.environ, CLAUDE_CONFIG_DIR=os.environ["CCSWITCH_SPIKE_CONFIG_DIR"])
    async with ClaudeSDKClient(options=ClaudeAgentOptions(env=env, cwd=str(cwd))) as c:
        await c.query("/spikeecho")
        saw_assistant = False
        async for m in c.receive_response():
            if isinstance(m, AssistantMessage):
                saw_assistant = True
        assert saw_assistant, "custom command did not produce an assistant turn"


@pytest.mark.anyio
async def test_compact_behavior():
    # Record whether query("/compact") is accepted and what it streams back.
    # The VERDICT (works vs no-op) decides whether /compact ships. Print, don't assert.
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    env = dict(os.environ, CLAUDE_CONFIG_DIR=os.environ["CCSWITCH_SPIKE_CONFIG_DIR"])
    async with ClaudeSDKClient(options=ClaudeAgentOptions(env=env, cwd=os.getcwd())) as c:
        await c.query("/compact")
        kinds = []
        async for m in c.receive_response():
            kinds.append(type(m).__name__)
        print("COMPACT STREAM:", kinds)
```

- [ ] **Step 2: Run the spike (operator step)**

Run (from the main checkout env, with a real account):
```bash
CCSWITCH_SPIKE=1 CCSWITCH_SPIKE_CONFIG_DIR=<path-to-account-cfg> \
  <main-checkout>/.venv/bin/python -m pytest server/tests/integration/test_sdk_commands_spike.py -v -s
```
Expected: command-shape and custom-command tests PASS; compact test prints its stream.

- [ ] **Step 3: Record verdicts**

Write `docs/superpowers/spikes/2026-06-22-slash-commands-verdicts.md` capturing: the exact key names in each command entry (e.g. `name`/`description`/`argumentHint`), whether custom commands expand via `query()`, and the `/compact` verdict (ship vs defer). Task 2 (`build_catalog` field reads, `EXCLUDED`) and Task 4 (`/compact`) reference this file.

- [ ] **Step 4: Commit**

```bash
git add server/tests/integration/test_sdk_commands_spike.py docs/superpowers/spikes/2026-06-22-slash-commands-verdicts.md
git commit -m "spike: pin SDK slash-command behavior (server_info shape, custom expansion, /compact)"
```

> **Decision gate:** If the verdict shows custom commands do NOT expand via `query()`, STOP and revisit the design — the passthrough mechanism is the feature's core. If `/compact` no-ops, add `"compact"` to `EXCLUDED` in Task 2.

---

## Task 2: Command catalog model + normalization

**Files:**
- Create: `server/sessions/commands/__init__.py` (empty)
- Create: `server/sessions/commands/catalog.py`
- Test: `server/tests/test_command_catalog.py`

**Interfaces:**
- Produces:
  - `Command` dataclass: fields `name: str`, `description: str`, `argument_hint: str | None`, `kind: str` (`"client"|"control"|"passthrough"`); method `to_wire() -> dict` → `{"name","description","argumentHint","kind"}`.
  - `classify(name: str) -> str`.
  - `build_catalog(server_info: dict | None) -> list[Command]`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_command_catalog.py
from server.sessions.commands.catalog import Command, classify, build_catalog


def test_classify_buckets():
    assert classify("help") == "client"
    assert classify("context") == "client"
    assert classify("model") == "control"
    assert classify("clear") == "control"
    assert classify("review") == "passthrough"   # unknown → custom/passthrough


def test_build_catalog_normalizes_and_filters():
    info = {"commands": [
        {"name": "help", "description": "List commands"},
        {"name": "/model", "description": "Switch model", "argumentHint": "<model>"},
        {"name": "review", "description": "Review a PR", "argumentHint": "<pr#>"},
        {"name": "login", "description": "Sign in"},          # excluded (interactive)
        "barecmd",                                             # string form tolerated
        {"description": "no name"},                            # dropped (no name)
    ]}
    cat = build_catalog(info)
    by = {c.name: c for c in cat}
    assert "login" not in by                                  # filtered
    assert by["model"].kind == "control"
    assert by["model"].argument_hint == "<model>"
    assert by["help"].kind == "client"
    assert by["review"].kind == "passthrough"
    assert by["barecmd"].kind == "passthrough"
    assert by["model"].to_wire() == {
        "name": "model", "description": "Switch model",
        "argumentHint": "<model>", "kind": "control",
    }


def test_build_catalog_handles_none():
    assert build_catalog(None) == []
    assert build_catalog({}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_command_catalog.py -v`
Expected: FAIL — `ModuleNotFoundError: server.sessions.commands.catalog`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/commands/__init__.py
```
```python
# server/sessions/commands/catalog.py
"""Per-session slash-command catalog, normalized from the SDK's get_server_info().

Single source of truth for WHAT commands exist (constitution VI): we never scan
.claude/commands/ ourselves — the SDK already enumerates built-ins + custom + skills.
`kind` is assigned here, server-side, so the client never re-derives command semantics.
"""
from __future__ import annotations
from dataclasses import dataclass

# Commands handled by a dedicated client-side render (no model turn).
CLIENT = {"help", "context"}
# Commands executed via a dedicated SDK method (not raw passthrough).
CONTROL = {"model", "clear"}
# Interactive-only / auth / config commands that make no sense in this web UI.
# Tunable; see docs/superpowers/spikes/2026-06-22-slash-commands-verdicts.md.
EXCLUDED = {
    "login", "logout", "config", "doctor", "bug", "init", "ide",
    "terminal-setup", "vim", "resume", "exit", "quit", "upgrade",
    "release-notes", "status", "cost", "export", "add-dir",
}


@dataclass
class Command:
    name: str
    description: str
    argument_hint: str | None
    kind: str  # "client" | "control" | "passthrough"

    def to_wire(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "argumentHint": self.argument_hint,
            "kind": self.kind,
        }


def classify(name: str) -> str:
    if name in CLIENT:
        return "client"
    if name in CONTROL:
        return "control"
    return "passthrough"


def _coerce(entry) -> Command | None:
    if isinstance(entry, str):
        name, desc, hint = entry, "", None
    elif isinstance(entry, dict):
        name = entry.get("name") or entry.get("command") or ""
        desc = entry.get("description") or entry.get("desc") or ""
        hint = entry.get("argumentHint") or entry.get("argument_hint") or entry.get("argHint")
    else:
        return None
    name = str(name).lstrip("/").strip()
    if not name or name in EXCLUDED:
        return None
    return Command(name=name, description=str(desc), argument_hint=hint, kind=classify(name))


def build_catalog(server_info: dict | None) -> list[Command]:
    entries = (server_info or {}).get("commands") or []
    out: list[Command] = []
    seen: set[str] = set()
    for e in entries:
        cmd = _coerce(e)
        if cmd is None or cmd.name in seen:
            continue
        seen.add(cmd.name)
        out.append(cmd)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_command_catalog.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/commands/__init__.py server/sessions/commands/catalog.py server/tests/test_command_catalog.py
git commit -m "feat(commands): catalog model + normalization from get_server_info"
```

---

## Task 3: Driver SDK wrappers (set_model, reconnect, server_info, context_usage)

**Files:**
- Modify: `server/sessions/sdk_contract.py` (add 3 methods to the Protocol)
- Modify: `server/tests/fakes/fake_sdk.py:46-63` (implement them on FakeSDKClient)
- Modify: `server/sessions/driver.py` (add 4 driver methods)
- Test: `server/tests/test_command_driver.py`

**Interfaces:**
- Consumes: `FakeSDKClient` (Task fakes), `SessionDriver` (existing).
- Produces on `SessionDriver`:
  - `async set_model(model: str | None) -> None`
  - `async reconnect() -> None` (tear down client, restart with `resume=None`, reset turn/title/translator state)
  - `async server_info() -> dict | None`
  - `async context_usage() -> dict`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_command_driver.py
import pytest
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk


def _driver(client):
    def factory(cwd, config_dir, can_use_tool, mode, resume):
        client.last_resume = resume
        return client
    return SessionDriver(
        "sess-1", "acct", "/tmp", "/tmp/cfg",
        mode="interactive", sdk_factory=factory,
        registry=PermissionRegistry(), identity="tester",
    )


@pytest.mark.anyio
async def test_set_model_delegates():
    client = fake_sdk.FakeSDKClient(messages=[])
    d = _driver(client)
    await d.start()
    await d.set_model("sonnet")
    assert client.model == "sonnet"


@pytest.mark.anyio
async def test_server_info_and_context_usage_delegate():
    client = fake_sdk.FakeSDKClient(
        messages=[], server_info={"commands": [{"name": "help"}]},
        context_usage={"percentage": 12.5},
    )
    d = _driver(client)
    await d.start()
    assert (await d.server_info())["commands"][0]["name"] == "help"
    assert (await d.context_usage())["percentage"] == 12.5


@pytest.mark.anyio
async def test_reconnect_starts_fresh_and_resets_state():
    client = fake_sdk.FakeSDKClient(messages=[])
    d = _driver(client)
    await d.start()
    d.claude_session_id = "old-sid"
    d._titled = True
    await d.reconnect()
    assert client.last_resume is None          # fresh session, resume dropped
    assert d.claude_session_id is None
    assert d._titled is False
    assert d.resume is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_command_driver.py -v`
Expected: FAIL — `AttributeError: 'FakeSDKClient' object has no attribute 'model'` / `SessionDriver` has no `set_model`.

- [ ] **Step 3a: Extend the Protocol**

In `server/sessions/sdk_contract.py`, inside `SDKClientProtocol`, after `set_permission_mode`:
```python
    async def set_model(self, model: str | None = None) -> None: ...
    async def get_server_info(self) -> dict | None: ...
    async def get_context_usage(self) -> dict: ...
```

- [ ] **Step 3b: Implement on the fake**

Replace the FakeSDKClient body in `server/tests/fakes/fake_sdk.py` (lines 46-63) with:
```python
class FakeSDKClient:
    """Implements server.sessions.sdk_contract.SDKClientProtocol."""
    def __init__(self, messages, on_can_use_tool=None, server_info=None, context_usage=None):
        self._messages = messages
        self.on_can_use_tool = on_can_use_tool
        self.interrupted = False
        self.permission_mode = "default"
        self.model = None
        self.sent: list[str] = []
        self._server_info = server_info
        self._context_usage = context_usage or {}

    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    async def query(self, prompt: str): self.sent.append(prompt)
    async def receive_response(self):
        for m in self._messages:
            if self.interrupted: break
            yield m
    async def interrupt(self): self.interrupted = True
    async def set_permission_mode(self, mode: str): self.permission_mode = mode
    async def set_model(self, model=None): self.model = model
    async def get_server_info(self): return self._server_info
    async def get_context_usage(self): return self._context_usage
```

- [ ] **Step 3c: Add driver methods**

In `server/sessions/driver.py`, after `set_permission_mode` (line 358), add:
```python
    async def set_model(self, model: str | None) -> None:
        if self._client:
            await self._client.set_model(model)

    async def server_info(self) -> dict | None:
        if self._client is None:
            return None
        return await self._client.get_server_info()

    async def context_usage(self) -> dict:
        if self._client is None:
            return {}
        return await self._client.get_context_usage()

    async def reconnect(self) -> None:
        """Tear down the SDK client and start a FRESH session (resume=None).

        Implements /clear: a conversation's history IS the resumed session, so
        clearing == dropping the resume handle and reconnecting. Resets per-turn
        and titling state so the new session behaves like a brand-new one.
        """
        if self._pump is not None and not self._pump.done():
            self._pump.cancel()
            try:
                await self._pump
            except asyncio.CancelledError:
                pass
        self._pump = None
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
        self.resume = None
        self.claude_session_id = None
        self._titled = False
        self._queue.clear()
        self._turn_active = False
        self._tr = Translator()
        self._client = self._factory(self.cwd, self.config_dir, self._can_use_tool, self.mode, None)
        await self._client.__aenter__()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_command_driver.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/sdk_contract.py server/tests/fakes/fake_sdk.py server/sessions/driver.py server/tests/test_command_driver.py
git commit -m "feat(commands): driver wrappers for set_model/reconnect/server_info/context_usage"
```

---

## Task 4: CommandDispatcher

**Files:**
- Create: `server/sessions/commands/dispatcher.py`
- Test: `server/tests/test_command_dispatcher.py`

**Interfaces:**
- Consumes: `Command` (Task 2); a driver exposing `set_model`, `reconnect`, `context_usage`, `send_prompt`.
- Produces: `async dispatch(driver, cmd: Command, args: str) -> dict | None`.
  - Returns a **commandResult payload dict** (without `type`/`sessionId`/`ok`, which the WS layer adds) for `client`/`control` outcomes: `{"name", "render", "data"}` where `render ∈ {"help","context","notice","clear"}`.
  - Returns `None` for `passthrough` (it streams as a normal turn via `send_prompt`).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_command_dispatcher.py
import pytest
from server.sessions.commands.catalog import Command
from server.sessions.commands.dispatcher import dispatch


class FakeDriver:
    def __init__(self):
        self.model = "unset"
        self.reconnected = False
        self.prompts: list[str] = []
        self._usage = {"percentage": 33.0}
    async def set_model(self, m): self.model = m
    async def reconnect(self): self.reconnected = True
    async def context_usage(self): return self._usage
    async def send_prompt(self, text, images=None): self.prompts.append(text)


def _cmd(name, kind):
    return Command(name=name, description="", argument_hint=None, kind=kind)


@pytest.mark.anyio
async def test_help_returns_render_only():
    d = FakeDriver()
    res = await dispatch(d, _cmd("help", "client"), "")
    assert res == {"name": "help", "render": "help", "data": None}


@pytest.mark.anyio
async def test_context_fetches_usage():
    d = FakeDriver()
    res = await dispatch(d, _cmd("context", "client"), "")
    assert res["render"] == "context"
    assert res["data"] == {"percentage": 33.0}


@pytest.mark.anyio
async def test_model_sets_and_notices():
    d = FakeDriver()
    res = await dispatch(d, _cmd("model", "control"), "sonnet")
    assert d.model == "sonnet"
    assert res == {"name": "model", "render": "notice", "data": {"text": "Model → sonnet"}}


@pytest.mark.anyio
async def test_model_no_arg_uses_default_label():
    d = FakeDriver()
    res = await dispatch(d, _cmd("model", "control"), "")
    assert d.model is None
    assert res["data"]["text"] == "Model → default"


@pytest.mark.anyio
async def test_clear_reconnects():
    d = FakeDriver()
    res = await dispatch(d, _cmd("clear", "control"), "")
    assert d.reconnected is True
    assert res == {"name": "clear", "render": "clear", "data": {"text": "Conversation cleared"}}


@pytest.mark.anyio
async def test_passthrough_sends_prompt_and_returns_none():
    d = FakeDriver()
    res = await dispatch(d, _cmd("review", "passthrough"), "64")
    assert res is None
    assert d.prompts == ["/review 64"]


@pytest.mark.anyio
async def test_passthrough_no_args():
    d = FakeDriver()
    await dispatch(d, _cmd("compact", "passthrough"), "")
    assert d.prompts == ["/compact"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_command_dispatcher.py -v`
Expected: FAIL — `ModuleNotFoundError: ...commands.dispatcher`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/commands/dispatcher.py
"""Routes a command invocation to its execution mechanism, by kind.

Authority for HOW each command executes. Returns a commandResult payload dict
for client/control outcomes (the WS layer wraps it with type/sessionId/ok), or
None for passthrough (which streams as a normal turn).
"""
from __future__ import annotations
from .catalog import Command


async def dispatch(driver, cmd: Command, args: str) -> dict | None:
    args = (args or "").strip()

    if cmd.kind == "client":
        if cmd.name == "context":
            return {"name": "context", "render": "context", "data": await driver.context_usage()}
        # /help (and any other client command) renders from the client's cached catalog.
        return {"name": cmd.name, "render": "help", "data": None}

    if cmd.kind == "control":
        if cmd.name == "model":
            await driver.set_model(args or None)
            return {"name": "model", "render": "notice", "data": {"text": f"Model → {args or 'default'}"}}
        if cmd.name == "clear":
            await driver.reconnect()
            return {"name": "clear", "render": "clear", "data": {"text": "Conversation cleared"}}

    # passthrough — custom commands, skills, /compact: stream as a normal turn.
    text = f"/{cmd.name}" + (f" {args}" if args else "")
    await driver.send_prompt(text)
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_command_dispatcher.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/commands/dispatcher.py server/tests/test_command_dispatcher.py
git commit -m "feat(commands): dispatcher routing by kind"
```

---

## Task 5: Orchestrator delegators

**Files:**
- Modify: `server/sessions/orchestrator.py` (add two methods near `set_permission_mode`, ~line 791)
- Test: `server/tests/test_command_dispatcher.py` (append orchestrator-level cases) — or a new `test_command_orchestrator.py`

**Interfaces:**
- Produces on `SessionOrchestrator`:
  - `async command_catalog(session_id) -> list[dict]` — wire dicts, `[]` if no driver/server_info.
  - `async run_command(session_id, name, args) -> dict | None` — looks up the command in the freshly-built catalog and dispatches; raises `KeyError` if the command is unknown; returns the dispatcher payload or `None`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_command_orchestrator.py
import pytest
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes import fake_sdk


def _orch_with_driver(session_id="s1", server_info=None):
    client = fake_sdk.FakeSDKClient(messages=[], server_info=server_info)
    orch = SessionOrchestrator(sdk_factory=lambda *a, **k: client)
    return orch, client


@pytest.mark.anyio
async def test_command_catalog_empty_when_no_driver():
    orch, _ = _orch_with_driver()
    assert await orch.command_catalog("missing") == []


@pytest.mark.anyio
async def test_run_command_unknown_raises():
    orch, client = _orch_with_driver(server_info={"commands": [{"name": "help"}]})
    from server.sessions.driver import SessionDriver
    from server.sessions.permissions import PermissionRegistry
    d = SessionDriver("s1", "a", "/tmp", "/tmp/c", mode="interactive",
                      sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
                      identity="t")
    await d.start()
    orch._drivers["s1"] = d
    with pytest.raises(KeyError):
        await orch.run_command("s1", "nope", "")


@pytest.mark.anyio
async def test_run_command_dispatches_passthrough():
    orch, client = _orch_with_driver(server_info={"commands": [{"name": "review"}]})
    from server.sessions.driver import SessionDriver
    from server.sessions.permissions import PermissionRegistry
    d = SessionDriver("s1", "a", "/tmp", "/tmp/c", mode="interactive",
                      sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
                      identity="t")
    await d.start()
    orch._drivers["s1"] = d
    res = await orch.run_command("s1", "review", "64")
    assert res is None
    assert client.sent == ["/review 64"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_command_orchestrator.py -v`
Expected: FAIL — `SessionOrchestrator` has no `command_catalog` / `run_command`.

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/orchestrator.py`, add the import near the top (with the other `server.sessions` imports, ~line 32):
```python
from server.sessions.commands import catalog as cmd_catalog
from server.sessions.commands import dispatcher as cmd_dispatcher
```
Then after `set_permission_mode` (~line 794) add:
```python
    async def command_catalog(self, session_id) -> list[dict]:
        """Build the per-session slash-command catalog (wire dicts). [] if no driver."""
        d = self._drivers.get(session_id)
        if d is None:
            return []
        try:
            info = await d.server_info()
        except Exception:
            log.warning("command_catalog: server_info failed session=%s", session_id, exc_info=True)
            return []
        return [c.to_wire() for c in cmd_catalog.build_catalog(info)]

    async def run_command(self, session_id, name, args) -> dict | None:
        """Dispatch a slash command. Raises KeyError if the command is unknown."""
        d = self._drivers.get(session_id)
        if d is None:
            raise KeyError(name)
        info = await d.server_info()
        by_name = {c.name: c for c in cmd_catalog.build_catalog(info)}
        cmd = by_name.get(str(name).lstrip("/"))
        if cmd is None:
            raise KeyError(name)
        return await cmd_dispatcher.dispatch(d, cmd, args or "")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_command_orchestrator.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_command_orchestrator.py
git commit -m "feat(commands): orchestrator command_catalog + run_command delegators"
```

---

## Task 6: WebSocket bridge — emit catalog, handle invocation

**Files:**
- Modify: `server/bridge/ws.py` (emit `commands` in `_handle_attach`; add a `command` branch in the receive loop)
- Test: `server/tests/test_ws_commands.py`

**Interfaces:**
- Consumes: `orchestrator.command_catalog`, `orchestrator.run_command`.
- Produces wire frames:
  - S→C `{"type":"commands","sessionId","commands":[...]}` — sent during attach after `replayComplete`.
  - C→S `{"type":"command","sessionId","name","args"}` handled → on a non-None dispatcher payload, send `{"type":"commandResult","sessionId","ok":true, **payload}`; on `KeyError` send `{"type":"commandResult","sessionId","name","ok":false,"error":"unknown command"}`; on other exceptions `ok:false,"error":"command failed"`.

- [ ] **Step 1: Write the failing test**

Follow the existing WS test pattern in `server/tests/` (a fake orchestrator + the TestClient websocket). Minimal contract test:
```python
# server/tests/test_ws_commands.py
import json
import pytest
from server.tests.helpers_ws import make_ws_app  # see note below


class FakeOrch:
    def __init__(self):
        self.ran = []
    def subscribe_events(self): 
        import asyncio; return asyncio.Queue()
    def unsubscribe_events(self, q): pass
    async def ensure_driver(self, sid, identity): return object()
    def get_session(self, sid): return None
    def transcript_path(self, sid): return None
    @property
    def _registry(self):
        class R:
            def pending(self, sid): return []
        return R()
    def pending_questions(self, sid): return []
    def pop_pending_initial_prompt(self, sid): return None
    def pop_pending_initial_images(self, sid): return None
    async def command_catalog(self, sid): return [{"name":"help","description":"","argumentHint":None,"kind":"client"}]
    async def run_command(self, sid, name, args):
        self.ran.append((name, args))
        if name == "model":
            return {"name":"model","render":"notice","data":{"text":"Model → sonnet"}}
        if name == "bogus":
            raise KeyError(name)
        return None
```

> **Note:** reuse whatever harness existing WS tests use (search `server/tests/` for `/ws` + `TestClient`/`websocket_connect`). The assertions that matter, however the socket is opened:
> 1. After sending `{"type":"attach","sessionId":"s1"}`, the client receives a frame `{"type":"commands","sessionId":"s1","commands":[...]}` (it MAY arrive after a `replayComplete`).
> 2. Sending `{"type":"command","sessionId":"s1","name":"model","args":"sonnet"}` yields `{"type":"commandResult","sessionId":"s1","ok":true,"name":"model","render":"notice","data":{"text":"Model → sonnet"}}`.
> 3. Sending `{"type":"command","sessionId":"s1","name":"bogus","args":""}` yields `commandResult` with `ok:false`.
> 4. A passthrough command (returns None) yields NO `commandResult` frame.

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_ws_commands.py -v`
Expected: FAIL — no `commands` frame emitted / unknown message type `command`.

- [ ] **Step 3a: Emit catalog on attach**

In `server/bridge/ws.py`, inside `_handle_attach`, immediately after the `await _send({"type": "replayComplete"})` line (line 167), add:
```python
            # Slash-command catalog (built from the SDK's get_server_info) for this session.
            try:
                catalog = await orchestrator.command_catalog(session_id)
                await _send({"type": "commands", "sessionId": session_id, "commands": catalog})
            except Exception:
                log.warning("ws.attach: command_catalog failed for %s", session_id, exc_info=True)
```

- [ ] **Step 3b: Handle the `command` message**

In the receive loop, add a branch after the `cancelQueued` branch (line 319), before the `else`:
```python
                elif msg_type == "command":
                    name = msg.get("name", "")
                    args = msg.get("args", "")
                    try:
                        result = await orchestrator.run_command(session_id, name, args)
                    except KeyError:
                        await _send({"type": "commandResult", "sessionId": session_id,
                                     "name": name, "ok": False, "error": "unknown command"})
                    except Exception:
                        log.warning("ws.command failed session=%s name=%s", session_id, name, exc_info=True)
                        await _send({"type": "commandResult", "sessionId": session_id,
                                     "name": name, "ok": False, "error": "command failed"})
                    else:
                        if result is not None:
                            await _send({"type": "commandResult", "sessionId": session_id,
                                         "ok": True, **result})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_ws_commands.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/bridge/ws.py server/tests/test_ws_commands.py
git commit -m "feat(commands): WS catalog emission + command dispatch"
```

---

## Task 7: Frontend WS client — types, inbound, outbound

**Files:**
- Modify: `web/src/session/events.ts` (add `Command` type)
- Modify: `web/src/ws/client.ts` (inbound `commands`/`commandResult`, outbound `command()`, event map)
- Test: `web/src/ws/__tests__/client.commands.test.ts` (new)

**Interfaces:**
- Produces:
  - `Command` (TS): `{ name: string; description: string; argumentHint: string | null; kind: 'client'|'control'|'passthrough' }`.
  - `WsCommandsMsg`, `WsCommandResultMsg` inbound types; `EventMap` keys `commands`, `commandResult`.
  - `SessionSocket.command(sessionId, name, args)` outbound; hook exposes `command`.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/ws/__tests__/client.commands.test.ts
import { describe, it, expect, vi } from 'vitest'
import { SessionSocket } from '@/ws/client'

describe('SessionSocket commands', () => {
  it('emits commands and commandResult to listeners', () => {
    const s = new SessionSocket('/ws')
    const onCommands = vi.fn()
    const onResult = vi.fn()
    s.on('commands', onCommands)
    s.on('commandResult', onResult)
    // drive the private inbound handler
    ;(s as unknown as { _handleInbound: (m: unknown) => void })._handleInbound(
      { type: 'commands', sessionId: 's1', commands: [{ name: 'help', description: '', argumentHint: null, kind: 'client' }] })
    ;(s as unknown as { _handleInbound: (m: unknown) => void })._handleInbound(
      { type: 'commandResult', sessionId: 's1', ok: true, name: 'model', render: 'notice', data: { text: 'Model → sonnet' } })
    expect(onCommands).toHaveBeenCalledOnce()
    expect(onResult).toHaveBeenCalledWith(expect.objectContaining({ render: 'notice' }))
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/ws/__tests__/client.commands.test.ts`
Expected: FAIL — `commands`/`commandResult` not in EventMap / not emitted.

- [ ] **Step 3a: Add the `Command` type**

Append to `web/src/session/events.ts`:
```ts
export interface Command {
  name: string
  description: string
  argumentHint: string | null
  kind: 'client' | 'control' | 'passthrough'
}
```

- [ ] **Step 3b: Wire the client**

In `web/src/ws/client.ts`:
- Add inbound shapes (after `WsBatchMsg`, line 21):
```ts
export interface WsCommandsMsg      { type: 'commands';      sessionId: string; commands: import('@/session/events').Command[] }
export interface WsCommandResultMsg { type: 'commandResult'; sessionId: string; ok: boolean; name?: string; render?: 'help' | 'context' | 'notice' | 'clear'; data?: unknown; error?: string }
```
- Add them to the `WsInboundMsg` union.
- Add to `EventMap` (line 32):
```ts
  commands:      (msg: WsCommandsMsg) => void
  commandResult: (msg: WsCommandResultMsg) => void
```
- Add the outbound method (near `cancelQueued`, line 161):
```ts
  command(sessionId: string, name: string, args: string) {
    this._send({ type: 'command', sessionId, name, args })
  }
```
- In `_handleInbound` (line 213), add cases before `default`:
```ts
      case 'commands':
        this._emit('commands', msg)
        break
      case 'commandResult':
        this._emit('commandResult', msg)
        break
```
- In the hook `useSessionSocket` (near line 322), add:
```ts
  const command = useCallback((sessionId: string, name: string, args: string) => socket.command(sessionId, name, args), [socket])
```
and include `command` in the returned object (line 327-333).

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/ws/__tests__/client.commands.test.ts && npx tsc --noEmit`
Expected: PASS + no type errors.

- [ ] **Step 5: Commit**

```bash
git add web/src/session/events.ts web/src/ws/client.ts web/src/ws/__tests__/client.commands.test.ts
git commit -m "feat(commands): web WS client types + command channel"
```

---

## Task 8: Frontend reducer — notice block + clear/notice actions

**Files:**
- Modify: `web/src/session/reducer.ts`
- Modify: `web/src/components/session/Turn.tsx` (render the notice block, ~line 56 switch)
- Test: `web/src/session/__tests__/reducer.notice.test.ts` (new)

**Interfaces:**
- Produces:
  - `Block` gains `{ kind: 'notice'; id: string; text: string }`.
  - `LocalAction` gains `{ type: '_notice'; text: string }` and `{ type: '_clear'; text: string }`.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/session/__tests__/reducer.notice.test.ts
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from '@/session/reducer'

describe('notice + clear actions', () => {
  it('_notice appends a notice block', () => {
    const s = transcriptReducer(initialTranscript, { type: '_notice', text: 'Model → sonnet' })
    const last = s.turns.at(-1)!
    expect(last.blocks[0]).toMatchObject({ kind: 'notice', text: 'Model → sonnet' })
  })

  it('_clear resets transcript and shows a single notice', () => {
    const seeded = transcriptReducer(initialTranscript, { type: 'user', turnId: 't1', text: 'hi' })
    const s = transcriptReducer(seeded, { type: '_clear', text: 'Conversation cleared' })
    expect(s.turns).toHaveLength(1)
    expect(s.turns[0].blocks[0]).toMatchObject({ kind: 'notice', text: 'Conversation cleared' })
    expect(s.replaying).toBe(false)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/session/__tests__/reducer.notice.test.ts`
Expected: FAIL — actions hit `default` and return unchanged state.

- [ ] **Step 3a: Extend reducer types**

In `web/src/session/reducer.ts`:
- Add to the `Block` union (line 5):
```ts
  | { kind: 'notice';     id: string; text: string }
```
- Add to `LocalAction` (line 76):
```ts
  | { type: '_notice'; text: string }
  | { type: '_clear';  text: string }
```
- Add cases before `default` (line 377):
```ts
    case '_notice': {
      const id = `notice:${state.turns.length}`
      return {
        ...state,
        turns: [...state.turns, { id, role: 'assistant', blocks: [{ kind: 'notice', id: `${id}:n`, text: ev.text }] }],
        openBlockId: null,
      }
    }

    case '_clear': {
      const id = 'notice:clear'
      return {
        ...initialTranscript,
        replaying: false,
        turns: [{ id, role: 'assistant', blocks: [{ kind: 'notice', id: `${id}:n`, text: ev.text }] }],
      }
    }
```

- [ ] **Step 3b: Render the notice block**

In `web/src/components/session/Turn.tsx`, inside the `switch (block.kind)` (line 56), add a case:
```tsx
          case 'notice':
            return (
              <div key={block.id} className="my-2 text-center text-xs text-gray-400 dark:text-gray-500 italic">
                {block.text}
              </div>
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/session/__tests__/reducer.notice.test.ts && npx tsc --noEmit`
Expected: PASS + no type errors.

- [ ] **Step 5: Commit**

```bash
git add web/src/session/reducer.ts web/src/components/session/Turn.tsx web/src/session/__tests__/reducer.notice.test.ts
git commit -m "feat(commands): notice transcript block + _notice/_clear actions"
```

---

## Task 9: CommandPalette component

**Files:**
- Create: `web/src/components/session/CommandPalette.tsx`
- Test: `web/src/components/session/__tests__/CommandPalette.test.tsx`

**Interfaces:**
- Produces: `CommandPalette` component.
  - Props: `{ commands: Command[]; query: string; onSelect: (cmd: Command) => void; onComplete: (cmd: Command) => void; onDismiss: () => void }`.
  - `query` is the text AFTER the leading `/` (e.g. for `/mod` it is `"mod"`).
  - Renders only when there is ≥1 match; filters by name prefix/substring (case-insensitive), built-ins (`control`/`client`) first then a `Custom` group (`passthrough`).
  - Keyboard: `↑/↓` move selection, `Enter` → `onSelect(selected)`, `Tab` → `onComplete(selected)`, `Esc` → `onDismiss()`. Returns `true`-ish handled state via a `handleKeyDown` the Composer delegates to (Task 10).

To keep keyboard handling testable and let the Composer's textarea keep focus, expose an imperative handle.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/session/__tests__/CommandPalette.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { createRef } from 'react'
import { CommandPalette, type CommandPaletteHandle } from '@/components/session/CommandPalette'
import type { Command } from '@/session/events'

const CMDS: Command[] = [
  { name: 'model', description: 'Switch model', argumentHint: '<model>', kind: 'control' },
  { name: 'help', description: 'List commands', argumentHint: null, kind: 'client' },
  { name: 'review', description: 'Review a PR', argumentHint: '<pr#>', kind: 'passthrough' },
]

describe('CommandPalette', () => {
  it('filters by query and groups custom commands', () => {
    render(<CommandPalette commands={CMDS} query="" onSelect={vi.fn()} onComplete={vi.fn()} onDismiss={vi.fn()} />)
    expect(screen.getByText('model')).toBeInTheDocument()
    expect(screen.getByText('Custom')).toBeInTheDocument() // group divider for passthrough
  })

  it('Enter selects the highlighted command', () => {
    const onSelect = vi.fn()
    const ref = createRef<CommandPaletteHandle>()
    render(<CommandPalette ref={ref} commands={CMDS} query="mod" onSelect={onSelect} onComplete={vi.fn()} onDismiss={vi.fn()} />)
    const handled = ref.current!.handleKeyDown({ key: 'Enter', preventDefault() {} } as unknown as React.KeyboardEvent)
    expect(handled).toBe(true)
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ name: 'model' }))
  })

  it('renders nothing when no match', () => {
    const { container } = render(<CommandPalette commands={CMDS} query="zzz" onSelect={vi.fn()} onComplete={vi.fn()} onDismiss={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/session/__tests__/CommandPalette.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```tsx
// web/src/components/session/CommandPalette.tsx
import { forwardRef, useImperativeHandle, useMemo, useState, useEffect } from 'react'
import { cn } from '@/lib/utils'
import type { Command } from '@/session/events'

export interface CommandPaletteHandle {
  /** Returns true if the key was consumed by the palette. */
  handleKeyDown: (e: React.KeyboardEvent) => boolean
}

interface Props {
  commands: Command[]
  query: string
  onSelect: (cmd: Command) => void
  onComplete: (cmd: Command) => void
  onDismiss: () => void
}

function rank(c: Command): number {
  return c.kind === 'passthrough' ? 1 : 0  // built-ins first, custom after
}

export const CommandPalette = forwardRef<CommandPaletteHandle, Props>(function CommandPalette(
  { commands, query, onSelect, onComplete, onDismiss }, ref,
) {
  const matches = useMemo(() => {
    const q = query.toLowerCase()
    return commands
      .filter((c) => c.name.toLowerCase().includes(q))
      .sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name))
  }, [commands, query])

  const [sel, setSel] = useState(0)
  useEffect(() => { setSel(0) }, [query])

  useImperativeHandle(ref, () => ({
    handleKeyDown(e: React.KeyboardEvent): boolean {
      if (matches.length === 0) return false
      if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => (s + 1) % matches.length); return true }
      if (e.key === 'ArrowUp')   { e.preventDefault(); setSel((s) => (s - 1 + matches.length) % matches.length); return true }
      if (e.key === 'Enter')     { e.preventDefault(); onSelect(matches[sel]); return true }
      if (e.key === 'Tab')       { e.preventDefault(); onComplete(matches[sel]); return true }
      if (e.key === 'Escape')    { e.preventDefault(); onDismiss(); return true }
      return false
    },
  }), [matches, sel, onSelect, onComplete, onDismiss])

  if (matches.length === 0) return null

  let lastRank = -1
  return (
    <div
      role="listbox"
      aria-label="Slash commands"
      className={cn(
        'absolute bottom-full left-0 right-0 mb-1 max-h-64 overflow-y-auto z-20',
        'rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 shadow-lg',
      )}
    >
      {matches.map((c, i) => {
        const showDivider = rank(c) !== lastRank && rank(c) === 1
        lastRank = rank(c)
        return (
          <div key={c.name}>
            {showDivider && (
              <div className="px-3 py-1 text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500">Custom</div>
            )}
            <button
              type="button"
              role="option"
              aria-selected={i === sel}
              onMouseEnter={() => setSel(i)}
              onClick={() => onSelect(c)}
              className={cn(
                'flex w-full items-center gap-2 px-3 py-2 text-left text-sm min-h-[44px]',
                i === sel ? 'bg-blue-50 dark:bg-blue-900/30' : 'hover:bg-gray-50 dark:hover:bg-gray-800',
              )}
            >
              <span className="font-mono text-gray-900 dark:text-gray-100">{c.name}</span>
              <span className="flex-1 truncate text-gray-500 dark:text-gray-400">{c.description}</span>
              {c.argumentHint && <span className="font-mono text-xs text-gray-400">{c.argumentHint}</span>}
            </button>
          </div>
        )
      })}
    </div>
  )
})
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/components/session/__tests__/CommandPalette.test.tsx && npx tsc --noEmit`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/CommandPalette.tsx web/src/components/session/__tests__/CommandPalette.test.tsx
git commit -m "feat(commands): CommandPalette autocomplete component"
```

---

## Task 10: Wire palette + command routing into runtime and Composer

**Files:**
- Modify: `web/src/session/useSessionRuntime.ts` (catalog state, command-result UI state, `onCommand`)
- Modify: `web/src/components/session/Composer.tsx` (detect `/`, mount palette, route command vs prompt)
- Modify: `web/src/components/session/SessionView.tsx` (pass new runtime props to Composer)
- Test: `web/src/session/__tests__/useSessionRuntime.commands.test.ts` (new)

**Interfaces:**
- `SessionRuntime` gains:
  - `commands: Command[]`
  - `commandResult: { render: 'help' | 'context'; data: unknown } | null` (overlay state; `notice`/`clear`/errors are folded into the transcript, not held here)
  - `onCommand: (name: string, args: string) => void`
  - `dismissCommandResult: () => void`
- Composer Props gain: `commands: Command[]`, `onCommand: (name: string, args: string) => void`.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/session/__tests__/useSessionRuntime.commands.test.ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

// Mock the socket hook so we can drive inbound commands/commandResult.
const listeners: Record<string, ((m: unknown) => void)[]> = {}
const command = vi.fn()
vi.mock('@/ws/client', () => ({
  useSessionSocket: () => ({
    attach: vi.fn(), detach: vi.fn(),
    on: (ev: string, cb: (m: unknown) => void) => { (listeners[ev] ||= []).push(cb) },
    off: vi.fn(),
    connectionState: 'connected',
    prompt: vi.fn(), sendInterrupt: vi.fn(), permissionResponse: vi.fn(),
    answerQuestion: vi.fn(), setPermissionMode: vi.fn(), cancelQueued: vi.fn(),
    command,
  }),
}))
vi.mock('@/api/hooks', () => ({ useSessions: () => ({ data: [] }), useAccounts: () => ({ data: [] }), QK: {} }))

import { useSessionRuntime } from '@/session/useSessionRuntime'

function emit(ev: string, msg: unknown) { (listeners[ev] || []).forEach((cb) => cb(msg)) }

describe('runtime commands', () => {
  beforeEach(() => { for (const k of Object.keys(listeners)) delete listeners[k]; command.mockClear() })

  it('captures catalog and forwards onCommand', () => {
    const { result } = renderHook(() => useSessionRuntime('s1'))
    act(() => emit('commands', { type: 'commands', sessionId: 's1', commands: [{ name: 'help', description: '', argumentHint: null, kind: 'client' }] }))
    expect(result.current.commands).toHaveLength(1)
    act(() => result.current.onCommand('model', 'sonnet'))
    expect(command).toHaveBeenCalledWith('s1', 'model', 'sonnet')
  })

  it('notice result appends to transcript; help result opens overlay', () => {
    const { result } = renderHook(() => useSessionRuntime('s1'))
    act(() => emit('commandResult', { type: 'commandResult', sessionId: 's1', ok: true, render: 'notice', data: { text: 'Model → sonnet' } }))
    expect(result.current.state.turns.at(-1)!.blocks[0]).toMatchObject({ kind: 'notice', text: 'Model → sonnet' })
    act(() => emit('commandResult', { type: 'commandResult', sessionId: 's1', ok: true, render: 'help', data: null }))
    expect(result.current.commandResult).toMatchObject({ render: 'help' })
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/session/__tests__/useSessionRuntime.commands.test.ts`
Expected: FAIL — `commands`/`onCommand`/`commandResult` not on the runtime.

- [ ] **Step 3a: Extend the runtime**

In `web/src/session/useSessionRuntime.ts`:
- Pull `command` from `useSessionSocket()` (add to the destructure, line 56).
- Add `Command` import: `import type { SessionEvent, Command } from '@/session/events'` (line 17).
- Add state near the other `useState`s (line 71):
```ts
  const [commands, setCommands] = useState<Command[]>([])
  const [commandResult, setCommandResult] = useState<{ render: 'help' | 'context'; data: unknown } | null>(null)
```
- In the attach/subscribe effect (line 82), register two more listeners alongside `onEvent`:
```ts
    const onCommands = (msg: { sessionId: string; commands: Command[] }) => {
      if (msg.sessionId !== sessionId) return
      setCommands(msg.commands)
    }
    const onCommandResult = (msg: { sessionId: string; ok: boolean; render?: string; data?: unknown; error?: string }) => {
      if (msg.sessionId !== sessionId) return
      if (!msg.ok) { dispatch({ type: '_notice', text: msg.error ? `Command failed: ${msg.error}` : 'Command failed' }); return }
      if (msg.render === 'clear')  { dispatch({ type: '_clear', text: (msg.data as { text: string })?.text ?? 'Conversation cleared' }); return }
      if (msg.render === 'notice') { dispatch({ type: '_notice', text: (msg.data as { text: string })?.text ?? '' }); return }
      if (msg.render === 'help' || msg.render === 'context') { setCommandResult({ render: msg.render, data: msg.data }) }
    }
    on('commands', onCommands)
    on('commandResult', onCommandResult)
```
  and mirror them in the cleanup `off(...)` calls (line 100).
- Add the handlers near `onSetMode` (line 165):
```ts
  const onCommand = useCallback((name: string, args: string) => command(sessionId, name, args), [sessionId, command])
  const dismissCommandResult = useCallback(() => setCommandResult(null), [])
```
- Add `commands`, `commandResult`, `onCommand`, `dismissCommandResult` to the `SessionRuntime` interface (line 25) and the returned object (line 220).

- [ ] **Step 3b: Route command vs prompt in the Composer**

In `web/src/components/session/Composer.tsx`:
- Extend `Props` (line 6): add `commands: Command[]` and `onCommand: (name: string, args: string) => void`; import `Command` and `CommandPalette`, `CommandPaletteHandle`.
- Add palette state and a ref:
```ts
  const paletteRef = useRef<CommandPaletteHandle>(null)
  const slash = text.startsWith('/') && !text.includes('\n')
  const slashQuery = slash ? text.slice(1).split(/\s+/)[0] : ''
```
- In `handleKeyDown` (line 55), let the palette consume keys first:
```ts
    if (slash && paletteRef.current?.handleKeyDown(e)) return
```
- Add a parser + submit routing. Replace `submit` (line 32) so a bare known command dispatches a command, otherwise it prompts:
```ts
  const runCommandFromText = useCallback((raw: string): boolean => {
    if (!raw.startsWith('/') || raw.includes('\n')) return false
    const m = raw.slice(1).match(/^(\S+)\s*(.*)$/)
    if (!m) return false
    const known = commands.find((c) => c.name === m[1])
    if (!known) return false
    onCommand(known.name, m[2] ?? '')
    return true
  }, [commands, onCommand])

  const submit = useCallback(() => {
    const trimmed = text.trim()
    const refs = att.refs()
    if (!trimmed && refs.length === 0) return
    if (refs.length === 0 && runCommandFromText(trimmed)) {
      setText(''); if (textareaRef.current) textareaRef.current.style.height = 'auto'; return
    }
    onPrompt(trimmed, refs)
    setText(''); att.clear()
    if (textareaRef.current) textareaRef.current.style.height = 'auto'
  }, [text, onPrompt, att, runCommandFromText])
```
- Render the palette inside the relative container, above the textarea. Wrap the textarea row container with `relative` and add:
```tsx
      {slash && (
        <CommandPalette
          ref={paletteRef}
          commands={commands}
          query={slashQuery}
          onSelect={(c) => { if (c.argumentHint) { setText(`/${c.name} `) } else { onCommand(c.name, ''); setText('') } }}
          onComplete={(c) => setText(`/${c.name} `)}
          onDismiss={() => { /* keep text; palette hides when slash becomes false */ }}
        />
      )}
```

- [ ] **Step 3c: Pass props from SessionView**

In `web/src/components/session/SessionView.tsx`, where `<Composer .../>` is rendered, pass `commands={runtime.commands}` and `onCommand={runtime.onCommand}` (use whatever the runtime variable is named in that file). Do the same anywhere else Composer is rendered (e.g. `DesktopShell.tsx`).

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `npx vitest run src/session/__tests__/useSessionRuntime.commands.test.ts && npx tsc --noEmit && npx eslint src/components/session/Composer.tsx src/session/useSessionRuntime.ts`
Expected: PASS + clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/session/useSessionRuntime.ts web/src/components/session/Composer.tsx web/src/components/session/SessionView.tsx web/src/components/DesktopShell.tsx web/src/session/__tests__/useSessionRuntime.commands.test.ts
git commit -m "feat(commands): palette + command routing wired into Composer/runtime"
```

---

## Task 11: Command result overlays (/help, /context)

**Files:**
- Create: `web/src/components/session/CommandResultOverlay.tsx`
- Modify: `web/src/components/session/SessionView.tsx` (render the overlay)
- Test: `web/src/components/session/__tests__/CommandResultOverlay.test.tsx`

**Interfaces:**
- Produces: `CommandResultOverlay` with props `{ result: { render: 'help' | 'context'; data: unknown } | null; commands: Command[]; onClose: () => void }`.
  - `render: 'help'` → lists `commands` (name, description, kind group).
  - `render: 'context'` → shows `percentage`/`totalTokens`/`maxTokens` from the `data` payload (shape per `get_context_usage()`; read defensively).

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/session/__tests__/CommandResultOverlay.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CommandResultOverlay } from '@/components/session/CommandResultOverlay'
import type { Command } from '@/session/events'

const CMDS: Command[] = [{ name: 'help', description: 'List commands', argumentHint: null, kind: 'client' }]

describe('CommandResultOverlay', () => {
  it('renders the help list', () => {
    render(<CommandResultOverlay result={{ render: 'help', data: null }} commands={CMDS} onClose={vi.fn()} />)
    expect(screen.getByText('help')).toBeInTheDocument()
    expect(screen.getByText('List commands')).toBeInTheDocument()
  })

  it('renders context usage', () => {
    render(<CommandResultOverlay result={{ render: 'context', data: { percentage: 42, totalTokens: 1000, maxTokens: 2000 } }} commands={CMDS} onClose={vi.fn()} />)
    expect(screen.getByText(/42/)).toBeInTheDocument()
  })

  it('renders nothing when result is null', () => {
    const { container } = render(<CommandResultOverlay result={null} commands={CMDS} onClose={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/session/__tests__/CommandResultOverlay.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```tsx
// web/src/components/session/CommandResultOverlay.tsx
import { cn } from '@/lib/utils'
import type { Command } from '@/session/events'

interface Props {
  result: { render: 'help' | 'context'; data: unknown } | null
  commands: Command[]
  onClose: () => void
}

export function CommandResultOverlay({ result, commands, onClose }: Props) {
  if (!result) return null
  return (
    <div
      className="fixed inset-0 z-30 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      onClick={onClose}
    >
      <div
        className={cn('w-full max-w-md max-h-[80vh] overflow-y-auto rounded-lg p-4',
          'bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 shadow-xl')}
        onClick={(e) => e.stopPropagation()}
      >
        {result.render === 'help' ? (
          <>
            <h2 className="mb-2 text-sm font-semibold">Commands</h2>
            <ul className="space-y-1">
              {commands.map((c) => (
                <li key={c.name} className="flex gap-2 text-sm">
                  <span className="font-mono text-gray-900 dark:text-gray-100">/{c.name}</span>
                  <span className="flex-1 truncate text-gray-500 dark:text-gray-400">{c.description}</span>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <>
            <h2 className="mb-2 text-sm font-semibold">Context usage</h2>
            <ContextBody data={result.data} />
          </>
        )}
        <button type="button" onClick={onClose}
          className="mt-3 min-h-[44px] rounded-md border border-gray-300 dark:border-gray-600 px-3 py-1 text-sm">
          Close
        </button>
      </div>
    </div>
  )
}

function ContextBody({ data }: { data: unknown }) {
  const d = (data ?? {}) as { percentage?: number; totalTokens?: number; maxTokens?: number }
  const pct = typeof d.percentage === 'number' ? d.percentage.toFixed(1) : '—'
  return (
    <div className="text-sm text-gray-700 dark:text-gray-300">
      <p>{pct}% of context used</p>
      {typeof d.totalTokens === 'number' && typeof d.maxTokens === 'number' && (
        <p className="text-gray-500 dark:text-gray-400">{d.totalTokens.toLocaleString()} / {d.maxTokens.toLocaleString()} tokens</p>
      )}
    </div>
  )
}
```

- [ ] **Step 4a: Render it in SessionView**

In `web/src/components/session/SessionView.tsx`, render near the root return:
```tsx
      <CommandResultOverlay
        result={runtime.commandResult}
        commands={runtime.commands}
        onClose={runtime.dismissCommandResult}
      />
```
(add the import; use the actual runtime variable name in that file).

- [ ] **Step 4b: Run tests**

Run (in `web/`): `npx vitest run src/components/session/__tests__/CommandResultOverlay.test.tsx && npx tsc --noEmit`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/CommandResultOverlay.tsx web/src/components/session/SessionView.tsx web/src/components/session/__tests__/CommandResultOverlay.test.tsx
git commit -m "feat(commands): /help and /context result overlays"
```

---

## Task 12: Full-suite verification + mobile polish

**Files:**
- Modify: `web/src/components/session/CommandPalette.tsx` (responsive bottom-sheet styling)
- No new test file; this task is the green-gate.

- [ ] **Step 1: Run the full server suite**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/ -q`
Expected: PASS (existing + new). Fix any regression before continuing.

- [ ] **Step 2: Run the full web gate**

Run (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint src`
Expected: all green.

- [ ] **Step 3: Mobile bottom-sheet styling**

In `CommandPalette.tsx`, make the container a bottom sheet on small screens: change the outer `className` to anchor to the viewport bottom under `sm:` breakpoint, e.g. add `max-sm:fixed max-sm:inset-x-0 max-sm:bottom-0 max-sm:rounded-b-none` and keep the `absolute bottom-full` positioning at `sm:` and up. Verify 44px row targets remain.

- [ ] **Step 4: Re-run the web gate**

Run (in `web/`): `npx vitest run && npx tsc --noEmit && npx eslint src`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/CommandPalette.tsx
git commit -m "feat(commands): mobile bottom-sheet palette + full-suite green"
```

---

## Self-Review (completed during planning)

**Spec coverage:**
- Discovery via `get_server_info()` → Tasks 2, 5, 6. ✓
- Server-authoritative dispatch by `kind` → Tasks 2, 4. ✓
- `/help`, `/context` (client) → Tasks 4, 11. ✓
- `/model`, `/clear` (control) → Tasks 3, 4, 8. ✓
- Custom commands & skills + `/compact` (passthrough) → Tasks 1, 4. ✓ (`/compact` gated by Task 1 verdict; if no-op, add to `EXCLUDED`.)
- Palette UX (Tab/Enter, grouping, arg hints, mobile) → Tasks 9, 10, 12. ✓
- 3 WS message types → Tasks 6, 7. ✓
- Error handling (unknown cmd falls through to prompt; dispatch failure → inline notice; catalog fetch failure → empty catalog) → Tasks 5, 6, 10. ✓
- Inline marker for `/clear` and `/model` → Task 8. ✓
- Testing incl. verification-spike-first → Task 1 (sequenced first). ✓

**Type consistency:** `Command` fields (`name/description/argumentHint/kind`) match across `catalog.py` `to_wire()`, `events.ts`, palette, and overlay. `commandResult.render` values (`help|context|notice|clear`) match between dispatcher, WS, client types, and runtime handling. Dispatcher returns `dict | None`; WS sends `commandResult` only on non-None; runtime folds `notice/clear/error` into the reducer and `help/context` into overlay state — consistent.

**Decision gate:** Task 1 must pass (custom-command expansion) before Tasks 9-11 are worth building; if it fails, the design — not just the plan — needs revisiting.
