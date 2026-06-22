# A2.2b-2b: Edge Cutover to the Session Daemon — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Behind one env flag (`CCSWITCH_SESSIOND_SOCKET`), make the FastAPI edge run against the session daemon over a Unix socket — session lifecycle lives in the daemon, the edge stays a thin restartable front — with the default (flag unset) byte-identical to today.

**Architecture:** `create_app` selects the remote facade (`RemoteOrchestrator(DaemonClient(socket))`) when the flag is set, otherwise the in-process adapter. The lifespan skips the three orchestrator-lifecycle calls in remote mode; healthz reads the busy-count through the facade (async, both modes); `DaemonUnavailable` maps to REST 503 / WS close(1013). The edge keeps its own DB access for the non-session tables on the shared WAL DB.

**Tech Stack:** Python 3.12 · FastAPI/Starlette · SQLAlchemy + aiosqlite (WAL) · uvicorn-on-UDS · httpx · pytest/pytest-asyncio · Starlette `TestClient`.

## Global Constraints

- The default (flag unset) path MUST stay byte-identical to today — in-process healthz JSON is `{status, sha, busy, activeSessions}` with NO `daemon` field.
- An injected `orchestrator=` argument to `create_app` ALWAYS wins over the flag (in-process), so existing tests are unaffected.
- Do NOT change `server/sessions/orchestrator.py`, the facade method set, or any `server/api/*` router. The only production files touched: `server/app.py` and `server/bridge/ws.py`.
- The remote facade type is `RemoteOrchestrator(client)` from `server.sessions.remote`; `DaemonClient(socket_path: str)` and `DaemonUnavailable` are from `server.ipc.client`.
- `app.state.sessions` is the facade in BOTH modes; `app.state.orchestrator` is the raw orchestrator in-process and `None` in remote mode; `app.state.remote_mode: bool` is the selector.
- Commit trailer on every commit: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Run server tests from this worktree with the main checkout's prebuilt venv and `PYTHONPATH=$(pwd)/src`:
  `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest <args>`
- CI gates that must stay green: `ruff check .` (full repo), `mypy server/ --ignore-missing-imports`.

---

### Task 1: Flag and facade wiring in `create_app`

**Files:**
- Modify: `server/app.py` (`create_app`, lines 138-149 region)
- Test: `server/tests/test_remote_mode_wiring.py` (create)

**Interfaces:**
- Consumes: `create_app(orchestrator=None, reauth_manager=None) -> FastAPI`; `RemoteOrchestrator(client)` from `server.sessions.remote`; `DaemonClient(socket_path)` from `server.ipc.client`.
- Produces: `app.state.remote_mode: bool`; in remote mode `app.state.orchestrator is None` and `type(app.state.sessions).__name__ == "RemoteOrchestrator"`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_remote_mode_wiring.py
import os
from unittest import mock
from server.app import create_app
from server.sessions.facade import InProcessOrchestratorAdapter
from server.sessions.remote import RemoteOrchestrator


def test_flag_set_selects_remote_facade():
    with mock.patch.dict(os.environ, {"CCSWITCH_SESSIOND_SOCKET": "/tmp/ccsd-test.sock"}):
        app = create_app()
    assert app.state.remote_mode is True
    assert app.state.orchestrator is None
    assert isinstance(app.state.sessions, RemoteOrchestrator)


def test_flag_unset_is_in_process():
    env = {k: v for k, v in os.environ.items() if k != "CCSWITCH_SESSIOND_SOCKET"}
    with mock.patch.dict(os.environ, env, clear=True):
        app = create_app()
    assert app.state.remote_mode is False
    assert app.state.orchestrator is not None
    assert isinstance(app.state.sessions, InProcessOrchestratorAdapter)


def test_injected_orchestrator_wins_over_flag():
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator()
    with mock.patch.dict(os.environ, {"CCSWITCH_SESSIOND_SOCKET": "/tmp/ccsd-test.sock"}):
        app = create_app(orchestrator=orch)
    assert app.state.remote_mode is False
    assert app.state.orchestrator is orch
    assert isinstance(app.state.sessions, InProcessOrchestratorAdapter)
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_mode_wiring.py -v`
Expected: FAIL — `AttributeError: ... 'remote_mode'` (attribute does not exist yet).

- [ ] **Step 3: Implement the wiring**

In `server/app.py`, replace the orchestrator/facade attach block (currently lines 144-149):

```python
    # Attach the session facade. CCSWITCH_SESSIOND_SOCKET selects remote mode:
    # the edge drives an orchestrator living in the session daemon over a Unix
    # socket. An explicitly injected orchestrator always wins (in-process), so
    # tests that pass one are unaffected.
    from server.sessions.facade import InProcessOrchestratorAdapter

    socket_path = os.environ.get("CCSWITCH_SESSIOND_SOCKET")
    if orchestrator is None and socket_path:
        from server.ipc.client import DaemonClient
        from server.sessions.remote import RemoteOrchestrator
        app.state.remote_mode = True
        app.state.orchestrator = None
        app.state.sessions = RemoteOrchestrator(DaemonClient(socket_path))
    else:
        if orchestrator is None:
            orchestrator = SessionOrchestrator()
        app.state.remote_mode = False
        app.state.orchestrator = orchestrator
        app.state.sessions = InProcessOrchestratorAdapter(orchestrator)
```

(Delete the old lines 144-149: the `if orchestrator is None: orchestrator = SessionOrchestrator()`, the two `app.state.*` assignments, and the local `from server.sessions.facade import ...` import that this block now subsumes.)

- [ ] **Step 4: Run the test, verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_mode_wiring.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Guard against regressions in the existing app tests**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_healthz.py server/tests/test_api.py -q`
Expected: PASS (no regressions — in-process path unchanged).

- [ ] **Step 6: Commit**

```bash
git add server/app.py server/tests/test_remote_mode_wiring.py
git commit -m "feat(edge): CCSWITCH_SESSIOND_SOCKET selects remote session facade (A2.2b-2b)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Lifespan branching (skip orchestrator lifecycle in remote mode)

**Files:**
- Modify: `server/app.py` (`_lifespan`, lines 90-96, 132-135)
- Test: `server/tests/test_remote_mode_wiring.py` (extend)

**Interfaces:**
- Consumes: `app.state.remote_mode` (Task 1); `app.state.sessions.aclose()` (exists on `RemoteOrchestrator`, NOT on `InProcessOrchestratorAdapter`).
- Produces: in remote mode the lifespan makes no `app.state.orchestrator.*` calls and awaits `app.state.sessions.aclose()` on shutdown.

- [ ] **Step 1: Write the failing test**

This drives the edge lifespan in remote mode against a *dead* socket (no daemon needed — the remote lifespan makes no daemon calls at startup). It asserts startup/shutdown complete without touching the (None) orchestrator, and that `aclose` is awaited on shutdown.

```python
# append to server/tests/test_remote_mode_wiring.py
import pytest
from starlette.testclient import TestClient


@pytest.mark.asyncio
async def test_remote_lifespan_skips_orchestrator_and_closes_facade(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'edge.db'}"
    env = {
        "CCSWITCH_SESSIOND_SOCKET": str(tmp_path / "dead.sock"),  # no daemon listening
        "CCSWITCH_DB_URL": db_url,
        "CCSWITCH_DEV": "1",  # allow empty allowlist on the default bind
    }
    with mock.patch.dict(os.environ, env):
        app = create_app()
        closed = {}
        real_aclose = app.state.sessions.aclose

        async def _spy_aclose():
            closed["called"] = True
            await real_aclose()

        app.state.sessions.aclose = _spy_aclose
        # TestClient runs lifespan startup on enter, shutdown on exit.
        with TestClient(app):
            assert app.state.remote_mode is True
            assert app.state.orchestrator is None
        assert closed.get("called") is True
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_mode_wiring.py::test_remote_lifespan_skips_orchestrator_and_closes_facade -v`
Expected: FAIL — the current lifespan calls `app.state.orchestrator.rehydrate()` on `None` → `AttributeError`, OR `aclose` is never called.

- [ ] **Step 3: Implement the lifespan branching**

In `server/app.py` `_lifespan`, wrap the rehydrate call (currently lines 90-96):

```python
    # Rehydrate persisted sessions (lazy — drivers are created on attach, not here).
    # In remote mode the daemon owns the orchestrator lifecycle, so skip it here.
    if not app.state.remote_mode:
        try:
            await app.state.orchestrator.rehydrate()
        except Exception:
            _logging.getLogger("server").error(
                "rehydrate() failed — starting with no sessions restored", exc_info=True
            )
```

Wrap the start call (currently line 115):

```python
    # Start the singleton orchestrator (usage poller + rotation monitor).
    # In remote mode the daemon runs these, not the edge.
    if not app.state.remote_mode:
        await app.state.orchestrator.start()
```

Replace the shutdown block (currently lines 132-135):

```python
    yield
    # Shutdown
    await app.state.reauth.shutdown()
    if app.state.remote_mode:
        await app.state.sessions.aclose()
    else:
        await app.state.orchestrator.stop()
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_mode_wiring.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Confirm in-process lifespan still works**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_healthz.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/app.py server/tests/test_remote_mode_wiring.py
git commit -m "feat(edge): remote-mode lifespan skips rehydrate/start/stop, closes facade (A2.2b-2b)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Remote-mode integration harness + healthz over RPC

**Files:**
- Modify: `server/app.py` (`healthz`, lines 164-178)
- Test: `server/tests/test_remote_mode_edge.py` (create — this module's `remote_edge` fixture is reused by Tasks 4-6)

**Interfaces:**
- Consumes: `serve_daemon_uds` from `server.tests.ipc_harness`; `create_daemon_app` from `server.ipc.daemon`; `register_orchestrator_rpc` from `server.ipc.orchestrator_rpc`; `SessionOrchestrator`; `app.state.sessions.active_session_count()` (async, both facades).
- Produces: a `remote_edge` pytest fixture yielding `(orch, client)` where `orch` is the daemon-side `SessionOrchestrator` (seed its `_records`) and `client` is a Starlette `TestClient` for the edge in remote mode sharing one DB. Also a `dead_socket_edge` fixture for the daemon-down cases.

**Harness design notes (read before implementing):** `serve_daemon_uds` serves a *real* uvicorn on a *real* UDS path. Run it in a background thread with its own event loop so the edge can use the synchronous `TestClient` (which runs lifespan and supports `websocket_connect`). The daemon-side orchestrator must NOT touch the DB (seed in-memory `_records`, mock heavy paths) — in-process the daemon thread and the edge share `server.db`'s module-global engine, which is bound to the edge's loop; keeping daemon DB access out avoids cross-loop aiosqlite errors. The edge owns the DB on its own loop (its lifespan runs `init_db`). This is faithful for a *wiring* test; daemon DB persistence is covered by the orchestrator's own tests.

- [ ] **Step 1: Write the failing test (harness fixture + healthz cases)**

```python
# server/tests/test_remote_mode_edge.py
import asyncio
import os
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from starlette.testclient import TestClient

from server.app import create_app
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.ipc_harness import serve_daemon_uds

_IDENT = {"Tailscale-User-Login": "edge@example.com"}


def _rec(sid: str, status: str = "idle") -> SessionRecord:
    r = SessionRecord(id=sid, name=sid.upper(), account="acct",
                      claude_session_id="cs-" + sid, cols=80, rows=24, cwd="/tmp")
    r.status = status
    return r


class _DaemonThread:
    """Run a daemon app on a real UDS in a background thread/loop. Real socket =
    the edge (its own loop) can reach it cross-thread, as in production."""

    def __init__(self, orch):
        self._orch = orch
        self.sock = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._err = None
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _serve():
            dapp = create_daemon_app()
            register_orchestrator_rpc(dapp, self._orch)  # running loop → pump starts
            async with serve_daemon_uds(dapp) as sock:
                self.sock = sock
                self._ready.set()
                while not self._stop.is_set():
                    await asyncio.sleep(0.05)

        try:
            loop.run_until_complete(_serve())
        except Exception as exc:  # noqa: BLE001 — surface to the main thread
            self._err = exc
            self._ready.set()
        finally:
            loop.close()

    def start(self):
        self._t.start()
        if not self._ready.wait(5):
            raise RuntimeError("daemon thread did not start")
        if self._err is not None:
            raise self._err

    def stop(self):
        self._stop.set()
        self._t.join(5)


@pytest.fixture()
def remote_edge(tmp_path):
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    daemon = _DaemonThread(orch)
    daemon.start()
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'edge.db'}"
    env = {
        "CCSWITCH_SESSIOND_SOCKET": daemon.sock,
        "CCSWITCH_DB_URL": db_url,
        "CCSWITCH_TAILNET_ALLOWLIST": _IDENT["Tailscale-User-Login"],
    }
    try:
        with mock.patch.dict(os.environ, env):
            app = create_app()
            with TestClient(app, raise_server_exceptions=False) as client:
                yield orch, client
    finally:
        daemon.stop()


def test_healthz_busy_count_over_rpc(remote_edge):
    orch, client = remote_edge
    # Two active sessions on the daemon → edge healthz reflects them over RPC.
    orch._records["a"] = _rec("a", status="in_progress")
    orch._records["b"] = _rec("b", status="in_progress")
    resp = client.get("/healthz")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["activeSessions"] == 2
    assert body["busy"] is True
    assert body["daemon"] == "ok"
```

NOTE: confirm `SessionRecord` accepts a settable `.status` and which statuses `active_session_count` counts (see `_ACTIVE_STATUSES` in `server/sessions/orchestrator.py`); if `"in_progress"` is not in that set, use a status that is. The implementer MUST check this before asserting `activeSessions == 2`.

- [ ] **Step 2: Run the test, verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_mode_edge.py::test_healthz_busy_count_over_rpc -v`
Expected: FAIL — healthz still reads `app.state.orchestrator` (None in remote mode → `active=0`), and there is no `daemon` key.

- [ ] **Step 3: Implement healthz unification + daemon-down handling**

In `server/app.py`, replace the `healthz` body (currently lines 164-178):

```python
    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        # Exercises every Session model column; a drifted DB raises → 500.
        # Active count comes through the facade (async) so it works in both modes;
        # in remote mode an unreachable daemon degrades gracefully (200 + flag).
        from server.ipc.client import DaemonUnavailable

        active = 0
        daemon_state = None
        try:
            active = await app.state.sessions.active_session_count()
            if app.state.remote_mode:
                daemon_state = "ok"
        except DaemonUnavailable:
            active = 0
            daemon_state = "unavailable"
        try:
            async with _db.db_session() as db:
                await db.execute(_sa_select(_SessionModel).limit(1))
            payload = {
                "status": "ok", "sha": running_git_sha(),
                "busy": active > 0, "activeSessions": active,
            }
            if daemon_state is not None:
                payload["daemon"] = daemon_state
            return JSONResponse(payload)
        except Exception as exc:  # noqa: BLE001 — any DB/schema failure is unhealthy
            logging.getLogger("server").warning("healthz schema check failed", exc_info=exc)
            return JSONResponse({"status": "schema_error", "sha": running_git_sha()}, status_code=500)
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_mode_edge.py -v`
Expected: PASS.

- [ ] **Step 5: Confirm in-process healthz JSON is byte-identical (no `daemon` key)**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_healthz.py -v`
Expected: PASS — existing assertions on `{status, sha, busy, activeSessions}` still hold (no `daemon` key in-process).

- [ ] **Step 6: Commit**

```bash
git add server/app.py server/tests/test_remote_mode_edge.py
git commit -m "feat(edge): healthz busy-count over RPC with daemon-down degrade (A2.2b-2b)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `DaemonUnavailable` → 503 (REST exception handler)

**Files:**
- Modify: `server/app.py` (`create_app`, register an exception handler)
- Test: `server/tests/test_remote_mode_edge.py` (extend)

**Interfaces:**
- Consumes: `DaemonUnavailable` from `server.ipc.client`; the `dead_socket_edge` fixture (added here).
- Produces: any REST route that raises `DaemonUnavailable` returns HTTP 503 `{"detail": "session daemon unavailable"}`.

- [ ] **Step 1: Write the failing test**

```python
# append to server/tests/test_remote_mode_edge.py

@pytest.fixture()
def dead_socket_edge(tmp_path):
    """Edge in remote mode pointed at a socket with no daemon → every RPC raises
    DaemonUnavailable."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'edge.db'}"
    env = {
        "CCSWITCH_SESSIOND_SOCKET": str(tmp_path / "dead.sock"),
        "CCSWITCH_DB_URL": db_url,
        "CCSWITCH_TAILNET_ALLOWLIST": _IDENT["Tailscale-User-Login"],
    }
    with mock.patch.dict(os.environ, env):
        app = create_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client


def test_rest_call_returns_503_when_daemon_down(dead_socket_edge):
    # GET /api/sessions → facade.list_sessions() → DaemonUnavailable → 503.
    resp = dead_socket_edge.get("/api/sessions", headers=_IDENT)
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == "session daemon unavailable"
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_mode_edge.py::test_rest_call_returns_503_when_daemon_down -v`
Expected: FAIL — without a handler, `DaemonUnavailable` surfaces as 500.

- [ ] **Step 3: Register the exception handler**

In `server/app.py` `create_app`, after `app.add_middleware(IdentityMiddleware)` (line 158) and before the health routes, add:

```python
    # In remote mode an unreachable daemon surfaces as DaemonUnavailable from any
    # facade RPC; map it to 503 so clients see "try again", not a 500. Harmless
    # in-process (never raised there).
    from server.ipc.client import DaemonUnavailable

    @app.exception_handler(DaemonUnavailable)
    async def _daemon_unavailable_handler(_request, _exc):  # noqa: ANN001
        return JSONResponse(status_code=503, content={"detail": "session daemon unavailable"})
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_mode_edge.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/app.py server/tests/test_remote_mode_edge.py
git commit -m "feat(edge): map DaemonUnavailable to HTTP 503 (A2.2b-2b)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: WS attach-boundary guard → close(1013)

**Files:**
- Modify: `server/bridge/ws.py` (the `/ws` handler — `subscribe_events()` at line 91 and the `ensure_driver()` attach at line 152)
- Test: `server/tests/test_remote_mode_edge.py` (extend)

**Interfaces:**
- Consumes: `DaemonUnavailable` from `server.ipc.client`; the `dead_socket_edge` fixture (Task 4).
- Produces: a WS connect when the daemon is unreachable closes cleanly with code 1013 instead of crashing the handler.

- [ ] **Step 1: Write the failing test**

```python
# append to server/tests/test_remote_mode_edge.py
from starlette.websockets import WebSocketDisconnect


def test_ws_connect_closes_1013_when_daemon_down(dead_socket_edge):
    # The first facade call in the WS handler is subscribe_events(); with the
    # daemon down it must close 1013 (try again later), not crash the handler.
    with pytest.raises(WebSocketDisconnect) as exc:
        with dead_socket_edge.websocket_connect("/ws", headers=_IDENT) as ws:
            ws.receive_text()  # forces the server frames to be read
    assert exc.value.code == 1013
```

NOTE: confirm how the existing WS tests pass identity to `websocket_connect` (header vs `?identity=` query param) by reading `require_ws_identity` in `server/bridge/ws.py` / an existing ws test, and match that exactly. If identity goes via query string, use `dead_socket_edge.websocket_connect("/ws?identity=edge@example.com")` and drop the headers.

- [ ] **Step 2: Run the test, verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_mode_edge.py::test_ws_connect_closes_1013_when_daemon_down -v`
Expected: FAIL — `subscribe_events()` raises `DaemonUnavailable`, crashing the handler (close code is not 1013, or an internal-error close).

- [ ] **Step 3: Guard the attach boundary in `ws.py`**

Add the import near the top of `server/bridge/ws.py` (with the other imports):

```python
from server.ipc.client import DaemonUnavailable
```

Replace the lifecycle subscribe (currently line 91):

```python
        # Subscribe to orchestrator lifecycle events (rotated, etc.). In remote
        # mode an unreachable daemon must close the socket cleanly (1013 = try
        # again later), not crash the handler.
        try:
            event_q = await orchestrator.subscribe_events()
        except DaemonUnavailable:
            log.warning("ws.connect: session daemon unavailable")
            await websocket.close(code=1013)
            return
```

In `_handle_attach`, add a `DaemonUnavailable` arm BEFORE the existing broad `except Exception` at the `ensure_driver` call (currently lines 148-153):

```python
            try:
                driver = await orchestrator.ensure_driver(session_id, identity)
            except DaemonUnavailable:
                log.warning("ws.attach: session daemon unavailable for %s", session_id)
                await websocket.close(code=1013)
                return
            except Exception:
                # (existing handling unchanged below)
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_mode_edge.py -v`
Expected: PASS.

- [ ] **Step 5: Confirm in-process WS tests still pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/ -q -k "ws or websocket"`
Expected: PASS (the new import + guard don't disturb the in-process path).

- [ ] **Step 6: Commit**

```bash
git add server/bridge/ws.py server/tests/test_remote_mode_edge.py
git commit -m "feat(edge): WS attach guards DaemonUnavailable with clean close 1013 (A2.2b-2b)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Positive-path remote integration (REST round-trip + WS attach/stream)

**Files:**
- Test: `server/tests/test_remote_mode_edge.py` (extend — reuses the `remote_edge` fixture)

**Interfaces:**
- Consumes: the `remote_edge` fixture (Task 3); the daemon-side `orch` to seed records and patch `ensure_driver`.
- Produces: proof that REST list/get/rename through the facade RPC and a WS attach+stream both work end-to-end in remote mode. No production code changes — pure integration coverage.

- [ ] **Step 1: Write the REST round-trip test**

```python
# append to server/tests/test_remote_mode_edge.py

def test_rest_list_and_get_round_trip(remote_edge):
    orch, client = remote_edge
    orch._records["s1"] = _rec("s1")
    listed = client.get("/api/sessions", headers=_IDENT)
    assert listed.status_code == 200, listed.text
    assert any(s["id"] == "s1" for s in listed.json())
    got = client.get("/api/sessions/s1", headers=_IDENT)
    assert got.status_code == 200, got.text
    assert got.json()["id"] == "s1"
```

NOTE: confirm the exact session-detail route (`GET /api/sessions/{id}` vs another shape) and the list/detail JSON keys by reading `server/api/sessions.py`; match the real routes/keys. If `rename` is a simple facade pass-through, add a rename assertion too, but only against the real route signature.

- [ ] **Step 2: Run it, verify it passes (the wiring from Tasks 1-4 already supports this)**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_mode_edge.py::test_rest_list_and_get_round_trip -v`
Expected: PASS. (This is integration coverage of existing wiring; if it fails, a real cross-process bug exists — fix before proceeding.)

- [ ] **Step 3: Write the WS attach+stream test**

```python
# append to server/tests/test_remote_mode_edge.py

class _FakeEvent:
    def to_wire(self):
        return {"type": "textDelta", "turnId": "t1", "text": "hi", "sessionId": "s1"}


class _FakeDriver:
    def current_status(self):
        return "idle"

    def queued(self):
        return []

    async def events(self):
        yield _FakeEvent()


def test_ws_attach_streams_event_from_daemon(remote_edge):
    orch, client = remote_edge
    orch._records["s1"] = _rec("s1")
    with mock.patch.object(orch, "ensure_driver",
                           new=mock.AsyncMock(return_value=_FakeDriver())), \
         mock.patch.object(orch, "transcript_path", new=mock.AsyncMock(return_value=None)):
        with client.websocket_connect("/ws", headers=_IDENT) as ws:
            ws.send_json({"type": "attach", "sessionId": "s1"})
            # Drain frames until the streamed textDelta arrives (batch-wrapped).
            deadline = _time.time() + 5
            seen = None
            while _time.time() < deadline and seen is None:
                msg = ws.receive_json()
                events = msg.get("events", []) if msg.get("type") == "batch" else [msg]
                for e in events:
                    if e.get("type") == "textDelta" and e.get("text") == "hi":
                        seen = e
                        break
            assert seen is not None, "did not receive streamed textDelta"
```

NOTE: confirm the attach message shape (`{"type": "attach", "sessionId": ...}`) and the inbound/outbound frame envelope (batch vs raw) by reading `server/bridge/ws.py` and an existing WS attach test; match them exactly. The redactor may rewrite fields — assert on a field that survives redaction (e.g. `type`/`text`).

- [ ] **Step 4: Run it, verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_mode_edge.py -v`
Expected: PASS (all remote-mode edge tests).

- [ ] **Step 5: Full gate — ruff, mypy, and the touched suites**

```bash
cd /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.claude/worktrees/feature+api-retry-backoff
PY=/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python
"$PY" -m ruff check .
PYTHONPATH=$(pwd)/src "$PY" -m mypy server/ --ignore-missing-imports
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src "$PY" -m pytest \
  server/tests/test_remote_mode_wiring.py server/tests/test_remote_mode_edge.py \
  server/tests/test_healthz.py server/tests/test_api.py -q
```
Expected: ruff clean, mypy Success, all tests pass.

- [ ] **Step 6: Commit**

```bash
git add server/tests/test_remote_mode_edge.py
git commit -m "test(edge): remote-mode REST round-trip + WS attach/stream integration (A2.2b-2b)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Plan Self-Review

**Spec coverage:**
- §1 flag/facade wiring → Task 1. ✓
- §2 lifespan branching (skip rehydrate/start/stop, aclose) → Task 2. ✓
- §3 healthz unification + daemon-down degrade, in-process JSON unchanged → Task 3. ✓
- §4 DaemonUnavailable→503 REST → Task 4; WS close(1013) → Task 5. ✓
- §5 healthz/deploy-gate semantics → realized by Task 3's degrade behavior (no separate code). ✓
- Testing §: focused integration test (healthz over RPC, daemon-down, 503, WS 1013, REST round-trip, WS attach/stream) → Tasks 3-6; flag-selection unit assertion → Task 1. ✓
- Shared-DB WAL assumption → verified already configured (`server/db/__init__.py:41-46`); no task needed.

**Placeholder scan:** No TBD/TODO. Every code step shows the code. The three `NOTE:` callouts are explicit "verify-against-real-code-then-match" instructions (route shapes, identity-passing, active-status set) — not placeholders for logic, but guards against the plan guessing an existing contract; each names the file to check.

**Type/name consistency:** `app.state.remote_mode` / `app.state.orchestrator` / `app.state.sessions` used identically across Tasks 1-3. `DaemonUnavailable` imported from `server.ipc.client` in Tasks 3,4,5. `remote_edge` fixture defined in Task 3, reused in Task 6; `dead_socket_edge` defined in Task 4, reused in Task 5. Close code 1013 consistent in Task 5. Healthz keys (`status/sha/busy/activeSessions/daemon`) consistent between Task 3 code and its assertions.
