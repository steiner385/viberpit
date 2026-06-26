# Cross-host daemon transport (TCP over Tailscale) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the session daemon listen on a tailnet TCP address and the edge connect to it by URL, while same-host UDS behaviour stays byte-for-byte unchanged when unconfigured.

**Architecture:** One new shared address parser (`server/ipc/address.py`) driven by `CCSWITCH_SESSIOND_URL` produces a `DaemonAddress` descriptor with connect helpers; `serve.py` (daemon listener), `client.py` (`DaemonClient`), and the `app.py` remote-mode gate consume it. The edge↔daemon wire protocol is unchanged — only the connect primitive differs by transport.

**Tech Stack:** Python (FastAPI/uvicorn, asyncio, httpx, `websockets`, pytest, urllib.parse).

## Global Constraints

- Transport selected by env var `CCSWITCH_SESSIOND_URL`: unset → UDS at the default path (identical to today); `unix:///path` → UDS; `tcp://HOST:PORT` → TCP. When `CCSWITCH_SESSIOND_URL` is set it wins over `CCSWITCH_SESSIOND_SOCKET` (log a one-line warning).
- **Wildcard TCP host is forbidden:** `0.0.0.0`, `::`, `*`, and empty host raise `ValueError` at parse time. The daemon must bind a specific (tailnet) IP.
- No app-level TLS and no app-level auth are added (tailnet-ACL trust model). No protocol-version bump (wire contract unchanged).
- Back-compat: `DaemonClient(...)` must still accept a bare UDS path string (≈10 existing test files call `DaemonClient("/path.sock")`).
- Backend tests run via the prebuilt env (worktree has none): `PYTHONPATH="$PWD:$PWD/src" /mnt/ssk-ssd/tony/GitHub/cc-account-switcher-deploy/.venv/bin/python -m pytest <args>`. Lint gates `ruff` + `mypy` (skips untyped fn bodies) run first.
- Tests that bind a real listener use `127.0.0.1` (a specific IP — passes the wildcard guard); never `0.0.0.0`.

---

### Task 1: `server/ipc/address.py` — shared transport address parser

**Files:**
- Create: `server/ipc/address.py`
- Test: `server/tests/test_ipc_address.py`

**Interfaces:**
- Consumes: `server.ipc.paths.sessiond_socket` (default UDS path).
- Produces:
  - `class DaemonAddress` (frozen dataclass): fields `kind: str` (`"unix"|"tcp"`), `uds_path: str|None`, `host: str|None`, `port: int|None`; methods `uvicorn_kwargs() -> dict`, `httpx_base_url() -> str`, `httpx_client_kwargs() -> dict`, `ws_uri() -> str`, `ws_connect()` (returns a `websockets` connect context-manager, NOT awaited).
  - `parse_daemon_url(url: str) -> DaemonAddress`
  - `daemon_address() -> DaemonAddress` (reads env; the analogue of `sessiond_socket()`).

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_ipc_address.py
import pytest
from server.ipc.address import DaemonAddress, parse_daemon_url, daemon_address
from server.ipc.paths import DEFAULT_SOCKET_PATH


def test_parse_unix_url():
    a = parse_daemon_url("unix:///run/ccswitch/sessiond.sock")
    assert a.kind == "unix" and a.uds_path == "/run/ccswitch/sessiond.sock"
    assert a.uvicorn_kwargs() == {"uds": "/run/ccswitch/sessiond.sock"}
    assert a.httpx_base_url() == "http://daemon"
    assert a.ws_uri() == "ws://daemon/events"


def test_parse_tcp_url_v4():
    a = parse_daemon_url("tcp://100.64.0.5:8788")
    assert a.kind == "tcp" and a.host == "100.64.0.5" and a.port == 8788
    assert a.uvicorn_kwargs() == {"host": "100.64.0.5", "port": 8788}
    assert a.httpx_base_url() == "http://100.64.0.5:8788"
    assert a.ws_uri() == "ws://100.64.0.5:8788/events"


def test_parse_tcp_url_v6_bracketed():
    a = parse_daemon_url("tcp://[fd7a::5]:8788")
    assert a.kind == "tcp" and a.host == "fd7a::5" and a.port == 8788
    assert a.httpx_base_url() == "http://[fd7a::5]:8788"
    assert a.ws_uri() == "ws://[fd7a::5]:8788/events"


@pytest.mark.parametrize("url", ["tcp://0.0.0.0:8788", "tcp://[::]:8788", "tcp://:8788"])
def test_wildcard_host_rejected(url):
    with pytest.raises(ValueError, match="wildcard|specific"):
        parse_daemon_url(url)


@pytest.mark.parametrize("url", ["tcp://100.64.0.5", "tcp://100.64.0.5:0"])
def test_missing_or_zero_port_rejected(url):
    with pytest.raises(ValueError):
        parse_daemon_url(url)


def test_unknown_scheme_rejected():
    with pytest.raises(ValueError, match="scheme"):
        parse_daemon_url("https://100.64.0.5:8788")


def test_unix_url_needs_path():
    with pytest.raises(ValueError):
        parse_daemon_url("unix://")


def test_daemon_address_unset_defaults_to_uds(monkeypatch):
    monkeypatch.delenv("CCSWITCH_SESSIOND_URL", raising=False)
    monkeypatch.delenv("CCSWITCH_SESSIOND_SOCKET", raising=False)
    a = daemon_address()
    assert a.kind == "unix" and a.uds_path == DEFAULT_SOCKET_PATH


def test_daemon_address_socket_env_backcompat(monkeypatch):
    monkeypatch.delenv("CCSWITCH_SESSIOND_URL", raising=False)
    monkeypatch.setenv("CCSWITCH_SESSIOND_SOCKET", "/tmp/x.sock")
    a = daemon_address()
    assert a.kind == "unix" and a.uds_path == "/tmp/x.sock"


def test_daemon_address_url_wins_over_socket(monkeypatch, caplog):
    monkeypatch.setenv("CCSWITCH_SESSIOND_URL", "tcp://100.64.0.5:8788")
    monkeypatch.setenv("CCSWITCH_SESSIOND_SOCKET", "/tmp/x.sock")
    import logging
    with caplog.at_level(logging.WARNING):
        a = daemon_address()
    assert a.kind == "tcp" and a.host == "100.64.0.5"
    assert any("CCSWITCH_SESSIOND_URL" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH="$PWD:$PWD/src" …/python -m pytest server/tests/test_ipc_address.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.ipc.address'`.

- [ ] **Step 3: Implement `server/ipc/address.py`**

```python
"""Edge↔daemon transport address: parse one env var into a UDS or TCP descriptor.

Single source of truth for both the daemon listener (server/ipc/serve.py) and the
edge client (server/ipc/client.py) so neither re-implements transport branching.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import websockets

from server.ipc.paths import sessiond_socket

log = logging.getLogger("server.ipc.address")

# Hosts that would expose the no-app-auth daemon beyond the intended tailnet bind.
_WILDCARD_HOSTS = {"", "0.0.0.0", "::", "*"}


def _bracket(host: str) -> str:
    return f"[{host}]" if ":" in host else host


@dataclass(frozen=True)
class DaemonAddress:
    kind: str                       # "unix" | "tcp"
    uds_path: str | None = None
    host: str | None = None
    port: int | None = None

    def uvicorn_kwargs(self) -> dict:
        if self.kind == "unix":
            return {"uds": self.uds_path}
        return {"host": self.host, "port": self.port}

    def httpx_base_url(self) -> str:
        if self.kind == "unix":
            return "http://daemon"
        assert self.host is not None  # tcp branch always has host (parser guarantees it)
        return f"http://{_bracket(self.host)}:{self.port}"

    def httpx_client_kwargs(self) -> dict:
        if self.kind == "unix":
            return {"transport": httpx.AsyncHTTPTransport(uds=self.uds_path),
                    "base_url": "http://daemon"}
        return {"base_url": self.httpx_base_url()}

    def ws_uri(self) -> str:
        if self.kind == "unix":
            return "ws://daemon/events"
        assert self.host is not None  # tcp branch always has host (parser guarantees it)
        return f"ws://{_bracket(self.host)}:{self.port}/events"

    def ws_connect(self):
        """Return the websockets connect context-manager (use with `async with`)."""
        if self.kind == "unix":
            return websockets.unix_connect(self.uds_path, self.ws_uri())
        return websockets.connect(self.ws_uri())


def parse_daemon_url(url: str) -> DaemonAddress:
    parts = urlsplit(url)
    if parts.scheme == "unix":
        if not parts.path:
            raise ValueError(f"unix daemon URL needs a socket path: {url!r}")
        return DaemonAddress(kind="unix", uds_path=parts.path)
    if parts.scheme == "tcp":
        host = parts.hostname               # strips [] for IPv6, lowercases
        try:
            port = parts.port               # raises ValueError on out-of-range / non-numeric
        except ValueError as exc:
            raise ValueError(f"invalid daemon TCP port in {url!r}") from exc
        if host is None or host in _WILDCARD_HOSTS:
            raise ValueError(
                f"daemon TCP host must be a specific (tailnet) IP, not a wildcard: {url!r}")
        if not port:
            raise ValueError(f"daemon TCP URL needs a non-zero port: {url!r}")
        return DaemonAddress(kind="tcp", host=host, port=port)
    raise ValueError(
        f"unsupported daemon URL scheme {parts.scheme!r} (expected unix:// or tcp://): {url!r}")


def daemon_address() -> DaemonAddress:
    """Resolve the daemon transport from the environment.

    CCSWITCH_SESSIOND_URL wins when set (logs a warning if CCSWITCH_SESSIOND_SOCKET is
    also set). Otherwise UDS at the back-compat path (sessiond_socket())."""
    url = os.environ.get("CCSWITCH_SESSIOND_URL")
    if url:
        if os.environ.get("CCSWITCH_SESSIOND_SOCKET"):
            log.warning("both CCSWITCH_SESSIOND_URL and CCSWITCH_SESSIOND_SOCKET set — using URL")
        return parse_daemon_url(url)
    return DaemonAddress(kind="unix", uds_path=sessiond_socket())
```

- [ ] **Step 4: Run tests — pass**

Run: `PYTHONPATH="$PWD:$PWD/src" …/python -m pytest server/tests/test_ipc_address.py -v`
Expected: PASS (all cases).

- [ ] **Step 5: Lint + commit**

Run: `ruff check server/ipc/address.py && mypy server/ipc/address.py`

```bash
git add server/ipc/address.py server/tests/test_ipc_address.py
git commit -m "feat(ipc): shared daemon transport address parser (unix/tcp)"
```

---

### Task 2: daemon listener serves UDS or TCP (`server/ipc/serve.py`)

**Files:**
- Modify: `server/ipc/serve.py` (the `serve()` function, ~lines 88-93)
- Test: `server/tests/test_ipc_serve_transport.py` (create)

**Interfaces:**
- Consumes: `DaemonAddress`, `daemon_address` (Task 1).
- Produces: `serve()` binds via `daemon_address().uvicorn_kwargs()`; an optional `socket_path` arg still forces UDS for callers/tests that pass one.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_ipc_serve_transport.py
import pytest
from server.ipc import serve as serve_mod


def test_serve_tcp_binds_host_port(monkeypatch):
    captured = {}
    monkeypatch.setenv("CCSWITCH_SESSIOND_URL", "tcp://127.0.0.1:8790")
    monkeypatch.delenv("CCSWITCH_SESSIOND_SOCKET", raising=False)
    monkeypatch.setattr(serve_mod, "build_daemon", lambda: (_FakeApp(), object()))
    monkeypatch.setattr(serve_mod.uvicorn, "run", lambda app, **kw: captured.update(kw))
    serve_mod.serve()
    assert captured.get("host") == "127.0.0.1" and captured.get("port") == 8790
    assert "uds" not in captured


def test_serve_uds_default(monkeypatch):
    captured = {}
    monkeypatch.delenv("CCSWITCH_SESSIOND_URL", raising=False)
    monkeypatch.setenv("CCSWITCH_SESSIOND_SOCKET", "/tmp/sd.sock")
    monkeypatch.setattr(serve_mod, "build_daemon", lambda: (_FakeApp(), object()))
    monkeypatch.setattr(serve_mod.uvicorn, "run", lambda app, **kw: captured.update(kw))
    serve_mod.serve()
    assert captured.get("uds") == "/tmp/sd.sock" and "host" not in captured


def test_serve_wildcard_url_refused(monkeypatch):
    monkeypatch.setenv("CCSWITCH_SESSIOND_URL", "tcp://0.0.0.0:8790")
    monkeypatch.setattr(serve_mod, "build_daemon", lambda: (_FakeApp(), object()))
    with pytest.raises(ValueError, match="wildcard|specific"):
        serve_mod.serve()


class _FakeApp:
    class _Router:
        lifespan_context = None
    def __init__(self):
        self.router = self._Router()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH="$PWD:$PWD/src" …/python -m pytest server/tests/test_ipc_serve_transport.py -v`
Expected: FAIL — `serve()` still calls `uvicorn.run(app, uds=sock)` regardless of the URL (TCP test sees `uds`, not `host`); wildcard test does not raise.

- [ ] **Step 3: Update `serve()`**

Replace the body of `serve()` (currently lines ~88-93) with:

```python
def serve(socket_path: str | None = None) -> None:
    from server.ipc.address import DaemonAddress, daemon_address
    addr = (DaemonAddress(kind="unix", uds_path=socket_path)
            if socket_path else daemon_address())
    app, orchestrator = build_daemon()
    app.router.lifespan_context = lambda _app: daemon_lifespan(_app, orchestrator)
    log.info("session daemon serving on %s", addr.ws_uri())
    uvicorn.run(app, **addr.uvicorn_kwargs(), log_level="info")
```

(The wildcard guard is enforced inside `daemon_address()`/`parse_daemon_url`, so a `tcp://0.0.0.0:…` URL raises before `uvicorn.run`. Remove the now-unused `sessiond_socket` import from `serve.py` if it is no longer referenced — check with `grep -n sessiond_socket server/ipc/serve.py`.)

- [ ] **Step 4: Run tests — pass**

Run: `PYTHONPATH="$PWD:$PWD/src" …/python -m pytest server/tests/test_ipc_serve_transport.py server/tests/test_ipc_serve.py -v`
Expected: PASS (new transport tests + the existing serve test).

- [ ] **Step 5: Lint + commit**

Run: `ruff check server/ipc/serve.py && mypy server/ipc/serve.py`

```bash
git add server/ipc/serve.py server/tests/test_ipc_serve_transport.py
git commit -m "feat(ipc): daemon listener binds UDS or tailnet TCP via address"
```

---

### Task 3: `DaemonClient` connects over UDS or TCP (`server/ipc/client.py`)

**Files:**
- Modify: `server/ipc/client.py` (`__init__` ~lines 35-49; `_reader` ~lines 122-143)
- Test: `server/tests/test_ipc_client_tcp.py` (create)

**Interfaces:**
- Consumes: `DaemonAddress` (Task 1).
- Produces: `DaemonClient(address)` where `address` is a `DaemonAddress` OR a `str` (back-compat: a str is treated as a UDS path). `self._addr: DaemonAddress`.

- [ ] **Step 1: Write the failing test (RPC + events over real TCP)**

```python
# server/tests/test_ipc_client_tcp.py
import asyncio
import socket
import pytest
import uvicorn
from server.ipc.address import DaemonAddress
from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def _serve_tcp(app, host, port):
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    return server, task


@pytest.mark.asyncio
async def test_daemon_client_rpc_and_events_over_tcp():
    host, port = "127.0.0.1", _free_port()
    app = create_daemon_app()
    server, task = await _serve_tcp(app, host, port)
    client = DaemonClient(DaemonAddress(kind="tcp", host=host, port=port))
    try:
        # unary RPC over TCP
        assert (await client.call("ping"))["pong"] is True
        # event stream over TCP
        stream = client.subscribe("s1")
        await client.events_ready("s1")
        await client.call("echo", stream="s1", text="hi")
        ev = await asyncio.wait_for(stream.__anext__(), 2)
        assert ev["text"] == "hi"
    finally:
        await client.aclose()
        server.should_exit = True
        await task


@pytest.mark.asyncio
async def test_daemon_client_accepts_socket_path_string():
    # Back-compat: a bare path string is still treated as a UDS address.
    c = DaemonClient("/tmp/whatever.sock")
    assert c._addr.kind == "unix" and c._addr.uds_path == "/tmp/whatever.sock"
    await c.aclose()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH="$PWD:$PWD/src" …/python -m pytest server/tests/test_ipc_client_tcp.py -v`
Expected: FAIL — `DaemonClient.__init__` builds an `AsyncHTTPTransport(uds=<DaemonAddress>)` (TypeError) and `_reader` calls `websockets.unix_connect` unconditionally; `_addr` attribute does not exist.

- [ ] **Step 3: Update `DaemonClient.__init__`**

Replace the start of `__init__` (lines ~35-41) with:

```python
    def __init__(self, address):
        from server.ipc.address import DaemonAddress
        self._addr = (address if isinstance(address, DaemonAddress)
                      else DaemonAddress(kind="unix", uds_path=address))
        self._http = httpx.AsyncClient(**self._addr.httpx_client_kwargs(), timeout=30.0)
```

(Leave the rest of `__init__` — `self._ws`, `self._queues`, etc. — unchanged. Remove the old `self._sock = socket_path` line and the old `httpx.AsyncClient(transport=…, base_url=…)` block they replace.)

- [ ] **Step 4: Update `_reader` to connect via the address**

In `_reader`, replace the hard-coded connect line. The current block:

```python
        while True:
            try:
                async with websockets.unix_connect(self._sock, uri) as ws:
```

becomes:

```python
        while True:
            try:
                async with self._addr.ws_connect() as ws:
```

Delete the now-unused `uri = "ws://daemon/events"` local (the address supplies the URI). Confirm `self._sock` is no longer referenced anywhere: `grep -n "_sock" server/ipc/client.py` should return nothing.

- [ ] **Step 5: Run tests — pass**

Run: `PYTHONPATH="$PWD:$PWD/src" …/python -m pytest server/tests/test_ipc_client_tcp.py server/tests/test_ipc_client_rpc.py server/tests/test_ipc_client_events.py server/tests/test_ipc_client_reconnect.py -v`
Expected: PASS — new TCP test + existing UDS client tests (which pass a path string, exercising back-compat).

- [ ] **Step 6: Lint + commit**

Run: `ruff check server/ipc/client.py && mypy server/ipc/client.py`

```bash
git add server/ipc/client.py server/tests/test_ipc_client_tcp.py
git commit -m "feat(ipc): DaemonClient connects over UDS or TCP via address"
```

---

### Task 4: edge remote-mode gate + production construction sites (`server/app.py`, `server/deploy/controller.py`)

**Files:**
- Modify: `server/app.py` (the remote-mode gate, lines ~275-312)
- Modify: `server/deploy/controller.py:21`
- Test: `server/tests/test_remote_mode_url.py` (create)

**Interfaces:**
- Consumes: `daemon_address`, `DaemonAddress` (Task 1); `DaemonClient(address)` (Task 3).
- Produces: when `CCSWITCH_SESSIOND_URL` is set, `create_app()` enters remote mode against that transport and skips UDS auto-detect.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_remote_mode_url.py
import pytest
from server.app import create_app


def test_tcp_url_selects_remote_mode(monkeypatch):
    monkeypatch.setenv("CCSWITCH_SESSIOND_URL", "tcp://127.0.0.1:8799")
    monkeypatch.delenv("CCSWITCH_SESSIOND_SOCKET", raising=False)
    app = create_app()                      # no live daemon needed — client connects lazily
    assert app.state.remote_mode is True
    assert app.state.daemon_socket == "tcp://127.0.0.1:8799"


def test_unset_url_in_process_default(monkeypatch, tmp_path):
    # No URL, no socket, no daemon present → in-process (dev/CI default), unchanged.
    monkeypatch.delenv("CCSWITCH_SESSIOND_URL", raising=False)
    monkeypatch.delenv("CCSWITCH_SESSIOND_SOCKET", raising=False)
    monkeypatch.setattr("server.ipc.paths.DEFAULT_SOCKET_PATH", str(tmp_path / "none.sock"))
    app = create_app()
    assert app.state.remote_mode is False
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH="$PWD:$PWD/src" …/python -m pytest server/tests/test_remote_mode_url.py::test_tcp_url_selects_remote_mode -v`
Expected: FAIL — current gate ignores `CCSWITCH_SESSIOND_URL`; `remote_mode` is False.

- [ ] **Step 3: Update the `app.py` gate**

Replace lines ~275-312 (from `socket_path = os.environ.get("CCSWITCH_SESSIOND_SOCKET")` through the end of the `else:` in-process block) with:

```python
    socket_path = os.environ.get("CCSWITCH_SESSIOND_SOCKET")
    daemon_url = os.environ.get("CCSWITCH_SESSIOND_URL")
    app.state.daemon_protocol = None
    address = None  # server.ipc.address.DaemonAddress when remote

    if orchestrator is None and daemon_url:
        # Explicit transport via URL (opt-in cross-host TCP, or explicit UDS) — skips the
        # UDS auto-detect below. The wildcard-host guard lives in the parser.
        from server.ipc.address import daemon_address
        from server.ipc.protocol import PROTOCOL_VERSION
        address = daemon_address()
        app.state.daemon_protocol = PROTOCOL_VERSION
        logging.getLogger("server").info(
            "CCSWITCH_SESSIOND_URL=%s — running in remote mode", daemon_url)
    elif orchestrator is None and not socket_path:
        # Remote by default: auto-detect a compatible daemon at the well-known UDS path.
        from server.ipc.paths import DEFAULT_SOCKET_PATH
        if _daemon_compatible(DEFAULT_SOCKET_PATH):
            socket_path = DEFAULT_SOCKET_PATH
            from server.ipc.protocol import PROTOCOL_VERSION
            app.state.daemon_protocol = PROTOCOL_VERSION
            logging.getLogger("server").info(
                "session daemon detected at %s — running in remote mode", DEFAULT_SOCKET_PATH)
        elif _daemon_socket_present(DEFAULT_SOCKET_PATH):
            raise RuntimeError(
                f"session daemon socket {DEFAULT_SOCKET_PATH} exists but no compatible "
                "daemon answered /info after retries; refusing in-process fallback to "
                "avoid a split-brain over the shared database")
        # else: no daemon socket on this host → in-process (dev/CI default).

    if address is None and socket_path:
        from server.ipc.address import DaemonAddress
        address = DaemonAddress(kind="unix", uds_path=socket_path)

    if orchestrator is None and address is not None:
        from server.ipc.client import DaemonClient
        from server.sessions.remote import RemoteOrchestrator
        app.state.remote_mode = True
        app.state.daemon_socket = daemon_url or socket_path
        app.state.orchestrator = None
        app.state.sessions = RemoteOrchestrator(DaemonClient(address))
    else:
        if orchestrator is None:
            orchestrator = SessionOrchestrator()
        app.state.remote_mode = False
        app.state.daemon_socket = None
        app.state.orchestrator = orchestrator
        app.state.sessions = InProcessOrchestratorAdapter(orchestrator)
```

- [ ] **Step 4: Update `deploy/controller.py`**

`server/deploy/controller.py:21` currently:

```python
    return lambda: DaemonClient(sessiond_socket())
```

becomes:

```python
    return lambda: DaemonClient(daemon_address())
```

and update that file's import: replace `from server.ipc.paths import sessiond_socket` with `from server.ipc.address import daemon_address` (verify the old import has no other use in the file: `grep -n sessiond_socket server/deploy/controller.py`).

- [ ] **Step 5: Run tests — pass**

Run: `PYTHONPATH="$PWD:$PWD/src" …/python -m pytest server/tests/test_remote_mode_url.py server/tests/test_remote_mode_wiring.py server/tests/test_remote_mode_edge.py -v` (add `server/tests/test_deploy_controller.py` if it exists; otherwise `-k "deploy_controller or remote_mode"`).
Expected: PASS — new URL gate tests + existing remote-mode/deploy tests (UDS path unchanged).

- [ ] **Step 6: Document the operator contract**

Append to `specs/001-ccswitch-web/quickstart.md` (or the deployment section it points to) a short "Cross-host daemon (tailnet)" note: set `CCSWITCH_SESSIOND_URL=tcp://<tailnet-ip>:<port>` on BOTH `ccswitch.service` and `ccswitch-daemon.service`; the daemon binds that specific tailnet IP (a wildcard is refused); restrict the port to the edge host with a Tailscale ACL; no TLS/token is used (WireGuard encrypts, ACL authorizes).

- [ ] **Step 7: Full backend regression + lint + commit**

Run: `PYTHONPATH="$PWD:$PWD/src" …/python -m pytest server/tests/ -k "ipc or remote or deploy or app" -q` then `ruff check server/app.py server/deploy/controller.py && mypy server/app.py server/deploy/controller.py`
Expected: PASS / clean.

```bash
git add server/app.py server/deploy/controller.py server/tests/test_remote_mode_url.py specs/001-ccswitch-web/quickstart.md
git commit -m "feat(ipc): select remote transport from CCSWITCH_SESSIOND_URL (cross-host) — #205"
```

---

## Self-Review

**Spec coverage:** address parser + env precedence + wildcard guard → Task 1; daemon TCP listener + wildcard fail-fast → Task 2; `DaemonClient` URL transport + back-compat → Task 3; edge gate recognizing `CCSWITCH_SESSIOND_URL` + production call sites + operator doc → Task 4. Security posture (tailnet-IP bind, opt-in, no TLS/auth) is enforced by the Task-1 guard + Task-2 fail-fast and documented in Task 4 Step 6. The spec's "no protocol bump" holds (no wire change).

**Placeholder scan:** none — every step has complete code and exact commands.

**Type consistency:** `DaemonAddress` (fields `kind/uds_path/host/port`; methods `uvicorn_kwargs/httpx_base_url/httpx_client_kwargs/ws_uri/ws_connect`), `parse_daemon_url`, `daemon_address` are defined in Task 1 and consumed with the same names/signatures in Tasks 2-4. `DaemonClient(address)` accepts `DaemonAddress | str` (Task 3) and is called with a `DaemonAddress` in Task 4 and a str in legacy tests. `self._addr` introduced in Task 3 and asserted in its test.

**Risk note:** Task 4 edits the sensitive remote-mode gate (split-brain guard). The replacement preserves the UDS auto-detect and the `_daemon_socket_present` fail-loud branch unchanged; only an additive URL branch and the `address` indirection are new. Run the full remote-mode suite (Step 5) before committing.
