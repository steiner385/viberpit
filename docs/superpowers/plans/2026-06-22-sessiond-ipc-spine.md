# Session daemon A1 — IPC spine + event streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reusable edge↔daemon transport (unary RPC + multiplexed event streaming + auto-reconnect) over a Unix-domain socket, proven by a throwaway demo handler — no orchestrator logic.

**Architecture:** The daemon is a uvicorn-served FastAPI app bound to a Unix socket: unary RPC via `POST /rpc/{method}` against a handler registry, and a single multiplexed `GET /events` WebSocket fanning stream-id-tagged event frames. The edge `DaemonClient` calls RPC over httpx-UDS and consumes events over one shared websockets-UDS connection that auto-reconnects and re-subscribes.

**Tech Stack:** Python 3.12, FastAPI/Starlette, uvicorn (UDS bind), httpx (UDS transport), websockets (`unix_connect`), pytest + pytest-asyncio.

## Global Constraints

- Tests run via: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest <path> -q -p no:cacheprovider`.
- No new dependencies — uvicorn, httpx, websockets, fastapi are already in the tree.
- pytest-asyncio is **strict mode**: every async test needs `@pytest.mark.asyncio`.
- New code lives under `server/ipc/`. The daemon binds **only** a Unix socket (never a TCP port). Test sockets live under a `tempfile.mkdtemp()` dir (keeps the path under the ~108-char UDS limit).
- Event payloads are plain JSON dicts (the existing `to_wire()` shape) — no re-encoding layer.
- All four `server/ipc/*.py` files must pass `ruff check` and `mypy --ignore-missing-imports`.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: `protocol.py` — frame builders + constants

**Files:**
- Create: `server/ipc/__init__.py` (empty)
- Create: `server/ipc/protocol.py`
- Test: `server/tests/test_ipc_protocol.py`

**Interfaces:**
- Produces:
  - `rpc_response_ok(req_id: str, result: dict) -> dict`
  - `rpc_response_err(req_id: str, err_type: str, message: str) -> dict`
  - `event_frame(stream: str, event: dict) -> dict`
  - `subscribe_frame(stream: str) -> dict` / `unsubscribe_frame(stream: str) -> dict`
  - constants `EVENT = "event"`, `SUBSCRIBE = "subscribe"`, `UNSUBSCRIBE = "unsubscribe"`

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_ipc_protocol.py`:

```python
from server.ipc import protocol as p


def test_rpc_response_ok_shape():
    assert p.rpc_response_ok("id1", {"pong": True}) == {"id": "id1", "result": {"pong": True}}


def test_rpc_response_err_shape():
    assert p.rpc_response_err("id2", "ValueError", "bad") == {
        "id": "id2", "error": {"type": "ValueError", "message": "bad"}}


def test_event_frame_shape():
    assert p.event_frame("s1", {"type": "tick", "i": 0}) == {
        "type": "event", "stream": "s1", "event": {"type": "tick", "i": 0}}


def test_subscribe_unsubscribe_frames():
    assert p.subscribe_frame("s1") == {"type": "subscribe", "stream": "s1"}
    assert p.unsubscribe_frame("s1") == {"type": "unsubscribe", "stream": "s1"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_protocol.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.ipc'`.

- [ ] **Step 3: Write minimal implementation**

Create `server/ipc/__init__.py` (empty file). Create `server/ipc/protocol.py`:

```python
"""Wire envelopes for the edge↔daemon Unix-socket transport. Pure dict builders, no I/O."""

EVENT = "event"
SUBSCRIBE = "subscribe"
UNSUBSCRIBE = "unsubscribe"


def rpc_response_ok(req_id: str, result: dict) -> dict:
    return {"id": req_id, "result": result}


def rpc_response_err(req_id: str, err_type: str, message: str) -> dict:
    return {"id": req_id, "error": {"type": err_type, "message": message}}


def event_frame(stream: str, event: dict) -> dict:
    return {"type": EVENT, "stream": stream, "event": event}


def subscribe_frame(stream: str) -> dict:
    return {"type": SUBSCRIBE, "stream": stream}


def unsubscribe_frame(stream: str) -> dict:
    return {"type": UNSUBSCRIBE, "stream": stream}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_protocol.py -q -p no:cacheprovider`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add server/ipc/__init__.py server/ipc/protocol.py server/tests/test_ipc_protocol.py
git commit -m "feat(ipc): protocol envelopes for the daemon transport

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `daemon.py` — RPC registry + `/rpc/{method}` + `ping`

**Files:**
- Create: `server/ipc/daemon.py`
- Test: `server/tests/test_ipc_daemon_rpc.py`

**Interfaces:**
- Consumes: `server.ipc.protocol` (Task 1).
- Produces:
  - `create_daemon_app() -> fastapi.FastAPI`
  - `register_rpc(app, name: str, fn)` — `fn` is `async def fn(params: dict) -> dict`
  - the app serves `POST /rpc/{method}` → `{"result": ...}` (200) or `{"error": {...}}` (500)
  - a built-in `ping` RPC returning `{"pong": True}`

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_ipc_daemon_rpc.py`:

```python
from fastapi.testclient import TestClient
from server.ipc.daemon import create_daemon_app, register_rpc


def test_ping_rpc():
    app = create_daemon_app()
    with TestClient(app) as c:
        r = c.post("/rpc/ping", json={})
    assert r.status_code == 200
    assert r.json() == {"result": {"pong": True}}


def test_unknown_method_returns_error_envelope():
    app = create_daemon_app()
    with TestClient(app) as c:
        r = c.post("/rpc/nope", json={})
    assert r.status_code == 404
    assert r.json()["error"]["type"] == "UnknownMethod"


def test_handler_exception_returns_error_envelope():
    app = create_daemon_app()

    async def boom(params):
        raise ValueError("kaboom")

    register_rpc(app, "boom", boom)
    with TestClient(app) as c:
        r = c.post("/rpc/boom", json={})
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["type"] == "ValueError"
    assert "kaboom" in body["error"]["message"]


def test_registered_handler_receives_params():
    app = create_daemon_app()

    async def add(params):
        return {"sum": params["a"] + params["b"]}

    register_rpc(app, "add", add)
    with TestClient(app) as c:
        r = c.post("/rpc/add", json={"a": 2, "b": 3})
    assert r.json() == {"result": {"sum": 5}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_daemon_rpc.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.ipc.daemon'`.

- [ ] **Step 3: Write minimal implementation**

Create `server/ipc/daemon.py`:

```python
"""The session-daemon ASGI app: unary RPC registry + (Task 3) multiplexed event WS.

Served by uvicorn bound to a Unix socket. No TCP. No app-level auth — the socket's
filesystem permissions are the trust boundary.
"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def create_daemon_app() -> FastAPI:
    app = FastAPI(title="ccswitch-sessiond")
    app.state.rpc = {}

    @app.post("/rpc/{method}")
    async def rpc(method: str, request: Request) -> JSONResponse:
        fn = app.state.rpc.get(method)
        if fn is None:
            return JSONResponse(
                {"error": {"type": "UnknownMethod", "message": method}}, status_code=404)
        params = await request.json() if await request.body() else {}
        try:
            result = await fn(params)
            return JSONResponse({"result": result})
        except Exception as exc:  # noqa: BLE001 — surface handler errors as an envelope
            return JSONResponse(
                {"error": {"type": type(exc).__name__, "message": str(exc)}}, status_code=500)

    register_rpc(app, "ping", _ping)
    return app


async def _ping(params: dict) -> dict:
    return {"pong": True}


def register_rpc(app: FastAPI, name: str, fn) -> None:
    """Register an `async def fn(params: dict) -> dict` handler under `name`."""
    app.state.rpc[name] = fn
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_daemon_rpc.py -q -p no:cacheprovider`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add server/ipc/daemon.py server/tests/test_ipc_daemon_rpc.py
git commit -m "feat(ipc): daemon RPC registry + ping

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `daemon.py` — `/events` WebSocket + `publish()` + demo producers

**Files:**
- Modify: `server/ipc/daemon.py`
- Test: `server/tests/test_ipc_daemon_events.py`

**Interfaces:**
- Consumes: `server.ipc.protocol` (Task 1), `create_daemon_app`/`register_rpc` (Task 2).
- Produces:
  - `GET /events` WebSocket: accepts `{"type":"subscribe","stream":s}` / `unsubscribe`; receives `{"type":"event","stream":s,"event":{...}}` frames for subscribed streams.
  - `app.state.publish(stream: str, event: dict)` — async; fans an event to every connection subscribed to `stream`.
  - demo RPCs: `start_demo` (`params={"stream":s,"n":k}` → publishes `k` `{"type":"tick","i":j}` events) and `echo` (`params={"stream":s,"text":t}` → publishes `{"type":"echo","text":t}`).

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_ipc_daemon_events.py`:

```python
import json
from fastapi.testclient import TestClient
from server.ipc.daemon import create_daemon_app


def test_subscribe_receives_published_events():
    app = create_daemon_app()
    with TestClient(app) as c:
        with c.websocket_connect("/events") as ws:
            ws.send_text(json.dumps({"type": "subscribe", "stream": "s1"}))
            c.post("/rpc/start_demo", json={"stream": "s1", "n": 3})
            got = [json.loads(ws.receive_text()) for _ in range(3)]
    assert [f["event"]["i"] for f in got] == [0, 1, 2]
    assert all(f["type"] == "event" and f["stream"] == "s1" for f in got)


def test_echo_round_trips_to_subscriber():
    app = create_daemon_app()
    with TestClient(app) as c:
        with c.websocket_connect("/events") as ws:
            ws.send_text(json.dumps({"type": "subscribe", "stream": "s2"}))
            c.post("/rpc/echo", json={"stream": "s2", "text": "hi"})
            frame = json.loads(ws.receive_text())
    assert frame == {"type": "event", "stream": "s2", "event": {"type": "echo", "text": "hi"}}


def test_unsubscribed_stream_not_delivered():
    app = create_daemon_app()
    with TestClient(app) as c:
        with c.websocket_connect("/events") as ws:
            ws.send_text(json.dumps({"type": "subscribe", "stream": "mine"}))
            c.post("/rpc/echo", json={"stream": "other", "text": "x"})
            c.post("/rpc/echo", json={"stream": "mine", "text": "y"})
            frame = json.loads(ws.receive_text())
    # First (and only) delivered frame is for "mine", proving "other" was filtered.
    assert frame["stream"] == "mine" and frame["event"]["text"] == "y"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_daemon_events.py -q -p no:cacheprovider`
Expected: FAIL — no `/events` route (`websocket_connect` raises / 403).

- [ ] **Step 3: Write minimal implementation**

In `server/ipc/daemon.py`, update the imports at the **top** of the file (replace the existing `from fastapi import FastAPI, Request` line — do not duplicate it; all imports stay at module top to satisfy ruff E402):

```python
import json
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from server.ipc import protocol as proto
```

In `create_daemon_app()`, after `app.state.rpc = {}` and before the `/rpc` route, add the subscriber registry and publish fn:

```python
    # stream_id -> set of connected WebSockets subscribed to it
    app.state.subscribers = {}

    async def publish(stream: str, event: dict) -> None:
        frame = proto.event_frame(stream, event)
        for ws in list(app.state.subscribers.get(stream, ())):
            try:
                await ws.send_text(_dumps(frame))
            except Exception:  # noqa: BLE001 — drop a dead/slow connection, keep publishing
                app.state.subscribers.get(stream, set()).discard(ws)

    app.state.publish = publish
```

Add the WS route and demo RPCs inside `create_daemon_app()` (before `return app`):

```python
    @app.websocket("/events")
    async def events(ws: WebSocket) -> None:
        await ws.accept()
        mine: set[str] = set()
        try:
            while True:
                frame = _loads(await ws.receive_text())
                stream = frame.get("stream")
                if frame.get("type") == proto.SUBSCRIBE and stream:
                    app.state.subscribers.setdefault(stream, set()).add(ws)
                    mine.add(stream)
                elif frame.get("type") == proto.UNSUBSCRIBE and stream:
                    app.state.subscribers.get(stream, set()).discard(ws)
                    mine.discard(stream)
        except WebSocketDisconnect:
            pass
        finally:
            for stream in mine:
                app.state.subscribers.get(stream, set()).discard(ws)

    register_rpc(app, "start_demo", _make_start_demo(app))
    register_rpc(app, "echo", _make_echo(app))
```

Add module-level helpers at the bottom of the file (no new imports — `json` is imported at the top):

```python
def _dumps(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _loads(raw: str) -> dict:
    return json.loads(raw)


def _make_start_demo(app: FastAPI):
    async def start_demo(params: dict) -> dict:
        stream, n = params["stream"], int(params["n"])
        for i in range(n):
            await app.state.publish(stream, {"type": "tick", "i": i})
        return {"emitted": n}
    return start_demo


def _make_echo(app: FastAPI):
    async def echo(params: dict) -> dict:
        await app.state.publish(params["stream"], {"type": "echo", "text": params["text"]})
        return {"ok": True}
    return echo
```

Note: `start_demo` publishes synchronously within the RPC call, so by the time `POST /rpc/start_demo` returns, all frames are queued to the subscriber's socket — the test's `receive_text()` loop then drains them.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_daemon_events.py -q -p no:cacheprovider`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add server/ipc/daemon.py server/tests/test_ipc_daemon_events.py
git commit -m "feat(ipc): daemon /events WebSocket + publish fanout + demo producers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: in-process UDS test harness + `DaemonClient.call()` (unary)

**Files:**
- Create: `server/ipc/client.py`
- Create: `server/tests/ipc_harness.py` (a helper, not a test module)
- Test: `server/tests/test_ipc_client_rpc.py`

**Interfaces:**
- Consumes: `create_daemon_app` (Tasks 2–3).
- Produces:
  - `class DaemonClient(socket_path: str)` with `async call(method: str, **params) -> dict`, `async aclose()`.
  - exceptions `DaemonUnavailable(Exception)`, `DaemonError(Exception)` with `.err_type`, `.message`.
  - harness `serve_daemon_uds(app) -> async context manager` yielding `socket_path: str`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/ipc_harness.py`:

```python
"""Run a daemon app on a temp Unix socket via in-process uvicorn, for tests."""
import asyncio
import contextlib
import os
import tempfile

import uvicorn


@contextlib.asynccontextmanager
async def serve_daemon_uds(app):
    tmpdir = tempfile.mkdtemp(prefix="ccsd-")
    sock = os.path.join(tmpdir, "d.sock")
    server = uvicorn.Server(uvicorn.Config(app, uds=sock, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        yield sock
    finally:
        server.should_exit = True
        await task
        with contextlib.suppress(FileNotFoundError):
            os.unlink(sock)
        with contextlib.suppress(OSError):
            os.rmdir(tmpdir)
```

Create `server/tests/test_ipc_client_rpc.py`:

```python
import pytest
from server.ipc.client import DaemonClient, DaemonUnavailable, DaemonError
from server.ipc.daemon import create_daemon_app
from server.tests.ipc_harness import serve_daemon_uds


@pytest.mark.asyncio
async def test_call_ping_over_uds():
    async with serve_daemon_uds(create_daemon_app()) as sock:
        client = DaemonClient(sock)
        try:
            assert await client.call("ping") == {"pong": True}
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_call_unavailable_when_no_daemon():
    client = DaemonClient("/nonexistent/ccsd.sock")
    with pytest.raises(DaemonUnavailable):
        await client.call("ping")
    await client.aclose()


@pytest.mark.asyncio
async def test_call_surfaces_handler_error():
    app = create_daemon_app()
    from server.ipc.daemon import register_rpc

    async def boom(params):
        raise ValueError("nope")

    register_rpc(app, "boom", boom)
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            with pytest.raises(DaemonError) as ei:
                await client.call("boom")
            assert ei.value.err_type == "ValueError"
        finally:
            await client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_client_rpc.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.ipc.client'`.

- [ ] **Step 3: Write minimal implementation**

Create `server/ipc/client.py`:

```python
"""Edge-side client for the session daemon over a Unix socket."""
import httpx


class DaemonUnavailable(Exception):
    """The daemon socket could not be reached."""


class DaemonError(Exception):
    """The daemon returned an error envelope for an RPC call."""

    def __init__(self, err_type: str, message: str):
        super().__init__(f"{err_type}: {message}")
        self.err_type = err_type
        self.message = message


class DaemonClient:
    def __init__(self, socket_path: str):
        self._sock = socket_path
        self._http = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            base_url="http://daemon",
            timeout=30.0,
        )

    async def call(self, method: str, **params) -> dict:
        try:
            resp = await self._http.post(f"/rpc/{method}", json=params)
        except httpx.ConnectError as exc:
            raise DaemonUnavailable(str(exc)) from exc
        body = resp.json()
        if "error" in body:
            err = body["error"]
            raise DaemonError(err.get("type", "Error"), err.get("message", ""))
        return body["result"]

    async def aclose(self) -> None:
        await self._http.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_client_rpc.py -q -p no:cacheprovider`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add server/ipc/client.py server/tests/ipc_harness.py server/tests/test_ipc_client_rpc.py
git commit -m "feat(ipc): DaemonClient.call() over UDS + in-process daemon test harness

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: `DaemonClient.subscribe()` — shared event WS, demux, bounded queue

**Files:**
- Modify: `server/ipc/client.py`
- Test: `server/tests/test_ipc_client_events.py`

**Interfaces:**
- Consumes: `DaemonClient` (Task 4), the `/events` WS (Task 3), `protocol` (Task 1).
- Produces:
  - `DaemonClient.subscribe(stream: str, maxsize: int = 1000) -> AsyncIterator[dict]` — yields event dicts (the `event` payloads) for `stream` from one shared WS connection, lazily opened.
  - on queue overflow: drop the oldest item and log a warning (not raise).

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_ipc_client_events.py`:

```python
import asyncio
import pytest
from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app
from server.tests.ipc_harness import serve_daemon_uds


@pytest.mark.asyncio
async def test_subscribe_receives_demo_ticks():
    async with serve_daemon_uds(create_daemon_app()) as sock:
        client = DaemonClient(sock)
        try:
            stream = client.subscribe("s1")
            await client.events_ready("s1")          # subscription registered on the daemon
            await client.call("start_demo", stream="s1", n=3)
            got = [await asyncio.wait_for(stream.__anext__(), 2) for _ in range(3)]
        finally:
            await client.aclose()
    assert [e["i"] for e in got] == [0, 1, 2]


@pytest.mark.asyncio
async def test_two_streams_are_demuxed():
    async with serve_daemon_uds(create_daemon_app()) as sock:
        client = DaemonClient(sock)
        try:
            a = client.subscribe("a")
            b = client.subscribe("b")
            await client.events_ready("a")
            await client.events_ready("b")
            await client.call("echo", stream="b", text="forb")
            ev = await asyncio.wait_for(b.__anext__(), 2)
        finally:
            await client.aclose()
    assert ev == {"type": "echo", "text": "forb"}


@pytest.mark.asyncio
async def test_subscribe_after_connection_established():
    """A subscribe() issued AFTER the reader is already connected still registers
    (the dynamic-attach path A2 relies on), not only the streams present at connect."""
    async with serve_daemon_uds(create_daemon_app()) as sock:
        client = DaemonClient(sock)
        try:
            first = client.subscribe("first")
            await client.events_ready("first")        # reader is now connected
            later = client.subscribe("later")         # subscribe after connect
            await client.events_ready("later")
            await client.call("echo", stream="later", text="dynamic")
            ev = await asyncio.wait_for(later.__anext__(), 2)
        finally:
            await client.aclose()
    assert ev == {"type": "echo", "text": "dynamic"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_client_events.py -q -p no:cacheprovider`
Expected: FAIL — `AttributeError: 'DaemonClient' object has no attribute 'subscribe'`.

- [ ] **Step 3: Write minimal implementation**

In `server/ipc/client.py`, add imports at top:

```python
import asyncio
import json
import logging

import httpx
import websockets

from server.ipc import protocol as proto

log = logging.getLogger("server.ipc.client")
```

Extend `DaemonClient.__init__` to set up streaming state (add after the `self._http = ...` block):

```python
        self._ws = None
        self._reader_task = None
        self._queues: dict[str, asyncio.Queue] = {}      # stream -> event queue
        self._ready: dict[str, asyncio.Event] = {}        # stream -> subscribed-on-daemon
        self._desired: set[str] = set()                   # streams to (re)subscribe on connect
        self._started = False
```

Add the streaming methods to `DaemonClient`:

```python
    def subscribe(self, stream: str, maxsize: int = 1000):
        self._queues[stream] = asyncio.Queue(maxsize=maxsize)
        self._ready[stream] = asyncio.Event()
        self._desired.add(stream)
        if not self._started:
            self._started = True
            self._reader_task = asyncio.create_task(self._reader())
        elif self._ws is not None:
            # Reader already connected — register this new stream now. (On the first
            # subscribe, or after a reconnect, the reader subscribes all _desired itself.)
            asyncio.create_task(self._send_subscribe(stream))
        return self._iter(stream)

    async def _send_subscribe(self, stream: str) -> None:
        ws = self._ws
        if ws is None:
            return  # a reconnect will re-subscribe all _desired
        try:
            await ws.send(json.dumps(proto.subscribe_frame(stream)))
            self._ready[stream].set()
        except Exception:  # noqa: BLE001 — reader re-subscribes on its next connect
            pass

    async def events_ready(self, stream: str) -> None:
        """Await until the daemon has acknowledged the subscription (test sync point)."""
        await asyncio.wait_for(self._ready[stream].wait(), 5)

    async def _iter(self, stream: str):
        q = self._queues[stream]
        while True:
            yield await q.get()

    def _deliver(self, stream: str, event: dict) -> None:
        q = self._queues.get(stream)
        if q is None:
            return
        if q.full():
            try:
                q.get_nowait()  # drop oldest
            except asyncio.QueueEmpty:
                pass
            log.warning("ipc: subscriber queue for %s full — dropped oldest", stream)
        q.put_nowait(event)

    async def _reader(self) -> None:
        uri = "ws://daemon/events"
        async with websockets.unix_connect(self._sock, uri) as ws:
            self._ws = ws
            for stream in list(self._desired):
                await ws.send(json.dumps(proto.subscribe_frame(stream)))
                self._ready[stream].set()
            async for raw in ws:
                frame = json.loads(raw)
                if frame.get("type") == proto.EVENT:
                    self._deliver(frame["stream"], frame["event"])
```

Update `aclose()` to tear down the reader and WS:

```python
    async def aclose(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._http.aclose()
```

Note: `subscribe()` must be called before `start_demo`/`echo`, and the test awaits `events_ready(stream)` so the daemon has the subscription registered before events are published (otherwise early events are dropped — A1 has no replay; that's A3).

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_client_events.py -q -p no:cacheprovider`
Expected: PASS (3 passed).

- [ ] **Step 5: Add the backpressure unit test**

Append to `server/tests/test_ipc_client_events.py`:

```python
@pytest.mark.asyncio
async def test_deliver_drops_oldest_on_overflow():
    client = DaemonClient("/unused.sock")
    client._queues["s"] = asyncio.Queue(maxsize=2)
    client._deliver("s", {"i": 1})
    client._deliver("s", {"i": 2})
    client._deliver("s", {"i": 3})           # overflow → drop oldest ({"i":1})
    drained = [client._queues["s"].get_nowait() for _ in range(2)]
    assert drained == [{"i": 2}, {"i": 3}]
    await client.aclose()
```

- [ ] **Step 6: Run the file and commit**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_client_events.py -q -p no:cacheprovider`
Expected: PASS (4 passed).

```bash
git add server/ipc/client.py server/tests/test_ipc_client_events.py
git commit -m "feat(ipc): DaemonClient.subscribe() with demux + bounded drop-oldest queues

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: client auto-reconnect + re-subscribe

**Files:**
- Modify: `server/ipc/client.py` (the `_reader` loop)
- Test: `server/tests/test_ipc_client_reconnect.py`

**Interfaces:**
- Consumes: everything from Task 5.
- Produces: `_reader()` reconnects with capped backoff and re-sends `subscribe` for every active stream; a dropped WS does not surface to consumers — the stream resumes.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_ipc_client_reconnect.py`:

```python
import asyncio
import pytest
from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app
from server.tests.ipc_harness import serve_daemon_uds


@pytest.mark.asyncio
async def test_stream_resumes_after_ws_drop():
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            stream = client.subscribe("s1")
            await client.events_ready("s1")
            await client.call("echo", stream="s1", text="before")
            assert (await asyncio.wait_for(stream.__anext__(), 2))["text"] == "before"

            # Force every server-side /events connection closed.
            for ws in list(app.state.subscribers.get("s1", ())):
                await ws.close()

            # The client reader should reconnect + re-subscribe; clear the ready gate
            # so we can await re-subscription, then a new echo must arrive.
            client._ready["s1"].clear()
            await client.events_ready("s1")
            await client.call("echo", stream="s1", text="after")
            assert (await asyncio.wait_for(stream.__anext__(), 3))["text"] == "after"
        finally:
            await client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_client_reconnect.py -q -p no:cacheprovider`
Expected: FAIL — `_reader` exits on the first `ConnectionClosed`; `events_ready` times out after the drop.

- [ ] **Step 3: Write minimal implementation**

In `server/ipc/client.py`, replace the `_reader` method body with a reconnect loop:

```python
    async def _reader(self) -> None:
        uri = "ws://daemon/events"
        backoff = 0.05
        while True:
            try:
                async with websockets.unix_connect(self._sock, uri) as ws:
                    self._ws = ws
                    backoff = 0.05
                    for stream in list(self._desired):
                        await ws.send(json.dumps(proto.subscribe_frame(stream)))
                        self._ready[stream].set()
                    async for raw in ws:
                        frame = json.loads(raw)
                        if frame.get("type") == proto.EVENT:
                            self._deliver(frame["stream"], frame["event"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — drop/connect failures → retry
                log.warning("ipc: events WS error (%s) — reconnecting in %.2fs", exc, backoff)
            self._ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 2.0)
```

Note: on each (re)connect the reader re-sends `subscribe` for every active stream and re-sets its `_ready` event, so a consumer's `events_ready()` after a drop resolves once re-subscription lands.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_client_reconnect.py -q -p no:cacheprovider`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add server/ipc/client.py server/tests/test_ipc_client_reconnect.py
git commit -m "feat(ipc): auto-reconnect + re-subscribe on events WS drop

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: entrypoint — `python -m server.ipc` on a Unix socket

**Files:**
- Create: `server/ipc/__main__.py`
- Test: `server/tests/test_ipc_entrypoint.py`

**Interfaces:**
- Consumes: `create_daemon_app` (Tasks 2–3), `DaemonClient` (Task 4).
- Produces: `serve(socket_path: str) -> None` (blocking uvicorn run on the UDS); `python -m server.ipc <socket_path>` CLI.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_ipc_entrypoint.py`:

```python
import asyncio
import os
import sys
import tempfile
import pytest
from server.ipc.client import DaemonClient


@pytest.mark.asyncio
async def test_entrypoint_serves_ping_over_uds():
    tmpdir = tempfile.mkdtemp(prefix="ccsd-ep-")
    sock = os.path.join(tmpdir, "d.sock")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "server.ipc", sock,
        env={**os.environ, "PYTHONPATH": "src:server"},
    )
    try:
        for _ in range(100):                      # wait for the socket to appear
            if os.path.exists(sock):
                break
            await asyncio.sleep(0.05)
        client = DaemonClient(sock)
        try:
            assert await client.call("ping") == {"pong": True}
        finally:
            await client.aclose()
    finally:
        proc.terminate()
        await proc.wait()
        for p in (sock, tmpdir):
            try:
                os.remove(p) if os.path.isfile(p) else os.rmdir(p)
            except OSError:
                pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_entrypoint.py -q -p no:cacheprovider`
Expected: FAIL — `No module named server.ipc.__main__`.

- [ ] **Step 3: Write minimal implementation**

Create `server/ipc/__main__.py`:

```python
"""Run the session daemon on a Unix socket: `python -m server.ipc <socket_path>`."""
import sys

import uvicorn

from server.ipc.daemon import create_daemon_app


def serve(socket_path: str) -> None:
    uvicorn.run(create_daemon_app(), uds=socket_path, log_level="info")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m server.ipc <socket_path>", file=sys.stderr)
        raise SystemExit(2)
    serve(sys.argv[1])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_entrypoint.py -q -p no:cacheprovider`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add server/ipc/__main__.py server/tests/test_ipc_entrypoint.py
git commit -m "feat(ipc): python -m server.ipc entrypoint (uvicorn on UDS)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Full verification + PR

**Files:** none (verification only).

- [ ] **Step 1: Run the whole IPC test suite**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_protocol.py server/tests/test_ipc_daemon_rpc.py server/tests/test_ipc_daemon_events.py server/tests/test_ipc_client_rpc.py server/tests/test_ipc_client_events.py server/tests/test_ipc_client_reconnect.py server/tests/test_ipc_entrypoint.py -q -p no:cacheprovider`
Expected: all pass, output pristine.

- [ ] **Step 2: Lint + type-check**

Run:
```bash
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/ruff check server/ipc/ server/tests/test_ipc_*.py server/tests/ipc_harness.py
PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/mypy server/ --ignore-missing-imports
```
Expected: ruff clean; mypy success.

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin HEAD
gh pr create --base master --title "feat(ipc): session-daemon transport spine (A1)" --body "<summary: UDS transport — RPC + multiplexed event WS + reconnect; throwaway demo handler; no orchestrator yet. Link the spec. Note A2-A4 follow.>"
```

- [ ] **Step 4: Wait for CI green, then merge**

Run `gh pr checks <num>` until `lint-and-test` + `frontend` pass, then `gh pr merge <num> --merge --delete-branch`.

---

## Notes for the implementer

- pytest-asyncio is **strict** — every `async def test_*` needs `@pytest.mark.asyncio`.
- The daemon RPC/WS tests (Tasks 2–3) use Starlette's `TestClient` in-process (no UDS). The client tests (Tasks 4–6) use the real UDS via `serve_daemon_uds` from `server/tests/ipc_harness.py`.
- `serve_daemon_uds` runs uvicorn in the same event loop with `lifespan="off"` (the daemon app has no lifespan hooks in A1).
- UDS paths must stay short (<~108 chars). `tempfile.mkdtemp()` under the system temp dir is fine; do not nest deeply.
- `websockets.unix_connect(path, "ws://daemon/events")` — the host in the URI is ignored for routing; the path (`/events`) must match the daemon route.
- The full `server/tests` suite hangs in a firewalled sandbox (it polls real accounts; project memory). Run only the named `test_ipc_*` files locally; the full suite runs clean on GitHub CI.
