# X11 / VNC daemon→edge bridge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the per-session virtual browser work in remote/daemon mode by routing the RFB stream and x11 operations through the existing WebSocket-over-IPC transport, instead of the edge's empty local `X11Manager`.

**Architecture:** The daemon owns the displays. Add a daemon-side `/vnc` WebSocket that relays to the daemon-local `x11vnc`, plus `x11_metrics`/`x11_open_browser` RPCs. In remote mode the edge `/ws/vnc` becomes a WS↔WS proxy to that daemon endpoint, and the metrics/open-browser/healthz paths call the daemon over RPC. In-process mode is unchanged. Because everything rides the IPC transport, it is cross-host-ready the moment the transport becomes TCP (Project B).

**Tech Stack:** Python 3.12, FastAPI/Starlette, uvicorn (UDS), httpx (UDS transport), `websockets` (`unix_connect`), pytest + `TestClient`.

## Global Constraints

- Never fork/patch the Claude binary; account = `CLAUDE_CONFIG_DIR` (constitution I/II) — not touched here.
- Single source of truth / no parallel implementations (VI): reuse the existing `_relay` byte-pump; do **not** write a second relay.
- Purge replaced code in the same change (VI): when the relay logic moves to the daemon, the edge's in-process branch must call the *same* shared functions, not a copy.
- TDD + quality gates (V): every task is test-first; backend gate = `ruff` + `mypy` + `pytest`.
- Run backend tests from this worktree with the deploy venv + worktree `PYTHONPATH`:
  `VENV=/mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python`
  `PYTHONPATH=$PWD/src env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 $VENV -m pytest …`
- No frontend changes (the browser already talks to `/ws/vnc`).
- Commit message trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## File Structure

- `server/bridge/vnc_ws.py` (modify) — keep `_relay`/`_StreamAdapter`; add `_WSAdapter`; router takes `(get_x11, get_daemon_sock)`; add the remote WS↔WS proxy branch.
- `server/ipc/x11_rpc.py` (create) — daemon `/vnc` WS endpoint + `x11_metrics` / `x11_open_browser` RPCs + `register_x11(app, x11)`.
- `server/ipc/serve.py` (modify) — `build_daemon()` sets `app.state.x11` and calls `register_x11`.
- `server/sessions/remote.py` (modify) — `RemoteOrchestrator.x11_metrics()` / `.x11_open_browser()`.
- `server/app.py` (modify) — remote mode: `app.state.x11 = None`, store `app.state.daemon_socket`, skip local sweep; wire router with daemon sock; route metrics/open-browser/healthz vncAttached through the facade.
- `server/tests/test_ws_adapter.py` (create), `server/tests/test_x11_rpc.py` (create), `server/tests/test_vnc_ws_remote.py` (create) — new tests.
- `server/tests/test_remote_mode_edge.py` (modify) — teach `_DaemonThread` to register x11 (enables the e2e VNC test).

---

### Task 1: `_WSAdapter` — drive `_relay` over a `websockets` client connection

**Files:**
- Modify: `server/bridge/vnc_ws.py`
- Test: `server/tests/test_ws_adapter.py` (create)

**Interfaces:**
- Consumes: existing `_relay(ws, tcp, idle=30.0)` from `server/bridge/vnc_ws.py`.
- Produces: `class _WSAdapter` with `async read(n) -> bytes`, `write(b) -> None`, `async drain() -> None`. EOF (peer closed) surfaces as `read()` returning `b""`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_ws_adapter.py
"""_WSAdapter lets the _relay byte-pump speak to a `websockets` client connection."""
import pytest
from server.bridge.vnc_ws import _WSAdapter


class _FakeClientWS:
    def __init__(self, incoming):
        self._in = list(incoming)
        self.sent = []

    async def recv(self):
        if self._in:
            return self._in.pop(0)
        from websockets.exceptions import ConnectionClosedOK
        raise ConnectionClosedOK(None, None)

    async def send(self, b):
        self.sent.append(b)


@pytest.mark.asyncio
async def test_read_returns_bytes_and_eof_on_close():
    ws = _FakeClientWS([b"RFB 003.008\n", "text-frame"])
    a = _WSAdapter(ws)
    assert await a.read(65536) == b"RFB 003.008\n"
    assert await a.read(65536) == b"text-frame"     # str frame coerced to bytes
    assert await a.read(65536) == b""                # ConnectionClosed → EOF sentinel


@pytest.mark.asyncio
async def test_write_then_drain_sends():
    ws = _FakeClientWS([])
    a = _WSAdapter(ws)
    a.write(b"\x00\x01")
    await a.drain()
    assert ws.sent == [b"\x00\x01"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 $VENV -m pytest server/tests/test_ws_adapter.py -q`
Expected: FAIL with `ImportError: cannot import name '_WSAdapter'`.

- [ ] **Step 3: Add `_WSAdapter` to `server/bridge/vnc_ws.py`** (place directly after `_StreamAdapter`)

```python
class _WSAdapter:
    """Adapts a `websockets` client connection to the .read/.write/.drain shape _relay
    uses, so the SAME byte-pump drives both a local TCP RFB stream (in-process mode)
    and a WS-over-IPC tunnel to the daemon (remote mode). EOF (peer close) → read() == b"".
    """

    def __init__(self, ws):
        self._ws = ws
        self._pending: bytes | None = None

    async def read(self, n):
        from websockets.exceptions import ConnectionClosed
        try:
            data = await self._ws.recv()
        except ConnectionClosed:
            return b""
        return data.encode() if isinstance(data, str) else data

    def write(self, b):
        self._pending = b

    async def drain(self):
        if self._pending is not None:
            await self._ws.send(self._pending)
            self._pending = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: same as Step 2. Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add server/bridge/vnc_ws.py server/tests/test_ws_adapter.py
git commit -m "feat(vnc): _WSAdapter so _relay can pump over a websockets connection

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Daemon-side x11 surface (`/vnc` WS + RPCs)

**Files:**
- Create: `server/ipc/x11_rpc.py`
- Modify: `server/ipc/serve.py` (`build_daemon`)
- Test: `server/tests/test_x11_rpc.py` (create)

**Interfaces:**
- Consumes: `_relay`, `_StreamAdapter` from `server/bridge/vnc_ws.py`; `register_rpc` from `server/ipc/daemon.py`; an `X11Manager`-shaped object with sync `display_for(session_id)`, async `attach(session_id) -> int|None`, sync `detach(session_id)`, sync `metrics() -> dict`, async `launch_browser(session_id, url) -> bool`.
- Produces: `register_x11(app, x11)` registering WS route `GET /vnc?session=…`, RPC `x11_metrics`, RPC `x11_open_browser`. Close code `4404` (no display). `x11_open_browser` returns `{"ok": bool, "reason": str|None}` where reason ∈ {`"no_display"`, `"cap"`, `None`}.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_x11_rpc.py
"""Daemon-side x11 surface: /vnc WS close semantics + RPC handlers."""
import asyncio
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.ipc.daemon import create_daemon_app
from server.ipc.x11_rpc import register_x11


class _Spec:  # truthy stand-in for a DisplaySpec
    pass


class _FakeX11:
    def __init__(self, *, has_display, port=None, launch_ok=True):
        self._has = has_display
        self._port = port
        self._launch_ok = launch_ok
        self.attached = 0
        self.detached = 0
        self.launched = []

    def display_for(self, sid):
        return _Spec() if self._has else None

    async def attach(self, sid):
        self.attached += 1
        return self._port

    def detach(self, sid):
        self.detached += 1

    def metrics(self):
        return {"displays_allocated": 1, "vnc_clients_attached": self.attached}

    async def launch_browser(self, sid, url):
        self.launched.append((sid, url))
        return self._launch_ok


def _app(x11):
    app = create_daemon_app()
    register_x11(app, x11)
    return app


def test_vnc_closes_4404_when_no_display():
    x11 = _FakeX11(has_display=False)
    with TestClient(_app(x11)) as c:
        with c.websocket_connect("/vnc?session=S") as ws:
            with pytest.raises(WebSocketDisconnect) as ei:
                ws.receive_bytes()
            assert ei.value.code == 4404
    assert x11.attached == 0


def test_vnc_relays_to_local_rfb_and_detaches():
    # A real loopback "RFB" echo server stands in for x11vnc.
    async def _run():
        async def handle(reader, writer):
            data = await reader.read(16)
            writer.write(b"SRV:" + data)
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        return server, port

    loop = asyncio.new_event_loop()
    server, port = loop.run_until_complete(_run())
    x11 = _FakeX11(has_display=True, port=port)
    try:
        with TestClient(_app(x11)) as c:
            with c.websocket_connect("/vnc?session=S") as ws:
                ws.send_bytes(b"HELLO")
                assert ws.receive_bytes() == b"SRV:HELLO"
        assert x11.attached == 1 and x11.detached == 1
    finally:
        server.close()
        loop.run_until_complete(server.wait_closed())
        loop.close()


@pytest.mark.asyncio
async def test_x11_metrics_rpc_returns_manager_metrics():
    x11 = _FakeX11(has_display=True)
    app = _app(x11)
    result = await app.state.rpc["x11_metrics"]({})
    assert result["displays_allocated"] == 1


@pytest.mark.asyncio
async def test_x11_open_browser_rpc():
    x11 = _FakeX11(has_display=True, launch_ok=True)
    app = _app(x11)
    ok = await app.state.rpc["x11_open_browser"]({"session_id": "S", "url": "https://x"})
    assert ok == {"ok": True, "reason": None}
    assert x11.launched == [("S", "https://x")]

    no = await _app(_FakeX11(has_display=False)).state.rpc["x11_open_browser"]({"session_id": "S"})
    assert no == {"ok": False, "reason": "no_display"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 $VENV -m pytest server/tests/test_x11_rpc.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.ipc.x11_rpc'`.

- [ ] **Step 3: Create `server/ipc/x11_rpc.py`**

```python
"""Daemon-side x11 surface: a /vnc WebSocket relaying to the daemon-local x11vnc,
plus x11 RPCs. Mirrors orchestrator_rpc.py — x11 knowledge stays out of the generic
transport (daemon.py). The edge reaches all of this over the IPC transport, so the
VNC path never assumes the edge and daemon share 127.0.0.1.
"""
import asyncio
import logging

from fastapi import WebSocket

from server.bridge.vnc_ws import _relay, _StreamAdapter
from server.ipc.daemon import register_rpc

log = logging.getLogger("server.ipc.x11")

CLOSE_NO_DISPLAY = 4404


async def _connect_rfb(port: int, attempts: int = 20, interval: float = 0.05):
    """Connect to the daemon-local x11vnc, tolerating the spawn→bind race: ensure_vnc
    spawns x11vnc and returns immediately, so the RFB port may not be bound yet."""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return await asyncio.open_connection("127.0.0.1", port)
        except OSError as exc:
            last = exc
            await asyncio.sleep(interval)
    assert last is not None
    raise last


def register_x11(app, x11) -> None:
    """Register the daemon /vnc WS endpoint and x11 RPCs against `x11` (may be None)."""

    @app.websocket("/vnc")
    async def vnc(ws: WebSocket) -> None:
        session_id = ws.query_params.get("session", "")
        if x11 is None or x11.display_for(session_id) is None:
            await ws.accept()
            await ws.close(CLOSE_NO_DISPLAY, "no display")
            return
        await ws.accept()
        port = await x11.attach(session_id)
        try:
            reader, writer = await _connect_rfb(port)
            await _relay(ws, _StreamAdapter(reader, writer))
        except Exception:
            log.exception("daemon vnc: relay failed session=%s", session_id)
        finally:
            x11.detach(session_id)

    async def x11_metrics(params: dict) -> dict:
        return x11.metrics() if x11 is not None else {"enabled": False}

    async def x11_open_browser(params: dict) -> dict:
        session_id = params["session_id"]
        if x11 is None or x11.display_for(session_id) is None:
            return {"ok": False, "reason": "no_display"}
        ok = await x11.launch_browser(session_id, params.get("url"))
        return {"ok": bool(ok), "reason": None if ok else "cap"}

    register_rpc(app, "x11_metrics", x11_metrics)
    register_rpc(app, "x11_open_browser", x11_open_browser)
```

- [ ] **Step 4: Wire into `build_daemon` in `server/ipc/serve.py`**

Add the import near the other ipc imports:

```python
from server.ipc.x11_rpc import register_x11
```

In `build_daemon`, after `register_orchestrator_rpc(app, orchestrator)`:

```python
    app.state.x11 = orchestrator._x11
    register_x11(app, app.state.x11)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 $VENV -m pytest server/tests/test_x11_rpc.py -q`
Expected: PASS (4 passed).

- [ ] **Step 6: Commit**

```bash
git add server/ipc/x11_rpc.py server/ipc/serve.py server/tests/test_x11_rpc.py
git commit -m "feat(daemon): /vnc WS relay + x11_metrics/x11_open_browser RPCs

Daemon owns the displays; expose the RFB relay and x11 ops over the IPC transport.
Includes a bounded connect retry that also fixes the x11vnc spawn→bind race.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `RemoteOrchestrator` x11 facade methods

**Files:**
- Modify: `server/sessions/remote.py`
- Test: `server/tests/test_remote_orchestrator.py` (append) — if absent, create `server/tests/test_remote_orchestrator_x11.py` with the same content.

**Interfaces:**
- Consumes: `self._client.call(method, **params)` (the `DaemonClient`).
- Produces: `async RemoteOrchestrator.x11_metrics() -> dict`, `async RemoteOrchestrator.x11_open_browser(session_id, url=None) -> dict`.

- [ ] **Step 1: Write the failing test** (create `server/tests/test_remote_orchestrator_x11.py`)

```python
"""RemoteOrchestrator x11 facade methods proxy straight to daemon RPCs."""
import pytest
from server.sessions.remote import RemoteOrchestrator


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def call(self, method, **params):
        self.calls.append((method, params))
        if method == "x11_metrics":
            return {"displays_allocated": 2}
        if method == "x11_open_browser":
            return {"ok": True, "reason": None}
        raise AssertionError(method)


@pytest.mark.asyncio
async def test_x11_metrics_proxies():
    c = _FakeClient()
    orch = RemoteOrchestrator(c)
    assert (await orch.x11_metrics())["displays_allocated"] == 2
    assert c.calls == [("x11_metrics", {})]


@pytest.mark.asyncio
async def test_x11_open_browser_proxies():
    c = _FakeClient()
    orch = RemoteOrchestrator(c)
    res = await orch.x11_open_browser("S", "https://x")
    assert res == {"ok": True, "reason": None}
    assert c.calls == [("x11_open_browser", {"session_id": "S", "url": "https://x"})]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 $VENV -m pytest server/tests/test_remote_orchestrator_x11.py -q`
Expected: FAIL with `AttributeError: 'RemoteOrchestrator' object has no attribute 'x11_metrics'`.

- [ ] **Step 3: Add the methods to `RemoteOrchestrator` in `server/sessions/remote.py`** (place near `pr_for`/other facade methods)

```python
    async def x11_metrics(self) -> dict:
        return await self._client.call("x11_metrics")

    async def x11_open_browser(self, session_id: str, url: str | None = None) -> dict:
        return await self._client.call("x11_open_browser", session_id=session_id, url=url)
```

- [ ] **Step 4: Run test to verify it passes**

Run: same as Step 2. Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/remote.py server/tests/test_remote_orchestrator_x11.py
git commit -m "feat(remote): RemoteOrchestrator.x11_metrics/x11_open_browser facade

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Edge wiring — remote proxy + facade-routed endpoints

**Files:**
- Modify: `server/bridge/vnc_ws.py` (`build_vnc_ws_router` signature + remote branch)
- Modify: `server/app.py` (remote mode: `x11=None`, `daemon_socket`, skip sweep; router wiring; metrics/open-browser/healthz via facade)
- Test: `server/tests/test_vnc_ws_remote.py` (create)

**Interfaces:**
- Consumes: `_WSAdapter`, `_relay` (Task 1); `RemoteOrchestrator.x11_metrics/x11_open_browser` (Task 3); `app.state.daemon_socket: str | None`; `app.state.remote_mode: bool`; `app.state.sessions` (facade).
- Produces: `build_vnc_ws_router(get_x11, get_daemon_sock)`; `/ws/vnc` proxies to daemon `/vnc` when `get_daemon_sock()` is truthy, propagating a `4xxx` daemon close code to the browser; `/api/x11/metrics`, `/api/sessions/{id}/browser/open`, healthz `vncAttached` work in remote mode and degrade (no 500) when the daemon is unavailable.

- [ ] **Step 1: Write the failing test** (create `server/tests/test_vnc_ws_remote.py`)

```python
"""Edge /ws/vnc proxies to the daemon /vnc over the IPC socket in remote mode."""
import asyncio
import threading
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.ipc.daemon import create_daemon_app
from server.ipc.x11_rpc import register_x11
from server.tests.test_x11_rpc import _FakeX11  # reuse the fake


class _DaemonOnUDS:
    """Run a bare daemon app (x11 only) on a real UDS in a background loop."""
    def __init__(self, x11):
        self._x11 = x11
        self.sock = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        import uvicorn, tempfile, os
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.sock = os.path.join(tempfile.mkdtemp(), "d.sock")
        app = create_daemon_app()
        register_x11(app, self._x11)

        async def _serve():
            config = uvicorn.Config(app, uds=self.sock, log_level="warning")
            server = uvicorn.Server(config)
            t = loop.create_task(server.serve())
            while not server.started:
                await asyncio.sleep(0.02)
            self._ready.set()
            while not self._stop.is_set():
                await asyncio.sleep(0.05)
            server.should_exit = True
            await t
        loop.run_until_complete(_serve())
        loop.close()

    def start(self):
        self._t.start()
        assert self._ready.wait(5)

    def stop(self):
        self._stop.set()
        self._t.join(5)


def _edge_router(get_sock):
    from fastapi import FastAPI
    from server.bridge.vnc_ws import build_vnc_ws_router
    app = FastAPI()
    # No auth dependency override needed: build the router with a permissive identity.
    app.dependency_overrides = {}
    app.include_router(build_vnc_ws_router(lambda: None, get_sock))
    return app


@pytest.fixture()
def echo_daemon():
    async def _mk():
        async def handle(reader, writer):
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(b"D:" + data)
                await writer.drain()
            writer.close()
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        return server, server.sockets[0].getsockname()[1]
    loop = asyncio.new_event_loop()
    server, port = loop.run_until_complete(_mk())
    yield port
    server.close()
    loop.run_until_complete(server.wait_closed())
    loop.close()
```

> Auth note: `/ws/vnc` depends on `require_ws_identity`. In this focused router test we bypass app-level auth by overriding that dependency. Add at the top of `_edge_router`:
> ```python
> from server.auth.middleware import require_ws_identity
> app.dependency_overrides[require_ws_identity] = lambda: "test@example.com"
> ```

```python
def test_edge_proxies_relay_and_4404(echo_daemon):
    # Display present → bytes round-trip through edge→daemon→local RFB echo.
    x11 = _FakeX11(has_display=True, port=echo_daemon)
    d = _DaemonOnUDS(x11)
    d.start()
    try:
        app = _edge_router(lambda: d.sock)
        with TestClient(app, raise_server_exceptions=False) as c:
            with c.websocket_connect("/ws/vnc?session=S") as ws:
                ws.send_bytes(b"PING")
                assert ws.receive_bytes() == b"D:PING"
    finally:
        d.stop()

    # No display → edge surfaces the daemon's 4404 close to the browser.
    d2 = _DaemonOnUDS(_FakeX11(has_display=False))
    d2.start()
    try:
        app = _edge_router(lambda: d2.sock)
        with TestClient(app, raise_server_exceptions=False) as c:
            with c.websocket_connect("/ws/vnc?session=S") as ws:
                with pytest.raises(WebSocketDisconnect) as ei:
                    ws.receive_bytes()
                assert ei.value.code == 4404
    finally:
        d2.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 $VENV -m pytest server/tests/test_vnc_ws_remote.py -q`
Expected: FAIL — `build_vnc_ws_router()` takes 1 positional arg (the new `get_daemon_sock` param doesn't exist yet).

- [ ] **Step 3: Update `build_vnc_ws_router` in `server/bridge/vnc_ws.py`**

Change the signature and add the remote branch. Replace the existing `build_vnc_ws_router` with:

```python
def build_vnc_ws_router(get_x11, get_daemon_sock=lambda: None) -> APIRouter:
    """Return an APIRouter with /ws/vnc.

    Remote mode (get_daemon_sock() truthy): proxy the browser WS to the daemon's
    /vnc over the IPC socket, propagating a 4xxx daemon close code. In-process mode
    (default): relay straight to the local x11vnc."""
    router = APIRouter()

    @router.websocket("/ws/vnc")
    async def vnc_ws(
        websocket: WebSocket,
        identity: str = Depends(require_ws_identity),
    ) -> None:
        session_id = websocket.query_params.get("session", "")
        sock = get_daemon_sock()
        if sock:
            import websockets
            from websockets.exceptions import ConnectionClosed
            await websocket.accept()
            close_code = 1000
            try:
                uri = f"ws://daemon/vnc?session={session_id}"
                async with websockets.unix_connect(sock, uri) as dws:
                    await _relay(websocket, _WSAdapter(dws))
                    close_code = dws.close_code or 1000
            except ConnectionClosed as exc:
                close_code = exc.code or 1000
            except Exception:
                log.exception("vnc: remote proxy failed session=%s", session_id)
            if 4000 <= close_code < 5000:
                try:
                    await websocket.close(close_code)
                except Exception:
                    pass
            return

        x11 = get_x11()
        if x11 is None:
            await websocket.accept()
            await websocket.close(CLOSE_NOT_ENABLED, "xvfb disabled")
            return
        if x11.display_for(session_id) is None:
            await websocket.accept()
            await websocket.close(CLOSE_NO_DISPLAY, "no display")
            return
        await websocket.accept()
        port = await x11.attach(session_id)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await _relay(websocket, _StreamAdapter(reader, writer))
        except Exception:
            log.exception("vnc: relay failed session=%s", session_id)
        finally:
            x11.detach(session_id)

    return router
```

- [ ] **Step 4: Run the focused test to verify it passes**

Run: same as Step 2. Expected: PASS (1 passed).

- [ ] **Step 5: Wire the edge app in `server/app.py`**

(a) In the remote-mode branch (where `app.state.remote_mode = True` is set, ~line 214), store the socket path:

```python
        app.state.daemon_socket = socket_path
```

and in the in-process `else` branch set `app.state.daemon_socket = None`.

(b) Replace the x11 build block (~line 226–231) so remote mode does not build a local manager:

```python
    from server.sessions.x11manager import build_x11_manager
    if getattr(app.state, "remote_mode", False):
        app.state.x11 = None          # daemon owns the displays; edge proxies over IPC
    else:
        app.state.x11 = build_x11_manager(_gs())
        if orchestrator is not None and app.state.x11 is not None:
            orchestrator._x11 = app.state.x11
```

(c) The periodic `sweep_stale` loop (~line 143) already guards on `app.state.x11 is not None`, so with `x11=None` in remote mode it self-skips. Leave it.

(d) Update the router include (~line 304):

```python
    app.include_router(build_vnc_ws_router(
        lambda: getattr(app.state, "x11", None),
        lambda: getattr(app.state, "daemon_socket", None),
    ))
```

(e) `/api/x11/metrics` (~line 306): route through the facade in remote mode.

```python
    @app.get("/api/x11/metrics")
    async def x11_metrics(_identity: str = Depends(current_identity)) -> dict:
        if getattr(app.state, "remote_mode", False):
            try:
                return await app.state.sessions.x11_metrics()
            except DaemonUnavailable:
                return {"enabled": False}
        x11 = getattr(app.state, "x11", None)
        return x11.metrics() if x11 else {"enabled": False}
```

(f) `/api/sessions/{id}/browser/open` (~line 313): route through the facade in remote mode.

```python
    @app.post("/api/sessions/{session_id}/browser/open")
    async def open_browser(
        session_id: str,
        payload: dict = Body(default={}),
        _identity: str = Depends(current_identity),
    ) -> dict:
        if getattr(app.state, "remote_mode", False):
            res = await app.state.sessions.x11_open_browser(session_id, payload.get("url"))
            if not res.get("ok"):
                detail = "no display for session" if res.get("reason") == "no_display" else "browser cap reached"
                raise HTTPException(status_code=409, detail=detail)
            return {"ok": True}
        x11 = getattr(app.state, "x11", None)
        if x11 is None or x11.display_for(session_id) is None:
            raise HTTPException(status_code=409, detail="no display for session")
        ok = await x11.launch_browser(session_id, payload.get("url"))
        if not ok:
            raise HTTPException(status_code=409, detail="browser cap reached")
        return {"ok": True}
```

(g) healthz `vncAttached` (~line 271–272): in remote mode derive from the metrics RPC, degrade to False.

```python
        if app.state.remote_mode:
            try:
                m = await app.state.sessions.x11_metrics()
                vnc_attached = bool(m.get("vnc_clients_attached", 0) > 0)
            except DaemonUnavailable:
                vnc_attached = False
        else:
            x11 = getattr(app.state, "x11", None)
            vnc_attached = bool(x11 and x11.metrics().get("vnc_clients_attached", 0) > 0)
```

(Remove the old unconditional `x11 = …; vnc_attached = …` two lines.)

- [ ] **Step 6: Run the edge regression + new tests**

Run: `PYTHONPATH=$PWD/src env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 $VENV -m pytest server/tests/test_vnc_ws_remote.py server/tests/test_vnc_ws_mounted.py server/tests/test_healthz_vnc.py server/tests/test_remote_mode_edge.py -q`
Expected: PASS (all). If `test_healthz_vnc.py` builds the app in in-process mode it must still pass (x11 path unchanged).

- [ ] **Step 7: Commit**

```bash
git add server/bridge/vnc_ws.py server/app.py server/tests/test_vnc_ws_remote.py
git commit -m "feat(edge): proxy /ws/vnc + x11 endpoints to the daemon in remote mode

Remote mode no longer builds an empty local X11Manager; /ws/vnc proxies to the
daemon /vnc over the IPC socket (propagating 4404), and metrics/open-browser/healthz
route through the RemoteOrchestrator facade. In-process mode unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: End-to-end remote VNC test + full verification

**Files:**
- Modify: `server/tests/test_remote_mode_edge.py` (teach `_DaemonThread` to register x11; add e2e test)

**Interfaces:**
- Consumes: the `remote_edge` fixture (real daemon on UDS + edge `TestClient`); `register_x11`; `_FakeX11` from `test_x11_rpc`.
- Produces: a test proving the **full** production path: browser WS → edge `/ws/vnc` → daemon `/vnc` → local RFB → back.

- [ ] **Step 1: Teach `_DaemonThread` to register x11** — in `server/tests/test_remote_mode_edge.py`, in `_DaemonThread._run`'s `_serve()`, right after `register_orchestrator_rpc(dapp, self._orch)`:

```python
            from server.ipc.x11_rpc import register_x11
            register_x11(dapp, getattr(self._orch, "_x11", None))
```

(Safe for existing tests: with `_x11` absent/None the endpoint just 4404s and no existing test hits `/vnc`.)

- [ ] **Step 2: Write the failing e2e test** (append to `test_remote_mode_edge.py`)

```python
def test_vnc_end_to_end_remote(tmp_path):
    """browser → edge /ws/vnc → daemon /vnc → local RFB echo → browser, in real remote mode."""
    import asyncio as _a
    from server.tests.test_x11_rpc import _FakeX11

    # Local RFB echo server in this (main) thread's loop is awkward; run one in the
    # daemon thread by attaching a _FakeX11 whose port points at an echo server the
    # daemon loop owns. Simplest: start the echo server before the daemon and pass its port.
    loop = _a.new_event_loop()

    async def _mk():
        async def handle(r, w):
            data = await r.read(16)
            w.write(b"E:" + data)
            await w.drain()
            w.close()
        srv = await _a.start_server(handle, "127.0.0.1", 0)
        return srv, srv.sockets[0].getsockname()[1]

    srv, port = loop.run_until_complete(_mk())

    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    orch._x11 = _FakeX11(has_display=True, port=port)
    daemon = _DaemonThread(orch)
    daemon.start()
    env = {
        "CCSWITCH_SESSIOND_SOCKET": daemon.sock,
        "CCSWITCH_DB_URL": f"sqlite+aiosqlite:///{tmp_path / 'edge.db'}",
        "CCSWITCH_TAILNET_ALLOWLIST": _IDENT["Tailscale-User-Login"],
    }
    try:
        with mock.patch.dict(os.environ, env):
            app = create_app()
            assert app.state.x11 is None  # remote mode: no local manager
            with TestClient(app, raise_server_exceptions=False) as client:
                with client.websocket_connect("/ws/vnc?session=S", headers=_IDENT) as ws:
                    ws.send_bytes(b"HI")
                    assert ws.receive_bytes() == b"E:HI"
                # metrics now flow over RPC
                m = client.get("/api/x11/metrics", headers=_IDENT)
                assert m.status_code == 200 and m.json().get("displays_allocated") == 1
    finally:
        daemon.stop()
        srv.close()
        loop.run_until_complete(srv.wait_closed())
        loop.close()
```

> Note: the echo server runs in `loop` (main thread) but is reached via `127.0.0.1:port` from the daemon thread — a real TCP connection, exactly like x11vnc. Keep `loop` alive (don't close until teardown). If the cross-loop timing proves flaky, instead start the echo server inside `_DaemonThread` by extending it to accept an optional `rfb_port` coroutine; prefer the simple form first.

- [ ] **Step 3: Run to verify it fails, then passes** after Step 1 is in place.

Run: `PYTHONPATH=$PWD/src env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 $VENV -m pytest server/tests/test_remote_mode_edge.py -q`
Expected: the new test PASSES; all pre-existing tests in the file still PASS.

- [ ] **Step 4: Full backend verification**

```bash
VENV=/mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python
PYTHONPATH=$PWD/src env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 $VENV -m pytest server/tests/ -q
$VENV -m ruff check server/
$VENV -m mypy server/bridge/vnc_ws.py server/ipc/x11_rpc.py server/ipc/serve.py server/sessions/remote.py server/app.py
```
Expected: pytest green (pre-existing env-only failures noted in memory `ccswitch-worktree-backend-pythonpath` — `remote_mode_wiring`, `x11` — are environmental, not regressions; confirm the set of failures didn't grow). ruff clean. mypy clean (or only pre-existing notes).

- [ ] **Step 5: Commit**

```bash
git add server/tests/test_remote_mode_edge.py
git commit -m "test(vnc): end-to-end remote browser→edge→daemon→RFB relay

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- VNC relay bridged → Tasks 2 (daemon `/vnc`), 4 (edge proxy), 5 (e2e). ✓
- `/api/x11/metrics` bridged → Task 4(e), facade Task 3, daemon RPC Task 2. ✓
- `/api/sessions/{id}/browser/open` bridged → Task 4(f), Task 3, Task 2. ✓
- healthz `vncAttached` bridged → Task 4(g). ✓
- Single relay implementation (no parallel impl) → `_WSAdapter` + `_StreamAdapter` both feed `_relay`. ✓
- Connect-race fix → Task 2 `_connect_rfb`. ✓
- Remote mode stops building empty local manager + skips sweep → Task 4(b)(c). ✓
- Cross-host-ready (rides IPC transport, no edge-side 127.0.0.1) → Tasks 2/4 by construction. ✓
- In-process mode unchanged → Task 4 keeps the original branch; existing tests in Task 4 Step 6. ✓

**Placeholder scan:** none — every code step has full code; commands have expected output.

**Type consistency:** `display_for` sync, `attach` async→`int|None`, `detach` sync, `metrics` sync→dict, `launch_browser` async→bool used identically in `_FakeX11`, `x11_rpc.py`, and the edge in-process branch. `x11_open_browser` returns `{"ok","reason"}` consistently in daemon (Task 2), facade (Task 3), and edge consumer (Task 4f). `build_vnc_ws_router(get_x11, get_daemon_sock)` signature matches its one caller (Task 4d) and both tests.

**Scope:** single subsystem (the bridge). Project B (TCP/TLS/auth) and the SIGKILL-shutdown fix are explicitly separate, filed as issues.
