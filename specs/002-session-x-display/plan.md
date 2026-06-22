# Per-Session Virtual Browser with Human Takeover — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each managed `claude` session its own virtual X display so the agent can run a headed browser and a human can watch / take over that same browser from the web app over the existing Tailscale-auth path.

**Architecture:** A per-session `DisplayAllocator` hands out `(display, rfbport)` pairs; an `X11Manager` spawns/supervises Xvfb (at session create) and x11vnc (lazily on first human attach), plus optional managed Chromium. `DISPLAY`/`XAUTHORITY` are injected into the `claude` subprocess via a per-session factory closure (no factory-signature change). A FastAPI `/ws/vnc` route relays RFB bytes between noVNC and the loopback x11vnc port with flow control. Frontend adds a "Browser" tab with a noVNC canvas, state copy, reconnect, an "agent is driving" signal, and an open-tab action.

**Tech Stack:** Python 3 / FastAPI / asyncio / uvicorn · Xvfb · x11vnc · Playwright Chromium · React + Vite + TS + Tailwind/shadcn · noVNC (vendored) · pytest · vitest.

## Global Constraints

- Feature **off by default**: `CCSWITCH_ENABLE_XVFB=false`. Disabled deployments must pay zero cost (no Xvfb, no allocation).
- Every new config field is added to **both** the Pydantic `BaseSettings` branch **and** the dataclass fallback branch in `server/config.py` (the file duplicates every field; omitting the fallback is a silent `AttributeError` on hosts without `pydantic-settings`).
- Defaults (verbatim): `CCSWITCH_DISPLAY_START=100`, `CCSWITCH_DISPLAY_COUNT=12`, `CCSWITCH_MAX_BROWSERS=6`, `CCSWITCH_VNC_PORT_BASE=6100`, `CCSWITCH_XVFB_RESOLUTION=1280x1024x24`, `CCSWITCH_VNC_IDLE_TIMEOUT_S=1800`.
- All VNC/X resources bind **loopback only** (`-localhost`). The FastAPI route is the sole network gate, reusing `require_ws_identity`. No new port is exposed.
- Each Xvfb gets a **per-session xauth cookie**; x11vnc and the injected `claude` env both reference it via `XAUTHORITY`.
- Process teardown order, every time: clean (`proc.terminate()` → `await asyncio.wait_for(proc.wait(), timeout)` → `proc.kill()`); for Chromium use `server.sessions.adopt.terminate_process_tree` (it spawns a process tree). Children stay in the service cgroup (no `start_new_session`).
- Session create must **never fail** because the display pool or browser cap is exhausted — degrade to "no display" with a warning.
- noVNC pinned **≥ 1.4**, vendored under `web/`.
- Run server tests from this worktree with `PYTHONPATH=$(pwd)/src`, the main checkout's prebuilt `.venv` python, and either a pinned `CCSWITCH_TAILNET_ALLOWLIST` or `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1`. Web checks run in `web/`.
- Constitution v1.1.0 applies (no binary fork; per-process isolation; verify real; security-first; TDD; single source of truth; modern deps).

**Test runner shorthand** (used in every backend task below):
```bash
PY=/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python
run_pytest () { PYTHONPATH=$(pwd)/src CCSWITCH_DEV=1 env -u CCSWITCH_TAILNET_ALLOWLIST "$PY" -m pytest "$@"; }
```

---

## File Structure

**New (backend):**
- `server/sessions/x11manager.py` — `DisplayAllocator` + `X11Manager` (Xvfb/x11vnc/Chromium spawn/supervise, xauth, readiness poll, sweep, metrics, browser cap).
- `server/bridge/vnc_ws.py` — `/ws/vnc` router: auth, distinct close reasons, flow-controlled RFB relay, keepalive, attach tracking.
- `server/tests/test_x11_allocator.py`, `server/tests/test_x11_manager.py`, `server/tests/test_vnc_ws.py`, `server/tests/test_orchestrator_display.py` — tests.

**Modified (backend):**
- `server/config.py` — 7 settings, both branches.
- `server/sessions/sdk_factory.py` — optional `extra_env` param (backward compatible).
- `server/sessions/orchestrator.py` — allocate/reuse/teardown across all driver paths; hold an `X11Manager`.
- `server/app.py` — preflight + boot sweep on startup; close-all-sessions + X11 teardown on shutdown; mount `/ws/vnc`; `X11Manager` on `app.state`.
- `deploy/ccswitch.service` — `KillMode`, `TimeoutStopSec`, cgroup limits.
- Deploy idle-gate script — defer restart while a VNC client is attached.

**New (frontend):**
- `web/src/vnc/vncClient.ts` — thin WS+noVNC RFB wrapper with reconnect.
- `web/src/components/BrowserTab.tsx` — the Browser tab UI (states, badge, toggle, open-tab).
- `web/src/components/BrowserTab.test.tsx` — vitest.

**Modified (frontend):**
- `web/src/components/SessionView.tsx` (or the existing tab container) — register the Browser tab; two-stage Escape.
- `web/package.json` — `@novnc/novnc` ≥ 1.4 dependency.

---

# PHASE 1 — Display allocation, Xvfb, and agent headed-browser

Deliverable: with the feature enabled, a created session has `DISPLAY=:N` injected and a ready Xvfb; the agent's headed Playwright renders into it. No VNC yet. Disabled deployments unaffected.

### Task 1: Config settings (both branches)

**Files:**
- Modify: `server/config.py` (Pydantic branch ~15-40; dataclass branch ~54-90)
- Test: `server/tests/test_config_xvfb.py` (create)

**Interfaces:**
- Produces: `Settings` gains attributes `enable_xvfb: bool`, `display_start: int`, `display_count: int`, `max_browsers: int`, `vnc_port_base: int`, `xvfb_resolution: str`, `vnc_idle_timeout_s: float`.

- [ ] **Step 1: Write the failing test**
```python
# server/tests/test_config_xvfb.py
import importlib, os
import server.config as config

def _fresh():
    importlib.reload(config)
    return config.get_settings()

def test_xvfb_defaults(monkeypatch):
    for k in ("CCSWITCH_ENABLE_XVFB","CCSWITCH_DISPLAY_START","CCSWITCH_DISPLAY_COUNT",
              "CCSWITCH_MAX_BROWSERS","CCSWITCH_VNC_PORT_BASE","CCSWITCH_XVFB_RESOLUTION",
              "CCSWITCH_VNC_IDLE_TIMEOUT_S"):
        monkeypatch.delenv(k, raising=False)
    s = _fresh()
    assert s.enable_xvfb is False
    assert s.display_start == 100
    assert s.display_count == 12
    assert s.max_browsers == 6
    assert s.vnc_port_base == 6100
    assert s.xvfb_resolution == "1280x1024x24"
    assert s.vnc_idle_timeout_s == 1800.0

def test_xvfb_env_override(monkeypatch):
    monkeypatch.setenv("CCSWITCH_ENABLE_XVFB", "true")
    monkeypatch.setenv("CCSWITCH_DISPLAY_COUNT", "4")
    s = _fresh()
    assert s.enable_xvfb is True
    assert s.display_count == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `run_pytest server/tests/test_config_xvfb.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'enable_xvfb'`.

- [ ] **Step 3: Add fields to the Pydantic branch**

In the `BaseSettings` class (after `reauth_timeout_s`, before `model_config`):
```python
        enable_xvfb: bool = Field(default=False, alias="CCSWITCH_ENABLE_XVFB")
        display_start: int = Field(default=100, alias="CCSWITCH_DISPLAY_START")
        display_count: int = Field(default=12, alias="CCSWITCH_DISPLAY_COUNT")
        max_browsers: int = Field(default=6, alias="CCSWITCH_MAX_BROWSERS")
        vnc_port_base: int = Field(default=6100, alias="CCSWITCH_VNC_PORT_BASE")
        xvfb_resolution: str = Field(default="1280x1024x24", alias="CCSWITCH_XVFB_RESOLUTION")
        vnc_idle_timeout_s: float = Field(default=1800.0, alias="CCSWITCH_VNC_IDLE_TIMEOUT_S")
```

- [ ] **Step 4: Add the SAME fields to the dataclass fallback branch**

In the `@dataclasses.dataclass` `Settings` (after `reauth_timeout_s`):
```python
        enable_xvfb: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_ENABLE_XVFB", "false").lower() in ("1","true","yes","on")
        )
        display_start: int = dataclasses.field(
            default_factory=lambda: int(os.environ.get("CCSWITCH_DISPLAY_START", "100"))
        )
        display_count: int = dataclasses.field(
            default_factory=lambda: int(os.environ.get("CCSWITCH_DISPLAY_COUNT", "12"))
        )
        max_browsers: int = dataclasses.field(
            default_factory=lambda: int(os.environ.get("CCSWITCH_MAX_BROWSERS", "6"))
        )
        vnc_port_base: int = dataclasses.field(
            default_factory=lambda: int(os.environ.get("CCSWITCH_VNC_PORT_BASE", "6100"))
        )
        xvfb_resolution: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_XVFB_RESOLUTION", "1280x1024x24")
        )
        vnc_idle_timeout_s: float = dataclasses.field(
            default_factory=lambda: float(os.environ.get("CCSWITCH_VNC_IDLE_TIMEOUT_S", "1800"))
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `run_pytest server/tests/test_config_xvfb.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Commit**
```bash
git add server/config.py server/tests/test_config_xvfb.py
git commit -m "feat(002): xvfb/vnc config settings (both Settings branches)"
```

---

### Task 2: DisplayAllocator

**Files:**
- Create: `server/sessions/x11manager.py`
- Test: `server/tests/test_x11_allocator.py`

**Interfaces:**
- Produces:
  - `class DisplayLease` — frozen dataclass: `display: int`, `rfbport: int`.
  - `class DisplayAllocator(start: int, count: int, port_base: int)` with `async def alloc() -> DisplayLease` (raises `PoolExhausted` when empty), `async def release(lease: DisplayLease) -> None`, `def in_use() -> int`.
  - `class PoolExhausted(RuntimeError)`.

- [ ] **Step 1: Write the failing test**
```python
# server/tests/test_x11_allocator.py
import asyncio
import pytest
from server.sessions.x11manager import DisplayAllocator, PoolExhausted, DisplayLease

@pytest.mark.asyncio
async def test_alloc_release_roundtrip():
    a = DisplayAllocator(start=100, count=2, port_base=6100)
    l1 = await a.alloc(); l2 = await a.alloc()
    assert {l1.display, l2.display} == {100, 101}
    assert l1.rfbport == 6100 + (l1.display - 100)
    assert a.in_use() == 2
    await a.release(l1)
    assert a.in_use() == 1
    l3 = await a.alloc()
    assert l3.display == l1.display  # reused

@pytest.mark.asyncio
async def test_exhaustion_raises():
    a = DisplayAllocator(start=100, count=1, port_base=6100)
    await a.alloc()
    with pytest.raises(PoolExhausted):
        await a.alloc()

@pytest.mark.asyncio
async def test_no_double_alloc_under_concurrency():
    a = DisplayAllocator(start=100, count=50, port_base=6100)
    leases = await asyncio.gather(*[a.alloc() for _ in range(50)])
    assert len({l.display for l in leases}) == 50
```

- [ ] **Step 2: Run test to verify it fails**

Run: `run_pytest server/tests/test_x11_allocator.py -v`
Expected: FAIL — `ModuleNotFoundError: server.sessions.x11manager`.

- [ ] **Step 3: Write minimal implementation**
```python
# server/sessions/x11manager.py
"""Per-session virtual X display management (Xvfb + x11vnc + managed browser)."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass


class PoolExhausted(RuntimeError):
    """Raised when no display number is available."""


@dataclass(frozen=True)
class DisplayLease:
    display: int
    rfbport: int


class DisplayAllocator:
    """Asyncio-locked pool of (display, rfbport) pairs."""

    def __init__(self, start: int, count: int, port_base: int) -> None:
        self._free: list[int] = list(range(start, start + count))
        self._start = start
        self._port_base = port_base
        self._used: set[int] = set()
        self._lock = asyncio.Lock()

    async def alloc(self) -> DisplayLease:
        async with self._lock:
            if not self._free:
                raise PoolExhausted("display pool exhausted")
            display = self._free.pop(0)
            self._used.add(display)
            return DisplayLease(display=display, rfbport=self._port_base + (display - self._start))

    async def release(self, lease: DisplayLease) -> None:
        async with self._lock:
            if lease.display in self._used:
                self._used.discard(lease.display)
                self._free.append(lease.display)

    def in_use(self) -> int:
        return len(self._used)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `run_pytest server/tests/test_x11_allocator.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**
```bash
git add server/sessions/x11manager.py server/tests/test_x11_allocator.py
git commit -m "feat(002): DisplayAllocator (display+rfbport pool)"
```

---

### Task 3: X11Manager — Xvfb spawn, readiness, xauth, teardown, sweep, metrics

**Files:**
- Modify: `server/sessions/x11manager.py`
- Test: `server/tests/test_x11_manager.py`

**Interfaces:**
- Consumes: `DisplayAllocator`, `DisplayLease`, `terminate_process_tree` from `server.sessions.adopt`.
- Produces `class X11Manager`:
  - `__init__(self, allocator, *, resolution, max_browsers, spawn=asyncio.create_subprocess_exec, ready_check=None, run_dir="/tmp")`
  - `async def start_display(session_id: str) -> DisplaySpec | None` — alloc lease, write xauth cookie, spawn Xvfb, poll ready; on any failure release+cleanup and return `None`.
  - `def display_for(session_id: str) -> DisplaySpec | None`
  - `def env_for(session_id: str) -> dict[str, str]` — `{"DISPLAY": ":N", "XAUTHORITY": path}` or `{}`.
  - `async def stop_display(session_id: str) -> None` — kill browsers (tree), x11vnc, Xvfb; remove xauth + lock/socket files; release lease.
  - `def metrics() -> dict`
  - `def sweep_stale(self) -> int` — remove dead-owner `/tmp/.X{N}-lock` + `/tmp/.X11-unix/X{N}` in `[start, start+count)`; returns count removed.
  - `DisplaySpec` dataclass: `session_id`, `lease: DisplayLease`, `xauthority: str`, plus internal process handles.

- [ ] **Step 1: Write the failing test** (fake spawner — no real Xvfb)
```python
# server/tests/test_x11_manager.py
import asyncio
import pytest
from server.sessions.x11manager import DisplayAllocator, X11Manager

class FakeProc:
    def __init__(self): self.pid = 4242; self._terminated = False; self.returncode = None
    def terminate(self): self._terminated = True; self.returncode = 0
    def kill(self): self.returncode = -9
    async def wait(self): return self.returncode if self.returncode is not None else 0

@pytest.mark.asyncio
async def test_start_display_injects_env_when_ready(tmp_path):
    spawned = []
    async def fake_spawn(*argv, **kw):
        spawned.append(argv); return FakeProc()
    mgr = X11Manager(DisplayAllocator(100, 4, 6100), resolution="800x600x24",
                     max_browsers=2, spawn=fake_spawn,
                     ready_check=lambda d: True, run_dir=str(tmp_path))
    spec = await mgr.start_display("sess-1")
    assert spec is not None
    env = mgr.env_for("sess-1")
    assert env["DISPLAY"] == ":100"
    assert env["XAUTHORITY"].endswith("sess-1.Xauthority") or "Xauthority" in env["XAUTHORITY"]
    assert any("Xvfb" in a[0] for a in spawned)

@pytest.mark.asyncio
async def test_start_display_returns_none_when_never_ready(tmp_path):
    async def fake_spawn(*argv, **kw): return FakeProc()
    mgr = X11Manager(DisplayAllocator(100, 1, 6100), resolution="800x600x24",
                     max_browsers=2, spawn=fake_spawn,
                     ready_check=lambda d: False, run_dir=str(tmp_path),
                     ready_timeout_s=0.2, ready_interval_s=0.05)
    spec = await mgr.start_display("sess-x")
    assert spec is None
    assert mgr.env_for("sess-x") == {}
    # lease was released — pool full capacity available again
    assert (await mgr._allocator.alloc()).display == 100

@pytest.mark.asyncio
async def test_stop_display_idempotent(tmp_path):
    async def fake_spawn(*argv, **kw): return FakeProc()
    mgr = X11Manager(DisplayAllocator(100, 4, 6100), resolution="800x600x24",
                     max_browsers=2, spawn=fake_spawn,
                     ready_check=lambda d: True, run_dir=str(tmp_path))
    await mgr.start_display("sess-1")
    await mgr.stop_display("sess-1")
    await mgr.stop_display("sess-1")  # no raise
    assert mgr.display_for("sess-1") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `run_pytest server/tests/test_x11_manager.py -v`
Expected: FAIL — `ImportError: cannot import name 'X11Manager'`.

- [ ] **Step 3: Implement X11Manager** (append to `x11manager.py`)
```python
import logging
import os
import secrets
from pathlib import Path

from server.sessions.adopt import terminate_process_tree

log = logging.getLogger("server.x11")


def _default_ready_check(display: int, run_dir: str = "/tmp") -> bool:
    return Path(run_dir, ".X11-unix", f"X{display}").exists()


@dataclass
class DisplaySpec:
    session_id: str
    lease: DisplayLease
    xauthority: str
    xvfb: object | None = None
    x11vnc: object | None = None
    browsers: list = None              # managed Chromium procs
    vnc_clients: int = 0

    def __post_init__(self):
        if self.browsers is None:
            self.browsers = []


async def _terminate(proc, timeout: float = 5.0) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
    except ProcessLookupError:
        pass
    except Exception:
        log.exception("x11: process terminate failed")


class X11Manager:
    def __init__(self, allocator: DisplayAllocator, *, resolution: str, max_browsers: int,
                 spawn=asyncio.create_subprocess_exec, ready_check=None, run_dir: str = "/tmp",
                 ready_timeout_s: float = 2.0, ready_interval_s: float = 0.05) -> None:
        self._allocator = allocator
        self._resolution = resolution
        self._max_browsers = max_browsers
        self._spawn = spawn
        self._run_dir = run_dir
        self._ready_check = ready_check or (lambda d: _default_ready_check(d, run_dir))
        self._ready_timeout_s = ready_timeout_s
        self._ready_interval_s = ready_interval_s
        self._specs: dict[str, DisplaySpec] = {}
        self._lock = asyncio.Lock()
        self._display_start = allocator._start
        self._display_count = len(allocator._free) + allocator.in_use()

    def display_for(self, session_id: str) -> DisplaySpec | None:
        return self._specs.get(session_id)

    def env_for(self, session_id: str) -> dict[str, str]:
        spec = self._specs.get(session_id)
        if not spec:
            return {}
        return {"DISPLAY": f":{spec.lease.display}", "XAUTHORITY": spec.xauthority}

    async def start_display(self, session_id: str) -> DisplaySpec | None:
        async with self._lock:
            if session_id in self._specs:
                return self._specs[session_id]
            try:
                lease = await self._allocator.alloc()
            except PoolExhausted:
                log.warning("x11: pool exhausted, session %s gets no display", session_id)
                return None
            xauth = os.path.join(self._run_dir, f"ccswitch-{session_id}.Xauthority")
            try:
                cookie = secrets.token_hex(16)
                # MIT-MAGIC-COOKIE-1; xauth add tolerates a fresh file
                addp = await self._spawn(
                    "xauth", "-f", xauth, "add", f":{lease.display}", ".", cookie,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await addp.wait()
                xvfb = await self._spawn(
                    "Xvfb", f":{lease.display}", "-screen", "0", self._resolution,
                    "-auth", xauth, "-nolisten", "tcp",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
            except FileNotFoundError:
                log.exception("x11: Xvfb/xauth binary missing")
                await self._allocator.release(lease)
                return None
            if not await self._await_ready(lease.display):
                log.warning("x11: display :%d never became ready", lease.display)
                await _terminate(xvfb)
                await self._allocator.release(lease)
                return None
            spec = DisplaySpec(session_id=session_id, lease=lease, xauthority=xauth, xvfb=xvfb)
            self._specs[session_id] = spec
            return spec

    async def _await_ready(self, display: int) -> bool:
        deadline = self._ready_timeout_s
        waited = 0.0
        while waited <= deadline:
            if self._ready_check(display):
                return True
            await asyncio.sleep(self._ready_interval_s)
            waited += self._ready_interval_s
        return self._ready_check(display)

    async def stop_display(self, session_id: str) -> None:
        async with self._lock:
            spec = self._specs.pop(session_id, None)
        if spec is None:
            return
        for b in list(spec.browsers):
            try:
                pid = getattr(b, "pid", None)
                if pid:
                    await asyncio.to_thread(terminate_process_tree, pid)
            except Exception:
                log.exception("x11: browser tree kill failed")
        await _terminate(spec.x11vnc)
        await _terminate(spec.xvfb)
        for p in (spec.xauthority,
                  os.path.join(self._run_dir, f".X{spec.lease.display}-lock"),
                  os.path.join(self._run_dir, ".X11-unix", f"X{spec.lease.display}")):
            try:
                os.unlink(p)
            except OSError:
                pass
        await self._allocator.release(spec.lease)

    def metrics(self) -> dict:
        specs = list(self._specs.values())
        return {
            "displays_allocated": len(specs),
            "xvfb_count": sum(1 for s in specs if s.xvfb),
            "x11vnc_count": sum(1 for s in specs if s.x11vnc),
            "browser_count": sum(len(s.browsers) for s in specs),
            "vnc_clients_attached": sum(s.vnc_clients for s in specs),
            "pool_capacity": self._display_count,
        }

    def sweep_stale(self) -> int:
        removed = 0
        for n in range(self._display_start, self._display_start + self._display_count):
            if n in {s.lease.display for s in self._specs.values()}:
                continue
            lock = os.path.join(self._run_dir, f".X{n}-lock")
            if os.path.exists(lock):
                if not self._lock_owner_alive(lock):
                    for p in (lock, os.path.join(self._run_dir, ".X11-unix", f"X{n}")):
                        try:
                            os.unlink(p); removed += 1
                        except OSError:
                            pass
        return removed

    @staticmethod
    def _lock_owner_alive(lock_path: str) -> bool:
        try:
            pid = int(Path(lock_path).read_text().strip())
        except (OSError, ValueError):
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `run_pytest server/tests/test_x11_manager.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**
```bash
git add server/sessions/x11manager.py server/tests/test_x11_manager.py
git commit -m "feat(002): X11Manager Xvfb lifecycle, xauth, sweep, metrics"
```

---

### Task 4: Factory `extra_env` + per-session injecting closure

**Files:**
- Modify: `server/sessions/sdk_factory.py:8-21`
- Test: `server/tests/test_sdk_factory_env.py` (create)

**Interfaces:**
- Consumes: nothing new.
- Produces: `real_sdk_factory(cwd, config_dir, can_use_tool, mode, resume, extra_env=None)` — backward compatible; merges `extra_env` into the subprocess env. The orchestrator builds a 5-arg closure that calls this with `extra_env` set; `driver.py`'s positional 5-arg call is unchanged.

- [ ] **Step 1: Write the failing test**
```python
# server/tests/test_sdk_factory_env.py
from server.sessions import sdk_factory

def test_extra_env_merged(monkeypatch):
    captured = {}
    class FakeClient:
        def __init__(self, options): captured["env"] = options.env
    class FakeOpts:
        def __init__(self, **kw): self.env = kw["env"]
    monkeypatch.setattr(sdk_factory, "_import_sdk",
                        lambda: (FakeClient, FakeOpts), raising=False)
    sdk_factory.real_sdk_factory("/tmp", "/cfg", None, "interactive", None,
                                 extra_env={"DISPLAY": ":100", "XAUTHORITY": "/x"})
    assert captured["env"]["DISPLAY"] == ":100"
    assert captured["env"]["CLAUDE_CONFIG_DIR"] == "/cfg"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `run_pytest server/tests/test_sdk_factory_env.py -v`
Expected: FAIL — `real_sdk_factory()` takes 5 positional args / no `_import_sdk`.

- [ ] **Step 3: Implement** (rewrite `sdk_factory.py`)
```python
"""Production factory: builds a real ClaudeSDKClient for a SessionDriver."""
from __future__ import annotations
import os

from .completion import COMPLETION_INSTRUCTION


def _import_sdk():
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    return ClaudeSDKClient, ClaudeAgentOptions


def real_sdk_factory(cwd, config_dir, can_use_tool, mode, resume, extra_env=None):
    ClaudeSDKClient, ClaudeAgentOptions = _import_sdk()
    env = dict(os.environ, CLAUDE_CONFIG_DIR=config_dir)
    if extra_env:
        env.update(extra_env)
    opts = ClaudeAgentOptions(
        cwd=cwd, env=env, can_use_tool=can_use_tool,
        permission_mode=("plan" if mode == "plan" else "default"),
        include_partial_messages=True, resume=resume,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": COMPLETION_INSTRUCTION,
        },
    )
    return ClaudeSDKClient(options=opts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `run_pytest server/tests/test_sdk_factory_env.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add server/sessions/sdk_factory.py server/tests/test_sdk_factory_env.py
git commit -m "feat(002): sdk_factory accepts extra_env (backward compatible)"
```

---

### Task 5: Orchestrator — allocate on create, reuse on resume/rotate, fresh on adopt, teardown on close & archive

**Files:**
- Modify: `server/sessions/orchestrator.py` (`__init__` ~65-89; `create_session` ~229; `ensure_driver` ~584; `adopt_session` ~674; `_do_rotate` ~882; `close_session` ~283; `archive_session` ~919)
- Test: `server/tests/test_orchestrator_display.py` (create)

**Interfaces:**
- Consumes: `X11Manager`, `real_sdk_factory(..., extra_env=...)`.
- Produces: orchestrator accepts `x11: X11Manager | None = None`; `self._x11`. A private helper `self._session_factory(session_id)` returns a 5-arg closure that injects `self._x11.env_for(session_id)` (or the base factory unchanged when `self._x11` is None or has no display).

- [ ] **Step 1: Write the failing test** (fake X11Manager; fake SDK factory)
```python
# server/tests/test_orchestrator_display.py
import pytest
from server.sessions.orchestrator import SessionOrchestrator

class FakeX11:
    def __init__(self): self.started=[]; self.stopped=[]; self._env={}
    async def start_display(self, sid): self.started.append(sid); self._env[sid]={"DISPLAY":":100","XAUTHORITY":"/x"}; return object()
    def env_for(self, sid): return self._env.get(sid, {})
    def display_for(self, sid): return object() if sid in self._env else None
    async def stop_display(self, sid): self.stopped.append(sid); self._env.pop(sid, None)

@pytest.mark.asyncio
async def test_factory_closure_injects_display(monkeypatch):
    x11 = FakeX11()
    seen = {}
    def fake_factory(cwd, cfg, cut, mode, resume, extra_env=None):
        seen["extra_env"] = extra_env
        class C:  # minimal fake SDK client
            async def connect(self,*a,**k): ...
            async def disconnect(self): ...
        return C()
    orch = SessionOrchestrator(sdk_factory=fake_factory, x11=x11)
    await x11.start_display("S")  # simulate create-time allocation
    closure = orch._session_factory("S")
    closure("/cwd", "/cfg", None, "interactive", None)
    assert seen["extra_env"] == {"DISPLAY": ":100", "XAUTHORITY": "/x"}

@pytest.mark.asyncio
async def test_factory_closure_noop_without_x11():
    seen = {}
    def fake_factory(cwd, cfg, cut, mode, resume, extra_env=None):
        seen["extra_env"] = extra_env; return object()
    orch = SessionOrchestrator(sdk_factory=fake_factory, x11=None)
    orch._session_factory("S")("/c","/cfg",None,"interactive",None)
    assert seen["extra_env"] in (None, {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `run_pytest server/tests/test_orchestrator_display.py -v`
Expected: FAIL — `__init__() got an unexpected keyword argument 'x11'`.

- [ ] **Step 3: Wire `x11` + closure into `__init__`**

In `__init__` signature add `x11=None`; after `self._sdk_factory = sdk_factory or real_sdk_factory` add:
```python
        self._x11 = x11  # X11Manager | None — per-session virtual displays
```
Add the helper method (near the factory usage):
```python
    def _session_factory(self, session_id: str):
        """Return a 5-arg SDK factory closure injecting this session's DISPLAY."""
        base = self._sdk_factory
        x11 = self._x11
        def _factory(cwd, config_dir, can_use_tool, mode, resume):
            extra = x11.env_for(session_id) if x11 else {}
            return base(cwd, config_dir, can_use_tool, mode, resume, extra_env=extra or None)
        return _factory
```

- [ ] **Step 4: Run the closure tests**

Run: `run_pytest server/tests/test_orchestrator_display.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Use the closure + allocate at create_session**

In `create_session`, immediately after `session_id` is known and before building the driver, add:
```python
        if self._x11 is not None:
            try:
                await self._x11.start_display(session_id)
            except Exception:
                log.exception("session.display_start_failed id=%s", session_id)
```
Then change the `SessionDriver(... sdk_factory=self._sdk_factory ...)` (line ~231) to:
```python
            mode=permission_mode, sdk_factory=self._session_factory(session_id),
```

- [ ] **Step 6: Reuse display on ensure_driver and _do_rotate; fresh on adopt**

- `ensure_driver` (~590) and `_do_rotate` (~884): change `sdk_factory=self._sdk_factory` → `sdk_factory=self._session_factory(session_id)` (resp. the rotated session's id). Because `env_for` reads the **existing** spec, resumed/rotated drivers reuse the same display — no new allocation.
- `adopt_session` (~676): before building the driver add the same `start_display` block as create_session (fresh allocation), then `sdk_factory=self._session_factory(<adopted session id>)`.

- [ ] **Step 7: Teardown on close_session AND archive_session**

In `close_session`, alongside the worktree-cleanup block, add:
```python
        if self._x11 is not None:
            try:
                await self._x11.stop_display(session_id)
            except Exception:
                log.exception("session.display_cleanup_failed id=%s", session_id)
```
Add the **identical** block to `archive_session` at the point the driver is stopped.

- [ ] **Step 8: Add a teardown test**
```python
# append to server/tests/test_orchestrator_display.py
@pytest.mark.asyncio
async def test_close_session_tears_down_display(monkeypatch):
    x11 = FakeX11()
    orch = SessionOrchestrator(sdk_factory=lambda *a, **k: None, x11=x11)
    await x11.start_display("S")
    # close_session must call stop_display even if no driver registered
    await orch.close_session("S")
    assert "S" in x11.stopped
```

- [ ] **Step 9: Run the full display test file + existing orchestrator tests**

Run: `run_pytest server/tests/test_orchestrator_display.py server/tests/test_orchestrator*.py -q`
Expected: PASS (no regressions). If `close_session` early-returns on missing driver, move the X11 teardown above that guard.

- [ ] **Step 10: Commit**
```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_display.py
git commit -m "feat(002): orchestrator allocates/reuses/tears-down per-session display across all driver paths"
```

---

### Task 6: app.py — preflight, boot sweep, ordered shutdown, X11Manager on app.state

**Files:**
- Modify: `server/app.py` (`_lifespan` ~88-107; `create_app` ~111-130)
- Test: `server/tests/test_app_x11_lifespan.py` (create)

**Interfaces:**
- Consumes: `X11Manager`, `DisplayAllocator`, `get_settings`.
- Produces: `app.state.x11` (`X11Manager | None`); helper `build_x11_manager(settings) -> X11Manager | None` in `server/sessions/x11manager.py` returning `None` when disabled or preflight fails.

- [ ] **Step 1: Write the failing test**
```python
# server/tests/test_app_x11_lifespan.py
from server.sessions.x11manager import build_x11_manager

class S:  # minimal settings stand-in
    enable_xvfb=False; display_start=100; display_count=4; max_browsers=2
    vnc_port_base=6100; xvfb_resolution="800x600x24"; vnc_idle_timeout_s=1800.0

def test_disabled_returns_none():
    assert build_x11_manager(S()) is None

def test_enabled_but_missing_binaries_returns_none(monkeypatch):
    s = S(); s.enable_xvfb = True
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert build_x11_manager(s) is None  # fail-loud preflight → disabled
```

- [ ] **Step 2: Run test to verify it fails**

Run: `run_pytest server/tests/test_app_x11_lifespan.py -v`
Expected: FAIL — no `build_x11_manager`.

- [ ] **Step 3: Add `build_x11_manager`** (append to `x11manager.py`)
```python
import shutil

def build_x11_manager(settings) -> "X11Manager | None":
    if not getattr(settings, "enable_xvfb", False):
        return None
    missing = [b for b in ("Xvfb", "x11vnc", "xauth") if shutil.which(b) is None]
    if missing:
        log.warning("x11: ENABLED but missing binaries %s — feature disabled. "
                    "Install: apt install xvfb x11vnc; playwright install --with-deps chromium",
                    missing)
        return None
    alloc = DisplayAllocator(settings.display_start, settings.display_count, settings.vnc_port_base)
    return X11Manager(alloc, resolution=settings.xvfb_resolution, max_browsers=settings.max_browsers)
```

- [ ] **Step 4: Run the build_x11_manager tests**

Run: `run_pytest server/tests/test_app_x11_lifespan.py -v`
Expected: PASS.

- [ ] **Step 5: Wire into create_app + lifespan**

In `create_app`, after the orchestrator is attached:
```python
    from server.config import get_settings as _gs
    from server.sessions.x11manager import build_x11_manager
    app.state.x11 = build_x11_manager(_gs())
    orchestrator._x11 = app.state.x11  # share the manager with the orchestrator
```
In `_lifespan` startup, after `await app.state.orchestrator.start()`:
```python
    if getattr(app.state, "x11", None) is not None:
        try:
            n = app.state.x11.sweep_stale()
            if n:
                _logging.getLogger("server").info("startup: swept %d stale X lock/socket(s)", n)
        except Exception:
            _logging.getLogger("server").warning("startup: X sweep failed", exc_info=True)
```
In `_lifespan` shutdown, **before** `await app.state.orchestrator.stop()`, close live sessions then tear down X:
```python
    if getattr(app.state, "x11", None) is not None:
        for sid in list(app.state.orchestrator._drivers.keys()):
            try:
                await app.state.orchestrator.close_session(sid)
            except Exception:
                _logging.getLogger("server").warning("shutdown: close %s failed", sid, exc_info=True)
```

- [ ] **Step 6: Run the app test + a smoke import**

Run: `run_pytest server/tests/test_app_x11_lifespan.py -v && run_pytest server/tests/test_app*.py -q`
Expected: PASS (no regressions).

- [ ] **Step 7: Commit**
```bash
git add server/app.py server/sessions/x11manager.py server/tests/test_app_x11_lifespan.py
git commit -m "feat(002): preflight+boot-sweep on startup, ordered X teardown on shutdown"
```

---

### Task 7: systemd hardening + deploy idle-gate VNC defer

**Files:**
- Modify: `deploy/ccswitch.service`
- Modify: the deploy idle-gate script (locate via `grep -rn "mid-turn\|idle" deploy/`)

**Interfaces:**
- Consumes: `X11Manager.metrics()["vnc_clients_attached"]` (exposed via an admin/health route in Phase 2; for now the gate may query the process list as a fallback).

- [ ] **Step 1: Add cgroup limits + kill policy to the unit**

In `[Service]` of `deploy/ccswitch.service` add:
```ini
KillMode=control-group
TimeoutStopSec=60
MemoryMax=12G
TasksMax=512
LimitNOFILE=65536
```

- [ ] **Step 2: Defer deploy while a VNC client is attached**

In the idle-gate, extend the "defer if a session is mid-turn" condition to also defer when `vnc_clients_attached > 0` (query the Phase-2 `/healthz`-adjacent metrics route, or `pgrep -af x11vnc` as a fallback). Add a max-defer cap so a permanently-attached client can't block deploys forever (e.g. defer up to 30 min, then proceed).

- [ ] **Step 3: Verify the unit parses**

Run: `systemd-analyze verify deploy/ccswitch.service` (if available) or `systemctl --user daemon-reload` against a copy. Expected: no errors.

- [ ] **Step 4: Commit**
```bash
git add deploy/ccswitch.service deploy/<idle-gate-script>
git commit -m "ops(002): systemd cgroup limits + defer deploy while VNC attached"
```

---

# PHASE 2 — VNC relay and lazy x11vnc

Deliverable: a human can reach a session's framebuffer over `/ws/vnc`; x11vnc starts on attach and tears down on idle. Tested against fakes and (gated) real x11vnc.

### Task 8: X11Manager — lazy x11vnc, managed browser launch, idle teardown

**Files:**
- Modify: `server/sessions/x11manager.py`
- Test: `server/tests/test_x11_manager.py` (extend)

**Interfaces:**
- Produces:
  - `async def ensure_vnc(session_id) -> int | None` — start x11vnc on the session's rfbport if not running; return the port, or `None` if the session has no display.
  - `async def attach(session_id) -> int | None` / `def detach(session_id) -> None` — bump/drop `vnc_clients`; `detach` schedules idle teardown of x11vnc after `vnc_idle_timeout_s` if count hits 0.
  - `async def launch_browser(session_id, url: str | None) -> bool` — spawn a managed Chromium into `:N` (respecting `max_browsers`); returns False if capped/no display.

- [ ] **Step 1: Write failing tests**
```python
# append to server/tests/test_x11_manager.py
@pytest.mark.asyncio
async def test_ensure_vnc_spawns_x11vnc_once(tmp_path):
    spawned=[]
    async def fake_spawn(*argv, **kw): spawned.append(argv[0]); return FakeProc()
    mgr = X11Manager(DisplayAllocator(100,2,6100), resolution="800x600x24", max_browsers=2,
                     spawn=fake_spawn, ready_check=lambda d: True, run_dir=str(tmp_path))
    await mgr.start_display("S")
    p1 = await mgr.ensure_vnc("S"); p2 = await mgr.ensure_vnc("S")
    assert p1 == p2 == 6100
    assert spawned.count("x11vnc") == 1

@pytest.mark.asyncio
async def test_launch_browser_respects_cap(tmp_path):
    async def fake_spawn(*argv, **kw): return FakeProc()
    mgr = X11Manager(DisplayAllocator(100,2,6100), resolution="800x600x24", max_browsers=1,
                     spawn=fake_spawn, ready_check=lambda d: True, run_dir=str(tmp_path))
    await mgr.start_display("S")
    assert await mgr.launch_browser("S", "https://example.com") is True
    assert await mgr.launch_browser("S", None) is False  # cap=1
```

- [ ] **Step 2: Run to verify fail**

Run: `run_pytest server/tests/test_x11_manager.py -k "ensure_vnc or launch_browser" -v`
Expected: FAIL — no such methods.

- [ ] **Step 3: Implement** (add methods to `X11Manager`)
```python
    async def ensure_vnc(self, session_id: str) -> int | None:
        spec = self._specs.get(session_id)
        if spec is None:
            return None
        async with self._lock:
            if spec.x11vnc is None:
                spec.x11vnc = await self._spawn(
                    "x11vnc", "-display", f":{spec.lease.display}",
                    "-rfbport", str(spec.lease.rfbport), "-localhost", "-nopw",
                    "-auth", spec.xauthority, "-forever", "-shared", "-quiet",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
            return spec.lease.rfbport

    async def attach(self, session_id: str) -> int | None:
        spec = self._specs.get(session_id)
        if spec is None:
            return None
        port = await self.ensure_vnc(session_id)
        spec.vnc_clients += 1
        return port

    def detach(self, session_id: str) -> None:
        spec = self._specs.get(session_id)
        if spec is None:
            return
        spec.vnc_clients = max(0, spec.vnc_clients - 1)
        if spec.vnc_clients == 0:
            asyncio.create_task(self._idle_teardown(session_id))

    async def _idle_teardown(self, session_id: str, timeout: float | None = None) -> None:
        await asyncio.sleep(timeout if timeout is not None else self._vnc_idle_timeout_s)
        spec = self._specs.get(session_id)
        if spec and spec.vnc_clients == 0 and spec.x11vnc is not None:
            await _terminate(spec.x11vnc)
            spec.x11vnc = None

    async def launch_browser(self, session_id: str, url: str | None) -> bool:
        spec = self._specs.get(session_id)
        if spec is None or len(spec.browsers) >= self._max_browsers:
            return False
        argv = ["chromium", f"--display=:{spec.lease.display}",
                "--disable-dev-shm-usage", "--no-first-run", "--start-maximized"]
        if url:
            argv.append(url)
        env = dict(os.environ, DISPLAY=f":{spec.lease.display}", XAUTHORITY=spec.xauthority)
        try:
            proc = await self._spawn(*argv, env=env,
                                     stdout=asyncio.subprocess.DEVNULL,
                                     stderr=asyncio.subprocess.DEVNULL)
        except FileNotFoundError:
            log.exception("x11: chromium missing")
            return False
        spec.browsers.append(proc)
        return True
```
Add `self._vnc_idle_timeout_s` to `__init__` (new kwarg `vnc_idle_timeout_s: float = 1800.0`) and pass it from `build_x11_manager` (`vnc_idle_timeout_s=settings.vnc_idle_timeout_s`).

- [ ] **Step 4: Run to verify pass**

Run: `run_pytest server/tests/test_x11_manager.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**
```bash
git add server/sessions/x11manager.py server/tests/test_x11_manager.py
git commit -m "feat(002): lazy x11vnc, attach/detach idle teardown, capped managed browser"
```

---

### Task 9: `/ws/vnc` flow-controlled relay

**Files:**
- Create: `server/bridge/vnc_ws.py`
- Test: `server/tests/test_vnc_ws.py`

**Interfaces:**
- Consumes: `require_ws_identity`, `X11Manager` (`attach`/`detach`/`display_for`), an async TCP connect.
- Produces: `build_vnc_ws_router(get_x11)` returning an `APIRouter` with `GET /ws/vnc`; close reasons — `4401` not enabled / `4404` no display (distinct from auth `1008`). A private `async def _pump(reader_fn, writer_fn)` with await-before-read flow control.

- [ ] **Step 1: Write the failing test** (fake WS + fake x11vnc TCP echo)
```python
# server/tests/test_vnc_ws.py
import asyncio
import pytest
from server.bridge.vnc_ws import _relay  # bytes pump used by the route

class FakeWS:
    def __init__(self, incoming): self._in=list(incoming); self.sent=[]; self.closed=None
    async def receive_bytes(self):
        if self._in: return self._in.pop(0)
        raise StopAsyncIteration
    async def send_bytes(self, b): self.sent.append(b)
    async def close(self, code=1000, reason=""): self.closed=(code, reason)

@pytest.mark.asyncio
async def test_relay_pumps_both_directions():
    # Fake TCP endpoint: echoes what the WS sent, plus a server-initiated frame.
    srv_to_ws = asyncio.Queue()
    await srv_to_ws.put(b"RFB 003.008\n")
    class FakeTCP:
        def __init__(self): self.written=[]
        async def read(self, n): 
            try: return await asyncio.wait_for(srv_to_ws.get(), 0.1)
            except asyncio.TimeoutError: return b""
        def write(self, b): self.written.append(b)
        async def drain(self): ...
    ws = FakeWS([b"\x00\x01"]); tcp = FakeTCP()
    await _relay(ws, tcp, idle=0.05)
    assert b"RFB 003.008\n" in ws.sent          # server→client
    assert b"\x00\x01" in tcp.written            # client→server
```

- [ ] **Step 2: Run to verify fail**

Run: `run_pytest server/tests/test_vnc_ws.py -v`
Expected: FAIL — no `vnc_ws` module / `_relay`.

- [ ] **Step 3: Implement**
```python
# server/bridge/vnc_ws.py
"""WebSocket <-> x11vnc RFB byte relay, gated by Tailscale identity."""
from __future__ import annotations
import asyncio
import logging

from fastapi import APIRouter, WebSocket
from server.auth.middleware import require_ws_identity

log = logging.getLogger("server.vnc")

CLOSE_NOT_ENABLED = 4401
CLOSE_NO_DISPLAY = 4404


async def _relay(ws, tcp, idle: float = 30.0) -> None:
    """Flow-controlled bidirectional pump. Each direction awaits its write
    before reading more, bounding in-process buffering."""
    async def ws_to_tcp():
        while True:
            try:
                data = await ws.receive_bytes()
            except (StopAsyncIteration, Exception):
                return
            tcp.write(data)
            await tcp.drain()

    async def tcp_to_ws():
        while True:
            data = await tcp.read(65536)
            if not data:
                return
            await ws.send_bytes(data)

    t1 = asyncio.create_task(ws_to_tcp())
    t2 = asyncio.create_task(tcp_to_ws())
    done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()


def build_vnc_ws_router(get_x11):
    router = APIRouter()

    @router.websocket("/ws/vnc")
    async def vnc_ws(websocket: WebSocket) -> None:
        identity = await require_ws_identity(websocket)  # closes 1008 on failure
        if identity is None:
            return
        x11 = get_x11()
        session_id = websocket.query_params.get("session", "")
        if x11 is None:
            await websocket.accept(); await websocket.close(CLOSE_NOT_ENABLED, "xvfb disabled"); return
        if x11.display_for(session_id) is None:
            await websocket.accept(); await websocket.close(CLOSE_NO_DISPLAY, "no display"); return
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


class _StreamAdapter:
    """Adapts asyncio (reader, writer) to the .read/.write/.drain shape _relay uses."""
    def __init__(self, reader, writer):
        self._r = reader; self._w = writer
    async def read(self, n): return await self._r.read(n)
    def write(self, b): self._w.write(b)
    async def drain(self): await self._w.drain()
```

> Note: confirm `require_ws_identity`'s actual return/raise contract (middleware.py:199) and adapt the early-return accordingly; mirror the existing `/ws` accept/close ordering exactly.

- [ ] **Step 4: Run to verify pass**

Run: `run_pytest server/tests/test_vnc_ws.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add server/bridge/vnc_ws.py server/tests/test_vnc_ws.py
git commit -m "feat(002): /ws/vnc flow-controlled RFB relay with distinct close reasons"
```

---

### Task 10: Mount the VNC router + metrics route

**Files:**
- Modify: `server/app.py` (`create_app`, near the existing `build_ws_router` mount)
- Test: `server/tests/test_vnc_ws_mounted.py` (create)

- [ ] **Step 1: Write the failing test**
```python
# server/tests/test_vnc_ws_mounted.py
from starlette.testclient import TestClient
from server.app import create_app

def test_vnc_ws_disabled_closes_4401(monkeypatch):
    app = create_app()
    app.state.x11 = None
    client = TestClient(app)
    with client.websocket_connect("/ws/vnc?session=S") as ws:
        # disabled path closes with 4401 after accept
        import pytest
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_bytes()
        assert ei.value.code == 4401
```
(If the auth middleware rejects the test identity first, pin `CCSWITCH_DEV=1` / allowlist per the global constraints.)

- [ ] **Step 2: Run to verify fail**

Run: `run_pytest server/tests/test_vnc_ws_mounted.py -v`
Expected: FAIL — route not mounted (404/connection error).

- [ ] **Step 3: Mount the router + metrics**

In `create_app`, after the existing WS router include:
```python
    from server.bridge.vnc_ws import build_vnc_ws_router
    app.include_router(build_vnc_ws_router(lambda: getattr(app.state, "x11", None)))

    @app.get("/api/x11/metrics")
    async def x11_metrics(_identity: str = Depends(current_identity)) -> dict:
        x11 = getattr(app.state, "x11", None)
        return x11.metrics() if x11 else {"enabled": False}
```

- [ ] **Step 4: Run to verify pass**

Run: `run_pytest server/tests/test_vnc_ws_mounted.py -v`
Expected: PASS.

- [ ] **Step 5: Integration smoke (gated on real binaries)**

```python
# server/tests/test_x11_integration.py
import shutil, pytest
pytestmark = pytest.mark.skipif(
    not all(shutil.which(b) for b in ("Xvfb","x11vnc")), reason="no Xvfb/x11vnc")

@pytest.mark.asyncio
async def test_real_xvfb_x11vnc_rfb_handshake(tmp_path):
    from server.sessions.x11manager import DisplayAllocator, X11Manager
    import asyncio
    mgr = X11Manager(DisplayAllocator(190, 2, 6190), resolution="640x480x24", max_browsers=1)
    spec = await mgr.start_display("S")
    assert spec is not None
    port = await mgr.ensure_vnc("S")
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    banner = await asyncio.wait_for(reader.read(12), 5)
    assert banner.startswith(b"RFB ")     # real RFB handshake
    writer.close()
    await mgr.stop_display("S")
```
Run: `run_pytest server/tests/test_x11_integration.py -v` (skips if binaries absent).

- [ ] **Step 6: Commit**
```bash
git add server/app.py server/tests/test_vnc_ws_mounted.py server/tests/test_x11_integration.py
git commit -m "feat(002): mount /ws/vnc + x11 metrics route; real-binary integration smoke"
```

---

# PHASE 3 — Frontend Browser tab

Deliverable: a "Browser" tab renders the session's framebuffer with scaleViewport, state copy, reconnect, an "agent is driving" badge, keyboard/focus handling, and an open-tab action.

### Task 11: Vendor noVNC + VNC client wrapper

**Files:**
- Modify: `web/package.json`
- Create: `web/src/vnc/vncClient.ts`
- Test: `web/src/vnc/vncClient.test.ts`

**Interfaces:**
- Produces: `class VncClient` wrapping noVNC `RFB`: `connect(canvasEl, url)`, `disconnect()`, `on(event, cb)` for `'connect'|'disconnect'|'reconnecting'`; auto-reconnect with backoff on unexpected close, **no** reconnect loop on clean close codes `4401`/`4404` (emit a terminal `'disabled'`/`'nodisplay'` instead).

- [ ] **Step 1: Add the dependency**

In `web/`, run: `npm install @novnc/novnc@^1.4.0`. Verify `web/package.json` lists `"@novnc/novnc": "^1.4.0"`.

- [ ] **Step 2: Write the failing test** (mock noVNC RFB)
```ts
// web/src/vnc/vncClient.test.ts
import { describe, it, expect, vi } from 'vitest'
import { classifyClose } from './vncClient'

describe('classifyClose', () => {
  it('maps distinct close codes to terminal states', () => {
    expect(classifyClose(4401)).toBe('disabled')
    expect(classifyClose(4404)).toBe('nodisplay')
  })
  it('treats other codes as reconnectable', () => {
    expect(classifyClose(1006)).toBe('reconnect')
  })
})
```

- [ ] **Step 3: Run to verify fail**

In `web/`: `npx vitest run src/vnc/vncClient.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 4: Implement `vncClient.ts`** (include `classifyClose` + RFB wrapper)
```ts
// web/src/vnc/vncClient.ts
import RFB from '@novnc/novnc/core/rfb'

export type VncState = 'connecting' | 'connected' | 'reconnecting' | 'disabled' | 'nodisplay' | 'disconnected'

export function classifyClose(code: number): 'disabled' | 'nodisplay' | 'reconnect' {
  if (code === 4401) return 'disabled'
  if (code === 4404) return 'nodisplay'
  return 'reconnect'
}

export class VncClient {
  private rfb: any = null
  private listeners: Record<string, ((s: VncState) => void)[]> = {}
  constructor(private url: string) {}
  on(ev: string, cb: (s: VncState) => void) { (this.listeners[ev] ||= []).push(cb) }
  private emit(ev: string, s: VncState) { (this.listeners[ev] || []).forEach(cb => cb(s)) }
  connect(canvas: HTMLElement) {
    this.rfb = new RFB(canvas, this.url, {})
    this.rfb.scaleViewport = true            // mobile-first: fit to viewport
    this.rfb.addEventListener('connect', () => this.emit('connect', 'connected'))
    this.rfb.addEventListener('disconnect', (e: any) => {
      const kind = classifyClose(e?.detail?.clean ? (e.detail.code ?? 1000) : 1006)
      if (kind === 'disabled') this.emit('disconnect', 'disabled')
      else if (kind === 'nodisplay') this.emit('disconnect', 'nodisplay')
      else this.emit('reconnecting', 'reconnecting')   // caller drives backoff reconnect
    })
  }
  setScale(fit: boolean) { if (this.rfb) this.rfb.scaleViewport = fit }
  focus() { this.rfb?.focus() }
  blur() { this.rfb?.blur() }
  disconnect() { this.rfb?.disconnect(); this.rfb = null }
}
```

- [ ] **Step 5: Run to verify pass**

In `web/`: `npx vitest run src/vnc/vncClient.test.ts` → PASS. Then `npx tsc --noEmit` → clean.

- [ ] **Step 6: Commit**
```bash
git add web/package.json web/package-lock.json web/src/vnc/
git commit -m "feat(002): vendor noVNC>=1.4 + VncClient wrapper (scaleViewport, close-code classification)"
```

---

### Task 12: Browser tab component (states, badge, toggle, open-tab, focus)

**Files:**
- Create: `web/src/components/BrowserTab.tsx`
- Test: `web/src/components/BrowserTab.test.tsx`
- Modify: the session-view tab container (e.g. `web/src/components/SessionView.tsx`) to register the tab + two-stage Escape.

**Interfaces:**
- Consumes: `VncClient`; the existing session `status.state` (`'working'` ⇒ agent active); a POST `/api/sessions/{id}/browser/open` (Task 13).
- Produces: `<BrowserTab sessionId status />` rendering canvas + overlays.

- [ ] **Step 1: Write the failing test**
```tsx
// web/src/components/BrowserTab.test.tsx
import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { BrowserTab } from './BrowserTab'

vi.mock('../vnc/vncClient', () => ({ VncClient: class { on(){} connect(){} disconnect(){} setScale(){} focus(){} blur(){} } }))

describe('BrowserTab', () => {
  it('shows the agent-driving badge when status is working', () => {
    render(<BrowserTab sessionId="S" statusState="working" />)
    expect(screen.getByText(/agent is driving/i)).toBeInTheDocument()
  })
  it('shows the disabled copy on a disabled close', () => {
    render(<BrowserTab sessionId="S" statusState="idle" initialState="disabled" />)
    expect(screen.getByText(/isn.t enabled on this server/i)).toBeInTheDocument()
  })
  it('shows empty-state copy with an open-tab action when connected but blank', () => {
    render(<BrowserTab sessionId="S" statusState="idle" initialState="connected" />)
    expect(screen.getByText(/hasn.t opened a browser yet/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /open a browser/i })).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run to verify fail**

In `web/`: `npx vitest run src/components/BrowserTab.test.tsx` → FAIL (no component).

- [ ] **Step 3: Implement `BrowserTab.tsx`** (states + copy verbatim from the spec)
```tsx
// web/src/components/BrowserTab.tsx
import { useEffect, useRef, useState } from 'react'
import { VncClient, type VncState } from '../vnc/vncClient'

const COPY = {
  disabled: "Browser sharing isn't enabled on this server.",
  nodisplay: 'No display was allocated for this session.',
  empty: "The agent hasn't opened a browser yet. When it does, you'll see it here.",
  reconnecting: 'Reconnecting…',
  disconnected: 'Disconnected — tap to reconnect.',
}

export function BrowserTab({ sessionId, statusState, initialState }:
  { sessionId: string; statusState: string; initialState?: VncState }) {
  const canvasRef = useRef<HTMLDivElement>(null)
  const [state, setState] = useState<VncState>(initialState ?? 'connecting')
  const [fit, setFit] = useState(true)
  const [focused, setFocused] = useState(false)
  const clientRef = useRef<VncClient | null>(null)

  useEffect(() => {
    if (initialState) return  // test/storybook injection
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const url = `${proto}://${location.host}/ws/vnc?session=${encodeURIComponent(sessionId)}`
    const c = new VncClient(url); clientRef.current = c
    c.on('connect', () => setState('connected'))
    c.on('reconnecting', () => setState('reconnecting'))
    c.on('disconnect', (s) => setState(s))
    if (canvasRef.current) c.connect(canvasRef.current)
    return () => c.disconnect()
  }, [sessionId, initialState])

  async function openBrowser() {
    await fetch(`/api/sessions/${sessionId}/browser/open`, { method: 'POST' })
  }

  const agentDriving = statusState === 'working'
  const blankConnected = state === 'connected'  // framebuffer may be blank until a browser opens

  return (
    <div className="relative h-full w-full">
      <div ref={canvasRef} className="h-full w-full"
           tabIndex={0}
           onFocus={() => { setFocused(true); clientRef.current?.focus() }}
           onBlur={() => { setFocused(false); clientRef.current?.blur() }} />
      {agentDriving && (
        <div className="absolute top-2 left-2 rounded bg-amber-500/90 px-2 py-1 text-xs text-white">
          Agent is driving
        </div>
      )}
      {!focused && state === 'connected' && (
        <div className="absolute inset-x-0 bottom-2 text-center text-xs text-muted-foreground">
          Tap to control · pinch to zoom
        </div>
      )}
      <button className="absolute top-2 right-2 min-h-[44px] min-w-[44px] rounded border px-2 text-xs"
              onClick={() => { const f = !fit; setFit(f); clientRef.current?.setScale(f) }}>
        {fit ? '1:1' : 'Fit'}
      </button>
      {state === 'disabled' && <Overlay>{COPY.disabled}</Overlay>}
      {state === 'nodisplay' && <Overlay>{COPY.nodisplay}</Overlay>}
      {state === 'reconnecting' && <Overlay>{COPY.reconnecting}</Overlay>}
      {state === 'disconnected' && <Overlay>{COPY.disconnected}</Overlay>}
      {blankConnected && (
        <Overlay>
          {COPY.empty}
          <button className="mt-3 rounded bg-primary px-3 py-2 text-sm text-primary-foreground"
                  onClick={openBrowser}>Open a browser</button>
        </Overlay>
      )}
    </div>
  )
}

function Overlay({ children }: { children: React.ReactNode }) {
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center
                    bg-background/80 text-center text-sm text-muted-foreground p-6">
      {children}
    </div>
  )
}
```
> The blank-connected empty state overlay is a v1 simplification (we can't cheaply detect a non-blank framebuffer). Acceptable: it shows the open-tab affordance; once a browser is open the user taps through. Refine later with a framebuffer-activity heuristic if needed.

- [ ] **Step 4: Two-stage Escape in the tab container**

In the session-view container, when the Browser tab is active: first `Escape` blurs the canvas (`clientRef.current?.blur()` / move focus to the tab strip) if the canvas is focused; only a second `Escape` runs the existing back-navigation. Guard the existing global `Escape`/`c` handlers to no-op while the canvas is focused.

- [ ] **Step 5: Run web checks**

In `web/`: `npx vitest run src/components/BrowserTab.test.tsx` → PASS; `npx tsc --noEmit` → clean; `npm run build` → succeeds (catches tsc -b project-ref errors that `--noEmit` misses).

- [ ] **Step 6: Commit**
```bash
git add web/src/components/BrowserTab.tsx web/src/components/BrowserTab.test.tsx web/src/components/SessionView.tsx
git commit -m "feat(002): Browser tab — states/copy, agent-driving badge, fit toggle, open-tab, two-stage Escape"
```

---

### Task 13: Open-tab backend action

**Files:**
- Modify: `server/app.py` (or the sessions API router) — add `POST /api/sessions/{id}/browser/open`
- Test: `server/tests/test_browser_open.py` (create)

**Interfaces:**
- Consumes: `X11Manager.launch_browser(session_id, url)`.
- Produces: `POST /api/sessions/{id}/browser/open` → `{ "ok": bool }`; 409 when capped/no display.

- [ ] **Step 1: Write the failing test**
```python
# server/tests/test_browser_open.py
import pytest
from starlette.testclient import TestClient
from server.app import create_app

class FakeX11:
    def __init__(self, ok): self._ok=ok; self.calls=[]
    def display_for(self, sid): return object()
    async def launch_browser(self, sid, url): self.calls.append((sid,url)); return self._ok

def test_open_browser_ok(monkeypatch):
    app = create_app(); app.state.x11 = FakeX11(True)
    client = TestClient(app)
    r = client.post("/api/sessions/S/browser/open", json={"url": "https://example.com"})
    assert r.status_code == 200 and r.json()["ok"] is True

def test_open_browser_capped(monkeypatch):
    app = create_app(); app.state.x11 = FakeX11(False)
    client = TestClient(app)
    r = client.post("/api/sessions/S/browser/open", json={})
    assert r.status_code == 409
```
(Pin identity per the global constraints if the middleware rejects.)

- [ ] **Step 2: Run to verify fail**

Run: `run_pytest server/tests/test_browser_open.py -v` → FAIL (404).

- [ ] **Step 3: Implement the route**

In `create_app` (near the other `/api/sessions` routes):
```python
    from fastapi import Body, HTTPException
    @app.post("/api/sessions/{session_id}/browser/open")
    async def open_browser(session_id: str, payload: dict = Body(default={}),
                           _identity: str = Depends(current_identity)) -> dict:
        x11 = getattr(app.state, "x11", None)
        if x11 is None or x11.display_for(session_id) is None:
            raise HTTPException(status_code=409, detail="no display for session")
        ok = await x11.launch_browser(session_id, payload.get("url"))
        if not ok:
            raise HTTPException(status_code=409, detail="browser cap reached")
        return {"ok": True}
```

- [ ] **Step 4: Run to verify pass**

Run: `run_pytest server/tests/test_browser_open.py -v` → PASS.

- [ ] **Step 5: Commit**
```bash
git add server/app.py server/tests/test_browser_open.py
git commit -m "feat(002): POST /api/sessions/{id}/browser/open managed-browser action"
```

---

### Task 14: Docs — quickstart / runbook

**Files:**
- Create: `specs/002-session-x-display/quickstart.md`

- [ ] **Step 1: Write the runbook** covering: enabling (`CCSWITCH_ENABLE_XVFB=true` + the rest), host provisioning (`apt install xvfb x11vnc`; `playwright install --with-deps chromium`), the preflight WARNING behavior, the leak-check line (`pgrep -af 'Xvfb|x11vnc'` and `/api/x11/metrics`), the deploy-defer-on-VNC behavior, the `--disable-dev-shm-usage` guidance for agent-launched browsers, and the documented Chromium sandbox posture.

- [ ] **Step 2: Commit**
```bash
git add specs/002-session-x-display/quickstart.md
git commit -m "docs(002): quickstart + ops runbook for virtual browser feature"
```

---

## Final verification (run before opening the PR)

- [ ] Backend suite: `run_pytest server/tests/ -q` → green (integration tests skip if binaries absent).
- [ ] Web: in `web/` → `npx vitest run`, `npx tsc --noEmit`, `npm run build` → all green.
- [ ] Feature-off regression: with `CCSWITCH_ENABLE_XVFB` unset, full backend suite green and no Xvfb/x11vnc spawned (`app.state.x11 is None`).
- [ ] Manual (host with binaries): enable, create a session, open the Browser tab, click "Open a browser", confirm the page renders and is interactive over the real `tailscale serve` path on a phone.
- [ ] Security self-review (REQUIRED SUB-SKILL: superpowers:security-self-review): loopback-only bind, no VNC port exposed, xauth cookie present, no identity/secret in logs or URLs.

---

## Self-review notes (author)

- **Spec coverage:** config (T1), DisplayAllocator+rfbport pool (T2), Xvfb/xauth/readiness/sweep/metrics (T3), closure injection no-sig-change (T4), all four driver paths + close & archive teardown (T5), preflight+boot-sweep+ordered shutdown (T6), systemd+deploy-gate (T7), lazy x11vnc+idle+browser cap (T8), flow-controlled relay+distinct close reasons (T9), mount+metrics+real-binary smoke (T10), noVNC vendor+scaleViewport+close classification (T11), Browser tab states/badge/toggle/open-tab/Escape (T12), open-tab backend (T13), runbook (T14). Residual-risk items (shm flag for agent browsers, sandbox posture) documented in T14.
- **Type consistency:** `DisplayLease(display, rfbport)`, `X11Manager.env_for/display_for/start_display/stop_display/ensure_vnc/attach/detach/launch_browser/metrics/sweep_stale`, `build_x11_manager`, `build_vnc_ws_router(get_x11)`, `_relay(ws, tcp, idle)`, `classifyClose`, `VncClient`, and `/api/sessions/{id}/browser/open` are used consistently across tasks.
- **Known follow-ups (not v1 blockers):** framebuffer-activity heuristic to replace the blank-connected empty-state assumption; swipe-to-scroll gesture translation.
