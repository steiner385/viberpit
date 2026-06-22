# Session daemon A2.1 — RemoteOrchestrator vertical slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the daemon-owns-orchestrator model end-to-end — a `RemoteOrchestrator` proxy on the edge driving a real `SessionOrchestrator` inside the A1 daemon for list/get/create/send_prompt + live event streaming — without wiring it into the live edge yet.

**Architecture:** A daemon-side host (`server/ipc/orchestrator_rpc.py`) registers RPC handlers that delegate to a real `SessionOrchestrator` and pumps each driver's `events()` into A1's `publish()`. A `RemoteOrchestrator` (`server/sessions/remote.py`) implements the same method names over `DaemonClient`. Proven by an in-process daemon-on-UDS integration test; `app.py`/`ws.py` wiring is deferred to A2.2.

**Tech Stack:** Python 3.12, FastAPI/Starlette, the A1 `server/ipc/` transport, SQLAlchemy/aiosqlite, pytest + pytest-asyncio.

## Global Constraints

- Tests run via: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest <path> -q -p no:cacheprovider`.
- No new dependencies. pytest-asyncio is **strict** — every async test needs `@pytest.mark.asyncio`.
- New code: `server/sessions/remote.py`, `server/ipc/orchestrator_rpc.py`; modify `server/sessions/types.py`, `server/db/__init__.py`. All must pass `ruff check` and `mypy --ignore-missing-imports`.
- A2.1 does NOT modify `server/app.py` or `server/bridge/ws.py` (the edge wiring is A2.2).
- The RPC method set is exactly: `list_sessions`, `get_session`, `create_session`, `send_prompt`, `ensure_driver`. Nothing else (A2.2).
- `RemoteOrchestrator` methods are **async** (they await RPC), even where the in-process orchestrator method is sync — reconciling that with the sync API callers is an A2.2 concern.
- Event payloads stay the existing `to_wire()` dicts; the daemon pumps per-session events to A1's bus keyed by `session_id`.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: `SessionRecord` serialization

**Files:**
- Modify: `server/sessions/types.py`
- Test: `server/tests/test_session_record_serde.py`

**Interfaces:**
- Produces: `SessionRecord.to_dict(self) -> dict` and `SessionRecord.from_dict(cls, d: dict) -> SessionRecord`. `created_at` ↔ ISO-8601 string (or `None`); all other fields pass through.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_session_record_serde.py`:

```python
from datetime import datetime, timezone
from server.sessions.types import SessionRecord


def _rec(**kw):
    base = dict(id="s1", name="S1", account="acct", claude_session_id="cs",
                cols=80, rows=24, cwd="/tmp")
    base.update(kw)
    return SessionRecord(**base)


def test_round_trip_with_created_at():
    r = _rec(created_at=datetime(2026, 6, 22, 10, 30, tzinfo=timezone.utc),
             repo="o/r", status="in_progress", name_is_auto=False)
    d = r.to_dict()
    assert d["createdAt"] == "2026-06-22T10:30:00+00:00"
    back = SessionRecord.from_dict(d)
    assert back == r


def test_round_trip_with_none_created_at():
    r = _rec(created_at=None)
    d = r.to_dict()
    assert d["createdAt"] is None
    assert SessionRecord.from_dict(d) == r


def test_to_dict_is_json_safe():
    import json
    json.dumps(_rec(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)).to_dict())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_session_record_serde.py -q -p no:cacheprovider`
Expected: FAIL — `AttributeError: 'SessionRecord' object has no attribute 'to_dict'`.

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/types.py`, add these two methods to the `SessionRecord` dataclass (after the fields). The field list is fixed (id, name, account, claude_session_id, cols, rows, cwd, repo, detached, status, name_is_auto, permission_mode, created_at, adopted_in_place):

```python
    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "account": self.account,
            "claudeSessionId": self.claude_session_id, "cols": self.cols, "rows": self.rows,
            "cwd": self.cwd, "repo": self.repo, "detached": self.detached,
            "status": self.status, "nameIsAuto": self.name_is_auto,
            "permissionMode": self.permission_mode,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "adoptedInPlace": self.adopted_in_place,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SessionRecord":
        from datetime import datetime
        created = d.get("createdAt")
        return cls(
            id=d["id"], name=d["name"], account=d["account"],
            claude_session_id=d["claudeSessionId"], cols=d["cols"], rows=d["rows"],
            cwd=d["cwd"], repo=d.get("repo"), detached=d.get("detached", False),
            status=d.get("status", "idle"), name_is_auto=d.get("nameIsAuto", True),
            permission_mode=d.get("permissionMode", "interactive"),
            created_at=datetime.fromisoformat(created) if created else None,
            adopted_in_place=d.get("adoptedInPlace", False),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_session_record_serde.py -q -p no:cacheprovider`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/types.py server/tests/test_session_record_serde.py
git commit -m "feat(sessions): SessionRecord to_dict/from_dict for IPC serialization

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: SQLite WAL + busy_timeout

**Files:**
- Modify: `server/db/__init__.py` (`_make_engine`)
- Test: `server/tests/test_db_wal.py`

**Interfaces:**
- Produces: `_make_engine` enables `journal_mode=WAL` + `busy_timeout=5000` on every sqlite connection (via a SQLAlchemy connect listener). No signature change.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_db_wal.py`:

```python
import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_sqlite_engine_uses_wal(tmp_path):
    from server.db import _make_engine
    url = f"sqlite+aiosqlite:///{tmp_path / 'wal.db'}"
    engine = _make_engine(url)
    try:
        async with engine.connect() as conn:
            mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
            busy = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
    finally:
        await engine.dispose()
    assert str(mode).lower() == "wal"
    assert int(busy) == 5000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_db_wal.py -q -p no:cacheprovider`
Expected: FAIL — journal_mode is `delete` (or `memory`), not `wal`.

- [ ] **Step 3: Write minimal implementation**

In `server/db/__init__.py`, replace `_make_engine`. Current:

```python
def _make_engine(db_url: str):
    connect_args = {}
    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_async_engine(db_url, connect_args=connect_args, echo=False)
```

New (add `from sqlalchemy import event` to the imports at the top of the file if not present):

```python
def _make_engine(db_url: str):
    connect_args = {}
    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_async_engine(db_url, connect_args=connect_args, echo=False)
    if db_url.startswith("sqlite"):
        # WAL = concurrent readers + a single serialized writer, so the daemon
        # (sessions) and edge (audit/settings) can share one DB file across
        # processes; busy_timeout makes a contended writer wait instead of
        # raising "database is locked". Set per-connection via the sync driver.
        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver callback
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()
    return engine
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_db_wal.py -q -p no:cacheprovider`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add server/db/__init__.py server/tests/test_db_wal.py
git commit -m "feat(db): enable SQLite WAL + busy_timeout for multi-process access

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Daemon orchestrator RPC host — read/write methods

**Files:**
- Create: `server/ipc/orchestrator_rpc.py`
- Test: `server/tests/test_orchestrator_rpc.py`

**Interfaces:**
- Consumes: A1 `server.ipc.daemon.create_daemon_app`/`register_rpc`; `SessionRecord.to_dict` (Task 1); `SessionOrchestrator`.
- Produces: `register_orchestrator_rpc(app, orch) -> None` registering RPC methods `list_sessions`, `get_session`, `create_session`, `send_prompt` (and `ensure_driver` in Task 4). Wire shapes: `list_sessions` → `{"sessions": [rec.to_dict(), ...]}`; `get_session` → `{"session": rec.to_dict() | None}`; `create_session` → `{"session": rec.to_dict()}`; `send_prompt` → `{"ok": True}`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_orchestrator_rpc.py`:

```python
from unittest import mock
from fastapi.testclient import TestClient
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


def _rec(sid="s1"):
    return SessionRecord(id=sid, name="S1", account="acct", claude_session_id="cs",
                         cols=80, rows=24, cwd="/tmp")


def _app_with_orch():
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    register_orchestrator_rpc(app, orch)
    return app, orch


def test_list_and_get_sessions():
    app, orch = _app_with_orch()
    orch._records["s1"] = _rec("s1")
    with TestClient(app) as c:
        listed = c.post("/rpc/list_sessions", json={}).json()["result"]["sessions"]
        got = c.post("/rpc/get_session", json={"session_id": "s1"}).json()["result"]["session"]
        missing = c.post("/rpc/get_session", json={"session_id": "nope"}).json()["result"]["session"]
    assert [s["id"] for s in listed] == ["s1"]
    assert got["id"] == "s1"
    assert missing is None


def test_create_session_serializes_result():
    app, orch = _app_with_orch()
    with mock.patch.object(orch, "create_session",
                           new=mock.AsyncMock(return_value=_rec("new1"))) as cs:
        with TestClient(app) as c:
            r = c.post("/rpc/create_session", json={"name": "S", "account": "acct"}).json()
        cs.assert_awaited_once()
        assert cs.await_args.kwargs == {"name": "S", "account": "acct"}
    assert r["result"]["session"]["id"] == "new1"


def test_send_prompt_forwards_to_orch():
    app, orch = _app_with_orch()
    with mock.patch.object(orch, "send_prompt", new=mock.AsyncMock()) as sp:
        with TestClient(app) as c:
            r = c.post("/rpc/send_prompt", json={"session_id": "s1", "text": "hi"}).json()
        sp.assert_awaited_once_with("s1", "hi", None)
    assert r["result"] == {"ok": True}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_rpc.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.ipc.orchestrator_rpc'`.

- [ ] **Step 3: Write minimal implementation**

Create `server/ipc/orchestrator_rpc.py`:

```python
"""Expose a real SessionOrchestrator over the A1 daemon RPC/event transport.

Keeps orchestrator knowledge out of the generic transport (daemon.py). A2.1 wires
the vertical-slice methods; A2.2 adds the rest.
"""
from server.ipc.daemon import register_rpc


def register_orchestrator_rpc(app, orch) -> None:
    async def list_sessions(params: dict) -> dict:
        return {"sessions": [r.to_dict() for r in orch.list_sessions()]}

    async def get_session(params: dict) -> dict:
        rec = orch.get_session(params["session_id"])
        return {"session": rec.to_dict() if rec is not None else None}

    async def create_session(params: dict) -> dict:
        rec = await orch.create_session(**params)
        return {"session": rec.to_dict()}

    async def send_prompt(params: dict) -> dict:
        await orch.send_prompt(params["session_id"], params["text"], params.get("images"))
        return {"ok": True}

    register_rpc(app, "list_sessions", list_sessions)
    register_rpc(app, "get_session", get_session)
    register_rpc(app, "create_session", create_session)
    register_rpc(app, "send_prompt", send_prompt)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_rpc.py -q -p no:cacheprovider`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add server/ipc/orchestrator_rpc.py server/tests/test_orchestrator_rpc.py
git commit -m "feat(ipc): orchestrator RPC host — list/get/create/send_prompt

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Daemon `ensure_driver` RPC + per-session event pump

**Files:**
- Modify: `server/ipc/orchestrator_rpc.py`
- Test: `server/tests/test_orchestrator_rpc_events.py`

**Interfaces:**
- Consumes: A1 `app.state.publish` (set by `create_daemon_app`); `orch.ensure_driver` (async → driver|None); a driver's `events()` async-generator of objects with `.to_wire()`.
- Produces: RPC `ensure_driver` → `{"found": bool}`; as a side effect, starts (once per session) a background task pumping `driver.events()` → `app.state.publish(session_id, e.to_wire())`. Idempotent via `app.state.pumped: set[str]`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_orchestrator_rpc_events.py`:

```python
import json
from unittest import mock
from fastapi.testclient import TestClient
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator


class _FakeEvent:
    def __init__(self, i): self.i = i
    def to_wire(self): return {"type": "textDelta", "turnId": "t1", "i": self.i}


class _FakeDriver:
    async def events(self):
        for i in range(3):
            yield _FakeEvent(i)


def test_ensure_driver_pumps_events_to_subscriber():
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    register_orchestrator_rpc(app, orch)
    with mock.patch.object(orch, "ensure_driver",
                           new=mock.AsyncMock(return_value=_FakeDriver())):
        with TestClient(app) as c:
            with c.websocket_connect("/events") as ws:
                ws.send_text(json.dumps({"type": "subscribe", "stream": "s1"}))
                found = c.post("/rpc/ensure_driver", json={"session_id": "s1"}).json()
                got = [json.loads(ws.receive_text()) for _ in range(3)]
    assert found["result"] == {"found": True}
    assert [f["event"]["i"] for f in got] == [0, 1, 2]
    assert all(f["stream"] == "s1" for f in got)


def test_ensure_driver_not_found_returns_false():
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    register_orchestrator_rpc(app, orch)
    with mock.patch.object(orch, "ensure_driver", new=mock.AsyncMock(return_value=None)):
        with TestClient(app) as c:
            r = c.post("/rpc/ensure_driver", json={"session_id": "x"}).json()
    assert r["result"] == {"found": False}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_rpc_events.py -q -p no:cacheprovider`
Expected: FAIL — `UnknownMethod` for `ensure_driver` (404 envelope).

- [ ] **Step 3: Write minimal implementation**

In `server/ipc/orchestrator_rpc.py`, add `import asyncio` at the top, initialize the pump-tracking set, and register the `ensure_driver` handler. Inside `register_orchestrator_rpc`, before the `register_rpc(...)` calls, add:

```python
    app.state.pumped = set()

    async def _pump(session_id: str, driver) -> None:
        async for e in driver.events():
            await app.state.publish(session_id, e.to_wire())

    async def ensure_driver(params: dict) -> dict:
        session_id = params["session_id"]
        driver = await orch.ensure_driver(session_id, params.get("identity", "system"))
        if driver is not None and session_id not in app.state.pumped:
            app.state.pumped.add(session_id)
            asyncio.create_task(_pump(session_id, driver), name=f"pump-{session_id}")
        return {"found": driver is not None}
```

And add `register_rpc(app, "ensure_driver", ensure_driver)` alongside the other registrations.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_rpc_events.py -q -p no:cacheprovider`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add server/ipc/orchestrator_rpc.py server/tests/test_orchestrator_rpc_events.py
git commit -m "feat(ipc): ensure_driver RPC + per-session event pump

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: `RemoteOrchestrator` proxy + end-to-end integration

**Files:**
- Create: `server/sessions/remote.py`
- Test: `server/tests/test_remote_orchestrator.py`

**Interfaces:**
- Consumes: A1 `DaemonClient` (`call`, `subscribe`, `events_ready`, `DaemonUnavailable`); `register_orchestrator_rpc` (Tasks 3–4); `SessionRecord.from_dict` (Task 1); the `serve_daemon_uds` harness (`server/tests/ipc_harness.py`).
- Produces: `class RemoteOrchestrator(client)` with async `list_sessions()`, `get_session(session_id)`, `create_session(**kw)`, `send_prompt(session_id, text, images=None)`, `ensure_driver(session_id, identity="system") -> bool`, and `subscribe(session_id)` (delegates to the client's event stream).

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_remote_orchestrator.py`:

```python
import asyncio
from unittest import mock
import pytest
from server.ipc.client import DaemonClient, DaemonUnavailable
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.sessions.types import SessionRecord
from server.tests.ipc_harness import serve_daemon_uds


def _rec(sid="s1"):
    return SessionRecord(id=sid, name="S1", account="acct", claude_session_id="cs",
                         cols=80, rows=24, cwd="/tmp")


class _FakeEvent:
    def to_wire(self): return {"type": "textDelta", "turnId": "t1", "text": "hi"}


class _FakeDriver:
    async def events(self):
        yield _FakeEvent()


def _build_app():
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    register_orchestrator_rpc(app, orch)
    return app, orch


@pytest.mark.asyncio
async def test_list_get_create_send_over_uds():
    app, orch = _build_app()
    orch._records["s1"] = _rec("s1")
    with mock.patch.object(orch, "create_session",
                           new=mock.AsyncMock(return_value=_rec("new1"))), \
         mock.patch.object(orch, "send_prompt", new=mock.AsyncMock()) as sp:
        async with serve_daemon_uds(app) as sock:
            ro = RemoteOrchestrator(DaemonClient(sock))
            try:
                listed = await ro.list_sessions()
                got = await ro.get_session("s1")
                missing = await ro.get_session("nope")
                created = await ro.create_session(name="S", account="acct")
                await ro.send_prompt("s1", "hello")
            finally:
                await ro.aclose()
    assert [r.id for r in listed] == ["s1"]
    assert got.id == "s1" and missing is None
    assert created.id == "new1"
    sp.assert_awaited_once_with("s1", "hello", None)


@pytest.mark.asyncio
async def test_ensure_driver_and_stream_over_uds():
    app, orch = _build_app()
    with mock.patch.object(orch, "ensure_driver",
                           new=mock.AsyncMock(return_value=_FakeDriver())):
        async with serve_daemon_uds(app) as sock:
            ro = RemoteOrchestrator(DaemonClient(sock))
            try:
                stream = ro.subscribe("s1")
                await ro._client.events_ready("s1")
                found = await ro.ensure_driver("s1")
                ev = await asyncio.wait_for(stream.__anext__(), 2)
            finally:
                await ro.aclose()
    assert found is True
    assert ev == {"type": "textDelta", "turnId": "t1", "text": "hi"}


@pytest.mark.asyncio
async def test_daemon_unavailable_propagates():
    ro = RemoteOrchestrator(DaemonClient("/nonexistent/ccsd.sock"))
    with pytest.raises(DaemonUnavailable):
        await ro.list_sessions()
    await ro.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_orchestrator.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.sessions.remote'`.

- [ ] **Step 3: Write minimal implementation**

Create `server/sessions/remote.py`:

```python
"""Edge-side proxy that drives a SessionOrchestrator living in the session daemon.

Methods are async (they await RPC) even where the in-process orchestrator method
is sync; reconciling that with the sync API callers is an A2.2 concern. A2.1 wires
the vertical-slice methods only.
"""
from server.sessions.types import SessionRecord


class RemoteOrchestrator:
    def __init__(self, client):
        self._client = client

    async def list_sessions(self) -> list[SessionRecord]:
        r = await self._client.call("list_sessions")
        return [SessionRecord.from_dict(d) for d in r["sessions"]]

    async def get_session(self, session_id: str) -> SessionRecord | None:
        r = await self._client.call("get_session", session_id=session_id)
        return SessionRecord.from_dict(r["session"]) if r["session"] is not None else None

    async def create_session(self, **kwargs) -> SessionRecord:
        r = await self._client.call("create_session", **kwargs)
        return SessionRecord.from_dict(r["session"])

    async def send_prompt(self, session_id: str, text: str, images=None) -> None:
        await self._client.call("send_prompt", session_id=session_id, text=text, images=images)

    async def ensure_driver(self, session_id: str, identity: str = "system") -> bool:
        r = await self._client.call("ensure_driver", session_id=session_id, identity=identity)
        return r["found"]

    def subscribe(self, session_id: str):
        return self._client.subscribe(session_id)

    async def aclose(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_orchestrator.py -q -p no:cacheprovider`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/remote.py server/tests/test_remote_orchestrator.py
git commit -m "feat(sessions): RemoteOrchestrator proxy + end-to-end vertical slice over UDS

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Full verification + PR

**Files:** none (verification only).

- [ ] **Step 1: Run all new A2.1 tests**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_session_record_serde.py server/tests/test_db_wal.py server/tests/test_orchestrator_rpc.py server/tests/test_orchestrator_rpc_events.py server/tests/test_remote_orchestrator.py -q -p no:cacheprovider`
Expected: all pass, output pristine.

- [ ] **Step 2: Lint + type-check + the existing IPC suite (no regression)**

Run:
```bash
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/ruff check server/ipc/ server/sessions/remote.py server/sessions/types.py server/db/__init__.py server/tests/test_session_record_serde.py server/tests/test_db_wal.py server/tests/test_orchestrator_rpc.py server/tests/test_orchestrator_rpc_events.py server/tests/test_remote_orchestrator.py
PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/mypy server/ --ignore-missing-imports
PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_*.py -q -p no:cacheprovider
```
Expected: ruff clean; mypy success; IPC suite still green.

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin HEAD
gh pr create --base master --title "feat(sessions): RemoteOrchestrator vertical slice (A2.1)" --body "<summary: daemon hosts a real orchestrator over A1 RPC; RemoteOrchestrator proxy + per-session event pump; WAL enabled; proven by an in-process daemon-on-UDS integration test. No app.py/ws.py wiring (A2.2). Link the spec.>"
```

- [ ] **Step 4: Wait for CI green, then merge**

Run `gh pr checks <num>` until `lint-and-test` + `frontend` pass, then `gh pr merge <num> --merge --delete-branch`.

---

## Notes for the implementer

- pytest-asyncio is **strict** — every `async def test_*` needs `@pytest.mark.asyncio`.
- Tasks 3–4 test the daemon RPC/streaming in-process via Starlette `TestClient` (no UDS). Task 5 uses the real UDS via `serve_daemon_uds` from `server/tests/ipc_harness.py` (A1).
- `SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0, usage_fn=lambda name: None)` constructs a no-network orchestrator; the heavy `create_session`/`ensure_driver` paths are mock-patched in these tests — A2.2 exercises them for real when the existing suite runs in remote mode.
- Do NOT touch `server/app.py` or `server/bridge/ws.py` — that wiring is A2.2.
- The full `server/tests` suite hangs in a firewalled sandbox (it polls real accounts; project memory). Run only the named files locally; the full suite runs clean on GitHub CI.
