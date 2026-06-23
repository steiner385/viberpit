# A4: Daemon as a Managed Process + Version Handshake — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the session daemon a real, systemd-managed process — own a `SessionOrchestrator`, init the shared DB, start the poller/rotation, serve on a well-known Unix socket, shut down cleanly — and add an edge↔daemon version/protocol handshake. The daemon runs alongside the unchanged in-process edge (flipping the default is A2.3).

**Architecture:** A `build_daemon()` factory mirrors the edge's orchestrator+x11 construction and registers the RPC surface; a daemon lifespan runs `init_db` + `orchestrator.start()`/`stop()`; `python -m server.ipc` serves it on `sessiond_socket()`. The daemon exposes `/info` (sha + `PROTOCOL_VERSION`) and `/healthz` (sha + status) for the handshake and deploy gate. A `ccswitch-daemon.service` unit ships in `deploy/`.

**Tech Stack:** Python 3.12 · FastAPI/uvicorn-on-UDS · systemd · pytest.

## Global Constraints

- The daemon must NOT be left permanently enabled by this slice (two orchestrators = double poller/rotation). A4's host step is a smoke test (start → verify → stop). Permanent enable is A2.3.
- Do NOT edit `/etc/ccswitch/ccswitch.env` (root-only). The socket path is a code constant.
- Do NOT change `server/app.py`, `server/bridge/ws.py`, or the edge behavior in A4.
- The daemon app's production lifespan must NOT break the test harness, which serves with `lifespan="off"` (`server/tests/ipc_harness.py`) — the lifespan only runs when uvicorn runs it (production), not in `serve_daemon_uds`.
- Run tests from this worktree: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest <args>`
- CI gates: `ruff check .` (full repo), `mypy server/ --ignore-missing-imports`.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: Socket path helper, PROTOCOL_VERSION, daemon `/info` + `/healthz`, `DaemonClient.info()`

**Files:**
- Create: `server/ipc/paths.py`
- Modify: `server/ipc/protocol.py` (add `PROTOCOL_VERSION`)
- Modify: `server/ipc/daemon.py` (`create_daemon_app`: add `/info` and `/healthz` GET routes)
- Modify: `server/ipc/client.py` (`DaemonClient.info()` + a `protocol_compatible` helper)
- Test: `server/tests/test_ipc_handshake.py` (create)

**Interfaces:**
- Produces: `server.ipc.paths.DEFAULT_SOCKET_PATH = "/run/ccswitch/sessiond.sock"`, `sessiond_socket() -> str`. `protocol.PROTOCOL_VERSION = 1`. Daemon `GET /info` → `{"sha": ..., "protocol": 1}`; `GET /healthz` → `{"status": "ok", "sha": ...}`. `DaemonClient.info() -> dict` (raises `DaemonUnavailable` on a dead socket). `server.ipc.client.protocol_compatible(daemon_protocol: int) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_ipc_handshake.py
import os
from unittest import mock
import pytest
from starlette.testclient import TestClient
from server.ipc.daemon import create_daemon_app
from server.ipc import protocol as proto
from server.ipc.paths import DEFAULT_SOCKET_PATH, sessiond_socket
from server.ipc.client import DaemonClient, DaemonUnavailable, protocol_compatible
from server.tests.ipc_harness import serve_daemon_uds


def test_default_socket_path_and_env_override():
    assert DEFAULT_SOCKET_PATH == "/run/ccswitch/sessiond.sock"
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CCSWITCH_SESSIOND_SOCKET", None)
        assert sessiond_socket() == DEFAULT_SOCKET_PATH
    with mock.patch.dict(os.environ, {"CCSWITCH_SESSIOND_SOCKET": "/tmp/x.sock"}):
        assert sessiond_socket() == "/tmp/x.sock"


def test_daemon_info_and_healthz_routes():
    app = create_daemon_app()
    client = TestClient(app)
    info = client.get("/info")
    assert info.status_code == 200
    assert info.json()["protocol"] == proto.PROTOCOL_VERSION
    assert "sha" in info.json()
    hz = client.get("/healthz")
    assert hz.status_code == 200
    assert hz.json()["status"] == "ok"


def test_protocol_compatible():
    assert protocol_compatible(proto.PROTOCOL_VERSION) is True
    assert protocol_compatible(proto.PROTOCOL_VERSION + 1) is False


@pytest.mark.asyncio
async def test_client_info_over_uds_and_dead_socket():
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            info = await client.info()
            assert info["protocol"] == proto.PROTOCOL_VERSION
        finally:
            await client.aclose()
    dead = DaemonClient("/nonexistent/ccsd.sock")
    with pytest.raises(DaemonUnavailable):
        await dead.info()
    await dead.aclose()
```

- [ ] **Step 2: Run, verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_handshake.py -v`
Expected: FAIL — `server.ipc.paths` missing; no `/info`/`/healthz`; `protocol_compatible`/`info` missing.

- [ ] **Step 3: Socket path helper**

Create `server/ipc/paths.py`:

```python
"""Well-known socket path for the session daemon — single source of truth for edge + daemon."""
import os

DEFAULT_SOCKET_PATH = "/run/ccswitch/sessiond.sock"


def sessiond_socket() -> str:
    """The daemon UDS path: CCSWITCH_SESSIOND_SOCKET if set, else the well-known default."""
    return os.environ.get("CCSWITCH_SESSIOND_SOCKET", DEFAULT_SOCKET_PATH)
```

- [ ] **Step 4: PROTOCOL_VERSION**

In `server/ipc/protocol.py`, add near the top (after the docstring):

```python
# Bumped only when the edge↔daemon RPC or event-wire contract changes incompatibly.
PROTOCOL_VERSION = 1
```

- [ ] **Step 5: Daemon `/info` + `/healthz` routes**

In `server/ipc/daemon.py` `create_daemon_app`, after the `/events` websocket route and before the demo-RPC registrations, add:

```python
    @app.get("/info")
    async def info() -> dict:
        from server.app import running_git_sha
        return {"sha": running_git_sha(), "protocol": proto.PROTOCOL_VERSION}

    @app.get("/healthz")
    async def healthz() -> dict:
        from server.app import running_git_sha
        return {"status": "ok", "sha": running_git_sha()}
```

(The import of `running_git_sha` is local to avoid a server.app import at daemon module load.)

- [ ] **Step 6: `DaemonClient.info()` + `protocol_compatible`**

In `server/ipc/client.py`, add a module-level helper (after the imports):

```python
def protocol_compatible(daemon_protocol: int) -> bool:
    """True when the daemon's wire protocol matches this build's."""
    from server.ipc import protocol as _proto
    return daemon_protocol == _proto.PROTOCOL_VERSION
```

And add an `info` method to `DaemonClient` (next to `call`):

```python
    async def info(self) -> dict:
        try:
            resp = await self._http.get("/info")
        except httpx.ConnectError as exc:
            raise DaemonUnavailable(str(exc)) from exc
        return resp.json()
```

- [ ] **Step 7: Run, verify pass + regression**

```bash
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_handshake.py server/tests/ -q -k "ipc or daemon or remote"
```
Expected: PASS. Then `ruff check server/ipc/ server/tests/test_ipc_handshake.py` clean.

- [ ] **Step 8: Commit**

```bash
git add server/ipc/paths.py server/ipc/protocol.py server/ipc/daemon.py server/ipc/client.py server/tests/test_ipc_handshake.py
git commit -m "feat(ipc): socket-path helper, PROTOCOL_VERSION, daemon /info+/healthz, client.info() (A4)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Runnable daemon — `build_daemon` factory + lifespan + entrypoint

**Files:**
- Create: `server/ipc/serve.py` (`build_daemon`, `daemon_lifespan`, `serve`)
- Modify: `server/ipc/__main__.py` (use `serve` + `sessiond_socket()`)
- Test: `server/tests/test_ipc_serve.py` (create)

**Interfaces:**
- Consumes: `create_daemon_app`, `register_orchestrator_rpc`, `SessionOrchestrator`, `build_x11_manager`, `init_db`, `sessiond_socket` (Task 1).
- Produces: `build_daemon(orchestrator=None) -> tuple[FastAPI, SessionOrchestrator]` — wires orchestrator (+ x11 via `orchestrator._x11`), registers RPC. `daemon_lifespan(app, orchestrator)` async context manager: `init_db` + `rehydrate` + `start` on enter, `stop` on exit. `serve(socket_path=None)` runs uvicorn on the UDS with the lifespan.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_ipc_serve.py
from unittest import mock
import pytest
from server.ipc.serve import build_daemon, daemon_lifespan


def test_build_daemon_wires_orchestrator_and_rpc():
    orch = mock.MagicMock()
    app, returned = build_daemon(orchestrator=orch)
    assert returned is orch
    # x11 manager shared onto the orchestrator (mirrors the edge).
    assert orch._x11 is not None
    # RPC surface registered (a representative method).
    assert "ensure_driver" in app.state.rpc
    assert "list_sessions" in app.state.rpc


@pytest.mark.asyncio
async def test_daemon_lifespan_starts_and_stops_orchestrator():
    orch = mock.MagicMock()
    orch.rehydrate = mock.AsyncMock()
    orch.start = mock.AsyncMock()
    orch.stop = mock.AsyncMock()
    app, _ = build_daemon(orchestrator=orch)
    with mock.patch("server.ipc.serve.init_db", new=mock.AsyncMock()) as initdb:
        async with daemon_lifespan(app, orch):
            initdb.assert_awaited_once()
            orch.start.assert_awaited_once()
        orch.stop.assert_awaited_once()
```

- [ ] **Step 2: Run, verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_serve.py -v`
Expected: FAIL — `server.ipc.serve` does not exist.

- [ ] **Step 3: Implement `server/ipc/serve.py`**

```python
"""Production entrypoint plumbing for the session daemon: build the app with a real
orchestrator, run its lifecycle, and serve it on a Unix socket."""
import logging
import os
from contextlib import asynccontextmanager

import uvicorn

from server.db import init_db
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.ipc.paths import sessiond_socket
from server.sessions.orchestrator import SessionOrchestrator

log = logging.getLogger("server.ipc.serve")


def build_daemon(orchestrator=None):
    """Build the daemon app with a real orchestrator + x11 manager + the RPC surface.
    Mirrors the edge's construction (server/app.py) so the daemon owns sessions identically."""
    app = create_daemon_app()
    if orchestrator is None:
        orchestrator = SessionOrchestrator()
    from server.config import get_settings
    from server.sessions.x11manager import build_x11_manager
    orchestrator._x11 = build_x11_manager(get_settings())
    register_orchestrator_rpc(app, orchestrator)
    return app, orchestrator


@asynccontextmanager
async def daemon_lifespan(app, orchestrator):
    """Daemon startup/shutdown: init the shared DB, start the orchestrator (poller +
    rotation + global pump), and stop it cleanly. Rehydrate is lazy — no driver spawned
    at startup, so a daemon start does not respawn every session at once."""
    db_url = os.environ.get("CCSWITCH_DB_URL", "sqlite+aiosqlite:///:memory:")
    await init_db(db_url=db_url)
    try:
        await orchestrator.rehydrate()
    except Exception:
        log.error("daemon rehydrate() failed — starting with no sessions restored", exc_info=True)
    await orchestrator.start()
    try:
        yield
    finally:
        await orchestrator.stop()


def serve(socket_path: str | None = None) -> None:
    sock = socket_path or sessiond_socket()
    app, orchestrator = build_daemon()
    app.router.lifespan_context = lambda _app: daemon_lifespan(_app, orchestrator)
    log.info("session daemon serving on %s", sock)
    uvicorn.run(app, uds=sock, log_level="info")
```

NOTE: confirm `app.router.lifespan_context = ...` is the correct way to attach a lifespan to an already-built FastAPI app in this version, by checking how `server/app.py` sets its lifespan and the installed Starlette/FastAPI version. If FastAPI requires the lifespan at construction, instead build the app with `FastAPI(lifespan=...)` inside `build_daemon` OR pass the lifespan through `create_daemon_app`. The test for the lifespan calls `daemon_lifespan(...)` directly (not via uvicorn), so it passes regardless; the `serve()` wiring is what must be verified against the real API. Adjust minimally and note what you did.

- [ ] **Step 4: Rewrite `server/ipc/__main__.py`**

```python
"""Run the session daemon: `python -m server.ipc [socket_path]` (default: well-known path)."""
import sys

from server.ipc.serve import serve

if __name__ == "__main__":
    serve(sys.argv[1] if len(sys.argv) > 1 else None)
```

- [ ] **Step 5: Run, verify pass + regression**

```bash
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_serve.py server/tests/ -q -k "ipc or daemon"
```
Expected: PASS. `ruff check server/ipc/` clean; `mypy server/ --ignore-missing-imports` Success.

- [ ] **Step 6: Commit**

```bash
git add server/ipc/serve.py server/ipc/__main__.py server/tests/test_ipc_serve.py
git commit -m "feat(ipc): runnable daemon — build_daemon factory + lifespan + entrypoint (A4)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: A3 follow-up — buffer eviction on session delete + truncation warning

**Files:**
- Modify: `server/ipc/daemon.py` (`publish` truncation warning; `app.state.evict(stream)` helper)
- Modify: `server/ipc/orchestrator_rpc.py` (`permanent_delete` handler evicts the stream buffer)
- Test: `server/tests/test_ipc_replay.py` (extend)

**Interfaces:**
- Consumes: `app.state.buffers`/`seqs` (A3).
- Produces: `app.state.evict(stream)` removes the stream's buffer + seq; `publish` logs a one-line warning when the ring is full and drops the oldest; the `permanent_delete` RPC evicts the deleted session's stream.

- [ ] **Step 1: Write the failing test**

```python
# append to server/tests/test_ipc_replay.py
import logging as _logging


@pytest.mark.asyncio
async def test_publish_warns_on_buffer_overflow(monkeypatch, caplog):
    monkeypatch.setenv("CCSWITCH_IPC_REPLAY_BUFFER", "2")
    app = create_daemon_app()
    async with serve_daemon_uds(app):
        with caplog.at_level(_logging.WARNING):
            for i in range(4):
                await app.state.publish("s1", {"type": str(i)})
    assert any("buffer" in r.message.lower() and "s1" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_evict_clears_stream_buffer():
    app = create_daemon_app()
    async with serve_daemon_uds(app):
        await app.state.publish("s1", {"type": "a"})
        assert "s1" in app.state.buffers
        app.state.evict("s1")
        assert "s1" not in app.state.buffers
        assert "s1" not in app.state.seqs
```

- [ ] **Step 2: Run, verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_replay.py -k "overflow or evict" -v`
Expected: FAIL — no warning emitted; `app.state.evict` missing.

- [ ] **Step 3: Implement in `server/ipc/daemon.py`**

Add a module logger at the top if absent: `import logging` + `log = logging.getLogger("server.ipc.daemon")`.

In `publish`, detect the at-capacity drop BEFORE appending and warn:

```python
        buf = app.state.buffers.get(stream)
        if buf is None:
            buf = app.state.buffers[stream] = deque(maxlen=_maxlen)
        if len(buf) == buf.maxlen:
            log.warning("ipc: replay buffer for %s full — dropping oldest event", stream)
        buf.append((seq, event))
```

Add an `evict` helper next to `head_seq`:

```python
    def evict(stream: str) -> None:
        app.state.buffers.pop(stream, None)
        app.state.seqs.pop(stream, None)

    app.state.evict = evict
```

- [ ] **Step 4: Evict on permanent_delete (`server/ipc/orchestrator_rpc.py`)**

Find the `permanent_delete` handler and evict the stream after the orchestrator call:

```python
    async def permanent_delete(params: dict):
        result = await orch.permanent_delete(params["session_id"])
        app.state.evict(params["session_id"])
        return result
```

NOTE: read the current `permanent_delete` handler first — match its exact return shape (it may already `return {"result": ...}` or a raw value). Keep that shape; only add the `evict` call. Also confirm `app.state.evict` exists at the time the handler runs (it is set in `create_daemon_app`, before `register_orchestrator_rpc`).

- [ ] **Step 5: Run, verify pass + regression**

```bash
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_replay.py server/tests/ -q -k "ipc or remote or daemon or replay"
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/ipc/daemon.py server/ipc/orchestrator_rpc.py server/tests/test_ipc_replay.py
git commit -m "feat(ipc): replay-buffer truncation warning + evict on permanent_delete (A4, A3 follow-up)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: systemd unit + guarded deploy-script daemon restart

**Files:**
- Create: `deploy/ccswitch-daemon.service`
- Modify: `scripts/auto-deploy.sh` (restart the daemon when its unit is enabled, after the edge restart)
- Modify: `deploy/README.md` (document the daemon unit + install)
- Test: `server/tests/test_deploy_daemon.py` (create) — unit-file key presence + script no-op-when-not-enabled

**Interfaces:**
- Produces: `deploy/ccswitch-daemon.service` running `python -m server.ipc`; `auto-deploy.sh` restarts `ccswitch-daemon.service` only if `systemctl is-enabled` succeeds (no-op otherwise, e.g. in CI/tests).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_deploy_daemon.py
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_daemon_unit_has_required_keys():
    unit = (ROOT / "deploy" / "ccswitch-daemon.service").read_text()
    assert "RuntimeDirectory=ccswitch" in unit
    assert "ExecStart=" in unit and "server.ipc" in unit
    assert "EnvironmentFile=/etc/ccswitch/ccswitch.env" in unit
    assert "WantedBy=multi-user.target" in unit


def test_auto_deploy_guards_daemon_restart_behind_is_enabled():
    script = (ROOT / "scripts" / "auto-deploy.sh").read_text()
    # The daemon restart must be guarded so a host without the unit is unaffected.
    assert "ccswitch-daemon.service" in script
    assert "is-enabled" in script


def test_auto_deploy_shellcheck_clean():
    # Skip if shellcheck isn't installed; otherwise it must pass.
    import shutil
    if not shutil.which("shellcheck"):
        return
    r = subprocess.run(["shellcheck", "-S", "error", str(ROOT / "scripts" / "auto-deploy.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
```

- [ ] **Step 2: Run, verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_deploy_daemon.py -v`
Expected: FAIL — unit file missing; script has no daemon restart.

- [ ] **Step 3: Create `deploy/ccswitch-daemon.service`**

```ini
[Unit]
Description=ccswitch session daemon — owns live claude sessions
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tony
RuntimeDirectory=ccswitch
WorkingDirectory=/mnt/ssk-ssd/tony/GitHub/cc-account-switcher
EnvironmentFile=/etc/ccswitch/ccswitch.env
ExecStart=/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m server.ipc
Restart=on-failure
RestartSec=3
TimeoutStopSec=60
KillMode=control-group
MemoryMax=12G
TasksMax=512
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Add the guarded daemon restart to `scripts/auto-deploy.sh`**

Read the script; find the `sudo systemctl restart ccswitch.service` line (the edge restart) and the health gate after it. AFTER the edge health gate succeeds, add a daemon-restart block guarded by `is-enabled`:

```bash
# Restart the session daemon too, but only if its unit is installed+enabled on this
# host. Guarded so a host (or CI) without the daemon unit is a no-op. The daemon holds
# no sessions until the edge runs in remote mode (A2.3), so an A4 daemon restart is
# harmless; A2.3 adds idle-gating + edge-vs-daemon classification.
if systemctl is-enabled ccswitch-daemon.service >/dev/null 2>&1; then
  echo "[deploy] restarting ccswitch-daemon.service"
  sudo systemctl restart ccswitch-daemon.service || \
    echo "[deploy] WARNING: daemon restart failed (edge already live)" >&2
fi
```

- [ ] **Step 5: Document in `deploy/README.md`**

Append a short section: the daemon unit, install (`cp deploy/ccswitch-daemon.service /etc/systemd/system/`, `systemctl daemon-reload`), that it is NOT enabled until A2.3, and that the socket lives at `/run/ccswitch/sessiond.sock` (created by `RuntimeDirectory`).

- [ ] **Step 6: Run, verify pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_deploy_daemon.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add deploy/ccswitch-daemon.service deploy/README.md scripts/auto-deploy.sh server/tests/test_deploy_daemon.py
git commit -m "feat(deploy): ccswitch-daemon.service unit + guarded daemon restart in auto-deploy (A4)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: End-to-end — daemon serves over UDS; edge handshake + RPC round-trip

**Files:**
- Test: `server/tests/test_ipc_serve.py` (extend) — `build_daemon`'s app served over the UDS harness answers `/info` and an RPC, and `DaemonClient` handshake passes.

**Interfaces:**
- Consumes: `build_daemon` (Task 2), `DaemonClient.info`/`protocol_compatible` (Task 1), `serve_daemon_uds`.

- [ ] **Step 1: Write the end-to-end test**

```python
# append to server/tests/test_ipc_serve.py
import asyncio
from server.ipc.client import DaemonClient, protocol_compatible
from server.tests.ipc_harness import serve_daemon_uds
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


@pytest.mark.asyncio
async def test_built_daemon_serves_handshake_and_rpc_over_uds():
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    orch._records["s1"] = SessionRecord(id="s1", name="S1", account="acct",
                                        claude_session_id="cs", cols=80, rows=24, cwd="/tmp")
    app, _ = build_daemon(orchestrator=orch)
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            info = await client.info()
            assert protocol_compatible(info["protocol"]) is True
            listed = await client.call("list_sessions")
            assert [r["id"] for r in listed["sessions"]] == ["s1"]
        finally:
            await client.aclose()
```

- [ ] **Step 2: Run, verify pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_ipc_serve.py -v`
Expected: PASS.

- [ ] **Step 3: Full gate**

```bash
cd /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.claude/worktrees/feature+api-retry-backoff
PY=/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python
"$PY" -m ruff check .
PYTHONPATH=$(pwd)/src "$PY" -m mypy server/ --ignore-missing-imports
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src "$PY" -m pytest server/tests/ -q -k "ipc or remote or daemon or replay or handshake or serve or deploy_daemon"
```
Expected: ruff clean, mypy Success, tests pass.

- [ ] **Step 4: Commit**

```bash
git add server/tests/test_ipc_serve.py
git commit -m "test(ipc): end-to-end daemon serve — handshake + RPC over UDS (A4)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Plan Self-Review

**Spec coverage:** socket path + PROTOCOL_VERSION + /info + /healthz + client.info → Task 1; runnable build_daemon + lifespan + entrypoint → Task 2; A3 follow-up (eviction + truncation warning) → Task 3; systemd unit + guarded deploy integration → Task 4; end-to-end serve+handshake → Task 5. The smoke-test-only host activation (no permanent enable) is a deploy-verify step performed by the controller after merge, not a task. ✓
**Placeholder scan:** complete code in each code step. The two NOTEs (lifespan-attachment API, permanent_delete return shape) are verify-against-real-API guards with explicit fallbacks, not logic placeholders. ✓
**Type/name consistency:** `sessiond_socket`/`DEFAULT_SOCKET_PATH`, `PROTOCOL_VERSION`, `build_daemon`/`daemon_lifespan`/`serve`, `app.state.evict`/`head_seq`, `DaemonClient.info`/`protocol_compatible` consistent across tasks. Daemon lifespan only runs under uvicorn (production), never in `serve_daemon_uds` (lifespan="off") — tests call `daemon_lifespan` directly. ✓
