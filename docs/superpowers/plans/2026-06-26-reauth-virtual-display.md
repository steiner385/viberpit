# Reauth on the Virtual X Display — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the Claude reauth/login browser on a per-session Xvfb display (instead of the physical `:0`) and surface it through the existing web VNC viewer, so reauth can be completed remotely from the browser.

**Architecture:** Run the whole reauth flow on the host that owns the X displays (the daemon in remote/prod mode, the edge in-process) on a daemon-allocated Xvfb display under a reserved pseudo-session key `__reauth__`. The edge proxies `start`/`status`/`cancel` over the existing `/rpc` channel (`RemoteReauthManager` ↔ `reauth_rpc.py`), mirroring the existing `InProcessOrchestratorAdapter` / `RemoteOrchestrator` split. The web VNC viewer rides the existing `/ws/vnc?session=__reauth__` path unchanged.

**Tech Stack:** Python FastAPI + asyncio + SQLAlchemy backend · React + Vite + TS + Tailwind + xterm/noVNC frontend · Xvfb/x11vnc virtual displays · pytest / vitest.

**Design doc:** `docs/superpowers/specs/2026-06-26-reauth-virtual-display-design.md`

## Global Constraints

- **Never worse than today:** when xvfb is disabled/missing, the pool is exhausted, or `reauth_virtual_display=False`, the reauth env must be byte-for-byte today's (`DISPLAY=settings.reauth_display` = `:0`, **no** `XAUTHORITY`).
- **Reserved key:** `REAUTH_DISPLAY_KEY = "__reauth__"` (the `__` prefix cannot collide with a UUID session id). Define once in `server/reauth.py`; import it elsewhere — never re-literal it.
- **`config.py` defines settings twice** (a pydantic `BaseSettings` block ~line 30 **and** a dataclass fallback ~line 98). Any new setting MUST be added to **both**.
- **Reauth is one-at-a-time** (`ReauthManager` lock; add-account + reauth share one manager). One reserved key only.
- **Frontend gating is backend-driven:** the UI shows the VNC panel iff `reauthStatus.vncSession` is truthy and connects `BrowserTab` to *that* key — never hardcode `__reauth__` in the frontend.
- **Real gates:** backend = `ruff` + `mypy` + `pytest`; web (in `web/`) = `tsc -b` (NOT `tsc --noEmit`, which checks nothing here) + `eslint --max-warnings 0` + `vitest run` (CI runs the FULL vitest suite).
- **Worktree test env:** run backend tests against the main checkout's prebuilt venv with `PYTHONPATH=/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084/src`; for web tests `rm -f web/node_modules && npm ci` in `web/`.
- **Commits:** end every commit message with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Stay on branch `feat/reauth-virtual-display`; do NOT run the PR/merge workflow per-task (the controller owns integration).

---

## File Structure

- `server/reauth.py` — MODIFY: add `x11` accessor param, `REAUTH_DISPLAY_KEY`, `vnc_session` state field + `to_dict`, runner display lease bracket.
- `server/config.py` — MODIFY: add `reauth_virtual_display` to both settings definitions.
- `server/reauth_remote.py` — CREATE: `RemoteReauthManager` (edge proxy) + `InProcessReauthAdapter`.
- `server/ipc/reauth_rpc.py` — CREATE: `register_reauth(app, reauth)` with 3 handlers.
- `server/ipc/serve.py` — MODIFY: `build_daemon` constructs daemon `ReauthManager` + registers reauth RPC; `daemon_lifespan` sweeps/shuts down reauth.
- `server/app.py` — MODIFY: edge wiring (`RemoteReauthManager` in remote mode, `InProcessReauthAdapter` in-process; pass `x11` accessor).
- `server/api/accounts.py` — MODIFY: `await reauth.status(...)` at the two sync call sites.
- `web/src/api/types.ts` — MODIFY: `ReauthState.vncSession`.
- `web/src/components/ReauthBrowserPanel.tsx` — CREATE: VNC panel wrapper.
- `web/src/components/AccountsManager.tsx` — MODIFY: render panel + copy fix in `AccountRow`.
- `web/src/components/AddAccountDialog.tsx` — MODIFY: render panel + copy fix.
- Tests: `server/tests/test_reauth_manager.py` (extend — existing reauth-manager tests), `server/tests/test_config_xvfb.py` (extend — uses an `importlib.reload(config)`+`get_settings()` `_fresh()` helper), `server/tests/test_reauth_rpc.py` (new), `server/tests/test_reauth_remote.py` (new), `server/tests/test_reauth_api.py` (extend — existing reauth API tests, has identity-header fixtures), `web/src/components/__tests__/AccountReauth.test.tsx` (extend), `web/src/components/__tests__/ReauthBrowserPanel.test.tsx` (new).
- **Paths:** `/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084` = `/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084`; `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/` = `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/` (python, pytest, ruff, mypy live there). `web/node_modules` is absent → run `npm ci` in `web/` first for web tasks.

---

## Task 1: `ReauthManager` x11 accessor, reserved key, `vnc_session`

**Files:**
- Modify: `server/reauth.py`
- Test: `server/tests/test_reauth_manager.py`

**Interfaces:**
- Produces: module constant `REAUTH_DISPLAY_KEY = "__reauth__"`; `ReauthManager(__init__)` gains keyword param `x11: Callable[[], "X11Manager | None"] | None = None` stored as `self._x11`; `ReauthState.vnc_session: str | None = None`; `ReauthState.to_dict()` includes `"vncSession"`.
- Consumes: `X11Manager.start_display(key) -> DisplaySpec | None`, `.stop_display(key)`, `DisplaySpec.lease.display: int`, `DisplaySpec.xauthority: str` (from `server/sessions/x11manager.py`).

- [ ] **Step 1: Write failing tests** in `server/tests/test_reauth_manager.py`

```python
import asyncio
import pytest
from server.reauth import ReauthManager, ReauthState, REAUTH_DISPLAY_KEY


class _FakeLease:
    def __init__(self, display): self.display = display


class _FakeSpec:
    def __init__(self, display, xauth):
        self.lease = _FakeLease(display)
        self.xauthority = xauth


class _FakeX11:
    """Records start/stop and hands back a fixed display spec."""
    def __init__(self, spec=_FakeSpec(105, "/tmp/ccswitch-__reauth__.Xauthority")):
        self.spec = spec
        self.started = []
        self.stopped = []
    async def start_display(self, key):
        self.started.append(key)
        return self.spec
    async def stop_display(self, key):
        self.stopped.append(key)


def test_reauth_state_to_dict_includes_vnc_session():
    s = ReauthState(state="running", vnc_session="__reauth__")
    assert s.to_dict()["vncSession"] == "__reauth__"


def test_reauth_state_vnc_session_defaults_none():
    assert ReauthState().to_dict()["vncSession"] is None


@pytest.mark.asyncio
async def test_runner_allocates_virtual_display_and_releases(monkeypatch):
    # Capture the env_extra the runner passes to capture_login.
    captured = {}

    def fake_capture_login(name, env_extra=None):
        captured.update(env_extra or {})
        return True

    monkeypatch.setattr("ccswitch.auth_login.capture_login", fake_capture_login)
    monkeypatch.setattr("ccswitch.auth_login.prepare_reauth_profile", lambda p: "/tmp/reauth-tmp-x")
    monkeypatch.setattr("ccswitch.auth_login.cleanup_reauth_profile", lambda t: None)
    monkeypatch.setattr("ccswitch.accounts.mark_reauthenticated", lambda n: None)

    fake = _FakeX11()
    mgr = ReauthManager(x11=lambda: fake, key_available=lambda: True, timeout_s=30)
    await mgr.start("primary", "me@example.com")
    # wait for the background task to finish
    await asyncio.sleep(0.05)
    assert fake.started == [REAUTH_DISPLAY_KEY]
    assert fake.stopped == [REAUTH_DISPLAY_KEY]      # released in finally
    assert captured["DISPLAY"] == ":105"
    assert captured["XAUTHORITY"] == "/tmp/ccswitch-__reauth__.Xauthority"


@pytest.mark.asyncio
async def test_runner_falls_back_to_host_display_without_x11(monkeypatch):
    captured = {}
    monkeypatch.setattr("ccswitch.auth_login.capture_login",
                        lambda name, env_extra=None: captured.update(env_extra or {}) or True)
    monkeypatch.setattr("ccswitch.auth_login.prepare_reauth_profile", lambda p: "/tmp/reauth-tmp-y")
    monkeypatch.setattr("ccswitch.auth_login.cleanup_reauth_profile", lambda t: None)
    monkeypatch.setattr("ccswitch.accounts.mark_reauthenticated", lambda n: None)

    mgr = ReauthManager(x11=None, key_available=lambda: True, timeout_s=30)   # no x11
    await mgr.start("primary", "me@example.com")
    await asyncio.sleep(0.05)
    assert captured["DISPLAY"] == ":0"          # settings.reauth_display default
    assert "XAUTHORITY" not in captured         # untouched fallback env
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084 && PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_reauth_manager.py -k "vnc_session or virtual_display or host_display" -v`
Expected: FAIL — `ImportError: cannot import name 'REAUTH_DISPLAY_KEY'` / `TypeError: ... unexpected keyword 'x11'`.

- [ ] **Step 3: Implement in `server/reauth.py`**

After the imports / `log =` line, add the constant:

```python
REAUTH_DISPLAY_KEY = "__reauth__"   # reserved pseudo-session key for the reauth virtual display
```

Add the field to `ReauthState` (after `_temp_profile`):

```python
    _temp_profile: str | None = field(default=None, repr=False, compare=False)
    vnc_session: str | None = None   # reserved key while a virtual display is live; None on :0 fallback
```

Add to `ReauthState.to_dict()`:

```python
            "startedAt": self.started_at,
            "vncSession": self.vnc_session,
```

Add the `x11` param to `__init__` (after `timeout_s`):

```python
        runner: Callable[[str], Awaitable[bool]] | None = None,
        key_available: Callable[[], bool] | None = None,
        timeout_s: float = 900.0,
        x11: "Callable[[], object] | None" = None,
    ) -> None:
```

Store it before the runner is built:

```python
        self._key_available = key_available
        self._timeout_s = timeout_s
        self._x11 = x11
        self._state = ReauthState()
```

In `_make_default_runner`'s `_production_runner`, replace the `env_extra` block + `capture_login` with the lease bracket:

```python
            temp = auth_login.prepare_reauth_profile(chrome_profile)
            manager._state._temp_profile = temp
            x11 = manager._x11() if manager._x11 else None
            spec = None
            if x11 is not None and getattr(settings, "reauth_virtual_display", True):
                spec = await x11.start_display(REAUTH_DISPLAY_KEY)
            try:
                env_extra = {
                    "BROWSER": launcher_real,
                    "CCSWITCH_REAUTH_PROFILE": temp,
                }
                if spec is not None:
                    env_extra["DISPLAY"] = f":{spec.lease.display}"
                    env_extra["XAUTHORITY"] = spec.xauthority
                    manager._state.vnc_session = REAUTH_DISPLAY_KEY
                else:
                    env_extra["DISPLAY"] = reauth_display
                ok = await asyncio.to_thread(
                    auth_login.capture_login, name, env_extra=env_extra
                )
                if ok:
                    from ccswitch import accounts
                    await asyncio.to_thread(accounts.mark_reauthenticated, name)
                return ok
            finally:
                manager._state._temp_profile = None
                auth_login.cleanup_reauth_profile(temp)
                if spec is not None:
                    await x11.stop_display(REAUTH_DISPLAY_KEY)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084 && PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_reauth_manager.py -v`
Expected: PASS (including the pre-existing reauth tests).

- [ ] **Step 5: Lint**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084 && /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/ruff check server/reauth.py && /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/mypy server/reauth.py`
Expected: clean (mypy may skip untyped bodies — no new errors).

- [ ] **Step 6: Commit**

```bash
git add server/reauth.py server/tests/test_reauth_manager.py
git commit -m "feat(reauth): lease a virtual X display for the reauth browser

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `reauth_virtual_display` config (both definitions)

**Files:**
- Modify: `server/config.py`
- Test: `server/tests/test_config_xvfb.py` (extend; create if absent)

**Interfaces:**
- Produces: `settings.reauth_virtual_display: bool` (default `True`, env alias `CCSWITCH_REAUTH_VIRTUAL_DISPLAY`).

- [ ] **Step 1: Write failing test** in `server/tests/test_config_xvfb.py`

This file already has a module-level `_fresh()` helper (`importlib.reload(config); return config.get_settings()`) used by the existing xvfb tests — reuse it; do NOT import `Settings` directly.

```python
def test_reauth_virtual_display_defaults_true(monkeypatch):
    monkeypatch.delenv("CCSWITCH_REAUTH_VIRTUAL_DISPLAY", raising=False)
    s = _fresh()
    assert s.reauth_virtual_display is True


def test_reauth_virtual_display_env_override(monkeypatch):
    monkeypatch.setenv("CCSWITCH_REAUTH_VIRTUAL_DISPLAY", "false")
    s = _fresh()
    assert s.reauth_virtual_display is False
```

- [ ] **Step 2: Run to verify fail**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084 && PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_config_xvfb.py -k reauth_virtual_display -v`
Expected: FAIL — `AttributeError: ... 'reauth_virtual_display'`.

- [ ] **Step 3: Implement — add to BOTH definitions in `server/config.py`**

In the pydantic block (next to `reauth_display`, ~line 30):

```python
        reauth_virtual_display: bool = Field(default=True, alias="CCSWITCH_REAUTH_VIRTUAL_DISPLAY")
```

In the dataclass fallback (next to the `reauth_display` field, ~line 98). There is NO `_env_bool` helper — the neighboring `enable_xvfb` field uses an inline truthy check; mirror it exactly, but default to TRUE:

```python
        reauth_virtual_display: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_REAUTH_VIRTUAL_DISPLAY", "true").lower() in ("1", "true", "yes", "on")
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084 && PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_config_xvfb.py -k reauth_virtual_display -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/config.py server/tests/test_config_xvfb.py
git commit -m "feat(config): add CCSWITCH_REAUTH_VIRTUAL_DISPLAY toggle

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Daemon reauth RPC surface (`reauth_rpc.py`)

**Files:**
- Create: `server/ipc/reauth_rpc.py`
- Test: `server/tests/test_reauth_rpc.py`

**Interfaces:**
- Consumes: `register_rpc(app, name, fn)` from `server/ipc/daemon.py`; a `ReauthManager`-like object with async `start(name, identity)`, sync `status(name)`, async `cancel(name)` each returning/`->` `ReauthState` (has `.to_dict()`).
- Produces: `register_reauth(app, reauth) -> None` registering RPC methods `reauth_start`, `reauth_status`, `reauth_cancel`.

- [ ] **Step 1: Write failing test** in `server/tests/test_reauth_rpc.py`

```python
import pytest
from server.ipc.daemon import create_daemon_app
from server.ipc.reauth_rpc import register_reauth
from server.reauth import ReauthState


class _FakeReauth:
    def __init__(self):
        self.calls = []
    async def start(self, name, identity):
        self.calls.append(("start", name, identity))
        return ReauthState(state="running", identity=identity, vnc_session="__reauth__")
    def status(self, name):
        return ReauthState(state="running", vnc_session="__reauth__")
    async def cancel(self, name):
        self.calls.append(("cancel", name))
        return ReauthState(state="idle", message="cancelled")


@pytest.mark.asyncio
async def test_register_reauth_dispatches():
    app = create_daemon_app()
    fake = _FakeReauth()
    register_reauth(app, fake)
    start = app.state.rpc["reauth_start"]
    status = app.state.rpc["reauth_status"]
    cancel = app.state.rpc["reauth_cancel"]

    r = await start({"name": "primary", "identity": "me@example.com"})
    assert r["state"] == "running" and r["vncSession"] == "__reauth__"
    assert ("start", "primary", "me@example.com") in fake.calls

    s = await status({"name": "primary"})
    assert s["state"] == "running"

    c = await cancel({"name": "primary"})
    assert c["state"] == "idle" and c["message"] == "cancelled"
```

- [ ] **Step 2: Run to verify fail**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084 && PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_reauth_rpc.py -v`
Expected: FAIL — `ModuleNotFoundError: server.ipc.reauth_rpc`.

- [ ] **Step 3: Implement `server/ipc/reauth_rpc.py`**

```python
"""Daemon RPC surface for the reauth state machine — thin wrappers over a ReauthManager.

The edge proxies start/status/cancel here in remote mode so the reauth browser launches
on the daemon host, where the X11 virtual displays live (server/sessions/x11manager.py)."""
from __future__ import annotations

from fastapi import FastAPI

from server.ipc.daemon import register_rpc


def register_reauth(app: FastAPI, reauth) -> None:
    async def reauth_start(p: dict) -> dict:
        return (await reauth.start(p["name"], p["identity"])).to_dict()

    async def reauth_status(p: dict) -> dict:
        return reauth.status(p.get("name", "")).to_dict()

    async def reauth_cancel(p: dict) -> dict:
        return (await reauth.cancel(p.get("name", ""))).to_dict()

    register_rpc(app, "reauth_start", reauth_start)
    register_rpc(app, "reauth_status", reauth_status)
    register_rpc(app, "reauth_cancel", reauth_cancel)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084 && PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_reauth_rpc.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + Commit**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084 && /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/ruff check server/ipc/reauth_rpc.py && /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/mypy server/ipc/reauth_rpc.py
git add server/ipc/reauth_rpc.py server/tests/test_reauth_rpc.py
git commit -m "feat(ipc): daemon reauth RPC handlers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Edge proxy + adapter (`reauth_remote.py`)

**Files:**
- Create: `server/reauth_remote.py`
- Test: `server/tests/test_reauth_remote.py`

**Interfaces:**
- Consumes: a `DaemonClient`-like object with `async call(method, **params) -> dict` that raises `DaemonError(type, message)` on an error envelope (`server/ipc/client.py`); `ReauthBusy`, `ReauthNoKey` from `server/reauth.py`.
- Produces:
  - `RemoteReauthManager(client)` with async `start(name, identity) -> ReauthState`, async `status(name) -> ReauthState`, async `cancel(name) -> ReauthState`. Re-raises `ReauthBusy`/`ReauthNoKey` when the daemon error `type` matches.
  - `InProcessReauthAdapter(manager)` with the SAME async signatures wrapping a real `ReauthManager` (async `start`/`cancel` awaited; sync `status` wrapped).
  - helper `ReauthState.from_dict(d) -> ReauthState`.

- [ ] **Step 1: Write failing tests** in `server/tests/test_reauth_remote.py`

```python
import pytest
from server.reauth import ReauthManager, ReauthState, ReauthBusy
from server.reauth_remote import RemoteReauthManager, InProcessReauthAdapter
from server.ipc.client import DaemonError


class _FakeClient:
    def __init__(self, responses=None, error=None):
        self.responses = responses or {}
        self.error = error
        self.calls = []
    async def call(self, method, **params):
        self.calls.append((method, params))
        if self.error:
            raise self.error
        return self.responses.get(method, {"state": "running", "vncSession": "__reauth__"})


@pytest.mark.asyncio
async def test_remote_start_returns_state():
    c = _FakeClient()
    mgr = RemoteReauthManager(c)
    s = await mgr.start("primary", "me@example.com")
    assert s.state == "running" and s.vnc_session == "__reauth__"
    assert c.calls[0] == ("reauth_start", {"name": "primary", "identity": "me@example.com"})


@pytest.mark.asyncio
async def test_remote_reraises_busy_by_type():
    c = _FakeClient(error=DaemonError("ReauthBusy", "reauth busy"))
    mgr = RemoteReauthManager(c)
    with pytest.raises(ReauthBusy):
        await mgr.start("primary", "me@example.com")


@pytest.mark.asyncio
async def test_remote_status_and_cancel():
    c = _FakeClient(responses={"reauth_status": {"state": "running", "vncSession": "__reauth__"},
                               "reauth_cancel": {"state": "idle", "message": "cancelled"}})
    mgr = RemoteReauthManager(c)
    assert (await mgr.status("primary")).vnc_session == "__reauth__"
    assert (await mgr.cancel("primary")).state == "idle"


@pytest.mark.asyncio
async def test_inprocess_adapter_wraps_sync_status():
    real = ReauthManager(key_available=lambda: True)
    adapter = InProcessReauthAdapter(real)
    s = await adapter.status("primary")        # sync status, awaited
    assert isinstance(s, ReauthState)
```

- [ ] **Step 2: Run to verify fail**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084 && PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_reauth_remote.py -v`
Expected: FAIL — `ModuleNotFoundError: server.reauth_remote`.

- [ ] **Step 3: Add `ReauthState.from_dict` to `server/reauth.py`**

```python
    @staticmethod
    def from_dict(d: dict) -> "ReauthState":
        return ReauthState(
            state=d.get("state", "idle"),
            message=d.get("message"),
            identity=d.get("identity"),
            started_at=d.get("startedAt"),
            vnc_session=d.get("vncSession"),
        )
```

- [ ] **Step 4: Implement `server/reauth_remote.py`**

```python
"""Edge-side reauth facade: a remote proxy to the daemon's ReauthManager, plus an in-process
adapter. Mirrors server/sessions/remote.py's RemoteOrchestrator / InProcessOrchestratorAdapter."""
from __future__ import annotations

from server.ipc.client import DaemonError
from server.reauth import ReauthBusy, ReauthNoKey, ReauthState

_ERROR_TYPES = {"ReauthBusy": ReauthBusy, "ReauthNoKey": ReauthNoKey}


def _reraise(exc: DaemonError):
    cls = _ERROR_TYPES.get(exc.err_type)   # DaemonError exposes .err_type (not .type)
    if cls is ReauthBusy:
        raise ReauthBusy(None, None) from exc
    if cls is ReauthNoKey:
        raise ReauthNoKey(str(exc)) from exc
    raise exc


class RemoteReauthManager:
    """Forwards start/status/cancel to the daemon over /rpc."""

    def __init__(self, client) -> None:
        self._client = client

    async def start(self, name: str, identity: str) -> ReauthState:
        try:
            d = await self._client.call("reauth_start", name=name, identity=identity)
        except DaemonError as exc:
            _reraise(exc)
        return ReauthState.from_dict(d)

    async def status(self, name: str) -> ReauthState:
        d = await self._client.call("reauth_status", name=name)
        return ReauthState.from_dict(d)

    async def cancel(self, name: str) -> ReauthState:
        d = await self._client.call("reauth_cancel", name=name)
        return ReauthState.from_dict(d)


class InProcessReauthAdapter:
    """Wraps a real ReauthManager behind the same async facade (sync status -> awaitable)."""

    def __init__(self, manager) -> None:
        self._m = manager

    async def start(self, name: str, identity: str) -> ReauthState:
        return await self._m.start(name, identity)

    async def status(self, name: str) -> ReauthState:
        return self._m.status(name)

    async def cancel(self, name: str) -> ReauthState:
        return await self._m.cancel(name)

    # passthroughs the lifespan/sweep code calls on the edge
    def sweep_stale(self) -> None:
        self._m.sweep_stale()

    async def shutdown(self) -> None:
        await self._m.shutdown()
```

- [ ] **Step 5: Run to verify pass**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084 && PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_reauth_remote.py server/tests/test_reauth_manager.py -v`
Expected: PASS.

- [ ] **Step 6: Lint + Commit**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084 && /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/ruff check server/reauth_remote.py server/reauth.py && /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/mypy server/reauth_remote.py
git add server/reauth_remote.py server/reauth.py server/tests/test_reauth_remote.py
git commit -m "feat(reauth): RemoteReauthManager proxy + InProcessReauthAdapter

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Wire daemon construction + lifespan

**Files:**
- Modify: `server/ipc/serve.py` (`build_daemon`, `daemon_lifespan`)
- Test: `server/tests/test_reauth_rpc.py` (extend with a `build_daemon` assertion)

**Interfaces:**
- Consumes: `register_reauth` (Task 3); `ReauthManager(x11=...)` (Task 1); `get_settings()`.
- Produces: daemon `app.state.reauth` is a real `ReauthManager` wired with `x11=lambda: app.state.x11`; RPC methods registered; lifespan sweeps at startup and shuts down at stop.

- [ ] **Step 1: Write failing test** (append to `server/tests/test_reauth_rpc.py`)

```python
def test_build_daemon_registers_reauth():
    from server.ipc.serve import build_daemon
    app, _orch = build_daemon()
    assert "reauth_start" in app.state.rpc
    assert "reauth_status" in app.state.rpc
    assert "reauth_cancel" in app.state.rpc
    assert app.state.reauth is not None
```

- [ ] **Step 2: Run to verify fail**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084 && PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_reauth_rpc.py::test_build_daemon_registers_reauth -v`
Expected: FAIL — `KeyError: 'reauth_start'` / `AttributeError: reauth`.

- [ ] **Step 3: Implement in `server/ipc/serve.py`**

Add imports near the top:

```python
from server.reauth import ReauthManager
from server.ipc.reauth_rpc import register_reauth
```

In `build_daemon`, after `register_x11(app, app.state.x11)`:

```python
    app.state.reauth = ReauthManager(
        timeout_s=get_settings().reauth_timeout_s,
        x11=lambda: app.state.x11,
    )
    register_reauth(app, app.state.reauth)
```

In `daemon_lifespan`, after the orchestrator start + x11 sweeps (before `yield`):

```python
    try:
        app.state.reauth.sweep_stale()
    except Exception:
        log.warning("daemon startup: reauth sweep_stale failed", exc_info=True)
```

In the `finally` block, after `x11.stop_all(...)` and before `await orchestrator.stop()`:

```python
        try:
            await app.state.reauth.shutdown()
        except Exception:
            log.warning("daemon shutdown: reauth shutdown failed", exc_info=True)
```

(`app` is in scope in `daemon_lifespan(app, orchestrator)`.)

- [ ] **Step 4: Run to verify pass**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084 && PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_reauth_rpc.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + Commit**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084 && /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/ruff check server/ipc/serve.py && /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/mypy server/ipc/serve.py
git add server/ipc/serve.py server/tests/test_reauth_rpc.py
git commit -m "feat(ipc): construct + sweep the daemon reauth manager

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Edge wiring + `await status` in API

**Files:**
- Modify: `server/app.py` (the reauth-attach block, currently ~`:339-343`), `server/api/accounts.py` (FOUR `reauth.status(...)` sites: lines 330, 341, 417, 435)
- Modify: `server/tests/test_reauth_api.py` — its `FakeReauthManager` is injected raw onto `app.state.reauth` (bypassing the adapter), so its `status` must become `async` and must copy `vnc_session`.

**Interfaces:**
- Consumes: `RemoteReauthManager`, `InProcessReauthAdapter` (Task 4).
- Produces: `app.state.reauth` is the async facade in both modes; all reauth endpoints await `status`.

**CRITICAL CONTEXT (verified):**
- `server/api/accounts.py` has FOUR non-awaited `reauth.status(...)` calls — lines **330** (`return reauth.status("").to_dict()`), **341** (`pre_cancel_state = reauth.status("")`), **417** (`state = reauth.status(name)`), **435** (`pre_cancel_state = reauth.status(name)`). The `reauth.start`/`reauth.cancel` calls (293, 342, 378, 436) are ALREADY awaited — leave them.
- `test_reauth_api.py` defines `FakeReauthManager` (async `start`/`cancel`, **sync** `status`) and injects it via `_make_client(fake_rm)` which does `app.state.reauth = fake_rm` AFTER `create_app` — so the adapter does NOT wrap it. Once endpoints await `status`, this fake's `status` must be `async def`. Also its `status()` rebuilds a `ReauthState` WITHOUT `vnc_session` — add `vnc_session=s.vnc_session` to that copy so the new test can assert it.
- `test_security_hardening.py` sets `app.state.reauth = ReauthManager()` but only drives `_lifespan` (boot gate) — it calls `sweep_stale()`/`shutdown()` (both exist on the raw manager), never the status endpoint. No change needed there.

- [ ] **Step 1: Make `FakeReauthManager.status` async + vnc_session-aware, then add the failing test** in `server/tests/test_reauth_api.py`

Change `def status` → `async def status` and add `vnc_session=s.vnc_session` to the `ReauthState(...)` it returns. Then add (reusing the file's existing `_make_client`, `temp_env`, `_seed_slot`, `IDENTITY_HEADERS` helpers):

```python
def test_reauth_status_returns_vnc_session():
    """GET reauth status surfaces the vncSession key when a virtual display is live."""
    fake_rm = FakeReauthManager()
    fake_rm._state = ReauthState(state="running", vnc_session="__reauth__")
    with temp_env():
        _seed_slot("primary")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
            client = _make_client(fake_rm)
        with client:
            resp = client.get("/api/accounts/primary/reauth", headers=IDENTITY_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["vncSession"] == "__reauth__"
```

- [ ] **Step 2: Run to verify fail**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084 && PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_reauth_api.py::test_reauth_status_returns_vnc_session -v`
Expected: FAIL — `vncSession` is `None` (the endpoint still calls sync `status` and the field isn't surfaced), or a coroutine error once the fake's status is async but the endpoint isn't awaiting yet.

- [ ] **Step 3: Implement edge wiring in `server/app.py`**

Replace the ReauthManager attach block (`:339-343`) with:

```python
    # Attach the reauth facade. Remote mode proxies to the daemon's manager (which owns
    # the X11 displays); in-process wraps a real manager behind the same async facade.
    from server.reauth_remote import InProcessReauthAdapter, RemoteReauthManager
    if getattr(app.state, "remote_mode", False):
        from server.ipc.client import DaemonClient
        app.state.reauth = RemoteReauthManager(DaemonClient(app.state.daemon_endpoint))
    else:
        from server.config import get_settings
        mgr = reauth_manager or ReauthManager(
            timeout_s=get_settings().reauth_timeout_s,
            x11=lambda: app.state.x11,
        )
        app.state.reauth = InProcessReauthAdapter(mgr)
```

(Confirm `DaemonClient(endpoint)` ctor signature matches the existing `RemoteOrchestrator` wiring at `:307-318`; reuse `app.state.daemon_endpoint`.)

- [ ] **Step 4: Update `server/api/accounts.py` — await all FOUR `status` sites**

Grep to confirm: `grep -n "reauth\.status\|reauth\.cancel\|reauth\.start" server/api/accounts.py`. Convert each `reauth.status(...)` call to `await reauth.status(...)`:
- Line 330: `return reauth.status("").to_dict()` → `return (await reauth.status("")).to_dict()`
- Line 341: `pre_cancel_state = reauth.status("")` → `pre_cancel_state = await reauth.status("")`
- Line 417: `state = reauth.status(name)` → `state = await reauth.status(name)`
- Line 435: `pre_cancel_state = reauth.status(name)` → `pre_cancel_state = await reauth.status(name)`

Do NOT touch the already-awaited `reauth.start(...)` (293, 378) or `reauth.cancel(...)` (342, 436). These are all inside `async def` endpoint handlers, so `await` is valid.

- [ ] **Step 5: Run to verify pass + regression**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084 && PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/ -k "reauth or accounts" -v`
Expected: PASS (watch for any test that injected a bare `ReauthManager` and called sync `.status` on `app.state.reauth` — those now get the adapter; update them to await).

- [ ] **Step 6: Full backend suite + lint + Commit**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084 && PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/ -q
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/ruff check server/app.py server/api/accounts.py && /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/mypy server/app.py server/api/accounts.py
git add server/app.py server/api/accounts.py server/tests/test_reauth_api.py
git commit -m "feat(reauth): edge facade wiring + await status in the API

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Frontend type + `ReauthBrowserPanel`

**Files:**
- Modify: `web/src/api/types.ts`
- Create: `web/src/components/ReauthBrowserPanel.tsx`
- Test: `web/src/components/__tests__/ReauthBrowserPanel.test.tsx`

**Interfaces:**
- Consumes: `BrowserTab` (`web/src/components/BrowserTab.tsx`, props `{ sessionId: string; statusState: string }`).
- Produces: `ReauthState.vncSession?: string | null`; `ReauthBrowserPanel({ vncSession }: { vncSession: string })`.

- [ ] **Step 1: Prep web env**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084/web && rm -f node_modules && npm ci`
Expected: completes (~seconds), lockfile committed.

- [ ] **Step 2: Write failing test** in `web/src/components/__tests__/ReauthBrowserPanel.test.tsx`

```tsx
import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'

// BrowserTab opens a real WebSocket; stub it so the panel renders in jsdom.
vi.mock('../BrowserTab', () => ({
  BrowserTab: ({ sessionId }: { sessionId: string }) => <div data-testid="browser-tab">{sessionId}</div>,
}))

import { ReauthBrowserPanel } from '../ReauthBrowserPanel'

describe('ReauthBrowserPanel', () => {
  it('renders a BrowserTab pointed at the given vnc session key', () => {
    render(<ReauthBrowserPanel vncSession="__reauth__" />)
    expect(screen.getByTestId('browser-tab')).toHaveTextContent('__reauth__')
  })
})
```

- [ ] **Step 3: Run to verify fail**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084/web && npx vitest run src/components/__tests__/ReauthBrowserPanel.test.tsx`
Expected: FAIL — cannot find `../ReauthBrowserPanel`.

- [ ] **Step 4: Add the type field** in `web/src/api/types.ts`

```tsx
export interface ReauthState {
  state: 'idle' | 'running' | 'cancelling' | 'success' | 'error' | 'timeout'
  message?: string | null
  startedAt?: number | null
  vncSession?: string | null
}
```

- [ ] **Step 5: Implement `web/src/components/ReauthBrowserPanel.tsx`**

```tsx
import { BrowserTab } from './BrowserTab'

/** In-page VNC viewer for the reauth browser, pointed at the backend-reported session key. */
export function ReauthBrowserPanel({ vncSession }: { vncSession: string }) {
  return (
    <div className="mt-2 h-[60vh] min-h-0 overflow-hidden rounded-md border">
      <BrowserTab sessionId={vncSession} statusState="working" />
    </div>
  )
}
```

- [ ] **Step 6: Run to verify pass**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084/web && npx vitest run src/components/__tests__/ReauthBrowserPanel.test.tsx`
Expected: PASS.

- [ ] **Step 7: Types + Commit**

```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084/web && npx tsc -b
git add web/src/api/types.ts web/src/components/ReauthBrowserPanel.tsx web/src/components/__tests__/ReauthBrowserPanel.test.tsx
git commit -m "feat(web): ReauthBrowserPanel + ReauthState.vncSession

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Render the panel in both reauth surfaces

**Files:**
- Modify: `web/src/components/AccountsManager.tsx` (the reauth status line in `AccountRow`, ~`:428-443`)
- Modify: `web/src/components/AddAccountDialog.tsx` (its running-state status text)
- Test: `web/src/components/__tests__/AccountReauth.test.tsx` (extend)

**Interfaces:**
- Consumes: `ReauthBrowserPanel` (Task 7); `reauthStatus.vncSession`, `isReauthRunning` (already in scope).

- [ ] **Step 1: Write/extend failing test** in `web/src/components/__tests__/AccountReauth.test.tsx`

Add a case asserting the panel mounts when status carries a `vncSession`, and that the copy switches. Mock `ReauthBrowserPanel` and the reauth-status hook to return `{ state: 'running', vncSession: '__reauth__' }`:

```tsx
vi.mock('../ReauthBrowserPanel', () => ({
  ReauthBrowserPanel: ({ vncSession }: { vncSession: string }) =>
    <div data-testid="reauth-vnc">{vncSession}</div>,
}))
// ...in a test where useReauthStatus resolves to {state:'running', vncSession:'__reauth__'}:
expect(await screen.findByTestId('reauth-vnc')).toHaveTextContent('__reauth__')
expect(screen.getByText(/sign in to claude in the window below/i)).toBeInTheDocument()
```

Also UPDATE the existing assertion that checks for the old "host machine's screen" copy so it only applies when `vncSession` is absent (or remove/repoint it to the fallback branch).

- [ ] **Step 2: Run to verify fail**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084/web && npx vitest run src/components/__tests__/AccountReauth.test.tsx`
Expected: FAIL — no `reauth-vnc` testid / copy mismatch.

- [ ] **Step 3: Implement in `AccountsManager.tsx`**

Import at top: `import { ReauthBrowserPanel } from './ReauthBrowserPanel'`.

Replace the running-state status line (currently the single `isReauthRunning` amber `<p>` with the "host machine's screen" copy) with:

```tsx
{!editing && isReauthRunning && (
  reauthStatus?.vncSession ? (
    <>
      <p className="text-xs text-amber-600 dark:text-amber-400">
        Sign in to Claude in the window below…
      </p>
      <ReauthBrowserPanel vncSession={reauthStatus.vncSession} />
    </>
  ) : (
    <p className="text-xs text-amber-600 dark:text-amber-400">
      Opening Chrome on the host screen — sign in there…
    </p>
  )
)}
```

- [ ] **Step 4: Mirror in `AddAccountDialog.tsx`**

Find its running-state status block (uses the add-account status query, same `ReauthState` shape) and apply the identical `vncSession ? <panel> : <host copy>` branch, importing `ReauthBrowserPanel`. Read the file's status-rendering region first and match its variable names (`addStatus?.vncSession` etc.).

- [ ] **Step 5: Run to verify pass**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084/web && npx vitest run src/components/__tests__/AccountReauth.test.tsx`
Expected: PASS.

- [ ] **Step 6: Full web gates**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084/web && npx tsc -b && npx eslint src --max-warnings 0 && npx vitest run`
Expected: all PASS (full vitest, per CI).

- [ ] **Step 7: Commit**

```bash
git add web/src/components/AccountsManager.tsx web/src/components/AddAccountDialog.tsx web/src/components/__tests__/AccountReauth.test.tsx
git commit -m "feat(web): show the reauth browser in-page via VNC

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Backend full suite + lint**

Run:
```bash
cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084 && PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/ -q
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/ruff check server && /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/mypy server
```
Expected: PASS / clean (env-only failures noted in CLAUDE.md — x11/remote_mode_wiring — are acceptable; nothing reauth-related red).

- [ ] **Step 2: Web full gates**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/9cd74084/web && npx tsc -b && npx eslint src --max-warnings 0 && npx vitest run`
Expected: PASS.

- [ ] **Step 3: Confirm no stale `:0`-only path left behind**

Run: `grep -rn "reauth_display\|__reauth__\|REAUTH_DISPLAY_KEY" server/ web/src/`
Expected: `reauth_display` only as the documented fallback; `REAUTH_DISPLAY_KEY` defined once and imported; no frontend hardcode of `__reauth__`.

- [ ] **Step 4: Final integration commit (if any fixups)**

```bash
git add -A && git commit -m "test: full-suite verification for reauth virtual display

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:** Component 1 (runner lease/fallback) → Task 1; `vnc_session`/`to_dict` → Task 1 + Task 4 (`from_dict`); config → Task 2; daemon RPC → Task 3; proxy/adapter → Task 4; daemon construct + lifespan sweep/shutdown → Task 5; edge wiring + `await status` → Task 6; frontend type + panel → Task 7; render gate + copy in both surfaces → Task 8; security/teardown/testing covered across Tasks 1/5/9. All design components map to a task.

**Type consistency:** `REAUTH_DISPLAY_KEY` defined once (Task 1), imported in Tasks 3-style tests/handlers; `ReauthState.vnc_session`/`vncSession`/`from_dict` consistent across Tasks 1/4/6/7; facade signatures (`async start/status/cancel`) identical across `RemoteReauthManager` and `InProcessReauthAdapter` (Task 4) and consumed by Task 6; `ReauthBrowserPanel({ vncSession })` consistent Tasks 7/8.

**Placeholders:** none — every code step shows full content; file-specific "read first and mirror" notes (config dataclass helper, AddAccountDialog var names) point at concrete existing patterns rather than deferring work.
