# User-Controlled Deploy — Plan 2: Detection + Notification Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Plan 1 engine into a long-lived `ccswitch-deploy-controller` service that polls `origin/master` (notify-only), then surface "N changes pending" + the commit list + a one-click **Deploy now** with a live progress checklist in the dashboard — the edge proxying to the controller over a Unix socket.

**Architecture:** A new long-lived systemd service (`python -m server.deploy serve`) runs a FastAPI app on `/run/ccswitch/deploy.sock`. It holds a background **detect loop** (`PendingState`) and the Plan 1 `DeployController`, exposing `GET /status` and `POST /start`. The edge gains a `server/api/deploy.py` router that proxies those two endpoints (behind `current_identity`) via a thin `DeployControllerClient`. The web app gets `usePendingDeploy` / `useDeployStatus` / `useStartDeploy` hooks and a banner + checklist UI. Because the controller is long-lived, deploy status survives the edge's own restart with no durable status file.

**Tech Stack:** Python FastAPI + uvicorn (UDS), httpx (UDS client), React + @tanstack/react-query, Tailwind/shadcn. No new dependencies.

## Global Constraints

- Depends on Plan 1 being merged: `server.deploy.controller.DeployController`, `server.deploy.types.DeployStatus`, `server.deploy.git`, `server.deploy.steps`.
- Constitution VI: do NOT add a parallel pending/version mechanism. `updatePending` (ws) is the **drain-buffering** signal and `useVersionWatch` is the **post-deploy frontend-reload** prompt — neither is origin/master detection. Name new hooks distinctly (`usePendingDeploy`, `useDeployStatus`) and reuse `useVersionWatch` as the "deploy finished → reload" trigger.
- Auth: every new edge route requires `Depends(current_identity)` (matches `server/api/sessions.py`).
- Adding any `@/api/hooks` hook to `Dashboard.tsx` breaks ~9 web tests' factory mocks (memory: `dashboard-hooks-partial-mocks`); update those mocks in the same task. Real type gate is `tsc -b`/`npm run build`, not `tsc --noEmit` (memory: `ccswitch-frontend-real-typecheck`). CI runs the FULL vitest + eslint `--max-warnings 0`.
- Web tests in the worktree: `cd web && rm -f node_modules && npm ci` (registry reachable, lockfile committed).
- Backend tests: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest`.

---

## File Structure

- Create `server/deploy/detect.py` — `PendingState` dataclass + `compute_pending(repo, *, ci_fn)` + `DetectLoop`.
- Create `server/deploy/app.py` — FastAPI app factory for the controller (routes `/status`, `/start`, `/healthz`) + lifespan (detect loop + DeployController).
- Create `server/deploy/serve.py` — `serve(socket_path)` uvicorn UDS entrypoint; extend `server/deploy/__main__.py` with a `serve` subcommand.
- Create `server/deploy/client.py` — `DeployControllerClient` (edge-side UDS httpx client).
- Create `server/api/deploy.py` — edge router: `GET /api/deploy/status`, `POST /api/deploy/start`.
- Modify `server/app.py` — `include_router(deploy_router)`.
- Create `deploy/ccswitch-deploy-controller.service` — systemd unit.
- Create `web/src/api/deploy.ts` — typed client calls (`fetchDeployStatus`, `startDeploy`).
- Modify `web/src/api/hooks.ts` — `usePendingDeploy`, `useDeployStatus`, `useStartDeploy`.
- Create `web/src/components/DeployPanel.tsx` — banner + commit list + Deploy-now + progress checklist.
- Modify `web/src/components/Dashboard.tsx` — render `<DeployPanel/>`; update affected test mocks.
- Tests: `server/tests/test_deploy_detect.py`, `test_deploy_app.py`, `test_deploy_client.py`, `test_api_deploy.py`, `web/src/components/DeployPanel.test.tsx`, `web/src/api/hooks.deploy.test.tsx`.

---

## Task 1: Pending-change detection (`detect.py`)

**Files:**
- Create: `server/deploy/detect.py`
- Test: `server/tests/test_deploy_detect.py`

**Interfaces:**
- Consumes: `server.deploy.git` (`fetch`, `current_sha`, `remote_sha`, `log_range`), `server.deploy.steps.ci_verdict`.
- Produces:
  - `@dataclass PendingState { behind: bool; live_sha: str; target_sha: str; commits: list[dict]; ci: str }` with `.to_dict()`.
  - `detect.compute_pending(repo: str, *, git_mod=git, ci_fn=steps.ci_verdict) -> PendingState` — fetches, compares; if equal → `behind=False, commits=[], ci="green"`; else lists `log_range(live..target)` and resolves `ci_fn(target)`.
  - `class DetectLoop` — `__init__(self, repo, *, interval_s=120.0, compute=compute_pending)`; `state: PendingState`; `async run(self, sleep=asyncio.sleep)` loop updating `self.state`; `tick()` does one update (test seam).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_deploy_detect.py
import asyncio
import pytest
from server.deploy import detect


class FakeGit:
    def __init__(self, live, remote, commits):
        self._live, self._remote, self._commits = live, remote, commits
        self.fetched = False
    def fetch(self, repo, ref="master", run=None): self.fetched = True
    def current_sha(self, repo, run=None): return self._live
    def remote_sha(self, repo, ref="origin/master", run=None): return self._remote
    def log_range(self, repo, a, b, run=None): return self._commits


def test_up_to_date_is_not_behind():
    g = FakeGit("abc", "abc", [])
    st = detect.compute_pending("/r", git_mod=g, ci_fn=lambda s: "green")
    assert st.behind is False and st.commits == [] and g.fetched is True


def test_behind_lists_commits_and_ci():
    commits = [{"sha": "d1", "subject": "feat x", "author": "t", "iso": "2026-06-24T00:00:00Z"}]
    g = FakeGit("abc", "def", commits)
    st = detect.compute_pending("/r", git_mod=g, ci_fn=lambda s: "pending")
    assert st.behind is True and st.target_sha == "def"
    assert st.commits[0]["subject"] == "feat x" and st.ci == "pending"


@pytest.mark.asyncio
async def test_detect_loop_tick_updates_state():
    g = FakeGit("a", "b", [{"sha": "1", "subject": "s", "author": "t", "iso": "i"}])
    loop = detect.DetectLoop("/r", interval_s=0,
                             compute=lambda repo: detect.compute_pending(repo, git_mod=g, ci_fn=lambda s: "green"))
    await loop.tick()
    assert loop.state.behind is True and loop.state.to_dict()["commits"][0]["sha"] == "1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_detect.py -v`
Expected: FAIL — `ModuleNotFoundError: ... server.deploy.detect`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/deploy/detect.py
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import Callable

from server.deploy import git as _git
from server.deploy import steps as _steps


@dataclass
class PendingState:
    behind: bool = False
    live_sha: str = ""
    target_sha: str = ""
    commits: list[dict] = field(default_factory=list)
    ci: str = "unavailable"

    def to_dict(self) -> dict:
        return {"behind": self.behind, "liveSha": self.live_sha, "targetSha": self.target_sha,
                "commits": self.commits, "ci": self.ci}


def compute_pending(repo: str, *, git_mod=_git, ci_fn: Callable[[str], str] = _steps.ci_verdict) -> PendingState:
    git_mod.fetch(repo)
    live = git_mod.current_sha(repo)
    target = git_mod.remote_sha(repo)
    if live == target:
        return PendingState(behind=False, live_sha=live, target_sha=target, commits=[], ci="green")
    commits = git_mod.log_range(repo, live, target)
    return PendingState(behind=True, live_sha=live, target_sha=target, commits=commits, ci=ci_fn(target))


class DetectLoop:
    def __init__(self, repo: str, *, interval_s: float = 120.0,
                 compute: Callable[[str], PendingState] = compute_pending):
        self.repo = repo
        self.interval_s = interval_s
        self._compute = compute
        self.state = PendingState()

    async def tick(self) -> None:
        try:
            self.state = await asyncio.to_thread(self._compute, self.repo)
        except Exception:
            pass   # transient git/network error — keep the last good state, retry next tick

    async def run(self, sleep=asyncio.sleep) -> None:
        while True:
            await self.tick()
            await sleep(self.interval_s)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_detect.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/deploy/detect.py server/tests/test_deploy_detect.py
git commit -m "feat(deploy): origin/master pending-change detection (notify-only)"
```

---

## Task 2: Controller FastAPI app (`app.py`) — /status, /start, lifespan

**Files:**
- Create: `server/deploy/app.py`
- Test: `server/tests/test_deploy_app.py`

**Interfaces:**
- Consumes: `DetectLoop`, `DeployController` (Plan 1), `DeployStatus`.
- Produces: `app.build_controller_app(*, repo, health_url, venv_pip, daemon_enabled, detect_loop=None, controller=None) -> (FastAPI, DetectLoop, DeployController)`.
  - `GET /status` → `{"pending": <PendingState.to_dict()>, "deploy": {"running": bool, "phases": {...}, "error": str|None, "warnings": [...], "targetSha": str}}`.
  - `POST /start` → triggers `controller.deploy_once()` as a background task; returns `{"started": true}` or `409 {"started": false, "reason": "in progress"}` if already running.
  - `GET /healthz` → `{"status": "ok"}`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_deploy_app.py
import asyncio
import pytest
from fastapi.testclient import TestClient
from server.deploy import app as deploy_app
from server.deploy.detect import DetectLoop, PendingState
from server.deploy.types import DeployStatus


class FakeController:
    def __init__(self):
        self.status = DeployStatus(target_sha="def", running=False)
        self.started = 0
        self._running = False
    async def deploy_once(self):
        if self._running:
            raise RuntimeError("deploy already in progress")
        self.started += 1
        self.status = DeployStatus(target_sha="def", running=False, phases={"update": "done"})
        return self.status


@pytest.fixture
def client():
    loop = DetectLoop("/r", compute=lambda r: PendingState(behind=True, target_sha="def",
                                                            commits=[{"sha": "1"}], ci="green"))
    loop.state = PendingState(behind=True, target_sha="def", commits=[{"sha": "1"}], ci="green")
    ctrl = FakeController()
    app, _, _ = deploy_app.build_controller_app(
        repo="/r", health_url="http://h", venv_pip="/v", daemon_enabled=False,
        detect_loop=loop, controller=ctrl)
    return TestClient(app), ctrl


def test_status_exposes_pending_and_deploy(client):
    c, _ = client
    body = c.get("/status").json()
    assert body["pending"]["behind"] is True
    assert body["pending"]["commits"][0]["sha"] == "1"
    assert "running" in body["deploy"]


def test_start_triggers_deploy(client):
    c, ctrl = client
    r = c.post("/start")
    assert r.status_code == 200 and r.json()["started"] is True
    # background task ran
    assert ctrl.started == 1


def test_start_conflict_when_running(client):
    c, ctrl = client
    ctrl._running = True
    r = c.post("/start")
    assert r.status_code == 409 and r.json()["started"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_app.py -v`
Expected: FAIL — `ModuleNotFoundError: ... server.deploy.app`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/deploy/app.py
from __future__ import annotations
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from server.deploy.controller import DeployController
from server.deploy.detect import DetectLoop

log = logging.getLogger("server.deploy.app")


def build_controller_app(*, repo: str, health_url: str, venv_pip: str, daemon_enabled: bool,
                         detect_loop: DetectLoop | None = None,
                         controller: object | None = None):
    loop = detect_loop or DetectLoop(repo)
    ctrl = controller or DeployController(repo, health_url=health_url, venv_pip=venv_pip,
                                          daemon_enabled=daemon_enabled)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(loop.run(), name="deploy-detect-loop")
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(lifespan=lifespan)

    def _deploy_dict() -> dict:
        s = ctrl.status
        return {"running": s.running, "phases": s.phases, "error": s.error,
                "warnings": s.warnings, "targetSha": s.target_sha}

    @app.get("/status")
    async def status() -> dict:
        return {"pending": loop.state.to_dict(), "deploy": _deploy_dict()}

    @app.post("/start")
    async def start() -> JSONResponse:
        if ctrl.status.running:
            return JSONResponse({"started": False, "reason": "in progress"}, status_code=409)
        asyncio.create_task(ctrl.deploy_once(), name="deploy-run")
        return JSONResponse({"started": True})

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    return app, loop, ctrl
```

Note: `TestClient` runs the lifespan + background `deploy_once` synchronously enough that `test_start_triggers_deploy` observes `started == 1` after the request; if flaky under the event loop, the test may `time.sleep(0)` — but `asyncio.create_task` on the TestClient loop resolves before the response returns here because `deploy_once` (FakeController) has no awaits before incrementing. Keep `FakeController.deploy_once` synchronous up to the increment.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_app.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/deploy/app.py server/tests/test_deploy_app.py
git commit -m "feat(deploy): controller FastAPI app — /status, /start, detect-loop lifespan"
```

---

## Task 3: UDS serve entrypoint + `serve` subcommand

**Files:**
- Create: `server/deploy/serve.py`
- Modify: `server/deploy/__main__.py`
- Test: `server/tests/test_deploy_cli.py` (extend)

**Interfaces:**
- Consumes: `build_controller_app`.
- Produces:
  - `serve.serve(socket_path: str | None = None) -> None` — builds the app, runs uvicorn on the UDS (default `/run/ccswitch/deploy.sock`, overridable `CCSWITCH_DEPLOY_SOCKET`). Mirrors `server/ipc/serve.py`.
  - `serve.deploy_socket() -> str` — `CCSWITCH_DEPLOY_SOCKET` or `/run/ccswitch/deploy.sock`.
  - `__main__.main` gains a `serve` subcommand (`python -m server.deploy serve`) alongside `--once`.

- [ ] **Step 1: Write the failing test (append to `server/tests/test_deploy_cli.py`)**

```python
def test_deploy_socket_default(monkeypatch):
    from server.deploy import serve
    monkeypatch.delenv("CCSWITCH_DEPLOY_SOCKET", raising=False)
    assert serve.deploy_socket() == "/run/ccswitch/deploy.sock"


def test_deploy_socket_env_override(monkeypatch):
    from server.deploy import serve
    monkeypatch.setenv("CCSWITCH_DEPLOY_SOCKET", "/tmp/x.sock")
    assert serve.deploy_socket() == "/tmp/x.sock"


def test_serve_subcommand_calls_serve(monkeypatch):
    from server.deploy import __main__ as cli
    called = {}
    monkeypatch.setattr("server.deploy.serve.serve", lambda socket_path=None: called.setdefault("ran", True))
    assert cli.main(["serve"]) == 0 and called.get("ran") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: ... server.deploy.serve` / unknown subcommand.

- [ ] **Step 3: Write minimal implementation**

```python
# server/deploy/serve.py
from __future__ import annotations
import logging
import os

import uvicorn

from server.deploy.app import build_controller_app

log = logging.getLogger("server.deploy.serve")
DEFAULT_DEPLOY_SOCKET = "/run/ccswitch/deploy.sock"


def deploy_socket() -> str:
    return os.environ.get("CCSWITCH_DEPLOY_SOCKET", DEFAULT_DEPLOY_SOCKET)


def serve(socket_path: str | None = None) -> None:
    sock = socket_path or deploy_socket()
    repo = os.environ.get("CCSWITCH_DEPLOY_REPO", os.getcwd())
    host = os.environ.get("CCSWITCH_HOST", "127.0.0.1")
    port = os.environ.get("CCSWITCH_PORT", "8787")
    venv_pip = os.environ.get("CCSWITCH_DEPLOY_VENV_PIP", os.path.join(repo, ".venv", "bin", "pip"))
    import subprocess
    daemon_enabled = subprocess.run(
        ["systemctl", "is-enabled", "ccswitch-daemon.service"], capture_output=True).returncode == 0
    app, _, _ = build_controller_app(repo=repo, health_url=f"http://{host}:{port}/healthz",
                                     venv_pip=venv_pip, daemon_enabled=daemon_enabled)
    log.info("deploy controller serving on %s", sock)
    uvicorn.run(app, uds=sock, log_level="info")
```

In `server/deploy/__main__.py`, replace `main`'s argparse with a subcommand-aware version:

```python
def main(argv: list[str] | None = None) -> int:
    import sys
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "serve":
        from server.deploy import serve as _serve
        _serve.serve()
        return 0
    # ... existing --once parsing unchanged below ...
```

(Keep the existing `--once` block intact after this guard.)

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_cli.py -v`
Expected: PASS (5 tests total).

- [ ] **Step 5: Commit**

```bash
git add server/deploy/serve.py server/deploy/__main__.py server/tests/test_deploy_cli.py
git commit -m "feat(deploy): UDS serve entrypoint + 'serve' subcommand for the controller"
```

---

## Task 4: systemd unit for the controller

**Files:**
- Create: `deploy/ccswitch-deploy-controller.service`
- Modify: `deploy/README.md`

**Interfaces:** none (ops artifact). The service runs `python -m server.deploy serve` long-lived, with the same env file as `ccswitch.service`.

- [ ] **Step 1: Write the unit file**

```ini
# deploy/ccswitch-deploy-controller.service
[Unit]
Description=ccswitch deploy controller (poll origin/master + user-triggered deploys)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tony
EnvironmentFile=/etc/ccswitch/ccswitch.env
# CCSWITCH_DEPLOY_REPO must point at the deploy checkout (the master worktree).
WorkingDirectory=/home/tony/ccswitch
RuntimeDirectory=ccswitch
ExecStart=/home/tony/ccswitch/.venv/bin/python -m server.deploy serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Document install in `deploy/README.md`**

Add a "Deploy controller" section:

```markdown
## Deploy controller (`ccswitch-deploy-controller.service`)

Long-lived service that polls `origin/master` (notify-only) and runs user-triggered deploys.
Serves a UDS at `/run/ccswitch/deploy.sock`; the edge proxies `/api/deploy/*` to it.

    sudo install -m644 deploy/ccswitch-deploy-controller.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now ccswitch-deploy-controller.service

Replaces the retired `ccswitch-deploy.timer`/`.service` (no more auto-deploy).
```

- [ ] **Step 3: Commit** (no test — ops artifact)

```bash
git add deploy/ccswitch-deploy-controller.service deploy/README.md
git commit -m "feat(deploy): systemd unit for the long-lived deploy controller"
```

---

## Task 5: Edge-side UDS client (`DeployControllerClient`)

**Files:**
- Create: `server/deploy/client.py`
- Test: `server/tests/test_deploy_client.py`

**Interfaces:**
- Produces: `class DeployControllerClient(socket_path: str)` with `async get_status() -> dict`, `async start() -> tuple[int, dict]` (returns `(http_status, body)`), `async healthz() -> bool`. Mirrors `server/ipc/client.py` (httpx UDS transport, `DeployControllerUnavailable` on connect error).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_deploy_client.py
import pytest
import httpx
from server.deploy.client import DeployControllerClient, DeployControllerUnavailable


def _mock_transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_get_status_returns_body():
    def handler(req):
        return httpx.Response(200, json={"pending": {"behind": True}, "deploy": {"running": False}})
    c = DeployControllerClient("/tmp/x.sock")
    c._http = httpx.AsyncClient(transport=_mock_transport(handler), base_url="http://deploy")
    body = await c.get_status()
    assert body["pending"]["behind"] is True


@pytest.mark.asyncio
async def test_start_returns_status_and_body():
    def handler(req):
        return httpx.Response(409, json={"started": False, "reason": "in progress"})
    c = DeployControllerClient("/tmp/x.sock")
    c._http = httpx.AsyncClient(transport=_mock_transport(handler), base_url="http://deploy")
    code, body = await c.start()
    assert code == 409 and body["started"] is False


@pytest.mark.asyncio
async def test_connect_error_raises_unavailable():
    def handler(req):
        raise httpx.ConnectError("no sock")
    c = DeployControllerClient("/tmp/x.sock")
    c._http = httpx.AsyncClient(transport=_mock_transport(handler), base_url="http://deploy")
    with pytest.raises(DeployControllerUnavailable):
        await c.get_status()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_client.py -v`
Expected: FAIL — `ModuleNotFoundError: ... server.deploy.client`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/deploy/client.py
from __future__ import annotations
import httpx


class DeployControllerUnavailable(Exception):
    """The deploy controller socket could not be reached."""


class DeployControllerClient:
    def __init__(self, socket_path: str):
        self._sock = socket_path
        self._http = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            base_url="http://deploy", timeout=10.0)

    async def get_status(self) -> dict:
        try:
            r = await self._http.get("/status")
        except httpx.ConnectError as exc:
            raise DeployControllerUnavailable(str(exc)) from exc
        return r.json()

    async def start(self) -> tuple[int, dict]:
        try:
            r = await self._http.post("/start")
        except httpx.ConnectError as exc:
            raise DeployControllerUnavailable(str(exc)) from exc
        return r.status_code, r.json()

    async def healthz(self) -> bool:
        try:
            r = await self._http.get("/healthz")
            return r.status_code == 200
        except httpx.ConnectError:
            return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_deploy_client.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/deploy/client.py server/tests/test_deploy_client.py
git commit -m "feat(deploy): edge-side UDS client for the deploy controller"
```

---

## Task 6: Edge API router (`server/api/deploy.py`) + registration

**Files:**
- Create: `server/api/deploy.py`
- Modify: `server/app.py` (import + `include_router`)
- Test: `server/tests/test_api_deploy.py`

**Interfaces:**
- Consumes: `DeployControllerClient`, `current_identity`.
- Produces:
  - `GET /api/deploy/status` → proxies `client.get_status()`; on `DeployControllerUnavailable` → `{"pending": {"behind": false}, "deploy": {"running": false, "error": "controller unavailable"}}` with 200 (UI shows a soft "controller offline" state, never a hard error).
  - `POST /api/deploy/start` → proxies `client.start()`; returns the controller's status code + body. Unavailable → 503.
  - `router` exported; client built from `CCSWITCH_DEPLOY_SOCKET` (default `/run/ccswitch/deploy.sock`), overridable via `app.state.deploy_client` for tests.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_api_deploy.py
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.api.deploy import router, get_deploy_client
from server.auth.middleware import current_identity
from server.deploy.client import DeployControllerUnavailable


class FakeClient:
    def __init__(self, status_body=None, start_ret=(200, {"started": True}), unavailable=False):
        self._status = status_body or {"pending": {"behind": True}, "deploy": {"running": False}}
        self._start = start_ret
        self._unavailable = unavailable
    async def get_status(self):
        if self._unavailable:
            raise DeployControllerUnavailable("x")
        return self._status
    async def start(self):
        if self._unavailable:
            raise DeployControllerUnavailable("x")
        return self._start


def make_app(client):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[current_identity] = lambda: "tester"
    app.dependency_overrides[get_deploy_client] = lambda: client
    return TestClient(app)


def test_status_proxies_controller():
    c = make_app(FakeClient())
    body = c.get("/api/deploy/status").json()
    assert body["pending"]["behind"] is True


def test_status_soft_degrades_when_unavailable():
    c = make_app(FakeClient(unavailable=True))
    r = c.get("/api/deploy/status")
    assert r.status_code == 200 and r.json()["deploy"]["error"] == "controller unavailable"


def test_start_proxies_status_code():
    c = make_app(FakeClient(start_ret=(409, {"started": False, "reason": "in progress"})))
    r = c.post("/api/deploy/start")
    assert r.status_code == 409 and r.json()["started"] is False


def test_start_unavailable_is_503():
    c = make_app(FakeClient(unavailable=True))
    assert c.post("/api/deploy/start").status_code == 503
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_api_deploy.py -v`
Expected: FAIL — `ModuleNotFoundError: ... server.api.deploy`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/api/deploy.py
from __future__ import annotations
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from server.auth.middleware import current_identity
from server.deploy.client import DeployControllerClient, DeployControllerUnavailable

router = APIRouter(prefix="/api/deploy", tags=["deploy"])


def get_deploy_client(request: Request) -> DeployControllerClient:
    client = getattr(request.app.state, "deploy_client", None)
    if client is None:
        sock = os.environ.get("CCSWITCH_DEPLOY_SOCKET", "/run/ccswitch/deploy.sock")
        client = DeployControllerClient(sock)
        request.app.state.deploy_client = client
    return client


@router.get("/status")
async def deploy_status(_identity: str = Depends(current_identity),
                        client: DeployControllerClient = Depends(get_deploy_client)) -> dict:
    try:
        return await client.get_status()
    except DeployControllerUnavailable:
        return {"pending": {"behind": False, "commits": [], "ci": "unavailable"},
                "deploy": {"running": False, "phases": {}, "error": "controller unavailable",
                           "warnings": [], "targetSha": ""}}


@router.post("/start")
async def deploy_start(_identity: str = Depends(current_identity),
                       client: DeployControllerClient = Depends(get_deploy_client)) -> JSONResponse:
    try:
        code, body = await client.start()
        return JSONResponse(body, status_code=code)
    except DeployControllerUnavailable:
        return JSONResponse({"started": False, "reason": "controller unavailable"}, status_code=503)
```

In `server/app.py`, add with the other router imports/registrations:

```python
from server.api.deploy import router as deploy_router
# ... in the include_router block:
app.include_router(deploy_router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest server/tests/test_api_deploy.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add server/api/deploy.py server/app.py server/tests/test_api_deploy.py
git commit -m "feat(deploy): edge /api/deploy/{status,start} proxy (auth-gated, soft-degrades)"
```

---

## Task 7: Web API client + hooks

**Files:**
- Create: `web/src/api/deploy.ts`
- Modify: `web/src/api/hooks.ts`, `web/src/api/types.ts`
- Test: `web/src/api/hooks.deploy.test.tsx`

**Interfaces:**
- Produces (types in `types.ts`):
  ```ts
  export interface PendingDeploy { behind: boolean; liveSha: string; targetSha: string;
    commits: { sha: string; subject: string; author: string; iso: string }[]; ci: string }
  export interface DeployProgress { running: boolean; phases: Record<string, string>;
    error: string | null; warnings: string[]; targetSha: string }
  export interface DeployStatusResponse { pending: PendingDeploy; deploy: DeployProgress }
  ```
- `deploy.ts`: `fetchDeployStatus(): Promise<DeployStatusResponse>` (GET `/api/deploy/status`), `startDeploy(): Promise<{started: boolean; reason?: string}>` (POST `/api/deploy/start`) — both via the existing `apiFetch`/`client.ts` helper.
- `hooks.ts`: `usePendingDeploy()` and `useDeployStatus()` (one `useQuery` keyed `['deploy','status']`, `refetchInterval: 5_000`, returning the whole response; `usePendingDeploy` selects `.pending`, `useDeployStatus` selects `.deploy`), `useStartDeploy()` (`useMutation` → invalidates `['deploy','status']`).

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/api/hooks.deploy.test.tsx
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { usePendingDeploy } from './hooks'
import * as deploy from './deploy'

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: React.ReactNode }) =>
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('usePendingDeploy', () => {
  beforeEach(() => vi.restoreAllMocks())
  it('returns pending state from the API', async () => {
    vi.spyOn(deploy, 'fetchDeployStatus').mockResolvedValue({
      pending: { behind: true, liveSha: 'a', targetSha: 'b',
        commits: [{ sha: '1', subject: 's', author: 't', iso: 'i' }], ci: 'green' },
      deploy: { running: false, phases: {}, error: null, warnings: [], targetSha: 'b' },
    })
    const { result } = renderHook(() => usePendingDeploy(), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.data?.behind).toBe(true))
    expect(result.current.data?.commits[0].subject).toBe('s')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run src/api/hooks.deploy.test.tsx`
Expected: FAIL — `usePendingDeploy` / `fetchDeployStatus` not exported.

- [ ] **Step 3: Write minimal implementation**

```ts
// web/src/api/deploy.ts
import { apiFetch } from './client'
import type { DeployStatusResponse } from './types'

export function fetchDeployStatus(): Promise<DeployStatusResponse> {
  return apiFetch('/api/deploy/status')
}

export function startDeploy(): Promise<{ started: boolean; reason?: string }> {
  return apiFetch('/api/deploy/start', { method: 'POST' })
}
```

(Use the project's existing fetch helper name from `web/src/api/client.ts` — if it is not `apiFetch`, match the actual export, e.g. `api(...)`.)

```ts
// add to web/src/api/hooks.ts
import { fetchDeployStatus, startDeploy } from './deploy'
import type { DeployStatusResponse, PendingDeploy, DeployProgress } from './types'

export function useDeployStatusResponse() {
  return useQuery<DeployStatusResponse>({
    queryKey: ['deploy', 'status'],
    queryFn: fetchDeployStatus,
    refetchInterval: 5_000,
  })
}

export function usePendingDeploy() {
  const q = useDeployStatusResponse()
  return { ...q, data: q.data?.pending as PendingDeploy | undefined }
}

export function useDeployStatus() {
  const q = useDeployStatusResponse()
  return { ...q, data: q.data?.deploy as DeployProgress | undefined }
}

export function useStartDeploy() {
  const qc = useQueryClient()
  return useMutation<{ started: boolean; reason?: string }, ApiRequestError, void>({
    mutationFn: () => startDeploy(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['deploy', 'status'] }),
  })
}
```

Add the three interfaces from the **Interfaces** block to `web/src/api/types.ts`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run src/api/hooks.deploy.test.tsx && npx tsc -b`
Expected: PASS; tsc clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/deploy.ts web/src/api/hooks.ts web/src/api/types.ts web/src/api/hooks.deploy.test.tsx
git commit -m "feat(web): deploy status/start API client + usePendingDeploy/useDeployStatus/useStartDeploy hooks"
```

---

## Task 8: `DeployPanel` UI + Dashboard integration

**Files:**
- Create: `web/src/components/DeployPanel.tsx`
- Modify: `web/src/components/Dashboard.tsx`
- Test: `web/src/components/DeployPanel.test.tsx`; update Dashboard test factory mocks
- Reference: `web/src/hooks/useVersionWatch.ts` (reuse as the "deploy done → reload" trigger)

**Interfaces:**
- Consumes: `usePendingDeploy`, `useDeployStatus`, `useStartDeploy`.
- Produces: `<DeployPanel/>` — renders nothing when `!pending.behind && !deploy.running`; otherwise:
  - **Banner**: "N changes pending deploy" (N = `pending.commits.length`) + expandable commit list (subject · author · short sha).
  - **Deploy now** button: disabled when `deploy.running` OR `pending.ci !== 'green'`; shows the disabled reason (`CI ${pending.ci}` or "deploying…").
  - **Progress checklist**: when `deploy.running` or has phases, list the 5 phases (save/update/report/restart/resume) with their `PhaseState` (pending/running/done/failed), and `deploy.error`/`warnings` if present.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/DeployPanel.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { DeployPanel } from './DeployPanel'
import * as hooks from '@/api/hooks'

function mockHooks(pending: any, deploy: any, mutate = vi.fn()) {
  vi.spyOn(hooks, 'usePendingDeploy').mockReturnValue({ data: pending } as any)
  vi.spyOn(hooks, 'useDeployStatus').mockReturnValue({ data: deploy } as any)
  vi.spyOn(hooks, 'useStartDeploy').mockReturnValue({ mutate, isPending: false } as any)
}

describe('DeployPanel', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('renders nothing when up to date and idle', () => {
    mockHooks({ behind: false, commits: [], ci: 'green' }, { running: false, phases: {} })
    const { container } = render(<DeployPanel />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows pending count and enables Deploy when CI green', () => {
    const mutate = vi.fn()
    mockHooks(
      { behind: true, commits: [{ sha: 'abc1234', subject: 'feat x', author: 't', iso: 'i' }], ci: 'green' },
      { running: false, phases: {} }, mutate)
    render(<DeployPanel />)
    expect(screen.getByText(/1 change.*pending/i)).toBeInTheDocument()
    const btn = screen.getByRole('button', { name: /deploy now/i })
    expect(btn).toBeEnabled()
    fireEvent.click(btn)
    expect(mutate).toHaveBeenCalled()
  })

  it('disables Deploy with reason when CI not green', () => {
    mockHooks({ behind: true, commits: [{ sha: 'a', subject: 's', author: 't', iso: 'i' }], ci: 'pending' },
              { running: false, phases: {} })
    render(<DeployPanel />)
    expect(screen.getByRole('button', { name: /deploy now/i })).toBeDisabled()
    expect(screen.getByText(/CI pending/i)).toBeInTheDocument()
  })

  it('renders the phase checklist while running', () => {
    mockHooks({ behind: true, commits: [], ci: 'green' },
              { running: true, phases: { save: 'done', update: 'running' }, warnings: [] })
    render(<DeployPanel />)
    expect(screen.getByText(/update/i)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run src/components/DeployPanel.test.tsx`
Expected: FAIL — `DeployPanel` not found.

- [ ] **Step 3: Write minimal implementation**

```tsx
// web/src/components/DeployPanel.tsx
import { useState } from 'react'
import { usePendingDeploy, useDeployStatus, useStartDeploy } from '@/api/hooks'

const PHASES = ['save', 'update', 'report', 'restart', 'resume'] as const
const STATE_ICON: Record<string, string> = {
  pending: '○', running: '◐', done: '●', failed: '✗',
}

export function DeployPanel() {
  const { data: pending } = usePendingDeploy()
  const { data: deploy } = useDeployStatus()
  const start = useStartDeploy()
  const [open, setOpen] = useState(false)

  const behind = pending?.behind ?? false
  const running = deploy?.running ?? false
  const hasPhases = deploy && Object.keys(deploy.phases ?? {}).length > 0
  if (!behind && !running && !hasPhases) return null

  const count = pending?.commits.length ?? 0
  const ci = pending?.ci ?? 'unavailable'
  const canDeploy = behind && !running && ci === 'green'
  const reason = running ? 'deploying…' : ci !== 'green' ? `CI ${ci}` : ''

  return (
    <div className="rounded-lg border border-amber-300 bg-amber-50 dark:bg-amber-950/30 p-3 text-sm">
      {behind && (
        <div className="flex items-center justify-between gap-3">
          <button className="font-medium" onClick={() => setOpen(o => !o)}>
            {count} change{count === 1 ? '' : 's'} pending deploy {open ? '▾' : '▸'}
          </button>
          <div className="flex items-center gap-2">
            {reason && <span className="text-xs text-amber-700 dark:text-amber-400">{reason}</span>}
            <button
              className="rounded bg-amber-600 px-3 py-1 text-white disabled:opacity-50"
              disabled={!canDeploy}
              onClick={() => start.mutate()}
            >Deploy now</button>
          </div>
        </div>
      )}
      {open && pending && (
        <ul className="mt-2 space-y-1">
          {pending.commits.map(c => (
            <li key={c.sha} className="flex gap-2 font-mono text-xs">
              <span className="text-amber-700 dark:text-amber-400">{c.sha.slice(0, 7)}</span>
              <span className="flex-1 truncate">{c.subject}</span>
              <span className="text-gray-500">{c.author}</span>
            </li>
          ))}
        </ul>
      )}
      {(running || hasPhases) && deploy && (
        <div className="mt-2 border-t border-amber-200 pt-2">
          <ol className="space-y-0.5">
            {PHASES.map(p => {
              const s = deploy.phases?.[p] ?? 'pending'
              return (
                <li key={p} className="flex gap-2 text-xs">
                  <span aria-hidden>{STATE_ICON[s] ?? '○'}</span>
                  <span className={s === 'failed' ? 'text-red-600' : ''}>{p}</span>
                  <span className="text-gray-500">{s}</span>
                </li>
              )
            })}
          </ol>
          {deploy.error && <p className="mt-1 text-xs text-red-600">{deploy.error}</p>}
          {deploy.warnings?.map((w, i) => (
            <p key={i} className="mt-1 text-xs text-amber-700">{w}</p>
          ))}
        </div>
      )}
    </div>
  )
}
```

In `web/src/components/Dashboard.tsx`, render `<DeployPanel/>` near the top of the dashboard body (above the sessions list). Import it: `import { DeployPanel } from './DeployPanel'`.

- [ ] **Step 4: Fix Dashboard test factory mocks**

Adding `usePendingDeploy`/`useDeployStatus`/`useStartDeploy` calls (via `<DeployPanel/>`) means any Dashboard test that mocks `@/api/hooks` with a partial factory must add these three (memory: `dashboard-hooks-partial-mocks`). In each failing Dashboard test's `vi.mock('@/api/hooks', ...)`, add:

```ts
usePendingDeploy: () => ({ data: { behind: false, commits: [], ci: 'green' } }),
useDeployStatus: () => ({ data: { running: false, phases: {} } }),
useStartDeploy: () => ({ mutate: vi.fn(), isPending: false }),
```

Run the Dashboard suite to find which files need it:
`cd web && npx vitest run src/components/Dashboard` → patch each `Cannot read properties of undefined` factory.

- [ ] **Step 5: Run the full web gate, then commit**

Run:
```bash
cd web
npx vitest run src/components/DeployPanel.test.tsx src/components/Dashboard
npx tsc -b
npx eslint src/components/DeployPanel.tsx src/api/deploy.ts src/api/hooks.ts --max-warnings 0
```
Expected: all PASS; tsc + eslint clean.

```bash
git add web/src/components/DeployPanel.tsx web/src/components/Dashboard.tsx \
        web/src/components/DeployPanel.test.tsx
git add -u web/src/components   # picks up patched Dashboard test mocks
git commit -m "feat(web): DeployPanel — pending banner + commit list + Deploy-now + progress checklist"
```

---

## Self-Review

**Spec coverage (Plan 2 scope):**
- Long-lived controller service subsuming the timer → Tasks 2–4. ✓
- Notify-only origin/master detection → Task 1. ✓
- Edge proxies status survives edge restart (controller is long-lived) → Tasks 5–6. ✓
- "List which changes are pending" (commit list) → Task 1 `log_range` + Task 8 commit list. ✓
- One-click Deploy-now + live progress checklist → Task 8. ✓
- Deploy-now disabled w/ reason when CI not green / running → Task 8. ✓
- Auth-gated endpoints → Task 6 (`current_identity`). ✓
- Reuse, not duplicate, `updatePending`/`useVersionWatch` → Global Constraints + Task 8 reference. ✓

**Placeholder scan:** none — every step has runnable code + a concrete command. The one judgement call (the `apiFetch` helper name) is flagged inline to match the actual `client.ts` export.

**Type consistency:** `PendingState.to_dict()` keys (`behind/liveSha/targetSha/commits/ci`) match the TS `PendingDeploy`; `_deploy_dict()` keys (`running/phases/error/warnings/targetSha`) match `DeployProgress`; `DeployControllerClient.start()->(int,dict)` consumed correctly in Task 6; `get_deploy_client` dependency overridden in Task 6 tests. ✓

**Deferred to Plan 3:** the SAVE/RESUME phases still no-op in the controller; the checklist will simply show them `pending→done` instantly until Plan 3 wires snapshot/restore. Per-session restore-retry UI is Plan 3.
