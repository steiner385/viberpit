# GitHub Copilot Pro+ Account Type — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a GitHub Copilot Pro+ subscription back `claude` sessions in ccswitch as a first-class account type, via a ccswitch-supervised vendored translating proxy, participating in usage-aware rotation.

**Architecture:** Introduce an explicit `provider` axis (`anthropic` | `copilot`). Copilot accounts store a proxy token-dir under `store/copilot/{name}/`, are activated by pointing `claude` at a per-account localhost proxy (`ANTHROPIC_BASE_URL`) instead of OAuth, and expose premium-request quota through the proxy's `/usage` endpoint mapped into the existing `Usage` model so the unchanged selector ranks them alongside Anthropic accounts. The proxy is a supervised child process modeled 1:1 on `X11Manager`.

**Tech Stack:** Python 3.12, FastAPI, asyncio subprocess supervision, SQLite/JSON ledger; React + Vite + TS frontend; vendored Go static binary `whtsky/copilot2api`.

**Design spec:** `docs/superpowers/specs/2026-07-06-copilot-account-type-design.md`

## Global Constraints

- Provider values are exactly the strings `"anthropic"` and `"copilot"`. Anthropic is the implicit default for any account with no recorded provider.
- Copilot credential lives ONLY under `store/copilot/{name}/` (mode `0700`); API responses NEVER include token material (existing FR-023).
- Proxy binds `127.0.0.1` only. `ANTHROPIC_AUTH_TOKEN` is a per-proxy shared secret, not a real credential.
- `Usage` dataclass signature is `Usage(session_pct: float, session_resets_at: datetime|None, weekly_pct: float, weekly_resets_at: datetime|None, extra_usage_enabled: bool)`.
- Copilot-backed sessions DISABLE the Haiku suggestion/summary one-shots.
- Graceful degradation: an unreachable proxy / unmapped usage yields `Usage=None` (status `unknown`), NEVER a false `limited` or `needs-reauth`.
- CI has Python 3.12 + Node 20 only — NO Go toolchain, NO proxy binary. All tests must pass with the binary absent (inject `spawn`/probes, like the x11 suite).
- Vendored proxy pinned to a specific release tag + verified checksum.
- Quality gates before any PR: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/ server/tests/`, and in `web/`: `npm run typecheck && npm run lint && npm run test`.
- Run server tests with the prebuilt env note from CLAUDE.md (system `python3` + `PYTHONPATH=.:src` + `CCSWITCH_DEV=1`; empty allowlist needs `CCSWITCH_DEV=1`).

---

## Phase 1 — Core provider foundation (`src/ccswitch/`)

### Task 1: Copilot paths

**Files:**
- Modify: `src/ccswitch/paths.py` (after `account_cfg_dir`, ~`:81`)
- Test: `tests/test_paths_copilot.py`

**Interfaces:**
- Produces: `paths.copilot_store_dir() -> Path` (= `store_dir()/"copilot"`); `paths.copilot_token_dir(name) -> Path` (= `copilot_store_dir()/name`).

- [ ] **Step 1: Write the failing test**
```python
# tests/test_paths_copilot.py
from ccswitch import paths

def test_copilot_dirs_nest_under_store():
    assert paths.copilot_store_dir() == paths.store_dir() / "copilot"
    assert paths.copilot_token_dir("work") == paths.store_dir() / "copilot" / "work"
```

- [ ] **Step 2: Run test to verify it fails**
Run: `PYTHONPATH=.:src python -m pytest tests/test_paths_copilot.py -v`
Expected: FAIL with `AttributeError: module 'ccswitch.paths' has no attribute 'copilot_store_dir'`

- [ ] **Step 3: Write minimal implementation**
```python
# src/ccswitch/paths.py — add after account_cfg_dir()
def copilot_store_dir() -> Path:
    return store_dir() / "copilot"


def copilot_token_dir(name: str) -> Path:
    return copilot_store_dir() / name
```

- [ ] **Step 4: Run test to verify it passes**
Run: `PYTHONPATH=.:src python -m pytest tests/test_paths_copilot.py -v` → Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add src/ccswitch/paths.py tests/test_paths_copilot.py
git commit -m "feat(copilot): add copilot token-dir paths"
```

### Task 2: `copilot.usage_from_proxy` — quota → Usage mapping

**Files:**
- Create: `src/ccswitch/copilot.py`
- Test: `tests/test_copilot_usage.py`

**Interfaces:**
- Consumes: `oauth.Usage`.
- Produces: `copilot.PROVIDER = "copilot"`; `copilot.usage_from_proxy(payload: dict, now: datetime) -> oauth.Usage | None`. Maps premium-request quota into both `session_pct` and `weekly_pct` (the binding dimension for a single monthly window), with the monthly reset in both `*_resets_at`. Returns `None` on a missing/garbage payload.

**Mapping contract:** the proxy `/usage` JSON is expected to carry a premium-request quota object. We read `used` and `limit` (integers) and `resets_at` (ISO-8601 string) from a `premium` (or top-level) block, defensively. `pct = 100 * used / limit` (clamped 0–100); `limit <= 0` or missing fields → `None`.

- [ ] **Step 1: Write the failing test**
```python
# tests/test_copilot_usage.py
from datetime import datetime, timezone
from ccswitch import copilot

NOW = datetime(2026, 7, 6, tzinfo=timezone.utc)

def test_maps_premium_requests_to_both_windows():
    u = copilot.usage_from_proxy(
        {"premium": {"used": 300, "limit": 1500, "resets_at": "2026-08-01T00:00:00+00:00"}}, NOW)
    assert u is not None
    assert u.session_pct == 20.0 and u.weekly_pct == 20.0
    assert u.session_resets_at == u.weekly_resets_at
    assert u.session_resets_at.isoformat() == "2026-08-01T00:00:00+00:00"
    assert u.extra_usage_enabled is False

def test_garbage_payload_returns_none():
    assert copilot.usage_from_proxy({}, NOW) is None
    assert copilot.usage_from_proxy({"premium": {"used": 1, "limit": 0}}, NOW) is None
    assert copilot.usage_from_proxy({"premium": {"used": 1}}, NOW) is None

def test_pct_clamped_to_100():
    u = copilot.usage_from_proxy({"premium": {"used": 5000, "limit": 1500}}, NOW)
    assert u is not None and u.session_pct == 100.0 and u.session_resets_at is None
```

- [ ] **Step 2: Run test to verify it fails**
Run: `PYTHONPATH=.:src python -m pytest tests/test_copilot_usage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ccswitch.copilot'`

- [ ] **Step 3: Write minimal implementation**
```python
# src/ccswitch/copilot.py
"""GitHub Copilot provider: the single source of truth for the copilot account type.

Copilot accounts are activated not by CLAUDE_CONFIG_DIR OAuth but by pointing claude at a
localhost translating proxy. This module owns: the provider constant, the proxy /usage ->
Usage mapping (so the provider-agnostic selector ranks copilot accounts unchanged), and the
launch-env builder.
"""
from __future__ import annotations

import logging
from datetime import datetime

from .oauth import Usage

log = logging.getLogger(__name__)

PROVIDER = "copilot"


def usage_from_proxy(payload: dict, now: datetime) -> Usage | None:
    """Map the proxy /usage premium-request quota into a Usage.

    Copilot Pro+ meters a single ~monthly premium-request allowance, so we project it onto
    BOTH the session and weekly dimensions (the selector takes whichever binds first; here
    they are identical). Returns None for a missing/garbage payload so callers degrade to
    'usage unknown' rather than a false ceiling.
    """
    block = payload.get("premium") if isinstance(payload.get("premium"), dict) else payload
    try:
        used = float(block["used"])
        limit = float(block["limit"])
    except (KeyError, TypeError, ValueError):
        return None
    if limit <= 0:
        return None
    pct = max(0.0, min(100.0, 100.0 * used / limit))
    resets_at = _parse_iso(block.get("resets_at"))
    return Usage(
        session_pct=pct, session_resets_at=resets_at,
        weekly_pct=pct, weekly_resets_at=resets_at,
        extra_usage_enabled=False,
    )


def _parse_iso(s) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None
```

- [ ] **Step 4: Run test to verify it passes**
Run: `PYTHONPATH=.:src python -m pytest tests/test_copilot_usage.py -v` → Expected: PASS (3 passed)

- [ ] **Step 5: Commit**
```bash
git add src/ccswitch/copilot.py tests/test_copilot_usage.py
git commit -m "feat(copilot): map proxy /usage premium quota to Usage"
```

### Task 3: `copilot.launch_env` — the proxy activation env

**Files:**
- Modify: `src/ccswitch/copilot.py`
- Test: `tests/test_copilot_env.py`

**Interfaces:**
- Produces: `copilot.launch_env(port: int, secret: str) -> dict[str, str]` returning `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ENABLE_TOOL_SEARCH`.

- [ ] **Step 1: Write the failing test**
```python
# tests/test_copilot_env.py
from ccswitch import copilot

def test_launch_env_points_at_loopback_proxy():
    env = copilot.launch_env(4142, "s3cret")
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4142"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "s3cret"
    # non-first-party base URL disables MCP tool search unless re-enabled
    assert env["ENABLE_TOOL_SEARCH"] == "true"
```

- [ ] **Step 2: Run test to verify it fails**
Run: `PYTHONPATH=.:src python -m pytest tests/test_copilot_env.py -v` → Expected: FAIL (`AttributeError: launch_env`)

- [ ] **Step 3: Write minimal implementation**
```python
# src/ccswitch/copilot.py — append
def launch_env(port: int, secret: str) -> dict[str, str]:
    """Env that makes claude route through the per-account proxy instead of OAuth.

    ENABLE_TOOL_SEARCH is set because claude disables MCP tool search by default for a
    non-first-party ANTHROPIC_BASE_URL.
    """
    return {
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        "ANTHROPIC_AUTH_TOKEN": secret,
        "ENABLE_TOOL_SEARCH": "true",
    }
```

- [ ] **Step 4: Run test to verify it passes** → Expected: PASS
- [ ] **Step 5: Commit**
```bash
git add src/ccswitch/copilot.py tests/test_copilot_env.py
git commit -m "feat(copilot): launch_env for proxy-backed sessions"
```

### Task 4: Provider discovery + `all_accounts` union

**Files:**
- Modify: `src/ccswitch/copilot.py` (discovery), `src/ccswitch/accounts.py` (`_account_names`, add `provider_of`, `all_accounts`)
- Test: `tests/test_provider_discovery.py`

**Interfaces:**
- Produces: `copilot.copilot_account_names() -> list[str]` (dirs under `copilot_store_dir()` containing `credentials.json`); `accounts.provider_of(name) -> str`; `accounts.all_accounts() -> list[tuple[str, str]]` ((name, provider), sorted, Anthropic then Copilot). `accounts._account_names()` continues to return the UNION so existing callers keep working.

- [ ] **Step 1: Write the failing test**
```python
# tests/test_provider_discovery.py
import json
from ccswitch import accounts, paths, copilot

def _mk_anthropic(tmp, name):
    (paths.store_dir()).mkdir(parents=True, exist_ok=True)
    paths.slot_path(name).write_text(json.dumps({"claudeAiOauth": {}}))

def _mk_copilot(name):
    d = paths.copilot_token_dir(name); d.mkdir(parents=True, exist_ok=True)
    (d / "credentials.json").write_text("{}")

def test_union_discovery_and_provider_of(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))  # paths.root() honors this
    _mk_anthropic(tmp_path, "work")
    _mk_copilot("copilot1")
    assert copilot.copilot_account_names() == ["copilot1"]
    assert set(accounts._account_names()) == {"work", "copilot1"}
    assert accounts.provider_of("work") == "anthropic"
    assert accounts.provider_of("copilot1") == "copilot"
    assert accounts.all_accounts() == [("work", "anthropic"), ("copilot1", "copilot")]
```
> Confirm the env var that reroutes `paths.root()` in tests (read `src/ccswitch/paths.py:root`); if it differs from `CCSWITCH_HOME`, use that. Do not invent one.

- [ ] **Step 2: Run test to verify it fails** → Expected: FAIL (`copilot_account_names` / `provider_of` missing)

- [ ] **Step 3: Write minimal implementation**
```python
# src/ccswitch/copilot.py — append
from . import paths  # add to imports at top

def copilot_account_names() -> list[str]:
    d = paths.copilot_store_dir()
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir()
                  if p.is_dir() and (p / "credentials.json").exists())
```
```python
# src/ccswitch/accounts.py — add near _account_names (:99)
from . import copilot  # add to imports

def _anthropic_account_names() -> list[str]:
    d = paths.store_dir()
    if not d.exists():
        return []
    return sorted(p.name[: -len(".cred.json")] for p in d.glob("*.cred.json"))

def provider_of(name: str) -> str:
    return copilot.PROVIDER if paths.copilot_token_dir(name).exists() else "anthropic"

def all_accounts() -> list[tuple[str, str]]:
    out = [(n, "anthropic") for n in _anthropic_account_names()]
    out += [(n, copilot.PROVIDER) for n in copilot.copilot_account_names()]
    return out
```
Then replace the body of the existing `_account_names()` (`accounts.py:99-103`) with:
```python
def _account_names() -> list[str]:
    return sorted(n for n, _ in all_accounts())
```

- [ ] **Step 4: Run test to verify it passes** → Expected: PASS
- [ ] **Step 5: Run the full accounts suite to catch regressions**
Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python -m pytest tests/ -k account -q` → Expected: PASS
- [ ] **Step 6: Commit**
```bash
git add src/ccswitch/copilot.py src/ccswitch/accounts.py tests/test_provider_discovery.py
git commit -m "feat(copilot): union provider discovery + provider_of/all_accounts"
```

### Task 5: `usage_for` copilot branch + `list_accounts` provider + `remove_account` teardown

**Files:**
- Modify: `src/ccswitch/accounts.py` (`usage_for` `:201`, `list_accounts` `:366`, `remove_account` `:358`)
- Test: `tests/test_copilot_accounts.py`

**Interfaces:**
- Consumes: `copilot.usage_from_proxy`, the daemon-provided usage reader (injected — see below).
- Produces: `accounts.set_copilot_usage_reader(fn: Callable[[str], dict | None] | None)` — the server registers a reader that fetches the proxy `/usage` payload for an account (None in CLI/core context). `usage_for(name)` routes copilot accounts through it; `list_accounts()` entries gain `"provider"`; `remove_account()` removes the copilot token-dir.

> Rationale: the core stays transport-free (mirrors `set_auth_failure_sink` at `accounts.py:33`). The `CopilotProxyManager` in the daemon registers the reader.

- [ ] **Step 1: Write the failing test**
```python
# tests/test_copilot_accounts.py
from datetime import datetime, timezone
from ccswitch import accounts, paths

def _mk_copilot(name):
    d = paths.copilot_token_dir(name); d.mkdir(parents=True, exist_ok=True)
    (d / "credentials.json").write_text("{}")

def test_usage_for_copilot_uses_registered_reader(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    _mk_copilot("c1")
    accounts.set_copilot_usage_reader(
        lambda name: {"premium": {"used": 750, "limit": 1500, "resets_at": None}})
    u = accounts.usage_for("c1")
    assert u is not None and u.session_pct == 50.0
    accounts.set_copilot_usage_reader(None)

def test_list_accounts_includes_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    _mk_copilot("c1")
    entry = next(a for a in accounts.list_accounts() if a["name"] == "c1")
    assert entry["provider"] == "copilot"

def test_remove_account_clears_copilot_token_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    _mk_copilot("c1")
    accounts.remove_account("c1")
    assert not paths.copilot_token_dir("c1").exists()
```

- [ ] **Step 2: Run test to verify it fails** → Expected: FAIL (`set_copilot_usage_reader` missing / no `provider` key)

- [ ] **Step 3: Write minimal implementation**
```python
# src/ccswitch/accounts.py

# near the auth-failure sink (top of module)
_copilot_usage_reader: "Callable[[str], dict | None] | None" = None

def set_copilot_usage_reader(fn: "Callable[[str], dict | None] | None") -> None:
    global _copilot_usage_reader
    _copilot_usage_reader = fn

def _copilot_usage_for(name: str) -> "oauth.Usage | None":
    reader = _copilot_usage_reader
    if reader is None:
        return None
    try:
        payload = reader(name)
    except Exception:
        log.warning("copilot usage read failed for %s", name, exc_info=True)
        return None
    if payload is None:
        return None
    now = datetime.now(timezone.utc)
    u = copilot.usage_from_proxy(payload, now)
    if u is not None:
        with ledger.locked_ledger() as led:
            ledger.record_usage(led, name, u, now)
            ledger.set_status(led, name, "ok")
    return u
```
Add a provider guard at the very top of `usage_for` (after the signature/docstring, before `cfg = load_config()` at `:213`):
```python
    if provider_of(name) == copilot.PROVIDER:
        return _copilot_usage_for(name)
```
In `list_accounts()` (`:366-377`) add `"provider"` to each dict:
```python
                "provider": provider_of(name),
```
In `remove_account()` (`:358-363`) add copilot teardown:
```python
    ct = paths.copilot_token_dir(name)
    if ct.exists():
        shutil.rmtree(ct)
```

- [ ] **Step 4: Run test to verify it passes** → Expected: PASS (3 passed)
- [ ] **Step 5: Commit**
```bash
git add src/ccswitch/accounts.py tests/test_copilot_accounts.py
git commit -m "feat(copilot): provider-aware usage_for/list_accounts/remove_account"
```

### Task 6: Selector regression guard (no code change — lock the invariant)

**Files:**
- Test: `tests/test_selector_copilot_parity.py`

**Interfaces:**
- Consumes: `selector.rank_accounts`, `oauth.Usage`.

- [ ] **Step 1: Write the passing-by-design test**
```python
# tests/test_selector_copilot_parity.py
from datetime import datetime, timezone, timedelta
from ccswitch.selector import rank_accounts
from ccswitch.oauth import Usage
from ccswitch.config import Config

NOW = datetime(2026, 7, 6, tzinfo=timezone.utc)

def test_synthesized_copilot_usage_ranks_alongside_anthropic():
    soon = NOW + timedelta(hours=2)
    usages = {
        "anthropic1": Usage(50.0, soon, 50.0, soon, False),          # perishable
        "copilot1": Usage(10.0, NOW + timedelta(days=20), 10.0, NOW + timedelta(days=20), False),
    }
    ranked, _ = rank_accounts(usages, reserved=frozenset(), cfg=Config(), now=NOW)
    names = [r.name for r in ranked]
    assert set(names) == {"anthropic1", "copilot1"}  # both eligible, unified ranking
```

- [ ] **Step 2: Run it** → Expected: PASS (proves the selector needs no change).
- [ ] **Step 3: Commit**
```bash
git add tests/test_selector_copilot_parity.py
git commit -m "test(copilot): lock selector provider-parity invariant"
```

---

## Phase 2 — Proxy supervisor (`server/sessions/copilot_proxy.py`)

### Task 7: Binary resolution + port allocator + factory

**Files:**
- Create: `server/sessions/copilot_proxy.py`
- Test: `server/tests/test_copilot_proxy_resolve.py`

**Interfaces:**
- Produces: `resolve_copilot_proxy() -> str | None` (mirrors `resolve_chromium`: `shutil.which("copilot2api")` or a vendored path via `CCSWITCH_COPILOT_PROXY_BIN`); `ProxyPortAllocator(start, count)` with async `alloc()/release()`; `build_copilot_proxy_manager(settings) -> CopilotProxyManager | None` (returns None + loud warning when the binary is absent).

- [ ] **Step 1: Write the failing test**
```python
# server/tests/test_copilot_proxy_resolve.py
import asyncio
from server.sessions import copilot_proxy as cp

def test_resolve_prefers_env_override(monkeypatch, tmp_path):
    b = tmp_path / "copilot2api"; b.write_text("#!/bin/sh\n"); b.chmod(0o755)
    monkeypatch.setenv("CCSWITCH_COPILOT_PROXY_BIN", str(b))
    assert cp.resolve_copilot_proxy() == str(b)

def test_resolve_none_when_absent(monkeypatch):
    monkeypatch.delenv("CCSWITCH_COPILOT_PROXY_BIN", raising=False)
    monkeypatch.setattr(cp.shutil, "which", lambda _: None)
    assert cp.resolve_copilot_proxy() is None

def test_port_allocator_roundtrips():
    async def go():
        a = cp.ProxyPortAllocator(4142, 2)
        p1 = await a.alloc(); p2 = await a.alloc()
        assert {p1, p2} == {4142, 4143}
        await a.release(p1)
        assert await a.alloc() == p1
    asyncio.run(go())
```

- [ ] **Step 2: Run test to verify it fails** → Expected: FAIL (module missing)

- [ ] **Step 3: Write minimal implementation**
```python
# server/sessions/copilot_proxy.py
"""Per-account GitHub Copilot translating-proxy supervision.

Modeled 1:1 on server/sessions/x11manager.py: one supervised child process per copilot
account, resolved via shutil.which/vendored path, health-gated before use, liveness-graced,
idle ref-count torn down, and orphan-reaped on startup. One proxy serves all of an account's
live sessions.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil

log = logging.getLogger("server.copilot_proxy")

PROXY_EXE = "copilot2api"


def resolve_copilot_proxy() -> str | None:
    """Vendored-path override first, then PATH. None (feature-disabled) when absent."""
    override = os.environ.get("CCSWITCH_COPILOT_PROXY_BIN")
    if override and os.path.exists(override):
        return override
    return shutil.which(PROXY_EXE)


class ProxyPortExhausted(RuntimeError):
    pass


class ProxyPortAllocator:
    """Asyncio-locked pool of loopback ports (mirrors DisplayAllocator)."""

    def __init__(self, start: int, count: int) -> None:
        self._free: list[int] = list(range(start, start + count))
        self._used: set[int] = set()
        self._lock = asyncio.Lock()

    async def alloc(self) -> int:
        async with self._lock:
            if not self._free:
                raise ProxyPortExhausted("copilot proxy port pool exhausted")
            p = self._free.pop(0)
            self._used.add(p)
            return p

    async def release(self, port: int) -> None:
        async with self._lock:
            if port in self._used:
                self._used.discard(port)
                self._free.append(port)
                self._free.sort()
```

- [ ] **Step 4: Run test to verify it passes**
Run: `PYTHONPATH=.:src CCSWITCH_DEV=1 python -m pytest server/tests/test_copilot_proxy_resolve.py -v` → Expected: PASS
- [ ] **Step 5: Commit**
```bash
git add server/sessions/copilot_proxy.py server/tests/test_copilot_proxy_resolve.py
git commit -m "feat(copilot): proxy binary resolution + port allocator"
```

### Task 8: `CopilotProxyManager.ensure` — spawn, health-gate, liveness grace

**Files:**
- Modify: `server/sessions/copilot_proxy.py`
- Test: `server/tests/test_copilot_proxy_ensure.py`

**Interfaces:**
- Produces: `CopilotProxyManager(allocator, *, token_dir_for, spawn=asyncio.create_subprocess_exec, ready_check=None, secret="…", ready_timeout_s=10.0, ready_interval_s=0.05, liveness_s=0.0)`. `async ensure(account) -> int` returns the ready port (idempotent per account; one proxy per account). `env_for(account) -> dict[str,str]` returns `copilot.launch_env(port, secret)` or `{}`. `port_for(account) -> int | None`.
- Consumes: `resolve_copilot_proxy`, `ProxyPortAllocator`, `ccswitch.copilot.launch_env`, `paths.copilot_token_dir`.

**Readiness/liveness:** after spawn, poll `ready_check(port)` (default: HTTP `GET /usage` → 200) until `ready_timeout_s`; if `liveness_s > 0`, first confirm the process didn't exit within the grace window ("a dead launch must not look healthy"). On failure: terminate, release the port, raise `ProxyStartError`.

- [ ] **Step 1: Write the failing test** (injected spawn + ready_check; no real binary)
```python
# server/tests/test_copilot_proxy_ensure.py
import asyncio
from server.sessions import copilot_proxy as cp

class FakeProc:
    def __init__(self): self.returncode = None; self.terminated = False
    def terminate(self): self.terminated = True; self.returncode = 0
    def kill(self): self.returncode = -9
    async def wait(self): return self.returncode

def _mgr(**kw):
    procs = []
    async def spawn(*argv, **kwargs):
        p = FakeProc(); procs.append((argv, p)); return p
    mgr = cp.CopilotProxyManager(
        cp.ProxyPortAllocator(4142, 4),
        token_dir_for=lambda a: f"/tmp/tok/{a}",
        spawn=spawn, ready_check=lambda port: True, liveness_s=0.0, **kw)
    return mgr, procs

def test_ensure_spawns_health_gated_and_is_idempotent():
    async def go():
        mgr, procs = _mgr()
        port = await mgr.ensure("c1")
        assert port == 4142
        argv, _ = procs[0]
        assert "copilot2api" in argv[0] or argv[0].endswith("copilot2api")
        assert "-port" in argv and "4142" in argv and "-token-dir" in argv
        assert await mgr.ensure("c1") == 4142      # idempotent, no 2nd spawn
        assert len(procs) == 1
        assert mgr.env_for("c1")["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4142"
    asyncio.run(go())

def test_ensure_raises_when_never_ready():
    async def go():
        mgr, procs = _mgr(ready_check=lambda port: False, ready_timeout_s=0.1)
        try:
            await mgr.ensure("c1")
            assert False, "expected ProxyStartError"
        except cp.ProxyStartError:
            pass
        assert procs[0][1].terminated  # cleaned up
    asyncio.run(go())
```
> If `resolve_copilot_proxy()` returns None in the test env, patch it: `monkeypatch.setattr(cp, "resolve_copilot_proxy", lambda: "/usr/bin/copilot2api")`. Add that to both tests.

- [ ] **Step 2: Run test to verify it fails** → Expected: FAIL (`CopilotProxyManager` / `ProxyStartError` missing)

- [ ] **Step 3: Write minimal implementation**
```python
# server/sessions/copilot_proxy.py — append
import secrets as _secrets
from dataclasses import dataclass, field

from ccswitch import copilot as _copilot


class ProxyStartError(RuntimeError):
    pass


def _default_ready_check(port: int) -> bool:
    """True once GET /usage returns 200 (proxy has bound + authenticated)."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/usage", timeout=1.0) as r:
            return r.status == 200
    except Exception:
        return False


@dataclass
class _ProxySpec:
    account: str
    port: int
    proc: object
    sessions: int = 0


async def _terminate(proc, timeout: float = 5.0) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill(); await proc.wait()
    except ProcessLookupError:
        pass
    except Exception:
        log.exception("copilot proxy terminate failed")


class CopilotProxyManager:
    def __init__(self, allocator, *, token_dir_for,
                 spawn=asyncio.create_subprocess_exec, ready_check=None,
                 secret: str | None = None, ready_timeout_s: float = 10.0,
                 ready_interval_s: float = 0.05, liveness_s: float = 0.0) -> None:
        self._alloc = allocator
        self._token_dir_for = token_dir_for
        self._spawn = spawn
        self._ready_check = ready_check or _default_ready_check
        self._secret = secret or _secrets.token_hex(16)
        self._ready_timeout_s = ready_timeout_s
        self._ready_interval_s = ready_interval_s
        self._liveness_s = liveness_s
        self._specs: dict[str, _ProxySpec] = {}
        self._lock = asyncio.Lock()

    def port_for(self, account: str) -> int | None:
        s = self._specs.get(account)
        return s.port if s else None

    def env_for(self, account: str) -> dict[str, str]:
        s = self._specs.get(account)
        return _copilot.launch_env(s.port, self._secret) if s else {}

    async def ensure(self, account: str) -> int:
        async with self._lock:
            if account in self._specs:
                return self._specs[account].port
            binary = resolve_copilot_proxy()
            if binary is None:
                raise ProxyStartError("copilot2api binary not found")
            port = await self._alloc.alloc()
            try:
                proc = await self._spawn(
                    binary, "-port", str(port), "-token-dir", str(self._token_dir_for(account)),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            except FileNotFoundError as e:
                await self._alloc.release(port)
                raise ProxyStartError(f"spawn failed: {e}") from e
            if not await self._await_alive(proc) or not await self._await_ready(port):
                await _terminate(proc)
                await self._alloc.release(port)
                raise ProxyStartError(f"copilot proxy for {account} never became ready")
            self._specs[account] = _ProxySpec(account=account, port=port, proc=proc)
            return port

    async def _await_alive(self, proc) -> bool:
        if self._liveness_s <= 0:
            return True
        await asyncio.sleep(self._liveness_s)
        return getattr(proc, "returncode", None) is None

    async def _await_ready(self, port: int) -> bool:
        waited = 0.0
        while waited <= self._ready_timeout_s:
            if await asyncio.to_thread(self._ready_check, port):
                return True
            await asyncio.sleep(self._ready_interval_s)
            waited += self._ready_interval_s
        return await asyncio.to_thread(self._ready_check, port)
```
> In the test, `ready_check` is a plain sync lambda — `asyncio.to_thread` runs it fine.

- [ ] **Step 4: Run test to verify it passes** → Expected: PASS
- [ ] **Step 5: Commit**
```bash
git add server/sessions/copilot_proxy.py server/tests/test_copilot_proxy_ensure.py
git commit -m "feat(copilot): CopilotProxyManager.ensure with health-gate + liveness grace"
```

### Task 9: Teardown, idle ref-count, `stop_all`, orphan reap, `usage`

**Files:**
- Modify: `server/sessions/copilot_proxy.py`
- Test: `server/tests/test_copilot_proxy_lifecycle.py`

**Interfaces:**
- Produces: `attach(account)` / `detach(account)` (ref-count; `detach` schedules idle teardown at 0); `async stop(account)`; `async stop_all()`; `usage(account) -> dict | None` (reads `/usage` payload via injected `usage_fetch`); `sweep_orphans(lister, killer) -> int` (reap stray `copilot2api` procs with no live spec, gated by cmdline match). `build_copilot_proxy_manager(settings) -> CopilotProxyManager | None`.

- [ ] **Step 1: Write the failing test**
```python
# server/tests/test_copilot_proxy_lifecycle.py
import asyncio
from server.sessions import copilot_proxy as cp
from server.tests.test_copilot_proxy_ensure import FakeProc, _mgr

def test_idle_detach_tears_down_after_grace():
    async def go():
        mgr, procs = _mgr(idle_grace_s=0.05)
        await mgr.ensure("c1"); mgr.attach("c1")
        mgr.detach("c1")
        await asyncio.sleep(0.15)
        assert mgr.port_for("c1") is None
        assert procs[0][1].terminated
    asyncio.run(go())

def test_stop_all_terminates_every_proxy():
    async def go():
        mgr, procs = _mgr()
        await mgr.ensure("c1"); await mgr.ensure("c2")
        await mgr.stop_all()
        assert mgr.port_for("c1") is None and mgr.port_for("c2") is None
    asyncio.run(go())

def test_usage_uses_injected_fetch():
    async def go():
        mgr, procs = _mgr(usage_fetch=lambda port: {"premium": {"used": 1, "limit": 10}})
        await mgr.ensure("c1")
        assert mgr.usage("c1") == {"premium": {"used": 1, "limit": 10}}
        assert mgr.usage("absent") is None
    asyncio.run(go())
```
> Extend `_mgr` in `test_copilot_proxy_ensure.py` to forward `idle_grace_s`/`usage_fetch` kwargs, or build the manager inline. Keep the helper the single constructor.

- [ ] **Step 2: Run test to verify it fails** → Expected: FAIL
- [ ] **Step 3: Write minimal implementation** — add `idle_grace_s`, `usage_fetch` ctor params; `attach/detach/_idle_teardown` mirroring `X11Manager.detach/_idle_teardown` (`x11manager.py:290-305`); `stop/stop_all` mirroring `stop_display/stop_all` (`:208-245`); `usage()` calling `self._usage_fetch(port)` guarded by try/except → None; `sweep_orphans` mirroring `sweep_orphan_processes` (`:407-444`) matching `exe == PROXY_EXE`; and:
```python
def build_copilot_proxy_manager(settings):
    if not getattr(settings, "enable_copilot", False):
        return None
    if resolve_copilot_proxy() is None:
        log.warning("copilot: ENABLED but %s binary not found — feature disabled. "
                    "Set CCSWITCH_COPILOT_PROXY_BIN or install the pinned release.", PROXY_EXE)
        return None
    return CopilotProxyManager(
        ProxyPortAllocator(settings.copilot_port_base, settings.copilot_port_count),
        token_dir_for=__import__("ccswitch.paths", fromlist=["copilot_token_dir"]).copilot_token_dir,
        idle_grace_s=settings.copilot_idle_grace_s)
```
Add `enable_copilot`, `copilot_port_base` (e.g. 4142), `copilot_port_count` (e.g. 8), `copilot_idle_grace_s` (e.g. 300.0) to the server settings model (find the Settings/config in `server/` and follow its existing field pattern).

- [ ] **Step 4: Run test to verify it passes** → Expected: PASS
- [ ] **Step 5: Commit**
```bash
git add server/sessions/copilot_proxy.py server/tests/test_copilot_proxy_lifecycle.py server/<settings-file>
git commit -m "feat(copilot): proxy teardown/idle/stop_all/orphan-reap/usage + factory"
```

---

## Phase 3 — Device-flow auth (`server/`)

### Task 10: `CopilotAuthManager` — drive the proxy's device flow, scrape the code

**Files:**
- Create: `server/sessions/copilot_auth.py`
- Test: `server/tests/test_copilot_auth.py`

**Interfaces:**
- Produces: `CopilotAuthManager(*, spawn=…, token_dir_for)`. `async start(account) -> dict` spawns `copilot2api -token-dir <dir>` (login mode) and begins scraping stdout; `async status(account) -> dict` returns `{"state": "pending"|"awaiting_authorization"|"authorized"|"failed", "userCode": str|None, "verificationUri": str|None}`; `async cancel(account) -> dict`. Transitions to `awaiting_authorization` when the scraped line matches `github.com/login/device` + a code like `XXXX-XXXX`; to `authorized` when the token-dir gains `credentials.json`.

**Scrape contract:** parse lines for a verification URL (`https://github.com/login/device`) and an 8-char code matching `[A-Z0-9]{4}-[A-Z0-9]{4}`. These come from the proxy's login output; a small regex module `_parse_device_prompt(line) -> tuple[str|None, str|None]` is unit-tested independently so the state machine test doesn't depend on subprocess I/O.

- [ ] **Step 1: Write the failing test**
```python
# server/tests/test_copilot_auth.py
from server.sessions.copilot_auth import _parse_device_prompt

def test_parse_device_prompt_extracts_code_and_url():
    line = "Please visit https://github.com/login/device and enter code ABCD-1234"
    uri, code = _parse_device_prompt(line)
    assert uri == "https://github.com/login/device"
    assert code == "ABCD-1234"

def test_parse_device_prompt_ignores_unrelated_lines():
    assert _parse_device_prompt("starting server on :4142") == (None, None)
```

- [ ] **Step 2: Run test to verify it fails** → Expected: FAIL (module missing)
- [ ] **Step 3: Write minimal implementation** — `_parse_device_prompt` (two regexes) + the async state machine that reads `proc.stdout` line-by-line in a background task, flips state on the parsed prompt, and polls `Path(token_dir)/"credentials.json"` for the `authorized` transition; `cancel` terminates the proc. Model the public `start/status/cancel` shape on `server/reauth.py`'s ReauthManager (read it first to match the state-dict contract used by the existing add/reauth routes).
- [ ] **Step 4: Run test to verify it passes** → Expected: PASS
- [ ] **Step 5: Add a state-machine test** feeding fake stdout lines through an injected reader; assert `pending → awaiting_authorization → authorized`. Commit.
```bash
git add server/sessions/copilot_auth.py server/tests/test_copilot_auth.py
git commit -m "feat(copilot): device-flow auth manager with prompt scraping"
```

---

## Phase 4 — Orchestrator + RPC wiring

### Task 11: Wire the proxy manager into the orchestrator (env injection + ensure-before-spawn + suggestions off)

**Files:**
- Modify: `server/sessions/orchestrator.py` (`_oat_env` `:436`, `_session_factory` `:444`, `create_session` `:674-712`; suggestion/summary spawn sites)
- Test: `server/tests/test_orchestrator_copilot_env.py`

**Interfaces:**
- Consumes: `CopilotProxyManager` (injected onto the orchestrator like `self._x11`), `accounts.provider_of`.
- Produces: for a copilot session, `_oat_env` returns `self._copilot.env_for(account)` instead of `CLAUDE_CODE_OAUTH_TOKEN`; `create_session` awaits `self._copilot.ensure(account)` before building the driver; the suggestion/summary factories are skipped when `provider_of(account) == "copilot"`.

- [ ] **Step 1: Write the failing test**
```python
# server/tests/test_orchestrator_copilot_env.py — sketch
# Build an orchestrator with a fake CopilotProxyManager whose env_for returns a sentinel dict;
# create a session bound to a copilot account; assert the SDK factory received extra_env with
# ANTHROPIC_BASE_URL and NOT CLAUDE_CODE_OAUTH_TOKEN, and that ensure(account) was awaited.
```
> Follow the existing orchestrator test harness (find `server/tests/test_*orchestrator*` / a fake sdk_factory) to construct the orchestrator; reuse its fixtures rather than inventing a new harness.

- [ ] **Step 2: Run test to verify it fails** → Expected: FAIL
- [ ] **Step 3: Write minimal implementation**
```python
# _oat_env (:436) — branch on provider
def _oat_env(self, session_id: str) -> dict:
    rec = self._records.get(session_id)
    if rec is None:
        return {}
    if self._copilot is not None and provider_of(rec.account) == "copilot":
        return self._copilot.env_for(rec.account)
    tok = crypto.read_oauth_token(rec.account)
    return {"CLAUDE_CODE_OAUTH_TOKEN": tok} if tok else {}
```
In `create_session`, right after `account` is resolved and before composing the driver:
```python
    if self._copilot is not None and provider_of(account) == "copilot":
        await self._copilot.ensure(account)
```
Guard the suggestion/summary one-shot spawns with `if provider_of(account) == "copilot": return None/skip` (locate the call sites that use `real_suggestion_factory`/`real_summary_factory`). Inject `self._copilot` in `__init__` and thread it from app startup wiring.

- [ ] **Step 4: Run test to verify it passes** → Expected: PASS
- [ ] **Step 5: Commit**
```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_copilot_env.py
git commit -m "feat(copilot): orchestrator proxy env injection + ensure + suggestions off"
```

### Task 12: Orchestrator methods + RPC 3-touch + RemoteOrchestrator proxies

**Files:**
- Modify: `server/sessions/orchestrator.py` (add `copilot_add_start/status/cancel`, `copilot_usage`), `server/ipc/orchestrator_rpc.py`, `server/sessions/remote.py`
- Test: `server/tests/test_copilot_rpc_breadth.py` (+ the existing RPC breadth test)

**Interfaces:**
- Produces (all JSON-safe): `async copilot_add_start(account) -> dict`, `async copilot_add_status(account) -> dict`, `async copilot_add_cancel(account) -> dict`, `async copilot_usage(account) -> dict | None`. Each proxied on `RemoteOrchestrator` and registered in `orchestrator_rpc.py`.

- [ ] **Step 1: Write the failing test** — assert every new method name is present in the RPC registration table AND on `RemoteOrchestrator` (mirror the existing breadth test that a project memory references; find it under `server/tests/`).
- [ ] **Step 2: Run test to verify it fails** → Expected: FAIL
- [ ] **Step 3: Implement** — the four orchestrator methods delegate to `self._copilot_auth` / `self._copilot`; append their names to `_SIMPLE` in `orchestrator_rpc.py` (`:224-236`) (JSON-safe returns); add `RemoteOrchestrator` proxies (`remote.py`) following the `self._client.call("copilot_usage", account=account)` pattern.
- [ ] **Step 4: Run test to verify it passes** → Expected: PASS
- [ ] **Step 5: Register the core usage reader at startup** — where the app wires `accounts.set_auth_failure_sink`, also call `accounts.set_copilot_usage_reader(lambda name: proxy_mgr.usage(name))`. Commit.
```bash
git add server/sessions/orchestrator.py server/ipc/orchestrator_rpc.py server/sessions/remote.py server/tests/test_copilot_rpc_breadth.py
git commit -m "feat(copilot): orchestrator copilot methods + RPC + RemoteOrchestrator proxies"
```

---

## Phase 5 — API routes (`server/api/accounts.py`)

### Task 13: Provider-aware existence + `provider` in the wire format

**Files:**
- Modify: `server/api/accounts.py` (`_check_name_and_slot` `:135`, `_account_response` `:106`)
- Test: `server/tests/test_accounts_api_copilot.py`

**Interfaces:**
- Produces: `_check_name_and_slot` treats a copilot token-dir as a valid slot; `_account_response` includes `"provider"`.

- [ ] **Step 1: Write the failing test** — GET `/api/accounts` for a seeded copilot account returns `provider == "copilot"`; DELETE on it returns 204 (not 404). Use the existing accounts-API test fixtures + auth allowlist pin (`CCSWITCH_TAILNET_ALLOWLIST`).
- [ ] **Step 2: Run test to verify it fails** → Expected: FAIL (404 on the copilot account)
- [ ] **Step 3: Implement**
```python
# _check_name_and_slot (:146) — accept either a slot or a copilot token-dir
    if not (paths.slot_path(name).exists() or paths.copilot_token_dir(name).exists()):
        raise HTTPException(status_code=404, detail=f"Account '{name}' not found")
```
```python
# _account_response (:116) — add provider (import provider_of from ccswitch.accounts)
        "provider": provider_of(name),
```
Thread `provider` through `_account_response` callers (they pass positionally today — add it inside the helper via `provider_of(name)` so call sites need no change).

- [ ] **Step 4: Run test to verify it passes** → Expected: PASS
- [ ] **Step 5: Commit**
```bash
git add server/api/accounts.py server/tests/test_accounts_api_copilot.py
git commit -m "feat(copilot): provider-aware account existence + provider in wire format"
```

### Task 14: Device-flow add endpoints

**Files:**
- Modify: `server/api/accounts.py` (new routes), app wiring to expose `CopilotAuthManager` on `request.app.state`
- Test: `server/tests/test_accounts_api_copilot_add.py`

**Interfaces:**
- Produces: `POST /api/accounts/add/copilot {name}` → 202 + `{state,userCode,verificationUri}` (409 if the token-dir already exists; 400 invalid name); `GET /api/accounts/add/copilot?name=` → current device-flow state; `POST /api/accounts/add/copilot/cancel {name}` → state.

- [ ] **Step 1: Write the failing test** — POST start returns 202 with `state == "pending"`/`awaiting_authorization`; a second POST for an existing token-dir returns 409; validates name via `assert_valid_account_name`. Drive a faked `CopilotAuthManager`.
- [ ] **Step 2: Run test to verify it fails** → Expected: FAIL
- [ ] **Step 3: Implement** the three routes mirroring the existing `/add` routes (`accounts.py:302-402`), calling `orch.copilot_add_start/status/cancel`, plus `record_audit` entries (`account.add.copilot.*`). Reject when `paths.copilot_token_dir(name).exists()`.
- [ ] **Step 4: Run test to verify it passes** → Expected: PASS
- [ ] **Step 5: Commit**
```bash
git add server/api/accounts.py server/tests/test_accounts_api_copilot_add.py server/<app-wiring>
git commit -m "feat(copilot): device-flow add endpoints"
```

---

## Phase 6 — Frontend (`web/`)

### Task 15: Types — `provider` + device-flow state (+ status dedup)

**Files:**
- Modify: `web/src/api/types.ts`
- Test: `web/src/api/__tests__/types.test.ts` (or co-located; follow the repo pattern)

**Interfaces:**
- Produces: `Account.provider?: 'anthropic' | 'copilot'`; `CopilotAddState { state: 'pending'|'awaiting_authorization'|'authorized'|'failed'; userCode: string|null; verificationUri: string|null }`. Collapse the duplicated `needs-relogin`/`needs-reauth` union entry to a single value.

- [ ] **Step 1–5:** add the field/type; a type-level test that an `Account` with `provider: 'copilot'` compiles and that `provider` is optional (back-compat). Run `npm run typecheck`. Commit.
```bash
cd web && npm run typecheck && cd ..
git add web/src/api/types.ts web/src/api/__tests__/types.test.ts
git commit -m "feat(copilot): Account.provider + CopilotAddState types"
```

### Task 16: Hooks — copilot add/poll/cancel

**Files:**
- Modify: `web/src/api/hooks.ts`
- Test: `web/src/api/__tests__/hooks.copilot.test.tsx`

**Interfaces:**
- Produces: `useAddCopilotAccount()` (POST `/accounts/add/copilot`), `useCopilotAddStatus(enabled)` (GET poll), `useCancelCopilotAdd()` (POST cancel). Follow the exact shape of `useAddAccount`/`useDeleteAccount` (`hooks.ts:372/361`).

- [ ] **Step 1–5:** TDD each hook against a mocked fetch/MSW as the existing hook tests do; assert URL + method + body. Run `npm run test`. Commit.
> Known gotcha: if `AccountsManager` starts calling a new hook, update every `vi.mock('@/api/hooks', …)` list that omits it (~16 files) — grep `vi.mock('@/api/hooks'` and add the new exports.

### Task 17: AddAccountDialog — provider toggle + device-flow view

**Files:**
- Modify: `web/src/components/AddAccountDialog.tsx`
- Test: `web/src/components/__tests__/AddAccountDialog.copilot.test.tsx`

**Interfaces:**
- Consumes: the Task 16 hooks; `CopilotAddState`.
- Produces: a provider toggle (Anthropic OAuth | GitHub Copilot); the Copilot branch renders `userCode` + a link to `verificationUri` and polls `useCopilotAddStatus` until `authorized`, then closes + invalidates the accounts query.

- [ ] **Step 1–5:** RTL test — selecting "GitHub Copilot" + entering a name + submit shows the device code and the `github.com/login/device` link; reaching `authorized` closes the dialog. Run `npm run test`. Commit.

### Task 18: AccountsManager/AccountRow — provider chip + copilot reauth

**Files:**
- Modify: `web/src/components/AccountsManager.tsx`
- Test: `web/src/components/__tests__/AccountsManager.copilot.test.tsx`

**Interfaces:**
- Produces: a "Copilot" chip on copilot rows; the usage badge label reflects premium requests; the reauth control re-runs the device flow for copilot accounts (reuse the AddAccountDialog device-flow view in reauth mode).

- [ ] **Step 1–5:** RTL test — a `provider: 'copilot'` account row shows the chip; the reauth button opens the device-flow view. Follow the a11y note: interactive controls go in the actions group, not the row's open-button (nested-interactive). Run `npm run test`. Commit.

---

## Phase 7 — Vendoring + docs

### Task 19: Pin + fetch the proxy binary; wire the vendored path

**Files:**
- Create: `scripts/fetch-copilot-proxy.sh` (downloads the pinned `copilot2api-linux-amd64` release asset, verifies a checked-in SHA256, installs to a known path); a `COPILOT_PROXY_VERSION` + checksum constant.
- Modify: deploy/setup docs to run the fetch; wire `CCSWITCH_COPILOT_PROXY_BIN` (or install onto PATH).

- [ ] **Step 1:** Write `scripts/fetch-copilot-proxy.sh` pinning `COPILOT_PROXY_VERSION=vX.Y.Z` (choose the current latest at implementation time), downloading `copilot2api-linux-amd64`, `sha256sum -c` against a committed `.sha256`, `chmod +x`, install to `~/.local/lib/ccswitch/copilot2api`.
- [ ] **Step 2:** Manually run it on the dev host; confirm `resolve_copilot_proxy()` finds it (set `CCSWITCH_COPILOT_PROXY_BIN` or add to PATH).
- [ ] **Step 3:** Smoke test: with the real binary present and `enable_copilot=true`, add a copilot account through the UI device flow, start a session, confirm a turn round-trips and `/usage` populates the badge. (Manual — not in CI.)
- [ ] **Step 4:** Commit the script + checksum + docs.
```bash
git add scripts/fetch-copilot-proxy.sh scripts/copilot2api.sha256 docs/ CLAUDE.md
git commit -m "feat(copilot): pin + fetch vendored copilot2api proxy binary"
```

### Task 20: CLAUDE.md + spec cross-links; enable flag default

**Files:**
- Modify: `CLAUDE.md` (a short "Copilot account type" note), server settings default (`enable_copilot=false` until the binary is provisioned).

- [ ] Add operational notes (how to provision the binary, that Copilot sessions disable suggestions/summaries, the ToS caveat). Commit.

---

## Self-Review (completed against the spec)

**Spec coverage:** provider axis → T4/T5; proxy vendoring/runtime → T7/T19; supervision (health/liveness/idle/orphan/shutdown) → T8/T9; device-flow setup → T10/T14/T17; env injection + tool-search flag → T3/T11; usage parity/rotation → T2/T5/T6/T9/T12; status mapping → T5 (unknown), selector ceiling (T6), needs-reauth (T10/T18); suggestions/summaries disabled → T11; security (loopback, secret, 0700, no tokens on wire) → T8/T13/Global Constraints; frontend types/hooks/dialog/manager → T15–T18. No uncovered spec sections.

**Placeholder scan:** the mechanical wiring tasks (T9 tail, T11–T18) intentionally reference existing patterns to copy (`X11Manager`, `ReauthManager`, `useAddAccount`, the RPC breadth test) rather than re-inlining hundreds of lines; each names the exact file:line to mirror and the exact interface to produce. The novel logic (usage mapping, env, port allocator, ensure/health-gate, prompt scraping) has complete code. Flagged for the executor: choose the concrete `COPILOT_PROXY_VERSION` (T19) and confirm the `paths.root()` test env var (T4) against the real code before writing those tests.

**Type consistency:** `provider` values `"anthropic"`/`"copilot"` and `PROVIDER` constant are uniform; `Usage(...)` positional order matches the verified dataclass; `env_for`/`launch_env`/`ensure`/`usage`/`copilot_usage` names are consistent across Python and the RPC/remote layers; `CopilotAddState` fields match the API state dict in T10/T14.
