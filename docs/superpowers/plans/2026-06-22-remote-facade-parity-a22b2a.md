# Session daemon A2.2b-2a — full remote facade parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete `RemoteOrchestrator`/`RemoteDriver` to full parity with the in-process facade — the 4 edge-helpers + `active_session_count` + the ws.py-compatible driver enrichment — dormant, proven by integration tests.

**Architecture:** Extend `server/ipc/orchestrator_rpc.py` (daemon handlers + a global-bus pump), `server/sessions/remote.py` (`RemoteDriver` enrichment + 5 proxy methods), `server/sessions/facade.py` (`active_session_count`), and add `Usage.to_dict/from_dict` to `src/ccswitch/oauth.py`. No `app.py`/`ws.py` changes.

**Tech Stack:** Python 3.12, FastAPI/Starlette, the A1 `server/ipc/` transport, pytest + pytest-asyncio.

## Global Constraints

- Tests run via: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest <path> -q -p no:cacheprovider`. pytest-asyncio strict (`@pytest.mark.asyncio`).
- No new dependencies. Do NOT modify `server/app.py`, `server/bridge/ws.py`, or `server/sessions/orchestrator.py`.
- `Usage` (`src/ccswitch/oauth.py`) fields: `session_pct: float`, `session_resets_at: datetime|None`, `weekly_pct: float`, `weekly_resets_at: datetime|None`, `extra_usage_enabled: bool`.
- The daemon `ensure_driver` RPC return shape changes from `{"found": bool}` to `{"found": True, "status": <str>, "queued": <list[str]>}` when found, `{"found": False}` when not — existing tests asserting the old shape get updated in Task 2.
- RPC wire shapes: `usage`→`{"usage": u.to_dict()|None}`; `pending_permissions`/`active_session_count`→`{"result": <value>}` (raw, read directly by the proxy — the A1 route wraps + `DaemonClient.call` unwraps).
- The full `server/tests` suite hangs in a firewalled sandbox; run the named files. CI runs everything.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: `Usage.to_dict` / `from_dict`

**Files:**
- Modify: `src/ccswitch/oauth.py` (the `Usage` dataclass)
- Test: `server/tests/test_usage_serde.py`

**Interfaces:**
- Produces: `Usage.to_dict(self) -> dict` and `Usage.from_dict(cls, d) -> Usage`; the two `*_resets_at` datetimes ISO-encode (or `None`).

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_usage_serde.py`:

```python
from datetime import datetime, timezone
from ccswitch.oauth import Usage


def test_usage_round_trip_with_datetimes():
    u = Usage(session_pct=42.0,
              session_resets_at=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
              weekly_pct=18.0, weekly_resets_at=None, extra_usage_enabled=True)
    d = u.to_dict()
    assert d["sessionResetsAt"] == "2026-06-22T10:00:00+00:00"
    assert d["weeklyResetsAt"] is None
    assert Usage.from_dict(d) == u


def test_usage_to_dict_is_json_safe():
    import json
    json.dumps(Usage(1.0, None, 2.0, None, False).to_dict())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_usage_serde.py -q -p no:cacheprovider`
Expected: FAIL — `AttributeError: 'Usage' object has no attribute 'to_dict'`.

- [ ] **Step 3: Write minimal implementation**

In `src/ccswitch/oauth.py`, add these two methods to the `Usage` dataclass (after its fields):

```python
    def to_dict(self) -> dict:
        return {
            "sessionPct": self.session_pct,
            "sessionResetsAt": self.session_resets_at.isoformat() if self.session_resets_at else None,
            "weeklyPct": self.weekly_pct,
            "weeklyResetsAt": self.weekly_resets_at.isoformat() if self.weekly_resets_at else None,
            "extraUsageEnabled": self.extra_usage_enabled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Usage":
        from datetime import datetime
        def _p(s):
            return datetime.fromisoformat(s) if s else None
        return cls(
            session_pct=d["sessionPct"],
            session_resets_at=_p(d["sessionResetsAt"]),
            weekly_pct=d["weeklyPct"],
            weekly_resets_at=_p(d["weeklyResetsAt"]),
            extra_usage_enabled=d["extraUsageEnabled"],
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_usage_serde.py -q -p no:cacheprovider`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/oauth.py server/tests/test_usage_serde.py
git commit -m "feat(oauth): Usage to_dict/from_dict for IPC serialization

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: RemoteDriver enrichment (status/queued snapshot + `_WireEvent`)

**Files:**
- Modify: `server/ipc/orchestrator_rpc.py` (the `ensure_driver` handler)
- Modify: `server/sessions/remote.py` (`RemoteDriver`, `ensure_driver`)
- Modify: `server/tests/test_orchestrator_rpc_events.py`, `server/tests/test_remote_orchestrator.py`, `server/tests/test_remote_driver.py` (update for the new shape)
- Test: `server/tests/test_remote_driver.py` (extend)

**Interfaces:**
- Produces: `ensure_driver` RPC returns `{"found": True, "status": str, "queued": list[str]}` (found) / `{"found": False}`; `RemoteDriver(client, session_id, status=None, queued=None)` with sync `current_status()->str` and `queued()->list`, and `events()` yielding `_WireEvent` (`.to_wire()->dict`); `RemoteOrchestrator.ensure_driver` builds the enriched `RemoteDriver`.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_remote_driver.py`:

```python
@pytest.mark.asyncio
async def test_remote_driver_snapshot_and_wire_events():
    from server.sessions.remote import RemoteDriver

    class _Client:
        async def call(self, method, **params):
            return {"found": True, "status": "in_progress", "queued": ["q1", "q2"]}
        async def _gen(self):
            yield {"type": "textDelta", "text": "hi"}
        def subscribe(self, session_id):
            return self._gen()

    ro = RemoteOrchestrator(_Client())
    d = await ro.ensure_driver("s1")
    assert d.current_status() == "in_progress"
    assert d.queued() == ["q1", "q2"]
    ev = await d.events().__anext__()
    assert ev.to_wire() == {"type": "textDelta", "text": "hi"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_driver.py::test_remote_driver_snapshot_and_wire_events -q -p no:cacheprovider`
Expected: FAIL — `current_status` missing / `events()` yields a dict (no `.to_wire`).

- [ ] **Step 3: Write minimal implementation**

In `server/ipc/orchestrator_rpc.py`, change the `ensure_driver` handler's return:

```python
    async def ensure_driver(params: dict) -> dict:
        session_id = params["session_id"]
        driver = await orch.ensure_driver(session_id, params.get("identity", "system"))
        if driver is None:
            return {"found": False}
        if session_id not in app.state.pumped:
            app.state.pumped.add(session_id)
            asyncio.create_task(_pump(session_id, driver), name=f"pump-{session_id}")
        return {"found": True, "status": driver.current_status(), "queued": driver.queued()}
```

In `server/sessions/remote.py`, replace `RemoteDriver` and `ensure_driver`:

```python
class _WireEvent:
    """Wraps an already-serialized event dict so ws.py's e.to_wire() works."""
    def __init__(self, wire: dict):
        self._wire = wire
    def to_wire(self) -> dict:
        return self._wire


class RemoteDriver:
    def __init__(self, client, session_id: str, status="idle", queued=None):
        self._client = client
        self._session_id = session_id
        self._status = status
        self._queued = queued or []

    def current_status(self) -> str:
        return self._status

    def queued(self) -> list:
        return self._queued

    async def events(self):
        async for wire in self._client.subscribe(self._session_id):
            yield _WireEvent(wire)
```

And `RemoteOrchestrator.ensure_driver`:

```python
    async def ensure_driver(self, session_id: str, identity: str = "system") -> "RemoteDriver | None":
        r = await self._client.call("ensure_driver", session_id=session_id, identity=identity)
        if not r["found"]:
            return None
        return RemoteDriver(self._client, session_id, status=r["status"], queued=r["queued"])
```

- [ ] **Step 4: Update the existing tests broken by the shape change**

In `server/tests/test_orchestrator_rpc_events.py`: the `ensure_driver` happy-path test mocks `orch.ensure_driver` to return a fake driver — give that fake `current_status`/`queued` and update the assertion. Change the fake driver class to add:

```python
        def current_status(self): return "idle"
        def queued(self): return []
```

and change `assert found["result"] == {"found": True}` → `assert found["result"]["found"] is True`. (The not-found test `{"found": False}` is unchanged.)

In `server/tests/test_remote_orchestrator.py`: the `_FakeDriver` used by `test_ensure_driver_and_stream_over_uds` must gain `current_status`/`queued`:

```python
    def current_status(self): return "idle"
    def queued(self): return []
```

(the `isinstance(found, RemoteDriver)` assertion stays valid).

In `server/tests/test_remote_driver.py`: the existing `test_remote_driver_events_wrap_subscribe` and `test_ensure_driver_returns_remotedriver_when_found` now see `_WireEvent`s and a `{found}` response without status/queued. Update:
- `test_remote_driver_events_wrap_subscribe`: change `got = [e async for e in d.events()]` assertion to `assert [e.to_wire() for e in got] == [...]` (i.e. map `.to_wire()` before comparing).
- `test_ensure_driver_returns_remotedriver_when_found`: the `_FakeClient.call` returns `{"found": True}` with no status/queued → `RemoteOrchestrator.ensure_driver` does `r["status"]` → KeyError. Update `_FakeClient.call` to return `{"found": True, "status": "idle", "queued": []}`. And `first = await driver.events().__anext__()` is now a `_WireEvent` → assert `first.to_wire() == {"type": "turnEnd"}`.

- [ ] **Step 5: Run all affected tests to verify they pass**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_driver.py server/tests/test_orchestrator_rpc_events.py server/tests/test_remote_orchestrator.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add server/ipc/orchestrator_rpc.py server/sessions/remote.py server/tests/test_remote_driver.py server/tests/test_orchestrator_rpc_events.py server/tests/test_remote_orchestrator.py
git commit -m "feat(sessions): enrich RemoteDriver (status/queued snapshot + _WireEvent)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `usage` + `pending_permissions` + `active_session_count`

**Files:**
- Modify: `server/ipc/orchestrator_rpc.py` (3 handlers)
- Modify: `server/sessions/remote.py` (3 proxy methods)
- Modify: `server/sessions/facade.py` (`active_session_count`)
- Test: `server/tests/test_remote_facade_helpers.py`

**Interfaces:**
- Consumes: `Usage.to_dict/from_dict` (Task 1).
- Produces: daemon handlers `usage`→`{"usage": u.to_dict()|None}`, `pending_permissions`→raw `list[dict]`, `active_session_count`→raw `int`; `RemoteOrchestrator.usage(name)->Usage|None`, `.pending_permissions(session_id)->list[dict]`, `.active_session_count()->int`; `InProcessOrchestratorAdapter.active_session_count()`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_remote_facade_helpers.py`:

```python
from datetime import datetime, timezone
from unittest import mock
import pytest
from ccswitch.oauth import Usage
from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.tests.ipc_harness import serve_daemon_uds


def _build():
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    register_orchestrator_rpc(app, orch)
    return app, orch


@pytest.mark.asyncio
async def test_usage_round_trips_a_usage_object():
    app, orch = _build()
    u = Usage(50.0, datetime(2026, 6, 22, tzinfo=timezone.utc), 10.0, None, False)
    orch.poller.get = mock.AsyncMock(return_value=u)
    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            got = await ro.usage("acct")
        finally:
            await ro.aclose()
    assert got == u


@pytest.mark.asyncio
async def test_pending_permissions_and_active_count():
    app, orch = _build()
    orch._registry.pending = mock.MagicMock(return_value=[{"permId": "p1", "tool": "Bash"}])
    orch.active_session_count = mock.MagicMock(return_value=3)
    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            assert await ro.pending_permissions("s1") == [{"permId": "p1", "tool": "Bash"}]
            assert await ro.active_session_count() == 3
        finally:
            await ro.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_facade_helpers.py -q -p no:cacheprovider`
Expected: FAIL — `UnknownMethod` for `usage` / `RemoteOrchestrator` has no `usage`.

- [ ] **Step 3: Write minimal implementation**

In `server/ipc/orchestrator_rpc.py`, register three more handlers (alongside the others):

```python
    async def usage(params: dict) -> dict:
        u = await orch.poller.get(params["name"])
        return {"usage": u.to_dict() if u is not None else None}

    async def pending_permissions(params: dict):
        return orch._registry.pending(params["session_id"])

    async def active_session_count(params: dict):
        return orch.active_session_count()

    register_rpc(app, "usage", usage)
    register_rpc(app, "pending_permissions", pending_permissions)
    register_rpc(app, "active_session_count", active_session_count)
```

In `server/sessions/remote.py`, add three proxy methods to `RemoteOrchestrator` (import `Usage` at the top: `from ccswitch.oauth import Usage`):

```python
    async def usage(self, name):
        r = await self._client.call("usage", name=name)
        return Usage.from_dict(r["usage"]) if r["usage"] is not None else None

    async def pending_permissions(self, session_id):
        return await self._client.call("pending_permissions", session_id=session_id)

    async def active_session_count(self):
        return await self._client.call("active_session_count")
```

In `server/sessions/facade.py`, add to `InProcessOrchestratorAdapter`:

```python
    async def active_session_count(self):
        return await _maybe_await(self._orch.active_session_count())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_facade_helpers.py -q -p no:cacheprovider`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add server/ipc/orchestrator_rpc.py server/sessions/remote.py server/sessions/facade.py server/tests/test_remote_facade_helpers.py
git commit -m "feat(sessions): remote usage/pending_permissions/active_session_count

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `subscribe_events` / `unsubscribe_events` (global lifecycle bus)

**Files:**
- Modify: `server/ipc/orchestrator_rpc.py` (global pump)
- Modify: `server/sessions/remote.py` (`subscribe_events`/`unsubscribe_events`)
- Test: `server/tests/test_remote_global_bus.py`

**Interfaces:**
- Consumes: A1 `publish`/`subscribe`; `orch.subscribe_events()` (returns an `asyncio.Queue[dict]`).
- Produces: the daemon pumps `orch.subscribe_events()` → `publish("_global", ev)`; `RemoteOrchestrator.subscribe_events() -> asyncio.Queue` filled from `client.subscribe("_global")`; `unsubscribe_events(q)` stops the feeder.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_remote_global_bus.py`:

```python
import asyncio
from unittest import mock
import pytest
from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.tests.ipc_harness import serve_daemon_uds


@pytest.mark.asyncio
async def test_global_events_reach_remote_queue():
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    register_orchestrator_rpc(app, orch)
    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            q = await ro.subscribe_events()
            await asyncio.sleep(0.1)  # let the feeder subscribe to "_global"
            orch._publish_event({"type": "rotated", "sessionId": "s1"})
            ev = await asyncio.wait_for(q.get(), 2)
            assert ev == {"type": "rotated", "sessionId": "s1"}
            await ro.unsubscribe_events(q)
        finally:
            await ro.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_global_bus.py -q -p no:cacheprovider`
Expected: FAIL — `RemoteOrchestrator` has no `subscribe_events`.

- [ ] **Step 3: Write minimal implementation**

In `server/ipc/orchestrator_rpc.py`, inside `register_orchestrator_rpc` (after `app.state.pumped = set()`), define the global pump and start it **only if a loop is already running**. Two facts drive this: (a) the sync TestClient tests call `register_orchestrator_rpc` OUTSIDE any event loop, so a bare `asyncio.create_task(...)` would raise `RuntimeError: no running event loop`; (b) the test harness `serve_daemon_uds` runs uvicorn with `lifespan="off"`, so an `@app.on_event("startup")` hook would never fire. The async remote tests (the only ones that use the global bus) call `register_orchestrator_rpc` from inside the running test loop, and `serve_daemon_uds` runs uvicorn in that same loop — so starting the pump on the running loop here works for them, and is correctly skipped for the sync tests that don't need it:

```python
    async def _global_pump():
        q = orch.subscribe_events()
        while True:
            ev = await q.get()
            await app.state.publish("_global", ev)

    try:
        loop = asyncio.get_running_loop()
        app.state.global_pump = loop.create_task(_global_pump(), name="global-pump")
    except RuntimeError:
        app.state.global_pump = None   # no loop at register time (sync TestClient tests don't use the bus)
```

In `server/sessions/remote.py`, add to `RemoteOrchestrator.__init__` a feeder registry, and the two methods. In `__init__`, add `self._feeders = {}`. Then:

```python
    async def subscribe_events(self):
        q: asyncio.Queue = asyncio.Queue()

        async def _feed():
            async for ev in self._client.subscribe("_global"):
                q.put_nowait(ev)

        self._feeders[id(q)] = asyncio.create_task(_feed(), name="global-feeder")
        return q

    async def unsubscribe_events(self, q):
        task = self._feeders.pop(id(q), None)
        if task is not None:
            task.cancel()
```

Add `import asyncio` at the top of `remote.py` if not present. Update `aclose()` to also cancel any remaining feeders:

```python
    async def aclose(self):
        for task in list(self._feeders.values()):
            task.cancel()
        self._feeders.clear()
        await self._client.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_global_bus.py -q -p no:cacheprovider`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add server/ipc/orchestrator_rpc.py server/sessions/remote.py server/tests/test_remote_global_bus.py
git commit -m "feat(sessions): remote subscribe_events via daemon global-bus pump

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Full verification + PR

**Files:** none (verification only).

- [ ] **Step 1: Run the A2.2b-2a + prior remote/ipc suites**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_usage_serde.py server/tests/test_remote_driver.py server/tests/test_remote_facade_helpers.py server/tests/test_remote_global_bus.py server/tests/test_remote_orchestrator.py server/tests/test_remote_orchestrator_breadth.py server/tests/test_orchestrator_rpc.py server/tests/test_orchestrator_rpc_breadth.py server/tests/test_orchestrator_rpc_events.py server/tests/test_orchestrator_facade.py server/tests/test_ipc_*.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 2: Lint + type-check**

Run:
```bash
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/ruff check src/ccswitch/oauth.py server/sessions/remote.py server/sessions/facade.py server/ipc/orchestrator_rpc.py server/tests/test_usage_serde.py server/tests/test_remote_facade_helpers.py server/tests/test_remote_global_bus.py
PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/mypy server/ --ignore-missing-imports
```
Expected: ruff clean; mypy success.

- [ ] **Step 3: Confirm no app.py/ws.py/orchestrator change**

Run: `git diff --name-only origin/master HEAD | grep -E "server/app.py|server/bridge/ws.py|server/sessions/orchestrator.py"`
Expected: no output.

- [ ] **Step 4: Push and open PR**

```bash
git push -u origin HEAD
gh pr create --base master --title "feat(sessions): full remote facade parity (A2.2b-2a)" --body "<summary: 4 edge-helpers + active_session_count + RemoteDriver enrichment (status/queued snapshot + _WireEvent → ws.py compatible) + Usage serde; dormant, integration-tested. A2.2b-2b does the flag/cutover. Link the spec.>"
```

- [ ] **Step 5: Wait for CI green, then merge**

master is branch-protected (required checks + up-to-date). If BEHIND, rebase onto `origin/master` and force-push (`--force-with-lease`). Run `gh pr checks <num>` until `lint-and-test` + `frontend` pass, then `gh pr merge <num> --merge --auto` (auto-merge; do NOT chain `--delete-branch` unconditionally — `--auto` deletes only on a real merge).

---

## Notes for the implementer

- The A1 daemon route wraps every handler return in `{"result": …}` and `DaemonClient.call` unwraps it. So `pending_permissions`/`active_session_count` handlers return the RAW value and their proxies read `call()` directly; `usage`/`ensure_driver` handlers return typed dicts (`{"usage"}`/`{"found",...}`) and their proxies read the typed key.
- The global pump and per-queue feeders are background tasks; `aclose()`/`unsubscribe_events` cancel them.
- Do NOT touch `server/app.py`, `server/bridge/ws.py`, `server/sessions/orchestrator.py`. The flag/lifespan/healthz cutover is A2.2b-2b.
- master is branch-protected — see Task 5 Step 5; never chain `--delete-branch` with `|| true`.
- The full `server/tests` suite hangs in this sandbox; run the named files. CI runs everything.
