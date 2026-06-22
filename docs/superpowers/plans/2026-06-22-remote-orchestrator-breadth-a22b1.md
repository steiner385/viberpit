# Session daemon A2.2b-1 — complete RemoteOrchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `RemoteOrchestrator` the 18 session-lifecycle/control methods (+ daemon RPC handlers) and a `RemoteDriver` streaming shim, so the remote facade matches the in-process one — dormant, proven by an integration test, with `ws.py` untouched.

**Architecture:** Extend `server/ipc/orchestrator_rpc.py` (daemon handlers — a data-driven loop for the 15 simple-return methods + 3 explicit quirk handlers) and `server/sessions/remote.py` (18 explicit proxy methods + a `RemoteDriver` whose `.events()` wraps `client.subscribe`). `ensure_driver` returns the `RemoteDriver` so the WS bridge consumes it exactly like the real driver.

**Tech Stack:** Python 3.12, FastAPI/Starlette, the A1 `server/ipc/` transport, pytest + pytest-asyncio.

## Global Constraints

- Tests run via: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest <path> -q -p no:cacheprovider`. pytest-asyncio strict (`@pytest.mark.asyncio`).
- No new dependencies. Do NOT modify `server/app.py`, `server/bridge/ws.py`, or `server/sessions/orchestrator.py`.
- The 4 edge-integration helpers (`usage`, `pending_permissions`, `subscribe_events`, `unsubscribe_events`) are A2.2b-2 — do NOT implement them here.
- RPC wire shapes: simple methods → `{"result": <json-safe value>}`; `unarchive` → `{"session": rec.to_dict() | None}`; `adopt_session` → `{"record": rec.to_dict(), "metadata": {...}}`; `transcript_events` → `{"events": [e.to_wire() for e in ...]}`.
- Method signatures (exact, from the orchestrator): `archive_session(session_id)→None`, `unarchive(session_id)→SessionRecord|None`, `permanent_delete(session_id)→bool`, `adopt_session(claude_session_id, identity)→(SessionRecord, dict)` (its `killer=` param is NOT sent over RPC), `rename_session(session_id, name)→None`, `list_archived()→list[dict]`, `transcript_events(session_id)→list[Event]`, `transcript_path(session_id)→str|None`, `pending_questions(session_id)→list[dict]`, `pop_pending_initial_prompt(session_id)→str|None`, `pop_pending_initial_attachment_ids(session_id)→list[str]|None`, `resolve_permission(session_id, perm_id, decision, identity)→bool`, `answer_question(session_id, question_id, selected, identity, mode="option")→bool`, `command_catalog(session_id)→list[dict]`, `run_command(session_id, name, args)→dict|None`, `cancel_queued(session_id, index)→bool`, `interrupt(session_id)→None`, `set_permission_mode(session_id, mode)→None`.
- The full `server/tests` suite hangs in a firewalled sandbox; run the named files. CI runs everything.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: `RemoteDriver` shim + `ensure_driver` returns it

**Files:**
- Modify: `server/sessions/remote.py`
- Test: `server/tests/test_remote_driver.py`

**Interfaces:**
- Consumes: `DaemonClient.subscribe` (A1), the existing `ensure_driver` RPC (returns `{"found": bool}`, starts the pump).
- Produces: `class RemoteDriver(client, session_id)` with `async def events(self)` yielding wire dicts from `client.subscribe(session_id)`; `RemoteOrchestrator.ensure_driver(session_id, identity="system") -> RemoteDriver | None`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_remote_driver.py`:

```python
import asyncio
import pytest
from server.sessions.remote import RemoteDriver, RemoteOrchestrator


class _FakeClient:
    """Minimal client: subscribe() yields preset frames; call() returns found."""
    def __init__(self, frames, found=True):
        self._frames = frames
        self._found = found
        self.calls = []

    async def call(self, method, **params):
        self.calls.append((method, params))
        return {"found": self._found}

    async def _gen(self):
        for f in self._frames:
            yield f

    def subscribe(self, session_id):
        return self._gen()


@pytest.mark.asyncio
async def test_remote_driver_events_wrap_subscribe():
    c = _FakeClient([{"type": "textDelta", "text": "hi"}, {"type": "turnEnd"}])
    d = RemoteDriver(c, "s1")
    got = [e async for e in d.events()]
    assert got == [{"type": "textDelta", "text": "hi"}, {"type": "turnEnd"}]


@pytest.mark.asyncio
async def test_ensure_driver_returns_remotedriver_when_found():
    c = _FakeClient([{"type": "turnEnd"}], found=True)
    ro = RemoteOrchestrator(c)
    driver = await ro.ensure_driver("s1")
    assert isinstance(driver, RemoteDriver)
    assert c.calls == [("ensure_driver", {"session_id": "s1", "identity": "system"})]
    first = await asyncio.wait_for(driver.events().__anext__(), 2)
    assert first == {"type": "turnEnd"}


@pytest.mark.asyncio
async def test_ensure_driver_returns_none_when_not_found():
    c = _FakeClient([], found=False)
    ro = RemoteOrchestrator(c)
    assert await ro.ensure_driver("missing") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_driver.py -q -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'RemoteDriver'`.

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/remote.py`, add the `RemoteDriver` class (top-level, near `RemoteOrchestrator`):

```python
class RemoteDriver:
    """Driver-shaped shim so ws.py can consume a remote session like a local one.

    ws.py only needs ``.events()`` (an async generator of wire dicts) and a
    non-None object; this wraps the daemon's per-session event stream.
    """
    def __init__(self, client, session_id: str):
        self._client = client
        self._session_id = session_id

    async def events(self):
        async for wire in self._client.subscribe(self._session_id):
            yield wire
```

Replace `RemoteOrchestrator.ensure_driver` (currently returns the `found` bool) with:

```python
    async def ensure_driver(self, session_id: str, identity: str = "system") -> "RemoteDriver | None":
        r = await self._client.call("ensure_driver", session_id=session_id, identity=identity)
        return RemoteDriver(self._client, session_id) if r["found"] else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_driver.py -q -p no:cacheprovider`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/remote.py server/tests/test_remote_driver.py
git commit -m "feat(sessions): RemoteDriver shim; ensure_driver returns it

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Daemon RPC handlers for the 18 methods

**Files:**
- Modify: `server/ipc/orchestrator_rpc.py`
- Test: `server/tests/test_orchestrator_rpc_breadth.py`

**Interfaces:**
- Consumes: `register_rpc` (A1); a real/mock `SessionOrchestrator`.
- Produces: RPC handlers registered for the 18 methods with the wire shapes from Global Constraints.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_orchestrator_rpc_breadth.py`:

```python
from unittest import mock
from fastapi.testclient import TestClient
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


def _rec(sid="s1"):
    return SessionRecord(id=sid, name="S", account="a", claude_session_id="cs",
                         cols=80, rows=24, cwd="/tmp")


def _app():
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    register_orchestrator_rpc(app, orch)
    return app, orch


def test_simple_returns_wrapped_in_result():
    app, orch = _app()
    orch.resolve_permission = mock.MagicMock(return_value=True)        # sync, bool
    orch.permanent_delete = mock.AsyncMock(return_value=False)         # async, bool
    orch.archive_session = mock.AsyncMock(return_value=None)           # async, None
    orch.transcript_path = mock.MagicMock(return_value="/p/t.jsonl")   # sync, str
    with TestClient(app) as c:
        assert c.post("/rpc/resolve_permission", json={"session_id": "s1", "perm_id": "p", "decision": "allow", "identity": "me"}).json()["result"] is True
        assert c.post("/rpc/permanent_delete", json={"session_id": "s1"}).json()["result"] is False
        assert c.post("/rpc/archive_session", json={"session_id": "s1"}).json()["result"] is None
        assert c.post("/rpc/transcript_path", json={"session_id": "s1"}).json()["result"] == "/p/t.jsonl"
    orch.resolve_permission.assert_called_once_with(session_id="s1", perm_id="p", decision="allow", identity="me")


def test_unarchive_returns_session_shape():
    app, orch = _app()
    orch.unarchive = mock.AsyncMock(return_value=_rec("u1"))
    with TestClient(app) as c:
        r = c.post("/rpc/unarchive", json={"session_id": "u1"}).json()
    assert r["session"]["id"] == "u1"


def test_unarchive_none():
    app, orch = _app()
    orch.unarchive = mock.AsyncMock(return_value=None)
    with TestClient(app) as c:
        assert c.post("/rpc/unarchive", json={"session_id": "x"}).json()["session"] is None


def test_adopt_session_unpacks_tuple():
    app, orch = _app()
    orch.adopt_session = mock.AsyncMock(return_value=(_rec("a1"), {"resumed": True, "terminatedPid": None}))
    with TestClient(app) as c:
        r = c.post("/rpc/adopt_session", json={"claude_session_id": "cs", "identity": "me"}).json()
    assert r["record"]["id"] == "a1"
    assert r["metadata"] == {"resumed": True, "terminatedPid": None}


def test_transcript_events_to_wire():
    app, orch = _app()

    class _Ev:
        def to_wire(self): return {"type": "textDelta", "text": "x"}

    orch.transcript_events = mock.AsyncMock(return_value=[_Ev(), _Ev()])
    with TestClient(app) as c:
        r = c.post("/rpc/transcript_events", json={"session_id": "s1"}).json()
    assert r["events"] == [{"type": "textDelta", "text": "x"}, {"type": "textDelta", "text": "x"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_rpc_breadth.py -q -p no:cacheprovider`
Expected: FAIL — `UnknownMethod` (404) for the new methods.

- [ ] **Step 3: Write minimal implementation**

In `server/ipc/orchestrator_rpc.py`, inside `register_orchestrator_rpc` (after the A2.1 registrations), add a data-driven registration for the 15 simple-return methods and 3 explicit quirk handlers. Reuse the `_maybe_await` pattern (define a tiny local if not importing the facade's):

```python
    import inspect

    async def _maybe_await(r):
        return await r if inspect.isawaitable(r) else r

    # 15 methods whose return value is already JSON-safe (bool / None / str / list-of-dicts).
    _SIMPLE = [
        "archive_session", "permanent_delete", "rename_session", "interrupt",
        "set_permission_mode", "cancel_queued", "resolve_permission", "answer_question",
        "transcript_path", "pending_questions", "pop_pending_initial_prompt",
        "pop_pending_initial_attachment_ids", "command_catalog", "run_command", "list_archived",
    ]

    def _make_simple(method_name):
        async def handler(params: dict) -> dict:
            return {"result": await _maybe_await(getattr(orch, method_name)(**params))}
        return handler

    for _m in _SIMPLE:
        register_rpc(app, _m, _make_simple(_m))

    async def unarchive(params: dict) -> dict:
        rec = await orch.unarchive(**params)
        return {"session": rec.to_dict() if rec is not None else None}

    async def adopt_session(params: dict) -> dict:
        rec, metadata = await orch.adopt_session(**params)
        return {"record": rec.to_dict(), "metadata": metadata}

    async def transcript_events(params: dict) -> dict:
        events = await orch.transcript_events(**params)
        return {"events": [e.to_wire() for e in events]}

    register_rpc(app, "unarchive", unarchive)
    register_rpc(app, "adopt_session", adopt_session)
    register_rpc(app, "transcript_events", transcript_events)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_rpc_breadth.py -q -p no:cacheprovider`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add server/ipc/orchestrator_rpc.py server/tests/test_orchestrator_rpc_breadth.py
git commit -m "feat(ipc): daemon RPC handlers for the 18 session-lifecycle methods

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: RemoteOrchestrator proxy methods + end-to-end integration

**Files:**
- Modify: `server/sessions/remote.py`
- Test: `server/tests/test_remote_orchestrator_breadth.py`

**Interfaces:**
- Consumes: the daemon handlers (Task 2); `DaemonClient` (A1); `serve_daemon_uds` (`server/tests/ipc_harness.py`); `SessionRecord.from_dict`.
- Produces: 18 async proxy methods on `RemoteOrchestrator` matching the orchestrator signatures, deserializing per the wire shapes.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_remote_orchestrator_breadth.py`:

```python
from unittest import mock
import pytest
from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.sessions.types import SessionRecord
from server.tests.ipc_harness import serve_daemon_uds


def _rec(sid="s1"):
    return SessionRecord(id=sid, name="S", account="a", claude_session_id="cs",
                         cols=80, rows=24, cwd="/tmp")


def _build():
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    register_orchestrator_rpc(app, orch)
    return app, orch


@pytest.mark.asyncio
async def test_simple_and_quirk_methods_round_trip():
    app, orch = _build()
    orch.permanent_delete = mock.AsyncMock(return_value=True)
    orch.resolve_permission = mock.MagicMock(return_value=False)
    orch.answer_question = mock.MagicMock(return_value=True)
    orch.rename_session = mock.AsyncMock(return_value=None)
    orch.list_archived = mock.AsyncMock(return_value=[{"id": "old"}])
    orch.unarchive = mock.AsyncMock(return_value=_rec("u1"))
    orch.adopt_session = mock.AsyncMock(return_value=(_rec("a1"), {"resumed": True, "terminatedPid": 7}))

    class _Ev:
        def to_wire(self): return {"type": "tick"}
    orch.transcript_events = mock.AsyncMock(return_value=[_Ev()])

    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            assert await ro.permanent_delete("s1") is True
            assert await ro.resolve_permission("s1", "p", "deny", "me") is False
            assert await ro.answer_question("s1", "q", ["opt"], "me") is True
            assert await ro.rename_session("s1", "New") is None
            assert await ro.list_archived() == [{"id": "old"}]
            un = await ro.unarchive("u1")
            assert un.id == "u1"
            rec, meta = await ro.adopt_session("cs", "me")
            assert rec.id == "a1" and meta == {"resumed": True, "terminatedPid": 7}
            assert await ro.transcript_events("s1") == [{"type": "tick"}]
        finally:
            await ro.aclose()
    orch.resolve_permission.assert_called_once_with(session_id="s1", perm_id="p", decision="deny", identity="me")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_orchestrator_breadth.py -q -p no:cacheprovider`
Expected: FAIL — `AttributeError: 'RemoteOrchestrator' object has no attribute 'permanent_delete'`.

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/remote.py`, add the 18 proxy methods to `RemoteOrchestrator` (signatures mirror the orchestrator; simple ones return `["result"]`):

```python
    async def archive_session(self, session_id):
        return await self._client.call("archive_session", session_id=session_id)

    async def permanent_delete(self, session_id):
        return await self._client.call("permanent_delete", session_id=session_id)

    async def rename_session(self, session_id, name):
        return await self._client.call("rename_session", session_id=session_id, name=name)

    async def interrupt(self, session_id):
        return await self._client.call("interrupt", session_id=session_id)

    async def set_permission_mode(self, session_id, mode):
        return await self._client.call("set_permission_mode", session_id=session_id, mode=mode)

    async def cancel_queued(self, session_id, index):
        return await self._client.call("cancel_queued", session_id=session_id, index=index)

    async def resolve_permission(self, session_id, perm_id, decision, identity):
        return await self._client.call("resolve_permission", session_id=session_id, perm_id=perm_id,
                                        decision=decision, identity=identity)

    async def answer_question(self, session_id, question_id, selected, identity, mode="option"):
        return await self._client.call("answer_question", session_id=session_id, question_id=question_id,
                                        selected=selected, identity=identity, mode=mode)

    async def transcript_path(self, session_id):
        return await self._client.call("transcript_path", session_id=session_id)

    async def pending_questions(self, session_id):
        return await self._client.call("pending_questions", session_id=session_id)

    async def pop_pending_initial_prompt(self, session_id):
        return await self._client.call("pop_pending_initial_prompt", session_id=session_id)

    async def pop_pending_initial_attachment_ids(self, session_id):
        return await self._client.call("pop_pending_initial_attachment_ids", session_id=session_id)

    async def command_catalog(self, session_id):
        return await self._client.call("command_catalog", session_id=session_id)

    async def run_command(self, session_id, name, args):
        return await self._client.call("run_command", session_id=session_id, name=name, args=args)

    async def list_archived(self):
        return await self._client.call("list_archived")

    async def unarchive(self, session_id):
        r = await self._client.call("unarchive", session_id=session_id)
        return SessionRecord.from_dict(r["session"]) if r["session"] is not None else None

    async def adopt_session(self, claude_session_id, identity):
        r = await self._client.call("adopt_session", claude_session_id=claude_session_id, identity=identity)
        return SessionRecord.from_dict(r["record"]), r["metadata"]

    async def transcript_events(self, session_id):
        return await self._client.call("transcript_events", session_id=session_id))["events"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_orchestrator_breadth.py -q -p no:cacheprovider`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/remote.py server/tests/test_remote_orchestrator_breadth.py
git commit -m "feat(sessions): RemoteOrchestrator proxy methods for the 18 lifecycle methods

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Full verification + PR

**Files:** none (verification only).

- [ ] **Step 1: Run the A2.2b-1 tests + the prior remote/ipc suites (no regression)**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_driver.py server/tests/test_orchestrator_rpc_breadth.py server/tests/test_remote_orchestrator_breadth.py server/tests/test_remote_orchestrator.py server/tests/test_orchestrator_rpc.py server/tests/test_orchestrator_rpc_events.py server/tests/test_ipc_*.py -q -p no:cacheprovider`
Expected: all pass, output pristine.

- [ ] **Step 2: Lint + type-check**

Run:
```bash
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/ruff check server/sessions/remote.py server/ipc/orchestrator_rpc.py server/tests/test_remote_driver.py server/tests/test_orchestrator_rpc_breadth.py server/tests/test_remote_orchestrator_breadth.py
PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/mypy server/ --ignore-missing-imports
```
Expected: ruff clean; mypy success.

- [ ] **Step 3: Confirm A2.2b-2 surface is NOT implemented here**

Run: `grep -nE "def (usage|pending_permissions|subscribe_events|unsubscribe_events)" server/sessions/remote.py`
Expected: no output (those 4 are A2.2b-2).

- [ ] **Step 4: Push and open PR**

```bash
git push -u origin HEAD
gh pr create --base master --title "feat(sessions): complete RemoteOrchestrator + RemoteDriver shim (A2.2b-1)" --body "<summary: 18 lifecycle methods over RPC + RemoteDriver streaming shim (ws.py untouched); dormant, proven by an integration test. 4 edge-helpers deferred to A2.2b-2. Link the spec.>"
```

- [ ] **Step 5: Wait for CI green, then merge**

master is branch-protected (required checks + up-to-date). If BEHIND, rebase onto `origin/master` and force-push (`--force-with-lease`). Run `gh pr checks <num>` until `lint-and-test` + `frontend` pass, then `gh pr merge <num> --merge` (do NOT chain `--delete-branch` unconditionally — only delete after a confirmed merge).

---

## Notes for the implementer

- `adopt_session`'s real signature has a `killer=os.kill` test seam — the proxy sends only `claude_session_id` + `identity`; the handler calls `orch.adopt_session(**params)` so `killer` defaults.
- Daemon handlers use `getattr(orch, name)(**params)` + `_maybe_await`, so they're correct whether the orchestrator method is sync or async (several are sync: `resolve_permission`, `answer_question`, `transcript_path`, `pending_questions`, `pop_*`).
- Do NOT touch `server/app.py`, `server/bridge/ws.py`, or `server/sessions/orchestrator.py`. Do NOT implement `usage`/`pending_permissions`/`subscribe_events`/`unsubscribe_events` (A2.2b-2).
- master is branch-protected now — see Task 4 Step 5; never chain `--delete-branch` with `|| true`.
- The full `server/tests` suite hangs in this sandbox; run the named files. CI runs everything.
